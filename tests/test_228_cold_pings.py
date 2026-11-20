"""#228: on a Claude route one cold miss no longer stops keep-warm for good; two cold pings in a row do."""
import json

import pytest

from heartbeat2 import cache
from heartbeat2.cache import keep_warm_plan
from test_engine import engine, session, KEY
from test_v0315_keep_warm import ROUTE, TTL, EDGE, no_reported_ttl, pulse, warm_chat, turn, warm_wake  # noqa: F401


def plan(e):
    return keep_warm_plan(e.store, e.store.get_session(KEY), e.policy, e.clock.now())


def seed_cold(e, cold):
    rows = json.loads(e.store.get_meta('cache_routes'))
    rows[ROUTE]['min_cold_gap'] = cold
    e.store.set_meta('cache_routes', json.dumps(rows))


def ping(e, *, read):
    """Advance to the next ping, let it fire and run its turn with this many cached tokens read (of 300000)."""
    e.tick()
    due = warm_wake(e).next_due_at
    assert due is not None
    e.clock.advance(due - e.clock.now())
    e.tick()
    calls = len(e.ctx.calls)
    assert calls and 'keep-warm' in e.ctx.calls[-1][0]
    turn(e, started_at=e.clock.now(), read=read)
    e.tick()


def reply(e, *, after, read):
    """A real turn (not a ping) `after` seconds later, reusing `read` of 300000 cached tokens."""
    e.clock.advance(after)
    cache.touch(e.store, e.store.get_session(KEY), started_at=e.clock.now(), route=ROUTE, prompt=300000, read=read)
    e.tick()


def test_the_reported_row_plans_a_ping(engine):
    """Warm across 60.1m, cold after 9.8m (a rebuilt prompt) on a Claude route: the hour is proven, so keep-warm plans."""
    e, now = engine, engine.clock.now()
    route = 'directsdk|claude-opus-5-5[1m]|'
    e.store.set_meta('cache_routes', json.dumps({route: {'max_warm_gap': 3603.9, 'min_cold_gap': 585.6}}))
    session(e, last_route=route, last_prompt_tokens=580000, last_cache_touch_at=now, last_human_at=now)
    s = e.store.get_session(KEY)
    assert cache.warm_lifetime(e.store, s) == (3600, 'warm_seen')   # capped at the hour
    assert keep_warm_plan(e.store, s, e.policy, now)['due_at'] == now + 3600 - .08 * 3600


def test_a_warm_gap_under_five_minutes_proves_nothing_on_claude(engine):
    e, now = engine, engine.clock.now()
    route = 'directsdk|claude-opus-5-5|'
    e.store.set_meta('cache_routes', json.dumps({route: {'max_warm_gap': 290, 'min_cold_gap': 200}}))
    session(e, last_route=route, last_prompt_tokens=580000, last_cache_touch_at=now, last_human_at=now)
    # 200 s is below the 5-minute floor, so it isn't a cold gap; 290 s warm is still only a lower bound under 30m.
    assert cache.warm_lifetime(e.store, e.store.get_session(KEY)) == (290, 'warm_seen')
    assert 'due_at' not in keep_warm_plan(e.store, e.store.get_session(KEY), e.policy, now)


def test_one_cold_ping_keeps_pinging_and_two_in_a_row_stop(engine, pulse):
    e = engine
    warm_chat(e)
    seed_cold(e, 585.6)
    pulse(e, 'warm on')
    ping(e, read=0)                       # a rebuilt prompt just before the ping: cold
    assert warm_wake(e).next_due_at is not None
    ping(e, read=299000)                  # the next one lands warm and clears the count
    assert e.store.get_meta('keep_warm_cold:' + KEY) in (None, '')
    ping(e, read=0)
    ping(e, read=0)                       # two in a row
    assert warm_wake(e).next_due_at is None
    reason = plan(e)['reason']
    assert 'the last 2 pings found the cache cold' in reason
    status = pulse(e, 'status')
    assert 'no ping due now: the last 2 pings found the cache cold' in status
    listed = [w for w in e.tool.handle({'action': 'list'}, session_id='sid')['wakes'] if w['id'] == warm_wake(e).id]
    assert 'the last 2 pings found the cache cold' in listed[0]['no_ping_reason']


def test_a_warm_reply_after_five_minutes_resumes_pings(engine, pulse):
    e = engine
    warm_chat(e)
    pulse(e, 'warm on')
    ping(e, read=0)
    ping(e, read=0)
    assert warm_wake(e).next_due_at is None
    reply(e, after=120, read=299000)      # warm, but too soon to prove anything
    assert warm_wake(e).next_due_at is None
    reply(e, after=400, read=299000)      # warm after under 15 minutes: could be timing skew on a 5-minute cache
    assert warm_wake(e).next_due_at is None
    reply(e, after=1000, read=299000)     # the cache survived more than 15 minutes
    assert warm_wake(e).next_due_at == pytest.approx(e.clock.now() + EDGE)


def test_warm_on_again_resumes_pings(engine, pulse):
    e = engine
    warm_chat(e)
    pulse(e, 'warm on')
    ping(e, read=0)
    ping(e, read=0)
    assert warm_wake(e).next_due_at is None
    pulse(e, 'warm on')
    e.tick()
    assert warm_wake(e).next_due_at is not None


def test_a_real_downgrade_to_five_minutes_costs_at_most_two_cold_pings(engine, pulse):
    """The route drops to a 5-minute cache: cold replies don't resume pings, and no third ping is ever sent."""
    e = engine
    warm_chat(e)
    pulse(e, 'warm on')
    ping(e, read=0)
    ping(e, read=0)
    sent = len(e.ctx.calls)
    for _ in range(6):
        reply(e, after=600, read=0)       # every reply after 10 minutes is cold now
    for _ in range(12):
        e.clock.advance(1800)
        e.tick()
    assert len(e.ctx.calls) == sent
    assert warm_wake(e).next_due_at is None


def test_a_cold_ping_counts_only_when_the_ping_itself_touched_the_cache(engine, pulse):
    """A ping whose turn made no model call leaves the chat's last (cold) human call alone: not a cold ping."""
    e = engine
    warm_chat(e)
    pulse(e, 'warm on')
    e.tick()
    e.clock.advance(warm_wake(e).next_due_at - e.clock.now())
    e.tick()
    text = e.ctx.calls[-1][0]
    session(e, last_cache_hit_ratio=0.0)  # the last call before the ping was cold
    assert e.intake.pre_llm_call(session_id='sid', turn_id='turn', user_message=text, platform='telegram') is None
    e.intake.transform_llm_output(response_text='[SILENT]', session_id='sid', turn_id='turn')
    e.intake.on_session_end(session_id='sid', turn_id='turn', completed=True, failed=False, interrupted=False)
    assert e.store.get_meta('keep_warm_cold:' + KEY) in (None, '')


def fire(e):
    """Advance to the next ping and let the poller send it; returns the ping text."""
    e.tick()
    e.clock.advance(warm_wake(e).next_due_at - e.clock.now())
    e.tick()
    return e.ctx.calls[-1][0]


def call(e, *, read, prompt=300000):
    e.intake.post_api_request(session_id='sid', turn_id='turn', provider='anthropic', model='claude-opus-5-5',
                              base_url='https://api.anthropic.com', started_at=e.clock.now(),
                              usage={'input_tokens': prompt, 'cache_read_tokens': read, 'cache_write_tokens': prompt-read,
                                     'output_tokens': 3})


def test_an_interrupted_ping_still_counts_its_cold_first_call(engine, pulse):
    """Review (Codex): the cold write is paid when the first call returns, even if the turn is then interrupted."""
    e = engine
    warm_chat(e)
    pulse(e, 'warm on')
    for _ in range(2):
        text = fire(e)
        assert e.intake.pre_llm_call(session_id='sid', turn_id='turn', user_message=text, platform='telegram') is None
        call(e, read=0)
        e.intake.on_session_end(session_id='sid', turn_id='turn', completed=False, failed=False, interrupted=True)
        e.tick()
    assert 'the last 2 pings found the cache cold' in plan(e)['reason']


def test_a_ping_with_a_cold_first_call_counts_even_if_a_later_call_is_warm(engine, pulse):
    """Review (Codex): a second call in the ping turn reads what the first one wrote; only the first call counts."""
    e = engine
    warm_chat(e)
    pulse(e, 'warm on')
    for _ in range(2):
        text = fire(e)
        assert e.intake.pre_llm_call(session_id='sid', turn_id='turn', user_message=text, platform='telegram') is None
        call(e, read=0)
        e.clock.advance(2)
        call(e, read=299000)
        e.intake.transform_llm_output(response_text='[SILENT]', session_id='sid', turn_id='turn')
        e.intake.on_session_end(session_id='sid', turn_id='turn', completed=True, failed=False, interrupted=False)
        e.tick()
    assert 'the last 2 pings found the cache cold' in plan(e)['reason']


def test_the_count_belongs_to_the_route_it_was_seen_on(engine, pulse):
    """Review (Codex): a warm reuse on another route doesn't clear it, and it doesn't hold off another route."""
    e = engine
    warm_chat(e)
    pulse(e, 'warm on')
    ping(e, read=0)
    ping(e, read=0)
    assert warm_wake(e).next_due_at is None
    other = 'anthropic|claude-sonnet-5-5|https://api.anthropic.com'
    rows = json.loads(e.store.get_meta('cache_routes'))
    rows[other] = {'max_warm_gap': TTL}
    e.store.set_meta('cache_routes', json.dumps(rows))
    for after in (0, 1000):               # two calls on the other route, the second warm after 1000 s
        e.clock.advance(after)
        cache.touch(e.store, e.store.get_session(KEY), started_at=e.clock.now(), route=other, prompt=300000, read=299000)
    assert 'due_at' in plan(e)            # the other route isn't held off by the first route's count
    e.clock.advance(5)
    cache.touch(e.store, e.store.get_session(KEY), started_at=e.clock.now(), route=ROUTE, prompt=300000, read=299000)
    assert 'the last 2 pings found the cache cold' in plan(e)['reason']   # back on the first route: still stopped


def test_a_route_that_reports_its_ttl_keeps_pinging_as_before(engine, pulse, monkeypatch):
    """Review (Codex): the stop applies only where a Claude route's lifetime was learned from gaps."""
    e = engine
    monkeypatch.setattr(cache, 'cache_ttl_for', lambda *a, **k: (3600, 'hermes'))
    warm_chat(e)
    pulse(e, 'warm on')
    ping(e, read=0)
    ping(e, read=0)
    assert warm_wake(e).next_due_at is not None


def test_a_cold_ping_on_another_route_keeps_the_first_routes_count(engine):
    """Review (Codex r2): each route in a chat keeps its own count."""
    e = engine
    a, b = 'anthropic|claude-opus-5-5|https://api.anthropic.com', 'anthropic|claude-sonnet-5-5|https://api.anthropic.com'
    cache.note_cold_ping(e.store, KEY, a)
    cache.note_cold_ping(e.store, KEY, a)
    cache.note_cold_ping(e.store, KEY, b)
    assert (cache.cold_pings(e.store, KEY, a), cache.cold_pings(e.store, KEY, b)) == (2, 1)
    cache.clear_cold_pings(e.store, KEY, b)
    assert (cache.cold_pings(e.store, KEY, a), cache.cold_pings(e.store, KEY, b)) == (2, 0)
    cache.clear_cold_pings(e.store, KEY)
    assert cache.cold_pings(e.store, KEY, a) == 0


def test_a_learned_claude_ttl_is_capped_and_stopped_too(engine, monkeypatch):
    """Review (Codex r3): with both gaps learned (warm 4000 s, cold 5000 s) on Claude, the hour cap and the two-ping stop apply."""
    e, now = engine, engine.clock.now()
    route = 'directsdk|claude-opus-5-5|'
    monkeypatch.setattr(cache, 'cache_ttl_for', lambda *a, **k: (4000, 'learned'))   # what the learned bracket gives
    session(e, last_route=route, last_prompt_tokens=580000, last_cache_touch_at=now, last_human_at=now)
    s = e.store.get_session(KEY)
    assert cache.warm_lifetime(e.store, s) == (3600, 'learned')
    cache.note_cold_ping(e.store, KEY, route)
    cache.note_cold_ping(e.store, KEY, route)
    assert 'the last 2 pings found the cache cold' in keep_warm_plan(e.store, s, e.policy, now)['reason']
