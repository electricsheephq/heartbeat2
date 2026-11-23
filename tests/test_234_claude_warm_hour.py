"""#234: on a Claude route a cache seen warm past 5 minutes has the hour; a keep-warm that can't ping says so once."""
import json

import pytest

from heartbeat2 import cache, commands
from heartbeat2.cache import keep_warm_plan
from heartbeat2.desk import render_desk
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


def test_a_five_minute_claude_cache_still_refuses(engine):
    e, now = engine, engine.clock.now()
    s = chat(e, CLAUDE, {'max_warm_gap': 240})
    plan = keep_warm_plan(e.store, s, e.policy, now)
    assert 'due_at' not in plan and plan['blocked'] == 'short' and 'needs 30m' in plan['reason']


@pytest.mark.parametrize('learned,expect', [(1359.3, 3600), (240, 240)])
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


def owner_context(e, turn):
    admit(e, mid='m'+turn)
    return str(e.intake.pre_llm_call(session_id='sid', turn_id=turn, user_message='hello', platform='telegram'))


def test_a_keep_warm_that_cannot_ping_says_so_once(engine):
    e = engine
    chat(e, CLAUDE, {'max_warm_gap': 240})    # a 5-minute cache: keep-warm can't help here
    w = keep_warm(e)
    e.tick()
    assert e.store.get_wake(w.id).next_due_at is None
    first = owner_context(e, 't1')
    assert "can't ping in this chat" in first and 'needs 30m' in first
    assert "can't ping in this chat" not in owner_context(e, 't2')   # once, not on every message
    desk = render_desk(e, e.store.get_session(KEY), clock=e.clock, timezone='UTC', policy=e.policy)
    assert f'keep-warm {w.id}: no ping' in desk
    # The route learns the hour: keep-warm pings again, and the desk line goes.
    chat(e, CLAUDE, {'max_warm_gap': 1359.3})
    e.tick()
    assert e.store.get_wake(w.id).next_due_at is not None
    assert 'keep-warm' not in render_desk(e, e.store.get_session(KEY), clock=e.clock, timezone='UTC', policy=e.policy)


def test_resting_and_small_context_stay_quiet(engine):
    e = engine
    chat(e, CLAUDE, {'max_warm_gap': 3594})
    session(e, last_prompt_tokens=1000)          # small context: a cold start is cheap, nothing to report
    w = keep_warm(e)
    e.tick()
    assert e.store.get_wake(w.id).next_due_at is None
    assert "can't ping" not in owner_context(e, 't1')
