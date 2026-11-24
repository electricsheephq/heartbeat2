"""Standalone backend behavior and the unchanged gateway path."""
from dataclasses import replace
from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from heartbeat2 import commands, policy, serving, tool
from heartbeat2.model import Session, Wake
from heartbeat2.notifier import NOTIFY_AFTER_S, Notifier
from heartbeat2.registration import Runtime
from heartbeat2.store import Store
from test_engine import KEY, GatewayCtx


CHAT = 'fixture-app-chat'
ARM = {'action': 'create', 'note': 'Check the task.', 'trigger': {'kind': 'at', 'at': 'in 1 minute'}}
RECEIPT = ('Fires while the app has this chat open; if it is closed when due, '
           'it fires once when the chat is open again.')


def bind(monkeypatch, engine, key, *, platform='', source='desktop'):
    values = {'KEY': key, 'PLATFORM': platform, 'SOURCE': source, 'ID': key,
              'CHAT_TYPE': 'dm' if platform else ''}
    env = lambda name: values.get(name.removeprefix('HERMES_SESSION_'), '')
    monkeypatch.setattr(tool, 'session_env', env)
    monkeypatch.setattr(commands, 'session_env', env)
    engine.tool.session_key_fn = lambda: key


@pytest.fixture
def hosts(tmp_path, clock, monkeypatch):
    settings = {'quiet_hours': '', 'timezone': 'UTC', 'own_time_hours': 1}
    gateway, backend = (GatewayCtx(tmp_path, settings=settings) for _ in range(2))
    backend._manager.has_gateway_message_injector = False
    backend._manager.has_tui_message_injector = True
    monkeypatch.setattr(policy, 'local_injection_supported', lambda: False)
    runtimes = [Runtime(ctx, clock=clock) for ctx in (gateway, backend)]
    try:
        engines = [rt.get_engine() for rt in runtimes]
        for e in engines:
            e.paused = lambda: False
        yield engines
    finally:
        for rt in runtimes:
            rt.close()


@pytest.mark.parametrize('blank_row', [False, True])
def test_backend_identity_and_allowed_schedules(hosts, monkeypatch, blank_row):
    gateway, backend = hosts
    if blank_row:
        gateway.store.upsert_session(Session(CHAT, enabled=False))
        gateway.store.cas_session(CHAT, {'enabled': False}, enabled=True, unanswered_count=2)
    bind(monkeypatch, backend, CHAT)
    result = backend.tool.handle(ARM)
    assert result.get('armed'), result
    assert RECEIPT in result['receipt']
    session = backend.store.get_session(CHAT)
    assert (session.platform, session.chat_type, session.unanswered_count) == ('desktop', 'dm', 2 if blank_row else 0)
    assert backend.store.get_meta('host:' + CHAT) == 'backend'
    assert gateway.store.get_session(CHAT) is None
    reply = commands.handle('add every 10m Check the task.', SimpleNamespace(get_engine=lambda: backend))
    assert RECEIPT in reply
    recurring = backend.tool.handle({**ARM, 'trigger': {'kind': 'every', 'every': '10m'}})
    assert recurring.get('armed'), recurring
    assert [w.kind for w in backend.store.list_wakes(CHAT)].count('every') == 2


def test_backend_refuses_unsupported_work(hosts, monkeypatch):
    _, backend = hosts
    bind(monkeypatch, backend, CHAT)
    result = backend.tool.handle({**ARM, 'trigger': {'kind': 'file', 'path': 'fixture.txt'}})
    assert result['error'] == 'backend_schedule_only'
    assert 'watcher' in result['detail'].lower()
    runtime = SimpleNamespace(get_engine=lambda: backend)
    for action, text in [('warm on', 'keep-warm'), ('owntime on', 'own time')]:
        assert text in commands.handle(action, runtime).lower()
    backend.tick()
    assert not backend.store.list_wakes()
    assert not backend.store.get_meta('own_time_on:' + CHAT)


def test_only_backend_reads_stamped_rows(hosts, monkeypatch):
    gateway, backend = hosts
    bind(monkeypatch, gateway, KEY)
    gateway.store.upsert_session(Session(KEY, 'telegram', 'dm', 'fixture', KEY))
    assert gateway.tool.identity().platform == 'telegram'
    assert gateway.store.get_meta('host:' + KEY) is None
    # Seed a backend stamp directly so row isolation is tested even before arming works.
    gateway.store.upsert_session(Session(CHAT, 'desktop', 'dm', '', CHAT))
    gateway.store.set_meta('host:' + CHAT, 'backend')
    for key, platform in ((KEY, 'telegram'), (CHAT, 'desktop')):
        gateway.store.create_wake(Wake(key, 'Check the task.', 'at', id=key, platform=platform,
                                       chat_type='dm', next_due_at=gateway.clock.now()))
        gateway.store.add_occurrence(key, 't:due')
    gateway.tick()
    assert [key for _, key in gateway.ctx.calls] == [KEY]
    assert gateway.store.get_wake(CHAT) is None
    assert gateway.store.get_occurrence(CHAT, 't:due') is None
    assert backend.store.get_wake(KEY) is None
    assert backend.store.get_occurrence(CHAT, 't:due').state == 'pending'


def test_old_manager_and_cli_keep_gateway_behavior(hosts):
    gateway, backend = hosts
    assert gateway.eligible() and backend.eligible()
    backend.ctx._manager._cli_ref = object()
    assert not backend.eligible()
    backend.ctx._manager._cli_ref = None
    del backend.ctx._manager.has_tui_message_injector
    assert not backend.eligible()


def test_profile_cap_applies_across_hosts(hosts, monkeypatch):
    gateway, backend = hosts
    gateway.ctx.settings['max_active_profile'] = 1
    bind(monkeypatch, backend, CHAT)
    assert backend.tool.handle(ARM).get('armed')
    bind(monkeypatch, gateway, KEY, platform='telegram', source='telegram')
    assert gateway.tool.handle(ARM).get('error') == 'global_max_active'


def test_backend_peers_see_inflight_notice_reservation(hosts, monkeypatch):
    _, backend = hosts
    bind(monkeypatch, backend, CHAT)
    backend.refresh_policy = None
    backend.policy = replace(backend.policy, notify_via='fixture:owner', max_messages_per_day=1)
    assert backend.tool.handle(ARM).get('armed')
    backend.ctx.accept = False
    backend.clock.advance(61)
    backend.tick()
    entered, release = Event(), Event()

    def send(*args):
        entered.set()
        assert release.wait(3)
        return {'ok': True}

    transport = Notifier(send)
    backend.notifier = lambda: transport
    backend.clock.advance(NOTIFY_AFTER_S)
    with Store(backend.store.path, backend.clock, backend=True) as peer:
        try:
            backend.notifications.tick(backend)
            assert entered.wait(1)
            assert peer.daily_counts(CHAT, backend.policy.resolved_timezone())['delivered'] == 1
        finally:
            release.set()
            assert backend.notifications.idle.wait(3)
        assert peer.daily_counts(CHAT, backend.policy.resolved_timezone())['delivered'] == 1


@pytest.mark.parametrize('action,kind,trigger,error', [
    ('resume', 'reaction', {}, 'backend_schedule_only'),
    ('update', 'reaction', {}, 'backend_schedule_only'),
    ('resume', 'self', {'builtin': True, 'keep_warm': True}, 'builtin_settings_owned'),
    ('resume', 'self', {'builtin': True, 'own_time': True}, 'backend_schedule_only'),
])
def test_backend_rejects_stored_non_schedule(hosts, monkeypatch, action, kind, trigger, error):
    gateway, backend = hosts
    gateway.store.upsert_session(Session(CHAT, 'desktop', 'dm', '', CHAT))
    gateway.store.create_wake(Wake(CHAT, 'Check the task.', kind, id='legacy', status='paused',
                                   platform='desktop', chat_type='dm', trigger_json=trigger,
                                   base_interval_s=600, eff_interval_s=600))
    bind(monkeypatch, backend, CHAT)
    result = backend.tool.handle({**ARM, 'id': 'legacy', 'action': action})
    assert result['error'] == error
    assert backend.store.get_wake('legacy').status == 'paused'


def test_backend_command_cannot_resume_stored_own_time(hosts, monkeypatch):
    gateway, backend = hosts
    gateway.store.upsert_session(Session(CHAT, 'desktop', 'dm', '', CHAT))
    gateway.store.create_wake(Wake(CHAT, 'Check the task.', 'self', id='legacy', status='paused',
                                   platform='desktop', chat_type='dm', base_interval_s=600, eff_interval_s=600,
                                   trigger_json={'builtin': True, 'own_time': True}))
    bind(monkeypatch, backend, CHAT)
    result = commands.handle('resume legacy', SimpleNamespace(get_engine=lambda: backend))
    assert result.startswith('Error: backend_schedule_only')
    assert backend.store.get_wake('legacy').status == 'paused'


@pytest.fixture
def process_table(monkeypatch):
    class NoSuchProcess(Exception):
        pass

    processes = {os.getpid(): 30.0, os.getpid() + 1: 10.0, os.getpid() + 2: 20.0}

    def process(pid):
        if pid not in processes:
            raise NoSuchProcess
        return SimpleNamespace(create_time=lambda: processes[pid])

    monkeypatch.setattr(serving, 'pid_alive', lambda pid: pid in processes)
    monkeypatch.setitem(sys.modules, 'psutil', SimpleNamespace(
        Process=process, NoSuchProcess=NoSuchProcess, ZombieProcess=NoSuchProcess, AccessDenied=PermissionError))
    return processes


def backend_occurrence(backend, monkeypatch, owner):
    bind(monkeypatch, backend, CHAT)
    assert backend.tool.handle(ARM).get('armed')
    w, = backend.store.list_wakes()
    backend.store.add_occurrence(w.id, 'fixture')
    assert backend.store.claim(w.id, 'fixture', epoch=owner, pid=int(owner.split(':')[0]))
    return backend.store.get_occurrence(w.id, 'fixture')


def test_dead_claimant_recovery_keeps_live_busy_mark(hosts, monkeypatch, process_table):
    _, backend = hosts
    dead, live = os.getpid() + 1, os.getpid() + 2
    del process_table[dead]
    o = backend_occurrence(backend, monkeypatch, f'{dead}:10.0:dead')
    assert backend.store.cas_occurrence(o.wake_id, o.dedupe_key, 'claimed', 'started',
                                        attempt=o.attempt, started_at=backend.clock.now())
    busy = f'{live}:20.0:live'
    assert backend.store.cas_session(CHAT, {'busy_since': None}, busy_since=backend.clock.now(), busy_epoch=busy)
    assert backend.acquire()
    assert backend.store.get_occurrence(o.wake_id, o.dedupe_key).state == 'interrupted'
    assert backend.store.get_session(CHAT).busy_epoch == busy


def test_backend_pid_reuse_recovers_claim_busy_and_notice(hosts, monkeypatch, process_table):
    _, backend = hosts
    pid = os.getpid() + 1
    epoch = f'{pid}:{process_table[pid]}:peer'
    o = backend_occurrence(backend, monkeypatch, epoch)
    db, zone = backend.store, backend.policy.resolved_timezone()
    assert db.cas_session(CHAT, {'busy_since': None}, busy_since=backend.clock.now(), busy_epoch=epoch)
    db.reserve_backend_notice(CHAT, epoch)
    backend.reconcile()
    assert db.get_occurrence(o.wake_id, o.dedupe_key).state == 'claimed'
    assert db.get_session(CHAT).busy_epoch == epoch
    assert db.daily_counts(CHAT, zone)['delivered'] == 2

    process_table[pid] += 1
    backend.process_start = True
    backend.reconcile()
    assert db.get_occurrence(o.wake_id, o.dedupe_key).state == 'pending'
    assert db.get_session(CHAT).busy_epoch is None
    assert db.daily_counts(CHAT, zone)['delivered'] == 0


def test_backend_mints_epoch_with_create_time(process_table, hosts):
    _, backend = hosts
    pid, started, nonce = backend.epoch.split(':')
    assert int(pid) == os.getpid() and float(started) == process_table[os.getpid()] and nonce


def test_backend_pid_only_without_psutil(hosts, monkeypatch, process_table):
    _, backend = hosts
    monkeypatch.setitem(sys.modules, 'psutil', None)
    pid = os.getpid() + 1
    db = backend.store
    epoch = f'{pid}::peer'
    db.reserve_backend_notice(CHAT, epoch)
    assert db.notice_reserved(CHAT)
    del process_table[pid]
    assert not db.notice_reserved(CHAT)


def test_backend_notice_cleanup_survives_sqlite_busy(hosts, monkeypatch):
    _, backend = hosts
    worker, db = backend.notifications, backend.store
    release = db.release_backend_notice
    attempts = []
    cleaned = Event()

    def send(engine, key, *_):
        db.reserve_backend_notice(key, engine.epoch)

    def flaky_release(key, epoch):
        attempts.append((worker.job is None, db.notify_reservation is None, worker.idle.is_set()))
        if len(attempts) == 1:
            busy = sqlite3.OperationalError('database is locked')
            busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise busy
        release(key, epoch)
        cleaned.set()

    monkeypatch.setattr(worker, '_send', send)
    monkeypatch.setattr(db, 'release_backend_notice', flaky_release)
    worker.job = (backend, CHAT, 'fixture', None)
    db.notify_reservation = (CHAT, 'fixture')
    worker.idle.clear()
    worker.thread = Thread(target=worker._run, daemon=True)
    worker.thread.start()
    worker.ready.set()
    assert worker.idle.wait(1)
    assert cleaned.wait(2)
    assert worker.thread.is_alive()
    assert all(all(state) for state in attempts)
    assert not db.notice_reserved(CHAT)


def gateway_trace(package, home, clock, monkeypatch):
    intake, model, notifier, poller, policy, store, tool = [__import__(f'{package}.{name}', fromlist=[name])
        for name in ('intake', 'model', 'notifier', 'poller', 'policy', 'store', 'tool')]
    monkeypatch.setattr(poller, 'uuid', SimpleNamespace(uuid4=lambda: SimpleNamespace(hex='fixture-epoch')))
    monkeypatch.setattr(notifier, 'uuid', SimpleNamespace(uuid4=lambda: SimpleNamespace(hex='fixture-notice')))
    monkeypatch.setattr(intake, 'uuid', SimpleNamespace(uuid4=lambda: SimpleNamespace(hex='fixture-human-episode')))
    monkeypatch.setattr(policy, 'local_injection_supported', lambda: True)
    monkeypatch.setattr(tool, 'session_env', lambda name: {
        'HERMES_SESSION_PLATFORM': 'telegram', 'HERMES_SESSION_CHAT_TYPE': 'dm',
        'HERMES_SESSION_ID': 'reminder'}.get(name, ''))
    ctx = GatewayCtx(home)
    sends = []
    transport = notifier.Notifier(lambda target, text: sends.append((target, text)) or {'ok': True})
    with store.Store(home / 'pulse.db', clock) as db:
        cfg = policy.Policy(clock=clock, timezone='UTC', quiet_hours='', notify_via='fixture:owner')
        engine = poller.Poller(ctx, db, cfg, clock, paused=lambda: False, jitter=lambda: 0,
                               session_key_fn=lambda: 'reminder', compression_tip=lambda sid: sid)
        engine.notifier = lambda: transport
        inject = ctx.inject_message
        ctx.inject_message = lambda content, *, session_key: (
            False if session_key == 'closed' else inject(content, session_key=session_key))
        try:
            for wid, kind, platform in [('reminder', 'at', 'telegram'), ('repeat', 'every', 'telegram'),
                                        ('blocked', 'at', 'cli'), ('closed', 'at', 'desktop')]:
                db.upsert_session(model.Session(wid, platform, 'dm', wid, wid))
                db.create_wake(model.Wake(wid, 'Check the task.', kind, id=wid, platform=platform,
                                          chat_type='dm', armed_session_id=wid, next_due_at=clock.now(),
                                          base_interval_s=600, eff_interval_s=600))
            # Include orphaned ledger history left by an older store, plus expired events
            # and metadata. The differential checks the complete tables after GC.
            db.connection.execute('PRAGMA foreign_keys=OFF')
            db.add_ledger('2000-01-01', 'expired-wake', delivered=1)
            db.connection.execute('PRAGMA foreign_keys=ON')
            db.set_meta('agent_spend:reminder:2000-01-01', '{}')
            db.set_meta('accepted:expired-wake/old-slot', '1')
            db.add_event('reaction', 'expired-event', {}, session_key='reminder')
            db.connection.execute('UPDATE events SET consumed_at=?', (clock.now()-8*86400,))
            engine.intake.pre_llm_call(session_id='reminder', turn_id='human', user_message='hello',
                                       platform='telegram', conversation_history=[
                                           {'role': 'user', 'content': 'hello', 'platform_message_id': 'fixture'}])
            assert db.get_session('reminder').last_human_at == clock.now()
            engine.intake.on_session_end(session_id='reminder', turn_id='human', completed=True)
            trace = []
            for step, accept in [(0, True), (601, False), (60, True)]:
                clock.advance(step)
                ctx.accept = accept
                engine.tick()
                assert engine.notifications.idle.wait(2)
                trace.append((engine.status(), ctx.calls[:], sends[:], {
                    table: sorted(tuple(row) for row in db.connection.execute(f'SELECT * FROM {table}'))
                    for table in ('meta', 'sessions', 'wakes', 'occurrences', 'ledger', 'events')}))
            assert len(sends) == 1 and 'window is closed' in sends[0][1]
            return trace
        finally:
            engine.close()


def test_gateway_matches_v0434(tmp_path, clock, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    try:
        archive = subprocess.check_output(['git', 'archive', 'c87874589518a539b6a40ea90c0482a329d607ab',
                                           'heartbeat2'], cwd=root, stderr=subprocess.PIPE)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip('v0.4.34 baseline is unavailable; run in a Git checkout with the pinned release object')
    with tarfile.open(fileobj=BytesIO(archive)) as tree:
        if hasattr(tarfile, 'data_filter'):
            tree.extractall(tmp_path, filter='data')
        else:
            # This archive is produced locally from the exact, trusted release object above.
            tree.extractall(tmp_path)
    spec = spec_from_file_location('hb_v0434', tmp_path / 'heartbeat2/__init__.py')
    baseline = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, baseline)
    spec.loader.exec_module(baseline)
    clock_pair = type(clock)(clock.now(), clock.monotonic()), clock
    previous = gateway_trace(spec.name, tmp_path / 'baseline', clock_pair[0], monkeypatch)
    current = gateway_trace('heartbeat2', tmp_path / 'current', clock_pair[1], monkeypatch)
    assert len(current[-1][1]) >= 2
    for (status, calls, notices, tables), (old_status, old_calls, old_notices, old_tables) in zip(current, previous, strict=True):
        assert (status, calls, notices) == (old_status, old_calls, old_notices)
        for table, rows in tables.items():
            assert rows == old_tables[table], table


def test_gateway_baseline_unavailable_has_clear_skip(tmp_path, clock, monkeypatch):
    def missing(args, **kwargs):
        raise subprocess.CalledProcessError(128, args, stderr=b'fatal: not a valid object name')

    monkeypatch.setattr(subprocess, 'check_output', missing)
    with pytest.raises(pytest.skip.Exception, match='v0.4.34 baseline'):
        test_gateway_matches_v0434(tmp_path, clock, monkeypatch)


def test_gateway_baseline_extracts_on_older_tarfile(tmp_path, clock, monkeypatch):
    extract = tarfile.TarFile.extractall
    supports_filter = hasattr(tarfile, 'data_filter')

    def without_filter(self, path):
        if supports_filter:
            return extract(self, path, filter='data')
        return extract(self, path)

    monkeypatch.delattr(tarfile, 'data_filter', raising=False)
    monkeypatch.setattr(tarfile.TarFile, 'extractall', without_filter)
    test_gateway_matches_v0434(tmp_path, clock, monkeypatch)
