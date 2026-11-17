"""A keep-warm ping held by a closed app window keeps the time it started waiting."""
from test_engine import engine, KEY, arm, pending
from test_notifier import held
from heartbeat2.notifier import EPISODE_PREFIX


def test_held_keep_warm_keeps_its_waiting_time_across_retries(engine, monkeypatch):
    from heartbeat2 import cache
    held(engine, monkeypatch, count=0)
    # A legacy local ping (new local pings aren't armed) retried while the app window stays closed.
    monkeypatch.setattr(cache, 'keep_warm_plan', lambda *args: {'due_at': engine.clock.now(), 'touch': 1})
    w = arm(engine, kind='every', platform='desktop', trigger_json={'builtin': True, 'keep_warm': True})
    o = pending(engine, w, key=cache.keep_warm_key(1))
    engine.dispatch(o, cache_line='')
    first = engine.store.get_meta('held_since:'+o.id)
    assert first
    for _ in range(3):
        engine.clock.advance(61)
        engine.dispatch(engine.store.get_occurrence(w.id, o.dedupe_key), cache_line='')
        assert engine.store.get_occurrence(w.id, o.dedupe_key).error == 'app_closed'
        assert engine.store.get_meta('held_since:'+o.id) == first
    assert engine.store.get_meta(EPISODE_PREFIX+KEY) is None
