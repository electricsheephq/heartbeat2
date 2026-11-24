"""Standalone backend behavior and the unchanged gateway path."""
from dataclasses import replace
from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
from pathlib import Path
import subprocess
import sys
import tarfile
from threading import Event
from types import SimpleNamespace

import pytest

from heartbeat2 import commands, policy, tool
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


def gateway_trace(package, home, clock, monkeypatch):
    model, poller, policy, store = [__import__(f'{package}.{name}', fromlist=[name])
                                   for name in ('model', 'poller', 'policy', 'store')]
    monkeypatch.setattr(poller, 'uuid', SimpleNamespace(uuid4=lambda: SimpleNamespace(hex='fixture-epoch')))
    ctx = GatewayCtx(home)
    with store.Store(home / 'pulse.db', clock) as db:
        engine = poller.Poller(ctx, db, policy.Policy(clock=clock, timezone='UTC', quiet_hours=''), clock,
                               paused=lambda: False, jitter=lambda: 0)
        try:
            for wid, kind, platform in [('reminder', 'at', 'telegram'), ('repeat', 'every', 'telegram'),
                                        ('blocked', 'at', 'cli')]:
                db.upsert_session(model.Session(wid, platform, 'dm', wid, wid))
                db.create_wake(model.Wake(wid, 'Check the task.', kind, id=wid, platform=platform,
                                          chat_type='dm', armed_session_id=wid, next_due_at=clock.now(),
                                          base_interval_s=600, eff_interval_s=600))
            trace = []
            for step, accept in [(0, True), (601, False), (60, True)]:
                clock.advance(step)
                ctx.accept = accept
                engine.tick()
                trace.append((engine.status(), ctx.calls[:], {
                    table: sorted(tuple(row) for row in db.connection.execute(f'SELECT * FROM {table}'))
                    for table in ('meta', 'sessions', 'wakes', 'occurrences', 'ledger', 'events')}))
            return trace
        finally:
            engine.close()


def test_gateway_matches_v0434(tmp_path, clock, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    archive = subprocess.check_output(['git', 'archive', 'c87874589518a539b6a40ea90c0482a329d607ab',
                                       'heartbeat2'], cwd=root)
    with tarfile.open(fileobj=BytesIO(archive)) as tree:
        tree.extractall(tmp_path, filter='data')
    spec = spec_from_file_location('hb_v0434', tmp_path / 'heartbeat2/__init__.py')
    baseline = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, baseline)
    spec.loader.exec_module(baseline)
    clock_pair = type(clock)(clock.now(), clock.monotonic()), clock
    previous = gateway_trace(spec.name, tmp_path / 'baseline', clock_pair[0], monkeypatch)
    current = gateway_trace('heartbeat2', tmp_path / 'current', clock_pair[1], monkeypatch)
    assert len(current[-1][1]) >= 2
    assert current == previous
