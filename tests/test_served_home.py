"""#113: only the gateway that serves a profile opens its store, holds its lock or delivers.

Hermes loads every profile's plugins at gateway boot and stamps its injector on
all of them, so a manager can carry an injector for a home this gateway never serves.
"""
import fcntl
import json
import logging
import time
from types import SimpleNamespace

from heartbeat2 import serving
from heartbeat2.model import Session
from heartbeat2.registration import register
from heartbeat2.store import Store
from test_engine import GatewayCtx, KEY, arm, engine, pending  # noqa: F401 (fixture)

SETTINGS = {'quiet_hours': '', 'timezone': 'UTC'}


def until(predicate, timeout=2):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if predicate(): return True
        time.sleep(.01)
    return predicate()


def locked(home):
    path = home/'plugin-data/heartbeat2/poller.lock'
    if not path.exists(): return False
    with open(path, 'a+') as f:
        try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


def unload(ctx):
    for cleanup in ctx.unloads: cleanup()


def boot(ctx, clock):
    return register(ctx, clock=clock, bootstrap_interval=.01, tick_interval=.01)


def test_foreign_home_opens_nothing(tmp_path, clock):
    ctx = GatewayCtx(tmp_path, settings=SETTINGS)
    ctx.runner.served = False
    rt = boot(ctx, clock)
    try:
        time.sleep(.2)
        assert rt._thread.is_alive()
        assert rt.hook('pre_llm_call', session_id='sid', turn_id='turn', platform='telegram') is None
        assert rt.hook('kanban_task_completed', task_id='t1') is None
        assert json.loads(rt.tool({'action': 'list'}))['error'] == 'engine_unavailable'
        assert 'inactive here' in rt.command('status')
        assert rt.status()['reason'] == serving.REASONS[serving.FOREIGN]
        assert rt._engine is None and rt._runner is None
        assert not (tmp_path/'plugin-data').exists()
        assert ctx.calls == []
    finally:
        unload(ctx)


def test_served_home_starts_when_injector_is_published(tmp_path, clock):
    ctx = GatewayCtx(tmp_path, settings=SETTINGS)
    ctx._manager.has_gateway_message_injector = False
    rt = boot(ctx, clock)
    try:
        time.sleep(.1)
        assert not (tmp_path/'plugin-data').exists()
        ctx._manager.has_gateway_message_injector = True
        assert until(lambda: locked(tmp_path))
        assert rt.status() is None
    finally:
        unload(ctx)


def test_unserved_mid_run_releases_lock_and_holds_due_wakes(tmp_path, clock):
    ctx = GatewayCtx(tmp_path, settings=SETTINGS)
    rt = boot(ctx, clock)
    store = None
    try:
        assert until(lambda: locked(tmp_path))
        ctx.runner.served = False
        assert until(lambda: not locked(tmp_path))
        store = Store(tmp_path/'plugin-data/heartbeat2/pulse.db', clock)
        store.upsert_session(Session(KEY, 'telegram', 'dm', 'unit', 'sid', clock.now(), clock.now(), 'episode'))
        arm(SimpleNamespace(store=store, clock=clock))
        clock.advance(10)
        time.sleep(.2)
        assert ctx.calls == [] and not locked(tmp_path)
        assert rt.status()['reason'] == serving.REASONS[serving.FOREIGN]
        ctx.runner.served = True
        clock.advance(10)
        assert ctx.injected.wait(timeout=2)
        assert len(ctx.calls) == 1
    finally:
        unload(ctx)
        if store: store.close()


def test_unknown_served_check_fails_closed_and_warns_once(tmp_path, clock, caplog):
    ctx = GatewayCtx(tmp_path, settings=SETTINGS)
    ctx._manager._gateway_message_injector = (object(), None)
    with caplog.at_level(logging.WARNING, logger='heartbeat2'):
        rt = boot(ctx, clock)
        try:
            time.sleep(.2)
            assert rt.hook('pre_llm_call', session_id='sid', turn_id='turn', platform='telegram') is None
            assert rt.status()['reason'] == serving.REASONS[serving.UNKNOWN]
            assert not (tmp_path/'plugin-data').exists()
        finally:
            unload(ctx)
    warned = [r for r in caplog.records if 'cannot tell whether this gateway serves' in r.getMessage()]
    assert len(warned) == 1


def test_dispatch_rechecks_before_claim(engine):
    e = engine
    w = arm(e)
    assert e.acquire()
    o = pending(e, w)
    e.ctx.runner.served = False
    e.dispatch(o)
    assert e.ctx.calls == []
    assert e.store.get_occurrence(w.id, o.dedupe_key).state == 'pending'
    e.ctx.runner.served = True
    e.dispatch(e.store.get_occurrence(w.id, o.dedupe_key))
    assert len(e.ctx.calls) == 1


def test_no_gateway_process_still_records_hook_events(tmp_path, clock):
    # CLI, TUI and kanban worker processes run this profile's own turns: they record, never deliver.
    ctx = GatewayCtx(tmp_path, cli=True, settings=SETTINGS)
    rt = boot(ctx, clock)
    try:
        rt.hook('kanban_task_completed', task_id='t1', board='main')
        assert [x['kind'] for x in rt.get_engine().store.pending_events()] == ['kanban_completed']
        assert not locked(tmp_path) and ctx.calls == []
    finally:
        unload(ctx)
