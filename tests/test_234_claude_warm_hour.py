"""#234: on a Claude route a cache seen warm past 5 minutes has the hour; a keep-warm that can't ping says so once."""
import json

import pytest

from heartbeat2 import cache, commands
from heartbeat2.cache import keep_warm_plan
from heartbeat2.desk import render_desk
from heartbeat2.poller import KEEP_WARM_NOTICE_AFTER
from test_engine import engine, session, admit, KEY

CLAUDE = 'claude-subscription|claude-fable-5-2[1m]|process://claude-subscription'
OTHER = 'custom|qwen3|http://localhost:8000/v1'


@pytest.fixture(autouse=True)
def no_reported_ttl(monkeypatch):
    monkeypatch.setattr(cache, 'cache_ttl_for', lambda *a, **k: (None, 'unknown'))


def chat(e, route, row):
    now = e.clock.now()
    e.store.set_meta('cache_routes', json.dumps({route: row} if row is not None else {}))
    session(e, last_route=route, last_prompt_tokens=370000, last_cache_touch_at=now, last_human_at=now)
    return e.store.get_session(KEY)


def test_new_claude_route_with_a_22_minute_warm_gap_pings_on_the_hour(engine):
    """The reported route: warm seen across 22.6m, cold after 69.7m. It used to refuse ('needs 30m')."""
    e, now = engine, engine.clock.now()
    s = chat(e, CLAUDE, {'max_warm_gap': 1359.3, 'min_cold_gap': 4183.1})
    assert cache.warm_lifetime(e.store, s) == (3600, 'warm_seen')
    plan = keep_warm_plan(e.store, s, e.policy, now)
    assert plan['due_at'] == now + 3600 - max(120, .08 * 3600) and 'blocked' not in plan
    # After an install or restart, the ping is timed from the chat's last model call, not from the restart.
    e.clock.advance(1800)
    plan = keep_warm_plan(e.store, e.store.get_session(KEY), e.policy, e.clock.now())
    assert plan['due_at'] == now + 3600 - max(120, .08 * 3600)


def test_a_five_minute_claude_cache_still_refuses(engine):
    e, now = engine, engine.clock.now()
    s = chat(e, CLAUDE, {'max_warm_gap': 240})
    plan = keep_warm_plan(e.store, s, e.policy, now)
    assert 'due_at' not in plan and plan['blocked'] == 'short' and 'needs 30m' in plan['reason']
    assert 'lasts 5m or less' in plan['reason']   # a Claude cache is 5 minutes or the hour, never "4m" (review)
    s = chat(e, OTHER, {'max_warm_gap': 240})
    assert 'lasts 4m;' in keep_warm_plan(e.store, s, e.policy, now)['reason']


@pytest.mark.parametrize('learned,expect', [(1359.3, 3600), (600, 600), (240, 240)])
def test_learned_claude_lifetime_past_5_minutes_is_the_hour(engine, monkeypatch, learned, expect):
    e = engine
    s = chat(e, CLAUDE, {})
    monkeypatch.setattr(cache, 'ttl_for_session', lambda *a, **k: (learned, 'learned'))
    assert cache.warm_lifetime(e.store, s) == (expect, 'learned')


def test_other_routes_keep_their_measured_gap(engine):
    e = engine
    s = chat(e, OTHER, {'max_warm_gap': 1359.3})
    assert cache.warm_lifetime(e.store, s) == (1359.3, 'warm_seen')


def keep_warm(e):
    from heartbeat2.model import Wake
    s = e.store.get_session(KEY)
    e.store.create_wake(Wake(KEY, 'Keep the prompt cache warm.', 'self', id=commands.keep_warm_id(KEY), platform=s.platform,
        chat_type=s.chat_type, chat_id=s.chat_id, creator='user', created_in='command', title='Keep-warm',
        trigger_json={'builtin': True, 'keep_warm': True}))
    return e.store.get_wake(commands.keep_warm_id(KEY))


def blocked_two_hours(e):
    """Keep-warm tells the owner only once it has been unable to ping for two hours (review)."""
    e.tick()
    e.clock.advance(KEEP_WARM_NOTICE_AFTER)
    e.tick()


def owner_context(e, turn):
    admit(e, mid='m'+turn)
    return str(e.intake.pre_llm_call(session_id='sid', turn_id=turn, user_message='hello', platform='telegram'))


def test_a_keep_warm_that_cannot_ping_says_so_once(engine):
    e = engine
    chat(e, CLAUDE, {'max_warm_gap': 240})    # a 5-minute cache: keep-warm can't help here
    w = keep_warm(e)
    blocked_two_hours(e)
    assert e.store.get_wake(w.id).next_due_at is None
    first = owner_context(e, 't1')
    assert "can't ping in this chat" in first and 'needs 30m' in first
    assert "can't ping in this chat" not in owner_context(e, 't2')   # once, not on every message
    # Role and own-time turns read the desk, so it never lists this (review); /pulse warm, status and list say why.
    assert 'keep-warm' not in render_desk(e, e.store.get_session(KEY), clock=e.clock, timezone='UTC', policy=e.policy)
    # The route learns the hour: keep-warm pings again, and the stored state goes.
    chat(e, CLAUDE, {'max_warm_gap': 1359.3})
    e.tick()
    assert e.store.get_wake(w.id).next_due_at is not None
    assert not e.store.get_meta('keep_warm_blocked:'+w.id)


def test_resting_and_small_context_stay_quiet(engine):
    e = engine
    chat(e, CLAUDE, {'max_warm_gap': 3594})
    session(e, last_prompt_tokens=1000)          # small context: a cold start is cheap, nothing to report
    w = keep_warm(e)
    e.tick()
    assert e.store.get_wake(w.id).next_due_at is None
    assert "can't ping" not in owner_context(e, 't1')


def test_no_stale_line_after_the_reason_changes_and_pings_resume(engine):
    """Review: 'lasts 4m' then 'lasts 4.7m' (same kind), then a long cache: no 'can't ping' line once it pings."""
    e = engine
    chat(e, OTHER, {'max_warm_gap': 240})
    w = keep_warm(e)
    blocked_two_hours(e)
    assert 'lasts 4m;' in (e.store.get_meta('card:'+w.id) or '')
    chat(e, OTHER, {'max_warm_gap': 280})
    e.tick()
    assert 'lasts 4.7m;' in (e.store.get_meta('card:'+w.id) or '')  # the unshown line follows the new numbers
    chat(e, OTHER, {'max_warm_gap': 3594})
    e.tick()
    assert e.store.get_wake(w.id).next_due_at is not None
    assert "can't ping" not in owner_context(e, 't1')


def test_rest_in_between_does_not_tell_again(engine):
    """Review: blocked, told once, then the owner rests 13 h, then blocked again for the same reason: no second line."""
    e = engine
    chat(e, CLAUDE, {'max_warm_gap': 240})
    w = keep_warm(e)
    blocked_two_hours(e)
    assert "can't ping" in owner_context(e, 't1')
    e.clock.advance(13*3600)                     # resting: no message for longer than keep_warm_idle_hours
    e.tick()
    assert e.store.get_meta('keep_warm_blocked:'+w.id)
    session(e, last_human_at=e.clock.now(), last_cache_touch_at=e.clock.now())
    e.tick()
    assert "can't ping" not in owner_context(e, 't2')


def test_no_line_in_the_first_two_hours_after_a_model_switch(engine):
    """Review: a new route is still learning its lifetime; that is not news until it has lasted two hours."""
    e = engine
    chat(e, CLAUDE, None)                        # just switched: nothing learned for this route yet
    w = keep_warm(e)
    e.tick()
    assert json.loads(e.store.get_meta('keep_warm_blocked:'+w.id))['kind'] == 'unknown'
    e.clock.advance(KEEP_WARM_NOTICE_AFTER - 60)
    e.tick()
    assert "can't ping" not in owner_context(e, 't1')
    chat(e, CLAUDE, {'max_warm_gap': 240})       # a different reason starts its own two hours
    e.tick()
    e.clock.advance(KEEP_WARM_NOTICE_AFTER - 60)
    e.tick()
    assert "can't ping" not in owner_context(e, 't2')
    e.clock.advance(60)
    e.tick()
    assert 'lasts 5m or less' in owner_context(e, 't3')


def test_small_or_resting_chats_report_no_blocked_keep_warm(engine):
    """Review: a small or resting chat would get no ping anyway, so a short cache there is not a blocked keep-warm."""
    e, now = engine, engine.clock.now()
    chat(e, CLAUDE, {'max_warm_gap': 240})
    session(e, last_prompt_tokens=1000)
    plan = keep_warm_plan(e.store, e.store.get_session(KEY), e.policy, now)
    assert 'blocked' not in plan and 'lasts 5m or less' in plan['reason']
    session(e, last_prompt_tokens=370000, last_human_at=now - 13*3600)
    plan = keep_warm_plan(e.store, e.store.get_session(KEY), e.policy, now)
    assert 'blocked' not in plan and 'lasts 5m or less' in plan['reason']
    w = keep_warm(e)
    session(e, last_prompt_tokens=1000)
    blocked_two_hours(e)
    assert "can't ping" not in owner_context(e, 't1')


def test_no_notice_while_heartbeat_is_off_in_the_chat(engine):
    """Review: /pulse off means HeartBeat may not speak here; the line waits until it may."""
    e = engine
    chat(e, CLAUDE, {'max_warm_gap': 240})
    w = keep_warm(e)
    blocked_two_hours(e)
    session(e, enabled=False)
    assert "can't ping" not in owner_context(e, 't1')
    assert e.store.get_meta('card:'+w.id)          # kept, not dropped
    session(e, enabled=True)
    assert "can't ping in this chat" in owner_context(e, 't2')


def test_a_new_kind_drops_an_unshown_line_and_tells_once(engine):
    """Review: an unshown line for one reason, then another reason: the owner is told once, about the new reason."""
    e = engine
    chat(e, OTHER, None)                         # nothing learned: lifetime unknown
    w = keep_warm(e)
    blocked_two_hours(e)
    assert e.store.get_meta('card:'+w.id)        # due, not shown yet
    e.store.set_meta('cache_routes', json.dumps({OTHER: {'max_warm_gap': 240}}))  # another call teaches 4 minutes
    e.tick()
    assert not e.store.get_meta('card:'+w.id)
    assert "can't ping" not in owner_context(e, 't1')
    e.clock.advance(KEEP_WARM_NOTICE_AFTER)
    e.tick()
    assert 'lasts 4m;' in owner_context(e, 't2')
    e.clock.advance(KEEP_WARM_NOTICE_AFTER)
    e.tick()
    assert "can't ping" not in owner_context(e, 't3')


def test_warm_off_and_on_starts_the_two_hours_afresh(engine, monkeypatch):
    """Review: state kept across /pulse warm off and on made the line due on the first tick after on."""
    monkeypatch.setattr(commands, 'session_env', lambda name: KEY if name == 'HERMES_SESSION_KEY' else '')
    pulse = lambda args: commands.handle(args, type('Ctx', (), {'get_engine': staticmethod(lambda: e)})())
    e = engine
    chat(e, CLAUDE, {'max_warm_gap': 240})
    w = keep_warm(e)
    e.tick()                                     # blocked, not yet two hours
    assert pulse('warm off') == 'Keep-warm is off for this chat.'
    e.clock.advance(KEEP_WARM_NOTICE_AFTER)
    session(e, last_human_at=e.clock.now(), last_cache_touch_at=e.clock.now())
    pulse('warm on')
    e.tick()
    assert not e.store.get_meta('card:'+w.id)
    assert "can't ping" not in owner_context(e, 't1')


def test_a_reaction_does_not_hand_the_line_to_a_turn_the_owner_did_not_start(engine):
    """Review: a reaction marks the chat as the owner's; the next turn may be one the agent started (a standing goal)."""
    e = engine
    chat(e, CLAUDE, {'max_warm_gap': 240})
    w = keep_warm(e)
    blocked_two_hours(e)
    e.intake.gateway_platform_event(platform='telegram', event_type='reaction', payload={'chat_id': 'unit', 'emoji': 'ok'})
    assert e.store.get_meta('human_context:'+KEY)
    auto = str(e.intake.pre_llm_call(session_id='sid', turn_id='auto1', user_message='continue the goal', platform='telegram'))
    assert "can't ping" not in auto
    assert e.store.get_meta('card:'+w.id)          # still waiting for the owner
    assert "can't ping in this chat" in owner_context(e, 't1')


def test_a_model_switch_gets_its_own_two_hours_and_drops_the_old_line(engine):
    """Review: the stored state belongs to the route; a new model must not inherit its timer, told flag or unshown line."""
    e = engine
    other = 'claude-subscription|claude-opus-5-5|process://claude-subscription'
    chat(e, CLAUDE, None)                        # unknown
    w = keep_warm(e)
    e.tick()
    e.clock.advance(KEEP_WARM_NOTICE_AFTER - 600)
    e.tick()
    chat(e, other, None)                         # switched: also unknown, but a new route
    e.tick()
    e.clock.advance(1200)
    e.tick()
    assert "can't ping" not in owner_context(e, 't1')
    e.clock.advance(KEEP_WARM_NOTICE_AFTER - 1200)
    e.tick()
    assert e.store.get_meta('card:'+w.id)        # its own two hours: due now, not yet shown
    session(e, last_route=CLAUDE, last_prompt_tokens=1000)  # back to the first model, small context: no ping, not blocked
    e.tick()
    assert not e.store.get_meta('card:'+w.id) and not e.store.get_meta('keep_warm_blocked:'+w.id)
    assert "can't ping" not in owner_context(e, 't2')


def test_a_warm_gap_under_15_minutes_does_not_prove_the_hour(engine):
    """Review: 400 s warm can be timing skew on a 5-minute cache (the bound the cold-ping reset already uses)."""
    e, now = engine, engine.clock.now()
    s = chat(e, CLAUDE, {'max_warm_gap': 400})
    assert cache.warm_lifetime(e.store, s) == (400, 'warm_seen')
    plan = keep_warm_plan(e.store, s, e.policy, now)
    assert 'due_at' not in plan and plan['blocked'] == 'short' and 'lasts 5m or less' in plan['reason']
    s = chat(e, CLAUDE, {'max_warm_gap': 901})
    assert cache.warm_lifetime(e.store, s) == (3600, 'warm_seen')


def test_new_conversation_keeps_a_due_line(engine):
    """Review: /new clears the chat's route until the next model call; that is not a model switch."""
    e = engine
    chat(e, CLAUDE, {'max_warm_gap': 240})
    w = keep_warm(e)
    blocked_two_hours(e)
    assert e.store.get_meta('card:'+w.id)
    session(e, last_route='')                    # what /new leaves behind
    e.tick()
    assert e.store.get_meta('card:'+w.id) and e.store.get_meta('keep_warm_blocked:'+w.id)
    session(e, last_route=CLAUDE)
    assert "can't ping in this chat" in owner_context(e, 't1')


def test_state_from_0_4_32_without_a_route_is_kept(engine):
    """Review: an owner already told under 0.4.32 (state without 'route') is not told again after the upgrade."""
    e = engine
    chat(e, CLAUDE, {'max_warm_gap': 240})
    w = keep_warm(e)
    e.store.set_meta('keep_warm_blocked:'+w.id, json.dumps({'kind': 'short', 'reason': 'x', 'since': e.clock.now(), 'told': True}))
    e.clock.advance(3 * KEEP_WARM_NOTICE_AFTER)
    session(e, last_human_at=e.clock.now(), last_cache_touch_at=e.clock.now())
    e.tick()
    e.clock.advance(KEEP_WARM_NOTICE_AFTER)      # the re-tell would come two hours after the upgrade
    session(e, last_human_at=e.clock.now(), last_cache_touch_at=e.clock.now())
    e.tick()
    assert not e.store.get_meta('card:'+w.id)
    assert "can't ping" not in owner_context(e, 't1')
