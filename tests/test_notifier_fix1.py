"""FIX1-197B regressions; isolated profiles and bounded thread coordination."""
from dataclasses import replace
import json
import threading
import time

import pytest

from test_app_sessions import bind
from test_engine import engine, KEY, arm, pending, session, GatewayCtx
from test_notifier import held, offer, settle
from heartbeat2 import commands
from heartbeat2.notifier import held_wakes, notice_state, EPISODE_PREFIX
from heartbeat2.registration import Runtime, register


def test_close_bounds_blocked_send_and_keeps_store_open(engine, monkeypatch):
    calls, registry, _ = held(engine, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    def send(*args):
        entered.set()
        release.wait(15)
        return {'ok': True}
    offer(registry, 2, send=send)
    rt = Runtime(engine.ctx, clock=engine.clock)
    rt._engine = engine
    engine.clock.advance(600)
    engine.notifications.tick(engine)
    assert entered.wait(1)
    closer = threading.Thread(target=rt.close, daemon=True)
    try:
        start = time.monotonic()
        closer.start()
        closer.join(6)
        assert not closer.is_alive(), 'close exceeded six seconds'
        assert time.monotonic()-start < 6
        assert engine.store.connection.execute('SELECT 1').fetchone()[0] == 1
    finally:
        release.set()
        closer.join(2)
        assert engine.notifications.idle.wait(2)


def test_close_does_not_deadlock_store_holder_restart(engine, monkeypatch):
    rt = Runtime(engine.ctx, clock=engine.clock)
    rt._engine = engine
    monkeypatch.setattr(rt, 'start', lambda: None)
    closing, locked = threading.Event(), threading.Event()
    failures = []
    close = engine.notifications.close
    def tracked_close():
        closing.set()
        close()
    monkeypatch.setattr(engine.notifications, 'close', tracked_close)
    def intake_restart():
        with engine.store._lock:
            locked.set()
            if not closing.wait(1):
                failures.append('close did not signal worker')
                return
            # Bound the old lock inversion so the negative test can release its store.
            if not rt._engine_lock.acquire(timeout=1):
                failures.append('restart blocked behind close while holding store')
                return
            try: rt.restart()
            finally: rt._engine_lock.release()
    holder = threading.Thread(target=intake_restart, daemon=True)
    closer = threading.Thread(target=rt.close, daemon=True)
    holder.start()
    assert locked.wait(1)
    closer.start()
    holder.join(2); closer.join(2)
    assert not holder.is_alive() and not closer.is_alive()
    assert failures == []


def test_other_profile_registers_while_send_blocks(engine, monkeypatch, tmp_path):
    _, registry, _ = held(engine, monkeypatch)
    entered, release, closing = threading.Event(), threading.Event(), threading.Event()
    def send(*args):
        entered.set(); release.wait(10)
        return {'ok': True}
    offer(registry, 2, send=send)
    rt = Runtime(engine.ctx, clock=engine.clock)
    rt._engine = engine
    close = engine.notifications.close
    def tracked_close(): closing.set(); close()
    monkeypatch.setattr(engine.notifications, 'close', tracked_close)
    monkeypatch.setattr(Runtime, 'start', lambda self: None)
    engine.clock.advance(600); engine.notifications.tick(engine)
    assert entered.wait(1)
    closer = threading.Thread(target=rt.close, daemon=True)
    registered, successors = threading.Event(), []
    def other():
        successors.append(register(GatewayCtx(tmp_path/'other'), clock=engine.clock))
        registered.set()
    registrar = threading.Thread(target=other, daemon=True)
    try:
        closer.start(); assert closing.wait(1)
        registrar.start()
        assert registered.wait(1), 'another profile blocked by hanging notifier'
    finally:
        release.set(); closer.join(2); registrar.join(2)
        for successor in successors: successor.close()


@pytest.mark.parametrize('setting', [None, ''])
def test_blank_notifier_runtime_has_no_error_or_status(engine, monkeypatch, setting):
    monkeypatch.setattr(Runtime, 'start', lambda self: None)
    engine.ctx.settings['notify_via'] = setting
    rt = register(engine.ctx, clock=engine.clock)
    rt._engine = engine
    try:
        engine.policy = rt.policy()
        assert rt.settings_error == ''
        assert not rt.notify_via_invalid
        result = json.loads(rt.tool({'action': 'list'}, session_id='sid'))
        assert 'settings_error' not in result and 'error' not in result
        bind(monkeypatch, 'telegram')
        assert 'notify_via:' not in commands.handle('status', rt)
    finally: rt.close()


# Literal errors from Birdie b72abdb integrations/hermes/__init__.py.
@pytest.mark.parametrize('error,label', [
    ("Hermes doesn't have the BIRDIE_HERMES_TOKEN secret yet. Run `birdie setup hermes`: "
     "it shows the token and how to give it to Hermes. Then restart Hermes.", 'notifier not ready'),
    ("can't reach birdie at http://birdie.invalid; is `birdie serve` running?", 'may repeat'),
    ('target and text are required', 'waiting until'),
    ('TimeoutError', 'may repeat'),
])
def test_birdie_errors_visible_and_clipped(engine, monkeypatch, error, label):
    _, registry, _ = held(engine, monkeypatch)
    offer(registry, 2, send=lambda *args: {'ok': False, 'error': error})
    engine.clock.advance(600); engine.notifications.tick(engine); settle(engine)
    state = notice_state(engine, KEY)
    assert label in state and state.endswith(error[:80])
    data = json.loads(engine.store.get_meta(EPISODE_PREFIX+KEY))
    assert data['error'] == error[:120]
    engine.clock.advance(86400); engine.notifications.tick(engine)
    assert notice_state(engine, KEY) == 'notify failed · '+error[:80]


@pytest.mark.parametrize('reason', ['wake_spacing', 'busy', 'held_unanswered', 'provider_unavailable', 'quiet_hours'])
def test_stale_app_closed_stamp_does_not_count(engine, monkeypatch, reason):
    calls, _, rows = held(engine, monkeypatch)
    w, o = rows[0]
    engine.clock.advance(600)
    if reason == 'wake_spacing':
        engine.ctx.accept = True
        session(engine, last_wake_turn_at=engine.clock.now())
        engine.dispatch(engine.store.get_occurrence(w.id, o.dedupe_key), cache_line='')
        assert engine.store.get_occurrence(w.id, o.dedupe_key).error == reason
    else:
        engine.store.update_occurrence(w.id, o.dedupe_key, 'pending', error=reason)
    assert held_wakes(engine, KEY) == []
    assert commands.app_closed_hold(engine, engine.store.get_wake(w.id)) == ''
    engine.notifications.tick(engine); settle(engine)
    # No notice while another reason holds the wake, but the wait (its episode) lasts while the stamp remains.
    assert calls == [] and engine.store.get_meta(EPISODE_PREFIX+KEY) is not None


def test_paused_held_wake_does_not_count(engine, monkeypatch):
    calls, _, rows = held(engine, monkeypatch)
    engine.store.cas_wake(rows[0][0].id, 'active', status='paused')
    engine.clock.advance(600)
    assert held_wakes(engine, KEY) == []
    assert commands.app_closed_hold(engine, engine.store.get_wake(rows[0][0].id)) == ''
    engine.notifications.tick(engine); settle(engine)
    assert calls == []


def test_chat_quiet_override_defers_notifier(engine, monkeypatch):
    calls, _, _ = held(engine, monkeypatch)
    engine.store.set_meta('quiet:'+KEY, json.dumps({'range': '00:00-23:59'}))
    engine.clock.advance(600); engine.notifications.tick(engine); settle(engine)
    assert calls == [] and 'quiet hours' in notice_state(engine, KEY)
    engine.store.set_meta('quiet:'+KEY, json.dumps({'range': ''}))
    engine.notifications.tick(engine); settle(engine)
    assert len(calls) == 1


def test_blank_notifier_hidden_on_desk_even_with_episode(engine, monkeypatch):
    held(engine, monkeypatch)
    engine.policy = replace(engine.policy, notify_via='')
    bind(monkeypatch, 'desktop')
    rt = type('View', (), {'get_engine': lambda self: engine})()
    for command in ('status', 'desk'):
        assert 'notify_via:' not in commands.handle(command, rt)


def test_refused_keep_warm_does_not_create_episode(engine, monkeypatch):
    from heartbeat2 import cache
    held(engine, monkeypatch, count=0)
    # Exercise a legacy local ping at the refund boundary; new local pings aren't armed.
    monkeypatch.setattr(cache, 'keep_warm_plan', lambda *args: {'due_at': engine.clock.now(), 'touch': 1})
    w = arm(engine, kind='every', platform='desktop', trigger_json={'builtin': True, 'keep_warm': True})
    o = pending(engine, w, key=cache.keep_warm_key(1))
    engine.dispatch(o, cache_line='')
    assert engine.store.get_occurrence(w.id, o.dedupe_key).error == 'app_closed'
    assert engine.store.get_meta(EPISODE_PREFIX+KEY) is None


def test_runtime_restart_preserves_episode_and_retry_deadline(engine, monkeypatch):
    monkeypatch.setattr(Runtime, 'start', lambda self: None)
    engine.ctx.settings.update(notify_via='birdie:assistant', quiet_hours='', timezone='UTC')
    rt = register(engine.ctx, clock=engine.clock)
    rt._engine = engine
    calls, registry, _ = held(engine, monkeypatch)
    offer(registry, 2, send=lambda *args: calls.append(args) or {'ok': False, 'error': 'refused'})
    engine.clock.advance(600); engine.notifications.tick(engine); settle(engine)
    before = json.loads(engine.store.get_meta(EPISODE_PREFIX+KEY))
    rt.close()
    successor = register(engine.ctx, clock=engine.clock)
    try:
        new = successor.get_engine()
        new.notifier = registry.get
        after = json.loads(new.store.get_meta(EPISODE_PREFIX+KEY))
        assert after == before
        new.notifications.tick(new); settle(new)
        assert len(calls) == 1
        engine.clock.advance(300); new.notifications.tick(new); settle(new)
        assert len(calls) == 2
        assert json.loads(new.store.get_meta(EPISODE_PREFIX+KEY))['id'] == before['id']
    finally: successor.close()


@pytest.mark.parametrize('reason', ['quiet_hours', 'provider_unavailable', 'paused'])
def test_deferral_flip_keeps_episode_and_sends_one_notice(engine, monkeypatch, reason):
    """A wake held overnight is deferred by quiet hours (or an outage, or a pause) and then held again: one notice per wait."""
    calls, _, rows = held(engine, monkeypatch)
    w, o = rows[0]
    engine.clock.advance(600); engine.notifications.tick(engine); settle(engine)
    assert len(calls) == 1
    if reason == 'paused': engine.store.cas_wake(w.id, 'active', status='paused')
    else: engine.store.update_occurrence(w.id, o.dedupe_key, 'pending', error=reason)
    engine.clock.advance(8*3600); engine.notifications.tick(engine); settle(engine)
    assert engine.store.get_meta(EPISODE_PREFIX+KEY) is not None
    if reason == 'paused': engine.store.cas_wake(w.id, 'paused', status='active')
    else: engine.store.update_occurrence(w.id, o.dedupe_key, 'pending', error='app_closed')
    engine.clock.advance(60); engine.notifications.tick(engine); settle(engine)
    assert len(calls) == 1
    # Reopening ends the wait; a later close starts a new one with its own single notice.
    engine.ctx.accept = True
    engine.dispatch(engine.store.get_occurrence(w.id, o.dedupe_key), cache_line='')
    assert engine.store.get_meta(EPISODE_PREFIX+KEY) is None
