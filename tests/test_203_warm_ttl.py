"""#203: a cache miss after a gap too short to be expiry doesn't decide a route's cache lifetime."""
import json

from heartbeat2 import cache
from heartbeat2.cache import keep_warm_plan
from test_engine import engine, session, KEY

ROUTE = 'directsdk|claude-opus-5-5|'


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
    assert cache.warm_lifetime(e.store, s) == (3606, 'warm_seen')
    plan = keep_warm_plan(e.store, s, e.policy, now)
    assert plan['due_at'] == now + 3606 - max(120, .08 * 3606) and plan['ttl'] == 3606
    assert 'cold' not in cache.line(e.store, s, now, pricing=False)


def test_unknown_lifetime_says_why(engine, monkeypatch):
    monkeypatch.setattr(cache, 'cache_ttl_for', lambda *a, **k: (None, 'unknown'))
    e, now = engine, engine.clock.now()
    session(e, last_route=ROUTE, last_prompt_tokens=570000, last_cache_touch_at=now, last_human_at=now)
    s = e.store.get_session(KEY)
    e.store.set_meta('cache_routes', json.dumps({ROUTE: {'max_warm_gap': 3606, 'min_cold_gap': 1200}}))
    reason = keep_warm_plan(e.store, s, e.policy, now)['reason']
    assert 'stayed warm across 60.1m but went cold after 20m' in reason
    e.store.set_meta('cache_routes', json.dumps({}))
    assert 'no call has reused the cache after a gap' in keep_warm_plan(e.store, s, e.policy, now)['reason']
