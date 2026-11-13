"""#163: the owner's per-chat cache keep-warm, and the raised active-wake caps."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from heartbeat2 import commands
from heartbeat2.cache import keep_warm_plan, keep_warm_key
from heartbeat2.policy import Policy, cap_counts, is_keep_warm
from test_engine import engine, arm, session, KEY

ROUTE = 'anthropic|claude-opus-5-5|https://api.anthropic.com'
TTL = 3594.0  # the longest gap seen warm on this route (the route reports no TTL itself)
EDGE = TTL - max(120, .08 * TTL)


@pytest.fixture(autouse=True)
def no_reported_ttl(monkeypatch):
    # Review (CodeRabbit): pin the route TTL lookup, so a Hermes-reported TTL in the test environment can't override
    # the learned warm gap these tests seed.
    from heartbeat2 import cache
    monkeypatch.setattr(cache, 'cache_ttl_for', lambda *a, **k: (None, 'unknown'))


@pytest.fixture
def pulse(monkeypatch):
    monkeypatch.setattr(commands, 'session_env', lambda name: KEY if name == 'HERMES_SESSION_KEY' else '')
    return lambda e, args: commands.handle(args, SimpleNamespace(get_engine=lambda: e))


def warm_chat(e, *, prompt=300000, warm_gap=TTL, touch=None, human=None):
    now = e.clock.now()
    e.store.set_meta('cache_routes', json.dumps({ROUTE: {'max_warm_gap': warm_gap}}))
    session(e, last_route=ROUTE, last_prompt_tokens=prompt,
            last_cache_touch_at=now if touch is None else touch,
            last_human_at=now if human is None else human)


def turn(e, *, started_at, prompt=300000, read=299000, reply='[SILENT]'):
    """One keep-warm turn as the gateway runs it: marker in, one priced call, silent reply, end."""
    text = e.ctx.calls[-1][0]
    assert e.intake.pre_llm_call(session_id='sid', turn_id='turn', user_message=text, platform='telegram') is None
    e.intake.post_api_request(session_id='sid', turn_id='turn', provider='anthropic', model='claude-opus-5-5',
                              base_url='https://api.anthropic.com', started_at=started_at,
                              usage={'input_tokens': prompt, 'cache_read_tokens': read, 'cache_write_tokens': prompt-read,
                                     'output_tokens': 3})
    out = e.intake.transform_llm_output(response_text=reply, session_id='sid', turn_id='turn')
    e.intake.on_session_end(session_id='sid', turn_id='turn', completed=True, failed=False, interrupted=False)
    return out


def warm_wake(e):
    return e.store.get_wake(commands.keep_warm_id(KEY))


def test_warm_on_status_off(engine, pulse):
    e = engine
    warm_chat(e)
    reply = pulse(e, 'warm on')
    assert reply.startswith('Keep-warm is on for this chat'), reply
    assert 'next ping' in reply and 'pauses after 12h' in reply
    w = warm_wake(e)
    assert is_keep_warm(w) and w.creator == 'user' and w.status == 'active' and w.kind == 'self'
    # Settings-owned: it never takes an active-wake slot.
    assert cap_counts(e.store, KEY) == (0, 0)
    assert pulse(e, 'warm').startswith('Keep-warm: on · pings today 0')
    assert 'Keep-warm: on' in pulse(e, 'status')
    assert pulse(e, 'warm on').startswith('Keep-warm is on')  # idempotent
    assert pulse(e, 'warm off') == 'Keep-warm is off for this chat.'
    assert warm_wake(e).status == 'cancelled'
    assert pulse(e, 'warm off') == 'Keep-warm is already off for this chat.'
    pulse(e, 'warm on')
    assert warm_wake(e).status == 'active'


def test_ping_fires_just_before_the_cache_expires_and_stays_silent(engine, pulse):
    e = engine
    warm_chat(e)
    pulse(e, 'warm on')
    t0 = e.clock.now()
    e.tick()
    assert not e.ctx.calls
    assert warm_wake(e).next_due_at == pytest.approx(t0 + EDGE)
    e.clock.advance(EDGE - 1)
    e.tick()
    assert not e.ctx.calls
    e.clock.advance(1)
    e.tick()
    assert len(e.ctx.calls) == 1
    text, key = e.ctx.calls[0]
    assert key == KEY and 'keep-warm' in text and text.rstrip().endswith('Reply with exactly: [SILENT]')
    assert len(text) < 300  # minimal prompt: no desk, payload or contract
    # While the ping is in flight it is neither a wake turn nor a message slot.
    assert e.daily(KEY) == {'model_turns': 0, 'delivered': 0}
    before = e.store.get_session(KEY)
    # Even a model that ignores the prompt and chats is kept out of the chat.
    assert turn(e, started_at=e.clock.now(), reply='Good morning! Anything I can help with?') == '[SILENT]'
    w = warm_wake(e)
    o, = e.store.list_occurrences(w.id)
    assert o.state == 'done' and not o.delivered and o.force_silent
    after = e.store.get_session(KEY)
    # Not a wake turn, not a message: unanswered, wake spacing and the daily wake-turn count are untouched.
    assert after.unanswered_count == before.unanswered_count and after.last_wake_turn_at == before.last_wake_turn_at
    assert e.daily(KEY) == {'model_turns': 0, 'delivered': 0}
    # Owner spend, never the agent's own-turn budget.
    assert e.store.agent_cost_today(KEY, e.policy.resolved_timezone()) == {'cost_microusd': 0, 'unpriced_calls': 0}
    assert w.noop_streak == 0 and w.status == 'active'
    # The ping touched the cache: the next one is a full edge later.
    assert after.last_cache_touch_at == pytest.approx(e.clock.now())
    e.tick()
    assert warm_wake(e).next_due_at == pytest.approx(e.clock.now() + EDGE)


def test_a_real_turn_moves_the_ping_out(engine, pulse):
    e = engine
    warm_chat(e)
    pulse(e, 'warm on')
    t0 = e.clock.now()
    e.clock.advance(1800)
    session(e, last_cache_touch_at=e.clock.now(), last_human_at=e.clock.now())  # the owner wrote; their turn read the cache
    e.clock.wall = t0 + EDGE
    e.tick()
    assert not e.ctx.calls
    e.clock.wall = t0 + 1800 + EDGE
    e.tick()
    assert len(e.ctx.calls) == 1


def test_queued_ping_is_dropped_when_a_turn_lands_first(engine, pulse):
    e = engine
    warm_chat(e)
    pulse(e, 'warm on')
    t0 = e.clock.now()
    session(e, busy_since=t0, busy_epoch=e.epoch)  # the chat is mid-turn when the ping comes due
    e.clock.advance(EDGE)
    e.tick()
    assert not e.ctx.calls
    o, = e.store.list_occurrences(warm_wake(e).id)
    assert o.state == 'pending' and o.error == 'busy'
    session(e, busy_since=None, last_cache_touch_at=e.clock.now())  # that turn read the cache
    e.tick()
    assert not e.ctx.calls
    o, = e.store.list_occurrences(warm_wake(e).id)
    assert o.state == 'skipped' and o.error == 'keep_warm_not_needed'


@pytest.mark.parametrize('case,reason', [
    ('cold', 'gone cold'), ('idle', 'resting'), ('small', 'context is small'),
    ('short_ttl', 'needs 30m'), ('unknown_ttl', "isn't known"), ('no_call', 'first model call')])
def test_no_ping_when_it_would_not_save(engine, pulse, case, reason):
    e = engine
    now = e.clock.now()
    if case == 'no_call':
        session(e, last_human_at=now)
    else:
        warm_chat(e, prompt=1000 if case == 'small' else 300000,
                  warm_gap={'short_ttl': 600, 'unknown_ttl': None}.get(case, TTL),
                  touch=now - TTL if case == 'cold' else now,
                  human=now - 12 * 3600 if case == 'idle' else now)
    assert ('No ping now' in pulse(e, 'warm on'))
    for _ in range(3):
        e.clock.advance(EDGE)
        e.tick()
    assert not e.ctx.calls
    plan = keep_warm_plan(e.store, e.store.get_session(KEY), e.policy, e.clock.now())
    assert 'due_at' not in plan and reason in plan['reason']


def test_owner_returning_resumes_an_idle_chat(engine, pulse):
    e = engine
    warm_chat(e, human=e.clock.now() - 13 * 3600)
    pulse(e, 'warm on')
    e.clock.advance(EDGE)
    e.tick()
    assert not e.ctx.calls
    session(e, last_human_at=e.clock.now())
    e.tick()
    assert len(e.ctx.calls) == 1


def test_pings_pass_holds_and_caps_and_never_trip_the_governor(engine, pulse):
    e = engine
    # Quiet hours all day, the unanswered mute engaged, and no wake turns or spend left today.
    e.policy = replace(e.policy, quiet_hours='00:00-23:59', max_model_wakes_per_day=0, daily_cost_limit_usd=0.01,
                       noop_pause_after=2, error_pause_after=2)
    warm_chat(e)
    session(e, unanswered_count=9)
    e.store.set_meta(f'agent_spend:{KEY}:{commands.local_day(e.clock.now(), e.policy.resolved_timezone())}',
                     json.dumps({'cost_microusd': 10**9, 'unpriced_calls': 0}))
    pulse(e, 'warm on')
    for n in range(1, 6):
        e.clock.advance(EDGE)
        session(e, last_human_at=e.clock.now())
        e.tick()
        assert len(e.ctx.calls) == n, n
        assert turn(e, started_at=e.clock.now()) == '[SILENT]'
    w = warm_wake(e)
    assert w.status == 'active' and w.noop_streak == 0 and w.fires == 5
    row = e.store.get_ledger(commands.local_day(e.clock.now(), e.policy.resolved_timezone()), w.id)
    assert row['model_turns'] == 5 and row['noops'] == 0 and row['no_verdict'] == 0 and row['cache_read_tokens'] == 5 * 299000
    assert 'keep-warm pings' in pulse(e, 'ledger 1d')


def test_the_agent_cannot_change_keep_warm(engine, pulse):
    e = engine
    warm_chat(e)
    pulse(e, 'warm on')
    wid = warm_wake(e).id
    for args in ({'action': 'cancel', 'id': wid}, {'action': 'pause', 'id': wid},
                 {'action': 'update', 'id': wid, 'note': 'x'},
                 {'action': 'update', 'id': wid, 'trigger': {'kind': 'every', 'every': '1h'}}):
        result = e.tool.handle(args, session_id='sid')
        assert result.get('error') == 'builtin_settings_owned' or (result.get('results') or [{}])[0].get('error') == 'builtin_settings_owned', (args, result)
    assert warm_wake(e).status == 'active'
    # Owner controls: resume/keep/slow/watch point at /pulse warm; pause and cancel work.
    assert 'pulse warm on' in pulse(e, f'resume {wid}')
    assert 'paused' in pulse(e, f'pause {wid}')
    assert 'Keep-warm: paused' in pulse(e, 'warm')
    pulse(e, 'warm on')
    assert warm_wake(e).status == 'active'


def test_nothing_can_be_armed_from_a_ping_turn(engine, pulse):
    e = engine
    warm_chat(e)
    pulse(e, 'warm on')
    e.clock.advance(EDGE)
    e.tick()
    e.intake.pre_llm_call(session_id='sid', turn_id='turn', user_message=e.ctx.calls[-1][0], platform='telegram')
    result = e.tool.handle({'action': 'create', 'note': 'x', 'trigger': {'kind': 'at', 'at': 'in 5m'}}, session_id='sid')
    assert result['error'] == 'keep_warm_turn'
    assert [w.id for w in e.store.list_wakes(KEY)] == [warm_wake(e).id]


def test_ping_does_not_delay_real_wakes(engine, pulse):
    e = engine
    warm_chat(e)
    pulse(e, 'warm on')
    e.clock.advance(EDGE)
    e.tick()
    turn(e, started_at=e.clock.now())
    w = arm(e, 'at', next_due_at=e.clock.now() + 60)
    e.clock.advance(60)
    e.tick()
    assert len(e.ctx.calls) == 2 and w.id in e.ctx.calls[-1][0]  # no wake-spacing wait behind the ping


def test_caps_are_raised_and_the_profile_cap_is_a_setting(engine):
    p = Policy()
    assert (p.max_active, p.max_active_profile) == (50, 200)
    p = Policy.from_settings({'max_active_profile': 80})
    gate = p.cap_gate(10, 80)
    assert gate.reason == 'global_max_active' and 'limit of 80 ' in gate.detail
    assert p.cap_gate(49, 79).allowed and p.cap_gate(50, 0).reason == 'max_active'
    with pytest.raises(ValueError):
        Policy.from_settings({'max_active_profile': 10001})
    with pytest.raises(ValueError):
        Policy.from_settings({'keep_warm_idle_hours': 0})


def test_probes_run_in_bounded_batches(engine, tmp_path):
    e = engine
    from heartbeat2 import poller
    seen = []
    e.probe_runner = lambda w: seen.append(w.id) or {'state': 1}
    for i in range(poller.PROBES_PER_TICK + 4):
        arm(e, 'file', trigger_json={'path': str(tmp_path / f'f{i}'), 'check_every': 60}, next_due_at=e.clock.now())
    e.tick()
    assert len(seen) == poller.PROBES_PER_TICK
    e.tick()
    assert len(seen) == poller.PROBES_PER_TICK + 4 and len(set(seen)) == len(seen)


def test_probe_batches_serve_the_most_overdue_first(engine, tmp_path):
    # With more watchers due than a batch holds, the newest ones must not wait behind older ones forever.
    e = engine
    from heartbeat2 import poller
    seen = []
    e.probe_runner = lambda w: seen.append(w.id) or {'state': 1}
    now = e.clock.now()
    old = [arm(e, 'file', trigger_json={'path': str(tmp_path / f'o{i}'), 'check_every': 60}, next_due_at=now)
           for i in range(poller.PROBES_PER_TICK)]
    late = arm(e, 'file', trigger_json={'path': str(tmp_path / 'late'), 'check_every': 60}, next_due_at=now - 600)
    e.tick()
    assert late.id in seen and len(seen) == poller.PROBES_PER_TICK


def test_keep_warm_key_is_per_touch():
    assert keep_warm_key(1.5) == 'k:1.5' and keep_warm_key(1.5) != keep_warm_key(1.25)


def test_ping_stays_silent_even_if_the_model_asks_to_notify(engine, pulse):
    e = engine
    warm_chat(e)
    pulse(e, 'warm on')
    e.clock.advance(EDGE)
    e.tick()
    e.intake.pre_llm_call(session_id='sid', turn_id='turn', user_message=e.ctx.calls[-1][0], platform='telegram')
    e.intake.post_api_request(session_id='sid', turn_id='turn', usage={})
    e.tool.handle({'action': 'respond', 'outcome': 'progress', 'notify': True, 'summary': 'Checked in.'}, session_id='sid')
    assert e.intake.transform_llm_output(response_text='Hi there!', session_id='sid', turn_id='turn') == '[SILENT]'
    e.intake.on_session_end(session_id='sid', turn_id='turn', completed=True, failed=False, interrupted=False)
    o, = e.store.list_occurrences(warm_wake(e).id)
    assert o.state == 'done' and not o.delivered
    assert e.store.get_session(KEY).unanswered_count == 0


def test_no_ping_when_prompt_caching_is_off(engine, pulse, monkeypatch):
    # Review (Astra): an old warm gap must not keep paid pings going after caching is turned off.
    e = engine
    from heartbeat2 import cache
    warm_chat(e)
    monkeypatch.setattr(cache, 'ttl_for_session', lambda store, session: (None, 'off'))
    assert 'prompt caching is off' in pulse(e, 'warm on')
    e.clock.advance(EDGE)
    e.tick()
    assert not e.ctx.calls


def test_a_ping_cancelled_mid_turn_still_cannot_change_anything(engine, pulse):
    # Review (Astra): /pulse warm off during a running ping ends its occurrence, but the turn is still a ping turn.
    e = engine
    warm_chat(e)
    pulse(e, 'warm on')
    e.clock.advance(EDGE)
    e.tick()
    e.intake.pre_llm_call(session_id='sid', turn_id='turn', user_message=e.ctx.calls[-1][0], platform='telegram')
    assert pulse(e, 'warm off') == 'Keep-warm is off for this chat.'
    result = e.tool.handle({'action': 'create', 'note': 'x', 'trigger': {'kind': 'at', 'at': 'in 5m'}}, session_id='sid')
    assert result['error'] == 'keep_warm_turn'
    assert [w.id for w in e.store.list_wakes(KEY)] == [warm_wake(e).id]


def test_new_conversation_waits_for_its_own_first_call(engine, pulse):
    # Review (Astra): after /new, the old chat's touch and size must not schedule a ping into the new one.
    e = engine
    warm_chat(e)
    pulse(e, 'warm on')
    e.clock.advance(EDGE - 60)
    e.intake.on_session_reset(old_session_id='sid', new_session_id='sid2')
    s = e.store.get_session(KEY)
    assert s.last_cache_touch_at is None and s.last_prompt_tokens == 0
    assert warm_wake(e).armed_session_id == 'sid2' and warm_wake(e).status == 'active'
    e.clock.advance(120)
    e.tick()
    assert not e.ctx.calls


def test_error_pause_card_shows_once_and_off_clears_it(engine, pulse):
    # Review (Claude): the keep-warm pause card must not repeat on every owner turn, nor outlive /pulse warm off.
    e = engine
    from heartbeat2 import governor
    warm_chat(e)
    pulse(e, 'warm on')
    w = warm_wake(e)
    for _ in range(e.policy.error_pause_after):
        governor.error(e, e.store.get_wake(w.id))
    assert warm_wake(e).status == 'paused'
    assert 'keep / slow' not in pulse(e, 'status')

    def owner_turn(n):
        e.store.set_meta('human_context:'+KEY, '1')
        out = e.intake.pre_llm_call(session_id='sid', turn_id=f'h{n}', user_message='hi', platform='telegram')
        e.intake.on_session_end(session_id='sid', turn_id=f'h{n}', completed=True)
        return (out or {}).get('context', '')
    assert 'Keep-warm' in owner_turn(1)
    assert 'Keep-warm' not in owner_turn(2)
    for _ in range(e.policy.error_pause_after):
        governor.error(e, e.store.get_wake(w.id))
    pulse(e, 'warm on')
    for _ in range(e.policy.error_pause_after):
        governor.error(e, e.store.get_wake(w.id))
    pulse(e, 'warm off')
    assert 'Keep-warm' not in owner_turn(3)


@pytest.mark.parametrize('setting', ['max_active', 'max_active_profile'])
def test_warm_on_needs_no_active_slot(engine, pulse, setting):
    # Review (Codex bot): with ordinary wakes capped to zero, the owner can still turn on keep-warm.
    e = engine
    e.policy = replace(e.policy, **{setting: 0})
    warm_chat(e)
    assert pulse(e, 'warm on').startswith('Keep-warm is on'), pulse(e, 'warm on')
    e.ctx.allowed = False  # consent still applies
    assert pulse(e, 'warm off') == 'Keep-warm is off for this chat.'
    assert 'injection_disabled' in pulse(e, 'warm on')


def test_cache_line_after_new_has_no_stale_or_zero_price(engine, pulse):
    # Review (Claude, nit): between /new and the first model call, the cache line must not price a 0-token warm cache.
    e = engine
    warm_chat(e)
    e.intake.on_session_reset(old_session_id='sid', new_session_id='sid2')
    w = arm(e, 'every')
    assert e.cache_line(w) == ''  # no cache line until the new conversation's first model call
    assert e.store.get_session(KEY).last_route == ''
