"""#203: a cache miss after a gap too short to be expiry doesn't decide a route's cache lifetime."""
import json

from heartbeat2 import cache
from heartbeat2.cache import keep_warm_plan
from test_engine import engine, session, KEY

ROUTE = 'directsdk|claude-opus-5-5|'
OTHER = 'custom|qwen3|http://localhost:8000/v1'  # #228: a Claude route trusts its warm gap; other routes still count colds


def rows(e):
    return json.loads(e.store.get_meta('cache_routes') or '{}')


def call(e, at, read, prompt=500000):
    cache.touch(e.store, e.store.get_session(KEY), started_at=at, route=ROUTE, prompt=prompt, read=read)


def test_short_gap_miss_is_not_recorded_as_cold(engine, monkeypatch):
    monkeypatch.setattr(cache, 'cache_ttl_for', lambda *a, **k: (None, 'unknown'))
    e, now = engine, engine.clock.now()
    session(e, last_human_at=now)
    call(e, now, 0)
    call(e, now + 6, 0)              # a prompt change 6 s later: not expiry
    assert 'min_cold_gap' not in rows(e).get(ROUTE, {})
    call(e, now + 6 + 3606, 490000)   # warm across an hour
    call(e, now + 6 + 3606 + 1200, 0) # a real cold after 20 minutes still counts
    assert rows(e)[ROUTE] == {'max_warm_gap': 3606, 'min_cold_gap': 1200}


def test_an_existing_short_gap_cold_no_longer_blocks_keep_warm(engine, monkeypatch):
    """The reported route: warm seen across 60.1m, cold after 0.1m. Keep-warm now plans from the warm gap."""
    monkeypatch.setattr(cache, 'cache_ttl_for', lambda *a, **k: (None, 'unknown'))
    e, now = engine, engine.clock.now()
    e.store.set_meta('cache_routes', json.dumps({ROUTE: {'max_warm_gap': 3606, 'min_cold_gap': 6}}))
    session(e, last_route=ROUTE, last_prompt_tokens=570000, last_cache_touch_at=now, last_human_at=now)
    s = e.store.get_session(KEY)
    assert cache.warm_lifetime(e.store, s) == (3600, 'warm_seen')   # #228: a Claude lifetime is capped at the hour
    plan = keep_warm_plan(e.store, s, e.policy, now)
    assert plan['due_at'] == now + 3600 - max(120, .08 * 3600) and plan['ttl'] == 3600
    assert 'cold' not in cache.line(e.store, s, now, pricing=False)


def test_unknown_lifetime_says_why(engine, monkeypatch):
    monkeypatch.setattr(cache, 'cache_ttl_for', lambda *a, **k: (None, 'unknown'))
    e, now = engine, engine.clock.now()
    session(e, last_route=OTHER, last_prompt_tokens=570000, last_cache_touch_at=now, last_human_at=now)
    s = e.store.get_session(KEY)
    e.store.set_meta('cache_routes', json.dumps({OTHER: {'max_warm_gap': 3606, 'min_cold_gap': 1200}}))
    reason = keep_warm_plan(e.store, s, e.policy, now)['reason']
    assert 'stayed warm across 60.1m but went cold after 20m' in reason
    e.store.set_meta('cache_routes', json.dumps({}))
    assert 'no call has reused the cache after a gap' in keep_warm_plan(e.store, s, e.policy, now)['reason']


def test_other_routes_still_count_a_short_gap_miss(engine):
    """A local or other non-Claude cache may evict early (LRU), so a miss after a minute still counts (review)."""
    e, now = engine, engine.clock.now()
    route = 'custom|qwen3|http://localhost:8000/v1'
    session(e, last_route=route, last_prompt_tokens=500000, last_cache_touch_at=now, last_human_at=now)
    for at, read in [(now, 0), (now + 3606, 490000), (now + 3666, 0)]:
        cache.touch(e.store, e.store.get_session(KEY), started_at=at, route=route, prompt=500000, read=read)
    assert rows(e)[route] == {'max_warm_gap': 3606, 'min_cold_gap': 60}
    s = e.store.get_session(KEY)
    assert cache.warm_lifetime(e.store, s) == (None, 'unknown')
    assert 'due_at' not in keep_warm_plan(e.store, s, e.policy, e.clock.now())


def test_a_cold_ping_stops_keep_warm_on_a_recovered_row(engine, monkeypatch):
    """Off Claude, a route seen warm across 60.1m plans a ping; if that ping lands cold, keep-warm stops (review; Claude: #228)."""
    monkeypatch.setattr(cache, 'cache_ttl_for', lambda *a, **k: (None, 'unknown'))
    e, now = engine, engine.clock.now()
    e.store.set_meta('cache_routes', json.dumps({OTHER: {'max_warm_gap': 3606}}))
    session(e, last_route=OTHER, last_prompt_tokens=570000, last_cache_touch_at=now, last_human_at=now)
    plan = keep_warm_plan(e.store, e.store.get_session(KEY), e.policy, now)
    cache.touch(e.store, e.store.get_session(KEY), started_at=plan['due_at'], route=OTHER, prompt=500000, read=0)  # cold ping
    assert rows(e)[OTHER]['min_cold_gap'] == plan['due_at'] - now
    s = e.store.get_session(KEY)
    assert cache.warm_lifetime(e.store, s) == (None, 'unknown')
    assert 'due_at' not in keep_warm_plan(e.store, s, e.policy, e.clock.now())


def test_learned_ttl_ignores_a_sub_floor_cold_on_claude_only(engine, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'agent.agent_runtime_helpers', None)   # no Hermes-reported TTL
    e = engine
    other = 'custom|qwen3|http://localhost:8000/v1'
    e.store.set_meta('cache_routes', json.dumps({ROUTE: {'max_warm_gap': 100, 'min_cold_gap': 200},
                                                 other: {'max_warm_gap': 100, 'min_cold_gap': 200}}))
    # On Claude a 200 s miss isn't expiry, so it can't close a learned bracket; elsewhere it still does.
    assert cache.cache_ttl_for(ROUTE, store=e.store) == (None, 'unknown')
    assert cache.cache_ttl_for(other, store=e.store) == (100, 'learned')


def test_status_says_why_keep_warm_has_no_ping(engine, monkeypatch):
    """A route whose lifetime is unknown (warm 60.1m, cold 9.8m off Claude): /pulse status says why no ping is due."""
    from heartbeat2.commands import _item, keep_warm_id
    from heartbeat2.model import Wake
    monkeypatch.setattr(cache, 'cache_ttl_for', lambda *a, **k: (None, 'unknown'))
    e, now = engine, engine.clock.now()
    e.store.set_meta('cache_routes', json.dumps({OTHER: {'max_warm_gap': 3603.9, 'min_cold_gap': 585.6}}))
    session(e, last_route=OTHER, last_prompt_tokens=580000, last_cache_touch_at=now, last_human_at=now)
    s = e.store.get_session(KEY)
    e.store.create_wake(Wake(KEY, 'Keep the prompt cache warm.', 'self', id=keep_warm_id(KEY), platform=s.platform,
        chat_type=s.chat_type, chat_id=s.chat_id, creator='user', created_in='command', title='Keep-warm',
        trigger_json={'builtin': True, 'keep_warm': True}))
    wake = e.store.get_wake(keep_warm_id(KEY))
    assert wake.next_due_at is None
    text = _item(e, wake)
    assert 'no ping due now: ' in text
    assert 'stayed warm across 60.1m but went cold after 9.8m' in text
    assert 'pings stay off here rather than pay for cold ones' in text
