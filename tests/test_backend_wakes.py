"""Standalone backend behavior and the unchanged gateway path."""
from dataclasses import replace
from hashlib import sha256
from importlib import import_module
from importlib.util import module_from_spec, spec_from_file_location
from inspect import getsource
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from heartbeat2 import commands, policy, poller, serving, tool
from heartbeat2.model import Session, Wake
from heartbeat2.notifier import EPISODE_PREFIX, NOTIFY_AFTER_S, Notifier, held_wakes, notice_text
from heartbeat2.poller import Poller
from heartbeat2.registration import Runtime
from heartbeat2.store import Store
from test_engine import KEY, GatewayCtx


CHAT = 'fixture-app-chat'
ARM = {'action': 'create', 'note': 'Check the task.', 'trigger': {'kind': 'at', 'at': 'in 1 minute'}}
RECEIPT = ('Fires while the app has this chat open; if it is closed when due, '
           'it fires once when the chat is open again.')
OTHER_WINDOW = ('Another Hermes app window on this profile delivers wakes right now; '
                'this wake waits until that one closes.')


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


@pytest.mark.parametrize('source', ['desktop', 'tui'])
def test_backend_repairs_stored_local_platform(hosts, monkeypatch, source):
    gateway, backend = hosts
    gateway.store.upsert_session(Session(CHAT, platform='local', unanswered_count=2))
    bind(monkeypatch, backend, CHAT, source=source)
    assert backend.tool.handle(ARM).get('armed')
    session = backend.store.get_session(CHAT)
    assert (session.platform, session.chat_type, session.unanswered_count) == (source, 'dm', 2)


@pytest.mark.parametrize('status,occurrence', [
    ('active', None), ('paused', None), ('proposed', None),
    ('cancelled', 'pending'), ('cancelled', 'claimed'), ('cancelled', 'started'),
])
def test_backend_does_not_stamp_gateway_work(hosts, monkeypatch, status, occurrence):
    gateway, backend = hosts
    db = gateway.store
    db.upsert_session(Session(CHAT, 'desktop', 'dm', '', CHAT))
    db.create_wake(Wake(CHAT, 'Existing wake.', 'at', id='legacy', status=status,
                        platform='desktop', chat_type='dm'))
    if occurrence:
        db.add_occurrence('legacy', 'slot')
        if occurrence != 'pending':
            assert db.claim('legacy', 'slot', epoch=gateway.epoch, pid=os.getpid())
        if occurrence == 'started':
            assert db.start('legacy', 'slot', attempt=1)
    bind(monkeypatch, backend, CHAT)
    assert backend.tool.handle(ARM) == {'error': 'non_gateway', 'detail': tool.APP_BACKEND_REFUSAL}
    assert commands.handle('add in 1m Check the task.', SimpleNamespace(get_engine=lambda: backend)) == tool.APP_BACKEND_REFUSAL
    assert db.get_meta('host:' + CHAT) is None
    assert db.get_wake('legacy').status == status
    if occurrence:
        assert db.get_occurrence('legacy', 'slot').state == occurrence


def test_backend_can_stamp_terminal_gateway_history(hosts, monkeypatch):
    gateway, backend = hosts
    db = gateway.store
    db.upsert_session(Session(CHAT, 'desktop', 'dm', '', CHAT))
    db.create_wake(Wake(CHAT, 'Existing wake.', 'at', id='legacy', status='cancelled',
                        platform='desktop', chat_type='dm'))
    db.add_occurrence('legacy', 'slot', state='skipped')
    bind(monkeypatch, backend, CHAT)
    assert backend.tool.handle(ARM).get('armed')
    assert backend.store.get_occurrence('legacy', 'slot').state == 'skipped'


def test_backend_commands_control_unstamped_gateway_work(hosts, monkeypatch):
    gateway, backend = hosts
    db = gateway.store
    db.upsert_session(Session(CHAT, 'desktop', 'dm', '', CHAT, enabled=True))
    db.create_wake(Wake(CHAT, 'Existing wake.', 'at', id='legacy', status='active',
                        platform='desktop', chat_type='dm', next_due_at=gateway.clock.now()+600))
    bind(monkeypatch, backend, CHAT, platform='local')
    runtime = SimpleNamespace(get_engine=lambda: backend, _other_engine=None)
    assert commands.handle('off', runtime) == 'HeartBeat 2.0 off for this chat.'
    assert not db.get_session(CHAT).enabled
    assert 'legacy active' in commands.handle('status', runtime)
    assert 'legacy active' in commands.handle('list', runtime)
    assert commands.handle('on', runtime) == 'HeartBeat 2.0 on for this chat.'
    assert db.get_session(CHAT).enabled
    assert commands.handle('add in 1m Check the task.', runtime) == tool.APP_BACKEND_REFUSAL
    assert db.get_meta('host:' + CHAT) is None
    assert [w.id for w in db.list_wakes(CHAT)] == ['legacy']
    assert backend.store.get_session(CHAT) is None
    assert not backend.store.list_wakes()


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
    gateway.store.set_meta('host:' + CHAT, 'backend')
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
    gateway.store.set_meta('host:' + CHAT, 'backend')
    gateway.store.create_wake(Wake(CHAT, 'Check the task.', 'self', id='legacy', status='paused',
                                   platform='desktop', chat_type='dm', base_interval_s=600, eff_interval_s=600,
                                   trigger_json={'builtin': True, 'own_time': True}))
    bind(monkeypatch, backend, CHAT)
    result = commands.handle('resume legacy', SimpleNamespace(get_engine=lambda: backend))
    assert result.startswith('Error: backend_schedule_only')
    assert backend.store.get_wake('legacy').status == 'paused'


@pytest.mark.parametrize('action,expected', [
    ('off', 'HeartBeat 2.0 off'), ('on', 'HeartBeat 2.0 on'),
    ('status', 'HeartBeat 2.0'), ('list', 'No wakes'),
    ('add in 1m Check the task.', 'own backend'), ('resume all', 'own backend'),
])
def test_unserved_app_command_only_refuses_arming(hosts, monkeypatch, action, expected):
    gateway, _ = hosts
    monkeypatch.setattr(policy, 'local_injection_supported', lambda: True)
    gateway.store.upsert_session(Session(CHAT, 'desktop', 'dm', '', CHAT, enabled=action != 'on'))
    bind(monkeypatch, gateway, CHAT, platform='local')
    # A non-serving Desktop runtime has the gateway store view for unstamped rows.
    gateway.ctx._manager.has_gateway_message_injector = False
    reply = commands.handle(action, SimpleNamespace(get_engine=lambda: gateway))
    assert expected in reply
    if action in {'off', 'on'}:
        assert gateway.store.get_session(CHAT).enabled == (action == 'on')
    assert gateway.store.get_meta('host:' + CHAT) is None


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


def test_only_backend_lock_holder_injects(hosts, monkeypatch, process_table):
    gateway, holder = hosts
    with Store(holder.store.path, holder.clock, backend=True) as store:
        ctx = GatewayCtx(holder.ctx._manager.home_path)
        ctx._manager = holder.ctx._manager
        peer = Poller(ctx, store, holder.policy, holder.clock, paused=lambda: False)
        peer_key = 'fixture-peer-chat'

        def inject(text, *, session_key):
            if session_key != CHAT:
                return False
            holder.ctx.calls.append(session_key)
            return True

        holder.ctx.inject_message = inject
        try:
            assert holder.acquire() and not peer.acquire()
            bind(monkeypatch, gateway, KEY, platform='telegram', source='telegram')
            assert OTHER_WINDOW not in gateway.tool.handle(ARM)['receipt']
            bind(monkeypatch, holder, CHAT)
            assert OTHER_WINDOW not in holder.tool.handle(ARM)['receipt']
            assert OTHER_WINDOW not in commands.handle(
                'add every 10m Check the task.', SimpleNamespace(get_engine=lambda: holder))
            # Distinct process identity with real competing lock files in one test process.
            pid = os.getpid() + 1
            store.set_meta('poller_pid', str(pid))
            store.set_meta('epoch', f'{pid}:{process_table[pid]}:holder')
            bind(monkeypatch, peer, peer_key)
            reply = peer.tool.handle(ARM)
            assert reply['armed']
            holder.clock.advance(61)
            holder.tick()
            peer.tick()
            assert len(peer.ctx.calls) == 0
            assert holder.ctx.calls == [CHAT]
            held, = store.list_occurrences(session_key=peer_key)
            assert (held.state, held.error) == ('pending', 'app_closed')
            assert store.get_meta('inject_failures:'+held.id) is None
            holder.close()
            peer.tick()
            assert peer._lock_file is not None
            assert [key for _, key in peer.ctx.calls] == [peer_key]
            peer.tick()
            assert len(peer.ctx.calls) == 1
        finally:
            peer.close()


def test_non_holder_receipts_and_status(hosts, monkeypatch, process_table):
    _, holder = hosts
    with Store(holder.store.path, holder.clock, backend=True) as store:
        peer = Poller(holder.ctx, store, holder.policy, holder.clock)
        pid = os.getpid() + 1
        try:
            assert holder.acquire() and not peer.acquire()
            bind(monkeypatch, peer, CHAT)
            store.set_meta('poller_pid', str(pid))
            store.set_meta('epoch', f'{pid}:{process_table[pid]}:holder')
            result = peer.tool.handle(ARM)
            assert result['armed'] and OTHER_WINDOW in result['receipt'].splitlines()
            runtime = SimpleNamespace(get_engine=lambda: peer)
            assert OTHER_WINDOW in commands.handle('add every 10m Check the task.', runtime).splitlines()
            holder.ctx.accept = False
            holder.clock.advance(61)
            holder.tick()
            assert OTHER_WINDOW in commands.handle('status', runtime).splitlines()
            status_runtime = SimpleNamespace(ctx=peer.ctx, _runner=peer, _last_bootstrap_error=None,
                                             _last_tick_monotonic=peer.clock.monotonic(),
                                             _thread=SimpleNamespace(is_alive=lambda: True))
            assert Runtime.status(status_runtime) == {'state': 'alive', 'reason': 'scheduler lock held elsewhere'}
            holder.release()
            assert OTHER_WINDOW not in commands.handle('status', runtime).splitlines()
            assert Runtime.status(status_runtime) == {'state': 'not running', 'reason': 'lock held elsewhere'}
        finally:
            peer.close()


def arm_backend_wake(backend, monkeypatch):
    bind(monkeypatch, backend, CHAT)
    result = backend.tool.handle(ARM)
    assert result['armed']
    return result['wake']['id']


def finish_backend_wake(backend, text):
    backend.intake.pre_llm_call(session_id=CHAT, turn_id='wake', user_message=text)
    backend.intake.on_session_end(session_id=CHAT, turn_id='wake')


@pytest.mark.parametrize('delay', [61, 599])
def test_backend_slow_first_hook_injects_once(hosts, monkeypatch, delay):
    _, backend = hosts
    wid = arm_backend_wake(backend, monkeypatch)
    backend.clock.advance(61)
    backend.tick()
    backend.clock.advance(delay)
    backend.tick()
    o, = backend.store.list_occurrences(wid)
    assert (o.state, o.attempt) == ('claimed', 1)
    assert len(backend.ctx.calls) == 1
    finish_backend_wake(backend, backend.ctx.calls[0][0])
    backend.clock.advance(601)
    backend.tick()
    assert len(backend.ctx.calls) == 1
    assert backend.store.get_wake(wid).fires == 1
    assert backend.store.get_occurrence(wid, o.dedupe_key).state == 'done'


def end_human_turn(backend, turn_id='human', **outcome):
    backend.intake.pre_llm_call(session_id=CHAT, turn_id=turn_id, user_message='Hello.')
    backend.intake.on_session_end(session_id=CHAT, turn_id=turn_id, **outcome)


def test_backend_delayed_first_hook_never_triggers_timed_resend(hosts, monkeypatch):
    _, backend = hosts
    wid = arm_backend_wake(backend, monkeypatch)
    db = backend.store
    backend.clock.advance(61)
    backend.tick()
    backend.clock.advance(600)
    backend.tick()
    o, = db.list_occurrences(wid)
    stamp = db.get_meta('held_since:'+o.id)
    episode = db.get_meta(EPISODE_PREFIX+CHAT)
    assert stamp and episode
    backend.clock.advance(1)
    backend.tick()
    # Astra probe 2: the original hook reaches Hermes near the second window.
    backend.clock.advance(599)
    backend.intake.pre_llm_call(session_id=CHAT, turn_id='delayed', user_message=backend.ctx.calls[0][0])
    backend.clock.advance(1)
    backend.tick()
    backend.intake.on_session_end(session_id=CHAT, turn_id='delayed', completed=True)
    for _ in range(3):
        backend.clock.advance(601)
        backend.tick()
    assert len(backend.ctx.calls) == 1
    assert (o.state, o.error, o.attempt) == ('claimed', 'app_closed', 1)
    assert db.get_wake(wid).fires == 1
    assert db.get_occurrence(wid, o.dedupe_key).state == 'done'


def test_backend_only_later_completed_turn_reopens_resend(hosts, monkeypatch):
    _, backend = hosts
    wid = arm_backend_wake(backend, monkeypatch)
    db = backend.store
    inject = backend.ctx.inject_message

    def accept_behind_human(text, *, session_key):
        backend.intake.pre_llm_call(session_id=CHAT, turn_id='earlier', user_message='Earlier.')
        backend.clock.advance(1)
        return inject(text, session_key=session_key)

    backend.clock.advance(61)
    with monkeypatch.context() as patch:
        patch.setattr(backend.ctx, 'inject_message', accept_behind_human)
        backend.tick()
    backend.clock.advance(1)
    backend.intake.on_session_end(session_id=CHAT, turn_id='earlier', completed=True)
    backend.clock.advance(600)
    backend.tick()
    o, = db.list_occurrences(wid)
    assert (o.state, o.error, o.attempt) == ('claimed', 'app_closed', 1)

    backend.intake.pre_llm_call(session_id=CHAT, turn_id='later', user_message='Later.')
    backend.clock.advance(601)
    backend.tick()
    assert db.get_occurrence(wid, o.dedupe_key) == o
    assert len(backend.ctx.calls) == 1
    backend.intake.on_session_end(session_id=CHAT, turn_id='later', completed=True)
    backend.tick()
    assert len(backend.ctx.calls) == 2
    assert db.backend_reinject_state(o.id)['count'] == 1
    backend.clock.advance(601)
    backend.tick()
    assert (current := db.get_occurrence(wid, o.dedupe_key)).state == 'claimed'
    assert (current.attempt, len(backend.ctx.calls)) == (2, 2)
    finish_backend_wake(backend, backend.ctx.calls[-1][0])
    assert db.get_occurrence(wid, o.dedupe_key).state == 'done'
    assert db.get_wake(wid).fires == 1


@pytest.mark.parametrize('outcome', [
    {'interrupted': True, 'completed': False},
    {'failed': True},
    {'completed': False},
    {'completed': True},
], ids=['interrupted', 'failed', 'incomplete', 'fenced'])
@pytest.mark.parametrize('valid_first', [False, True], ids=['invalid-first', 'invalid-second'])
def test_backend_invalid_turn_cannot_trigger_resend(hosts, monkeypatch, outcome, valid_first):
    _, backend = hosts
    wid = arm_backend_wake(backend, monkeypatch)
    db = backend.store
    backend.clock.advance(61)
    backend.tick()
    o, = db.list_occurrences(wid)
    backend.clock.advance(601)
    backend.tick()
    if valid_first:
        end_human_turn(backend, turn_id='first-valid', completed=True)
        backend.tick()
        assert len(backend.ctx.calls) == 1
    if outcome.get('completed'):
        stale = backend.ctx.calls[0][0].replace(' a:1', ' a:0', 1)
        backend.intake.pre_llm_call(session_id=CHAT, turn_id='stale', user_message=stale)
        backend.intake.on_session_end(session_id=CHAT, turn_id='stale', **outcome)
    else:
        end_human_turn(backend, **outcome)
    backend.tick()
    backend.clock.advance(601)
    backend.tick()
    assert len(backend.ctx.calls) == 1
    assert db.get_occurrence(wid, o.dedupe_key).error == 'app_closed'
    if not valid_first:
        end_human_turn(backend, turn_id='first-valid', completed=True)
        backend.tick()
        assert len(backend.ctx.calls) == 1
    end_human_turn(backend, turn_id='second-valid', completed=True)
    backend.tick()
    assert len(backend.ctx.calls) == 2


def test_backend_detached_chat_holds_until_attach(hosts, monkeypatch):
    _, backend = hosts
    wid = arm_backend_wake(backend, monkeypatch)
    db = backend.store
    backend.ctx.accept = False
    backend.clock.advance(61)
    backend.tick()
    o, = db.list_occurrences(wid)
    stamp = db.get_meta('held_since:'+o.id)
    assert stamp
    for _ in range(4):
        backend.clock.advance(600)
        backend.tick()
        assert db.get_occurrence(wid, o.dedupe_key).state == 'pending'
        assert db.get_meta('held_since:'+o.id) == stamp
        assert db.get_meta(EPISODE_PREFIX+CHAT)
        assert db.get_meta('accepted:'+o.id) is None
    backend.ctx.accept = True
    backend.clock.advance(60)
    backend.tick()
    finish_backend_wake(backend, backend.ctx.calls[-1][0])
    calls = len(backend.ctx.calls)
    backend.clock.advance(600)
    backend.tick()
    assert len(backend.ctx.calls) == calls
    assert db.get_occurrence(wid, o.dedupe_key).state == 'done'
    assert db.get_wake(wid).fires == 1


def test_backend_restart_drop_waits_for_completed_turn(hosts, monkeypatch):
    _, backend = hosts
    wid = arm_backend_wake(backend, monkeypatch)
    assert backend.acquire()
    backend.release()
    backend.clock.advance(61)
    backend.tick()  # The restarted backend accepts before the agent is attached.
    for _ in range(3):
        backend.clock.advance(601)
        backend.tick()
        backend.clock.advance(1)
        backend.tick()
    o, = backend.store.list_occurrences(wid)
    assert (o.state, o.error, len(backend.ctx.calls)) == ('claimed', 'app_closed', 1)
    end_human_turn(backend, turn_id='first', completed=True)
    backend.tick()
    assert len(backend.ctx.calls) == 1
    end_human_turn(backend, turn_id='second', completed=True)
    backend.tick()
    assert len(backend.ctx.calls) == 2
    assert '(held by a closed app window) (late by ' in backend.ctx.calls[-1][0]
    assert backend.store.backend_reinject_state(o.id)['count'] == 1
    finish_backend_wake(backend, backend.ctx.calls[-1][0])
    assert backend.store.get_occurrence(wid, o.dedupe_key).state == 'done'
    assert backend.store.get_wake(wid).fires == 1


@pytest.mark.parametrize('asked,minutes', [(True, 16), (True, 9), (False, 16)],
                         ids=['owner-16m', 'owner-9m', 'agent-16m'])
def test_backend_reinjected_wake_labels(hosts, monkeypatch, asked, minutes):
    _, backend = hosts
    wid = arm_backend_wake(backend, monkeypatch)
    db = backend.store
    wake = db.get_wake(wid)
    if asked:
        assert db.cas_wake(wid, 'active', expected_version=wake.version, owner_requested=True)
    due = wake.next_due_at
    backend.clock.advance(60)
    backend.tick()
    assert len(backend.ctx.calls) == 1
    for _ in range(10):
        backend.clock.advance(60)
        backend.tick()
    o, = db.list_occurrences(wid)
    assert (o.state, o.error) == ('claimed', 'app_closed')
    end_human_turn(backend, turn_id='first', completed=True)
    end_human_turn(backend, completed=True)
    backend.acks()
    assert db.get_occurrence(wid, o.dedupe_key).state == 'pending'
    if minutes == 9:
        # Rebase the wall clock after the hold.
        # A steady clock cannot be nine minutes late after a ten-minute timeout.
        backend.clock.wall = due + minutes * 60
    backend.clock.advance(due + minutes * 60 - backend.clock.now())
    assert not db.get_meta('outages')
    backend.dispatch_pending()
    assert len(backend.ctx.calls) == 2
    text = backend.ctx.calls[-1][0]
    finish_backend_wake(backend, text)
    assert db.get_occurrence(wid, o.dedupe_key).state == 'done'
    reminder = [line for line in text.splitlines() if line.startswith('This reminder was due ')]
    if asked and minutes == 16:
        line, = reminder
        assert ' and is 16m late: deliver it now and say it is late.' in line
    else:
        assert not reminder
    why, = [line for line in text.splitlines() if line.startswith('Why:')]
    assert f'(held by a closed app window) (late by {minutes} min)' in why


def test_backend_reinjected_agreed_watcher_labels(hosts, monkeypatch):
    _, backend = hosts
    bind(monkeypatch, backend, CHAT)
    assert backend.tool.identity()
    db = backend.store
    # Current backend arming is schedule-only. Exercise an already queued watcher.
    wake = db.create_wake(Wake(CHAT, 'Watch for a reaction.', 'reaction',
                               platform='desktop', chat_type='dm', owner_requested=True))
    assert db.add_occurrence(wake.id, 'reaction:fixture')
    backend.tick()
    assert len(backend.ctx.calls) == 1
    for _ in range(10):
        backend.clock.advance(60)
        backend.tick()
    held, = db.list_occurrences(wake.id)
    assert (held.state, held.error) == ('claimed', 'app_closed')
    end_human_turn(backend, turn_id='first', completed=True)
    end_human_turn(backend, completed=True)
    backend.acks()
    backend.clock.advance(360)
    backend.dispatch_pending()
    assert len(backend.ctx.calls) == 2
    text = backend.ctx.calls[-1][0]
    assert 'This reminder was due ' in text and ' and is 16m late' in text
    assert '(held by a closed app window) (late by 16 min)' in text


def test_backend_reinjection_count_in_list_and_status(hosts, monkeypatch):
    _, backend = hosts
    wid = arm_backend_wake(backend, monkeypatch)
    db = backend.store
    runtime = SimpleNamespace(get_engine=lambda: backend)

    def shown(expected):
        for command in ('list', 'status'):
            lines = commands.handle(command, runtime).splitlines()
            row = next((line for line in lines if line.startswith(wid + ' ')), '')
            if expected:
                assert expected in row
            else:
                assert 're-sent' not in '\n'.join(lines)
        row, = backend.tool.handle({'action': 'list'})['wakes']
        assert row['id'] == wid
        if expected:
            assert row['delivery_note'] == expected
        else:
            assert 'delivery_note' not in row

    backend.clock.advance(60)
    backend.tick()
    shown(None)
    for count in (1, 2):
        backend.clock.advance(600)
        backend.tick()
        o, = db.list_occurrences(wid)
        assert (o.state, o.error) == ('claimed', 'app_closed')
        end_human_turn(backend, turn_id='first', completed=True)
        end_human_turn(backend, completed=True)
        backend.tick()
        shown(f're-sent {count}× (app window not ready)')
    finish_backend_wake(backend, backend.ctx.calls[-1][0])
    assert db.get_occurrence(wid, o.dedupe_key).state == 'done'
    shown(None)
    assert db.get_meta('backend_reinject:'+o.id)
    backend.clock.advance(2 * 86400 + 1)
    assert db.add_occurrence(wid, 'newer', state='skipped')
    db.gc(ledger_days=7, keep_occurrences=1)
    assert db.get_occurrence(wid, o.dedupe_key) is None
    assert db.get_meta('backend_reinject:'+o.id) is None


def test_gateway_wake_hides_backend_reinjection_count(hosts, monkeypatch):
    gateway, _ = hosts
    bind(monkeypatch, gateway, KEY, platform='telegram', source='')
    result = gateway.tool.handle(ARM)
    assert result['armed']
    wid = result['wake']['id']
    assert gateway.store.add_occurrence(wid, 'fixture')
    o = gateway.store.get_occurrence(wid, 'fixture')
    gateway.store.set_meta('backend_reinject:'+o.id, '3')
    for command in ('list', 'status'):
        text = commands.handle(command, SimpleNamespace(get_engine=lambda: gateway))
        assert wid in text and 're-sent' not in text
    row, = gateway.tool.handle({'action': 'list'})['wakes']
    assert row['id'] == wid and 'delivery_note' not in row


@pytest.mark.parametrize('queued', [False, True], ids=['dropped', 'delivered'])
def test_backend_accept_during_turn_setup_stays_held(hosts, monkeypatch, queued):
    _, backend = hosts
    wid = arm_backend_wake(backend, monkeypatch)
    db = backend.store
    backend.clock.advance(61)
    backend.tick()
    # Hermes was already setting up T1 when it accepted the wake prompt.
    backend.clock.advance(1)
    backend.intake.pre_llm_call(session_id=CHAT, turn_id='T1', user_message='Hello.')
    backend.clock.advance(601)
    backend.tick()
    o, = db.list_occurrences(wid)
    assert (o.state, o.error) == ('claimed', 'app_closed')
    assert db.get_meta('accepted:'+o.id) == '1'
    assert db.get_meta('held_since:'+o.id)
    assert len(backend.ctx.calls) == 1
    backend.intake.on_session_end(session_id=CHAT, turn_id='T1', completed=True)
    backend.tick()
    assert len(backend.ctx.calls) == 1
    if queued:
        finish_backend_wake(backend, backend.ctx.calls[0][0])
        assert db.get_wake(wid).fires == 1
        assert db.get_occurrence(wid, o.dedupe_key).state == 'done'
    else:
        # T2 follows T1 and the queued wake would have started ahead of it.
        end_human_turn(backend, turn_id='T2', completed=True)
        backend.tick()
        assert len(backend.ctx.calls) == 2
        assert '(held by a closed app window) (late by ' in backend.ctx.calls[-1][0]
        assert db.backend_reinject_state(o.id)['count'] == 1
        # The evidence for the first acceptance cannot be reused for attempt 2.
        end_human_turn(backend, turn_id='T3', completed=True)
    backend.clock.advance(601)
    backend.tick()
    assert len(backend.ctx.calls) == (1 if queued else 2)


def test_backend_clock_rollback_cannot_make_earlier_turn_resend_evidence(hosts, monkeypatch):
    _, backend = hosts
    wid = arm_backend_wake(backend, monkeypatch)
    backend.clock.advance(62)
    end_human_turn(backend, turn_id='before-accept', completed=True)
    backend.clock.wall -= 1
    backend.tick()
    assert len(backend.ctx.calls) == 1
    backend.clock.advance(601)
    backend.tick()
    assert len(backend.ctx.calls) == 1
    o, = backend.store.list_occurrences(wid)
    assert (o.state, o.error, o.attempt) == ('claimed', 'app_closed', 1)
    end_human_turn(backend, turn_id='first-after-accept', completed=True)
    backend.tick()
    assert len(backend.ctx.calls) == 1
    end_human_turn(backend, turn_id='second-after-accept', completed=True)
    for _ in range(2):
        backend.clock.advance(601)
        backend.tick()
    assert len(backend.ctx.calls) == 2
    assert backend.store.backend_reinject_state(o.id)['count'] == 1


def test_backend_legacy_accept_needs_new_ordered_turns(hosts, monkeypatch):
    _, backend = hosts
    wid = arm_backend_wake(backend, monkeypatch)
    db = backend.store
    backend.clock.advance(61)
    backend.tick()
    o, = db.list_occurrences(wid)
    # The round-six accept state had a wall timestamp and no sequence.
    db.set_meta('backend_reinject:'+o.id, f'{{"count": 1, "accepted_at": {backend.clock.now()}}}')
    end_human_turn(backend, turn_id='old-first', completed=True)
    end_human_turn(backend, turn_id='old-second', completed=True)
    backend.clock.advance(601)
    backend.tick()
    assert len(backend.ctx.calls) == 1
    end_human_turn(backend, turn_id='new-first', completed=True)
    backend.tick()
    assert len(backend.ctx.calls) == 1
    end_human_turn(backend, turn_id='new-second', completed=True)
    backend.tick()
    assert len(backend.ctx.calls) == 2
    assert db.backend_reinject_state(o.id)['count'] == 2


def test_backend_turn_order_gc_keeps_evidence_for_current_claim(hosts, monkeypatch):
    _, backend = hosts
    wid = arm_backend_wake(backend, monkeypatch)
    db = backend.store
    backend.clock.advance(61)
    backend.tick()
    o, = db.list_occurrences(wid)
    accepted = db.backend_reinject_state(o.id)['accepted_seq']
    end_human_turn(backend, turn_id='first', completed=True)
    end_human_turn(backend, turn_id='second', completed=True)
    with Store(db.path, backend.clock, backend=True) as peer:
        assert peer.backend_turns_prove_drop(CHAT, accepted)
        peer.gc_backend_turns()
        assert peer.backend_turns_prove_drop(CHAT, accepted)
    backend.clock.advance(601)
    backend.tick()
    assert len(backend.ctx.calls) == 2
    assert db.backend_reinject_state(o.id)['accepted_seq'] > accepted
    finish_backend_wake(backend, backend.ctx.calls[-1][0])
    backend.clock.advance(3601)
    backend.tick()
    assert not db.backend_turns_prove_drop(CHAT, accepted)


@pytest.mark.parametrize('death', ['pid-gone', 'pid-reused'])
def test_backend_holder_recovers_accepting_peer_that_dies_after_takeover(hosts, monkeypatch, process_table, death):
    _, backend = hosts
    wid = arm_backend_wake(backend, monkeypatch)
    db = backend.store
    backend.clock.advance(61)
    backend.tick()
    o, = db.list_occurrences(wid)
    pid = os.getpid() + 1
    epoch = f'{pid}:{process_table[pid]}:peer'
    # Give the accepted claim and lock metadata the simulated older process's identity.
    backend.release()
    db.connection.execute('UPDATE occurrences SET pid=?,epoch=? WHERE wake_id=?', (pid, epoch, wid))
    db.set_meta('epoch', epoch)
    assert db.cas_session(CHAT, {'busy_since': None}, busy_since=backend.clock.now(),
                          busy_epoch=f'{pid}:{process_table[pid]}:peer-hook')
    assert backend.acquire()
    backend.clock.advance(1)
    backend.tick()
    assert len(backend.ctx.calls) == 1
    if death == 'pid-gone':
        del process_table[pid]
    else:
        process_table[pid] += 3
    backend.tick()  # The peer died after acquisition, before the ack window.
    assert len(backend.ctx.calls) == 2
    assert '(held by a closed app window) (late by less than 1 min)' in backend.ctx.calls[-1][0]
    assert db.backend_reinject_state(o.id)['count'] == 1
    backend.clock.advance(1201)
    backend.tick()
    assert len(backend.ctx.calls) == 2
    finish_backend_wake(backend, backend.ctx.calls[-1][0])
    assert db.get_occurrence(wid, o.dedupe_key).state == 'done'


def test_backend_three_restarts_count_labelled_copies_without_ack_limit(hosts, monkeypatch, process_table):
    _, backend = hosts
    wid = arm_backend_wake(backend, monkeypatch)
    db = backend.store
    backend.clock.advance(61)
    backend.tick()
    o, = db.list_occurrences(wid)
    pid = os.getpid() + 1
    del process_table[pid]
    epoch = f'{pid}:10:peer'
    for count in (1, 2, 3):
        backend.clock.advance(601)
        db.connection.execute('UPDATE occurrences SET pid=?,epoch=? WHERE wake_id=?', (pid, epoch, wid))
        db.set_meta('epoch', epoch)
        backend.release()
        backend.tick()
        assert (current := db.get_occurrence(wid, o.dedupe_key)).state == 'claimed'
        assert (current.attempt, len(backend.ctx.calls)) == (count+1, count+1)
    assert [('(held by a closed app window' in text and '(late by ' in text)
            for text, _ in backend.ctx.calls[1:]] == [True] * 3
    assert tool.backend_reinject_note(db, db.get_wake(wid)) == 're-sent 3× (app window not ready)'
    assert db.get_wake(wid).fires == 0
    finish_backend_wake(backend, backend.ctx.calls[-1][0])
    assert db.get_occurrence(wid, o.dedupe_key).state == 'done'


def test_backend_restart_recovery_labels_overdue_owner_reminder(hosts, monkeypatch, process_table):
    _, backend = hosts
    wid = arm_backend_wake(backend, monkeypatch)
    db = backend.store
    wake = db.get_wake(wid)
    assert db.cas_wake(wid, 'active', expected_version=wake.version, owner_requested=True)
    backend.clock.advance(61)
    backend.tick()
    o, = db.list_occurrences(wid)
    pid = os.getpid() + 1
    del process_table[pid]
    epoch = f'{pid}:10:peer'
    backend.release()
    db.connection.execute('UPDATE occurrences SET pid=?,epoch=? WHERE wake_id=?', (pid, epoch, wid))
    db.set_meta('epoch', epoch)
    # Keep the tick history current so the age instruction comes from the hold, not an outage.
    for _ in range(61):
        backend.clock.advance(60)
        db.set_meta('last_tick_at', str(backend.clock.now()))
    backend.tick()
    assert len(backend.ctx.calls) == 2
    text = backend.ctx.calls[-1][0]
    why, = [line for line in text.splitlines() if line.startswith('Why:')]
    assert '(held by a closed app window' in why and '(late by 1h' in why
    due, = [line for line in text.splitlines() if line.startswith('This reminder was due ')]
    assert ' and is 1h 01m late' in due
    assert db.backend_reinject_state(o.id)['count'] == 1


@pytest.mark.parametrize('accepted', [True, False], ids=['accepted', 'injector-refused'])
def test_backend_closed_window_hold_text_matches_acceptance(hosts, monkeypatch, accepted):
    _, backend = hosts
    wid = arm_backend_wake(backend, monkeypatch)
    db = backend.store
    wake = db.get_wake(wid)
    assert db.cas_wake(wid, 'active', expected_version=wake.version, owner_requested=True)
    backend.ctx.accept = accepted
    backend.clock.advance(61)
    backend.tick()
    backend.clock.advance(600)
    backend.tick()
    rows = held_wakes(backend, CHAT)
    assert len(rows) == 1
    text = notice_text(backend, db.get_session(CHAT), rows, rows[0][2])
    runtime = SimpleNamespace(get_engine=lambda: backend)
    cli = [commands.handle(command, runtime) for command in ('list', 'status')]
    row, = backend.tool.handle({'action': 'list'})['wakes']
    assert row['id'] == wid
    if accepted:
        assert text == "Check the task. is due but its Desktop chat isn't ready; it'll run on your next message there."
        for output in (*cli, row.get('delivery_note', '')):
            assert 'next message in that chat' in output
            assert 'app restarts' in output
            assert 'open that chat' not in output.lower()
    else:
        assert 'Open that chat to let them run.' in text
        assert all('held: app window is closed' in output for output in cli)
        assert 'delivery_note' not in row


@pytest.fixture
def notice_sends():
    sends = []
    transport = Notifier(lambda *args: sends.append(args) or {'ok': True})

    def connect(engine):
        engine.refresh_policy = None
        engine.policy = replace(engine.policy, notify_via='fixture:owner')
        engine.notifier = lambda: transport
        return sends

    return connect


@pytest.mark.parametrize('intent,surface,expected', [
    ('owner', 'desktop', 'Pay rent'),
    ('urgent', 'tui', 'x' * 59 + '…'),
    ('background', 'desktop', None),
])
def test_backend_accepted_hold_notice(hosts, monkeypatch, notice_sends, intent, surface, expected):
    _, backend = hosts
    bind(monkeypatch, backend, CHAT, source=surface)
    args = {**ARM, 'title': 'Pay rent'} if intent == 'owner' else {**ARM, 'note': 'x' * 70}
    result = backend.tool.handle(args)
    assert result['armed']
    db = backend.store
    wake = db.get_wake(result['wake']['id'])
    assert db.cas_wake(wake.id, 'active', expected_version=wake.version,
                       owner_requested=intent == 'owner', urgent=intent == 'urgent')
    sends = notice_sends(backend)
    backend.clock.advance(60)
    backend.tick()
    backend.clock.advance(600)
    backend.tick()
    held, = db.list_occurrences(wake.id)
    assert (held.state, held.error) == ('claimed', 'app_closed')

    for advance in (14 * 60, 59):
        backend.clock.advance(advance)
        backend.tick()
        assert backend.notifications.idle.wait(2)
        assert sends == []
    for advance in (1, 5 * 60):
        backend.clock.advance(advance)
        backend.tick()
        assert backend.notifications.idle.wait(2)
        if expected is None:
            assert sends == []
        else:
            label = 'Desktop' if surface == 'desktop' else 'TUI'
            assert sends == [('owner', f"{expected} is due but its {label} chat isn't ready; "
                                       "it'll run on your next message there.")]
        assert len(backend.ctx.calls) == 1
        assert db.get_occurrence(wake.id, held.dedupe_key).state == 'claimed'


def test_backend_accepted_notice_wait_starts_when_claim_is_held(hosts, monkeypatch, notice_sends):
    _, backend = hosts
    wid = arm_backend_wake(backend, monkeypatch)
    db = backend.store
    wake = db.get_wake(wid)
    assert db.cas_wake(wid, 'active', expected_version=wake.version, owner_requested=True, title='Pay rent')
    sends = notice_sends(backend)
    backend.ctx.accept = False
    backend.clock.advance(60)
    backend.tick()
    backend.ctx.accept = True
    backend.clock.advance(60)
    backend.tick()
    backend.clock.advance(600)
    backend.tick()
    held, = db.list_occurrences(wid)
    assert (held.state, held.error) == ('claimed', 'app_closed')
    backend.clock.advance(899)
    backend.tick()
    assert backend.notifications.idle.wait(2)
    assert sends == []
    backend.clock.advance(1)
    backend.tick()
    assert backend.notifications.idle.wait(2)
    assert sends == [('owner', "Pay rent is due but its Desktop chat isn't ready; it'll run on your next message there.")]
    assert len(backend.ctx.calls) == 2


def test_backend_injector_refused_notice_keeps_closed_window_timing_and_text(hosts, monkeypatch, notice_sends):
    _, backend = hosts
    arm_backend_wake(backend, monkeypatch)
    sends = notice_sends(backend)
    backend.ctx.accept = False
    backend.clock.advance(60)
    backend.tick()
    backend.clock.advance(599)
    backend.notifications.tick(backend)
    assert backend.notifications.idle.wait(2)
    assert sends == []
    backend.clock.advance(1)
    backend.notifications.tick(backend)
    assert backend.notifications.idle.wait(2)
    assert sends == [('owner', "HeartBeat: 1 wake(s) in default's Desktop chat have been waiting since 12:01 UTC "
                               "because the app window is closed. Open that chat to let them run.")]


def test_backend_accepted_notice_groups_only_due_eligible_wakes(hosts, monkeypatch, notice_sends):
    _, backend = hosts
    bind(monkeypatch, backend, CHAT)
    assert backend.tool.identity()
    db = backend.store
    sends = notice_sends(backend)
    for wid, kind, asked, age in [('reminder', 'at', True, 900), ('watcher', 'reaction', True, 900),
                                  ('younger', 'at', True, 899), ('background', 'at', False, 900)]:
        db.create_wake(Wake(CHAT, 'Watch the task.', kind, id=wid, platform='desktop',
                            chat_type='dm', owner_requested=asked))
        assert db.add_occurrence(wid, 'slot')
        assert db.claim(wid, 'slot', epoch=backend.epoch, pid=os.getpid())
        assert db.update_occurrence(wid, 'slot', 'claimed', attempt=1, error='app_closed')
        db.set_meta(f'held_since:{wid}/slot', str(backend.clock.now() - age))
    backend.notifications.tick(backend)
    assert backend.notifications.idle.wait(2)
    assert sends == [('owner', "2 wakes are due but their Desktop chat isn't ready; "
                               "they'll run on your next message there.")]
    assert not backend.ctx.calls


def test_gateway_does_not_notify_for_accepted_hold(hosts, notice_sends):
    gateway, _ = hosts
    db = gateway.store
    db.upsert_session(Session(CHAT, 'desktop', 'dm', CHAT, CHAT))
    db.create_wake(Wake(CHAT, 'Pay rent.', 'at', id='accepted', platform='desktop', chat_type='dm',
                        owner_requested=True, urgent=True))
    assert db.add_occurrence('accepted', 'slot')
    assert db.claim('accepted', 'slot', epoch=gateway.epoch, pid=os.getpid())
    assert db.update_occurrence('accepted', 'slot', 'claimed', attempt=1, error='app_closed')
    db.set_meta('held_since:accepted/slot', str(gateway.clock.now()))
    sends = notice_sends(gateway)
    gateway.clock.advance(900)
    gateway.notifications.tick(gateway)
    assert gateway.notifications.idle.wait(2)
    assert sends == []


def test_backend_hold_ends_on_actual_wake_start(hosts, monkeypatch):
    _, backend = hosts
    wid = arm_backend_wake(backend, monkeypatch)
    db = backend.store
    backend.refresh_policy = None
    backend.policy = replace(backend.policy, notify_via='fixture:owner')
    sends = []
    transport = Notifier(lambda *args: sends.append(args) or {'ok': True})
    backend.notifier = lambda: transport
    backend.ctx.accept = False
    backend.clock.advance(61)
    backend.tick()
    o, = db.list_occurrences(wid)
    stamp = db.get_meta('held_since:'+o.id)
    episode = db.get_meta(EPISODE_PREFIX+CHAT)
    assert stamp and episode
    backend.ctx.accept = True
    backend.clock.advance(60)
    backend.tick()
    assert db.get_occurrence(wid, o.dedupe_key).state == 'claimed'
    assert db.get_meta('held_since:'+o.id) == stamp
    assert db.get_meta(EPISODE_PREFIX+CHAT) == episode
    backend.intake.pre_llm_call(session_id=CHAT, turn_id='wake', user_message=backend.ctx.calls[-1][0])
    assert db.get_occurrence(wid, o.dedupe_key).state == 'started'
    assert not db.get_meta('held_since:'+o.id)
    assert not db.get_meta(EPISODE_PREFIX+CHAT)
    backend.clock.advance(NOTIFY_AFTER_S)
    backend.notifications.tick(backend)
    assert backend.notifications.idle.is_set()
    assert not sends


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


@pytest.mark.parametrize('owner,mark,keep', [
    ('peer', 'dead_hook', False), ('peer', 'local_hook', True),
    ('peer', 'replaced_local', True), ('peer', 'foreign_hook', True),
    ('self', 'dead_hook', False),
])
def test_backend_acks_respects_busy_owner(hosts, monkeypatch, process_table, owner, mark, keep):
    _, backend = hosts
    pid = os.getpid()
    dead, live = pid + 1, pid + 2
    del process_table[dead]
    runner = Poller(backend.ctx, backend.store, backend.policy, backend.clock)
    runner.close()
    claimed_by = runner.epoch if owner == 'self' else f'{dead}:10.0:dead_runner'
    o = backend_occurrence(backend, monkeypatch, claimed_by)
    db = backend.store
    assert db.cas_occurrence(o.wake_id, o.dedupe_key, 'claimed', 'started',
                             attempt=o.attempt, started_at=backend.clock.now())
    backend.clock.advance(7201)
    monkeypatch.setenv('HERMES_AGENT_TIMEOUT', '600')
    marks = {'dead_hook': f'{dead}:10.0:dead_hook', 'local_hook': backend.epoch,
             'replaced_local': runner.epoch, 'foreign_hook': f'{live}:20.0:live_hook'}
    busy = marks[mark]
    assert busy != claimed_by
    assert db.cas_session(CHAT, {'busy_since': None}, busy_since=backend.clock.now(), busy_epoch=busy)
    backend.acks()
    settled = db.get_occurrence(o.wake_id, o.dedupe_key)
    assert (settled.state, settled.error) == ('interrupted', 'no_session_end')
    session = db.get_session(CHAT)
    assert (session.busy_epoch, session.busy_since) == (
        (busy, backend.clock.now()) if keep else (None, None))


def test_backend_reconcile_keeps_claim_from_replaced_runner(hosts, monkeypatch, process_table):
    _, backend = hosts
    old_runner = Poller(backend.ctx, backend.store, backend.policy, backend.clock)
    o = backend_occurrence(backend, monkeypatch, old_runner.epoch)
    old_runner.close()
    assert backend.acquire()
    assert backend.store.get_occurrence(o.wake_id, o.dedupe_key).state == 'claimed'


@pytest.mark.parametrize('created,owned', [
    ('', True), ('invalid', True), ('30.0', True), ('29.0', True), ('28.0', True), ('27.0', False),
])
def test_backend_reload_recovers_only_reused_pid_claim(hosts, monkeypatch, process_table, created, owned):
    _, backend = hosts
    o = backend_occurrence(backend, monkeypatch, f'{os.getpid()}:{created}:evicted-module')
    monkeypatch.setattr(poller, 'CREATED_EPOCHS', set())
    monkeypatch.setattr(poller, 'LOCAL_EPOCHS', set())
    db = backend.store
    assert db.cas_session(CHAT, {'busy_since': None}, busy_since=backend.clock.now(), busy_epoch=o.epoch)
    backend.tick()
    settled = db.get_occurrence(o.wake_id, o.dedupe_key)
    assert (settled.state, settled.attempt) == (('claimed', 1) if owned else ('claimed', 2))
    assert len(backend.ctx.calls) == (0 if owned else 1)
    assert db.get_session(CHAT).busy_epoch == (o.epoch if owned else None)


def test_backend_failed_turn_keeps_live_foreign_busy_mark(hosts, monkeypatch, process_table):
    _, backend = hosts
    wid = arm_backend_wake(backend, monkeypatch)
    backend.clock.advance(61)
    backend.tick()
    db = backend.store
    o, = db.list_occurrences(wid)
    backend.intake.pre_llm_call(session_id=CHAT, turn_id='wake', user_message=backend.ctx.calls[-1][0])
    pid = os.getpid()+1
    busy = f'{pid}:{process_table[pid]}:live'
    assert db.cas_session(CHAT, {'busy_epoch': backend.epoch}, busy_epoch=busy)
    backend.intake.on_session_end(session_id=CHAT, turn_id='wake', failed=True)
    assert db.get_occurrence(wid, o.dedupe_key).state == 'interrupted'
    assert db.get_session(CHAT).busy_epoch == busy
    wid = arm_backend_wake(backend, monkeypatch)
    backend.clock.advance(61)
    backend.tick()
    o, = db.list_occurrences(wid)
    assert (o.state, o.error) == ('pending', 'busy')
    assert len(backend.ctx.calls) == 1


@pytest.mark.parametrize('created,owned', [(20.0, False), (30.0, True)])
def test_backend_own_busy_recovery_checks_process_start(process_table, hosts, monkeypatch, created, owned):
    _, backend = hosts
    bind(monkeypatch, backend, CHAT)
    assert backend.tool.identity() is not None
    db = backend.store
    epoch = f'{os.getpid()}:{created}:evicted-module'
    assert db.cas_session(CHAT, {'busy_since': None}, busy_since=backend.clock.now(), busy_epoch=epoch)
    monkeypatch.setenv('HERMES_AGENT_TIMEOUT', '600')
    backend.clock.advance(7200)
    backend.acks(own_only=True)
    assert db.get_session(CHAT).busy_epoch == (None if owned else epoch)


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

    process_table[pid] += 10
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


@pytest.mark.parametrize('identity,current,alive', [
    ('10.0:peer', 11.9, True), ('10.0:peer', 12.0, True), ('10.0:peer', 12.1, False),
    ('peer', 10.0, True), (':peer', 10.0, True), ('invalid:peer', 10.0, True),
    ('10.0:peer', None, True), ('10.0:peer', float('nan'), True),
])
def test_backend_epoch_liveness(process_table, identity, current, alive):
    pid = os.getpid() + 1
    process_table[pid] = current
    assert serving.backend_epoch_alive(f'{pid}:{identity}', pid=pid) is alive
    del process_table[pid]
    assert not serving.backend_epoch_alive(f'{pid}:{identity}', pid=pid)


def test_backend_epoch_access_denied_at_lookup_or_mint(monkeypatch, process_table):
    psutil = sys.modules['psutil']
    process = psutil.Process

    def denied(pid):
        raise psutil.AccessDenied()

    monkeypatch.setattr(psutil, 'Process', denied)
    assert serving.backend_epoch_alive(f'{os.getpid() + 1}:10.0:peer')
    epoch = serving.backend_epoch('fixture')
    assert epoch == f'{os.getpid()}::fixture'
    monkeypatch.setattr(psutil, 'Process', process)
    assert serving.backend_epoch_alive(epoch)


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


def test_backend_live_notice_reservation_expires_and_notice_resumes(hosts, monkeypatch):
    _, backend = hosts
    wid = arm_backend_wake(backend, monkeypatch)
    backend.refresh_policy = None
    backend.policy = replace(backend.policy, notify_via='fixture:owner', max_messages_per_day=1)
    sends = []
    transport = Notifier(lambda *args: sends.append(args) or {'ok': True})
    backend.notifier = lambda: transport
    backend.ctx.accept = False
    backend.clock.advance(61)
    backend.tick()
    db, zone = backend.store, backend.policy.resolved_timezone()
    db.reserve_backend_notice(CHAT, backend.epoch)
    backend.clock.advance(3600)
    assert db.notice_reserved(CHAT)
    assert db.daily_counts(CHAT, zone)['delivered'] == 1
    backend.clock.advance(1)
    assert not db.notice_reserved(CHAT)
    assert db.daily_counts(CHAT, zone)['delivered'] == 0
    backend.notifications.tick(backend)
    assert backend.notifications.idle.wait(2)
    assert len(sends) == 1
    assert db.get_ledger('2025-10-12', wid)['delivered'] == 1


def test_backend_replacement_worker_respects_inflight_notice_cap(hosts, monkeypatch):
    _, backend = hosts
    wid = arm_backend_wake(backend, monkeypatch)
    backend.refresh_policy = None
    backend.policy = replace(backend.policy, notify_via='fixture:owner', max_messages_per_day=1)
    backend.ctx.accept = False
    backend.clock.advance(61)
    backend.tick()
    entered, release = Event(), Event()
    sends = []

    def send(*args):
        sends.append(args)
        if len(sends) == 1:
            entered.set()
            assert release.wait(3)
        return {'ok': True}

    transport = Notifier(send)
    backend.notifier = lambda: transport
    backend.clock.advance(NOTIFY_AFTER_S)
    with Store(backend.store.path, backend.clock, backend=True) as store:
        replacement = Poller(backend.ctx, store, backend.policy, backend.clock)
        replacement.notifier = lambda: transport
        try:
            backend.notifications.tick(backend)
            assert entered.wait(1)
            backend.clock.advance(3599)
            replacement.notifications.tick(replacement)
            assert replacement.notifications.idle.wait(1)
            assert len(sends) == 1
        finally:
            release.set()
            replacement.close()
            assert backend.notifications.idle.wait(3)
        assert store.get_ledger('2025-10-12', wid)['delivered'] == 1


def test_backend_worker_closed_mid_send_releases_notice_reservation(hosts, monkeypatch):
    _, backend = hosts
    arm_backend_wake(backend, monkeypatch)
    backend.refresh_policy = None
    backend.policy = replace(backend.policy, notify_via='fixture:owner')
    backend.ctx.accept = False
    backend.clock.advance(61)
    backend.tick()
    entered, release = Event(), Event()
    worker, db = backend.notifications, backend.store

    def send(*args):
        entered.set()
        assert release.wait(3)
        return {'ok': True}

    transport = Notifier(send)
    backend.notifier = lambda: transport
    backend.clock.advance(NOTIFY_AFTER_S)
    try:
        worker.tick(backend)
        assert entered.wait(1)
        assert db.get_meta('backend:notice_in_flight:'+CHAT)
        worker.stop()
    finally:
        release.set()
        worker.close()
    assert not worker.thread.is_alive()
    assert not db.notice_reserved(CHAT)
    assert not db.get_meta('backend:notice_in_flight:'+CHAT)


@pytest.mark.parametrize('stop_early', [False, True], ids=['retry-cap', 'shutdown'])
def test_backend_notice_cleanup_bounds_persistent_writer(hosts, monkeypatch, caplog, stop_early):
    _, backend = hosts
    worker, db = backend.notifications, backend.store
    first_attempt, exhausted, next_job = Event(), Event(), Event()
    release = db.release_backend_notice
    attempts = []

    def release_notice(key, epoch):
        if key == CHAT:
            attempts.append(key)
            first_attempt.set()
            if len(attempts) == 10:
                exhausted.set()
        release(key, epoch)

    def send(engine, key, *_):
        if key != CHAT:
            next_job.set()

    monkeypatch.setattr(worker, '_send', send)
    monkeypatch.setattr(db, 'release_backend_notice', release_notice)
    db.reserve_backend_notice(CHAT, backend.epoch)
    with Store(db.path, backend.clock, backend=True) as writer:
        # A real SQLite write lock persists until after the worker assertions.
        # Shorten the connection's bounded SQLite wait to make the baseline probe fast.
        db.connection.execute('PRAGMA busy_timeout=25')
        with writer.atomic():
            worker.job = (backend, CHAT, 'fixture', None)
            worker.thread = Thread(target=worker._run, daemon=True)
            worker.thread.start()
            worker.ready.set()
            assert first_attempt.wait(1)
            if not stop_early:
                assert exhausted.wait(2)
                with db._lock:
                    worker.job = (backend, 'next-chat', 'next', None)
                    worker.ready.set()
                assert next_job.wait(1), 'reservation cleanup blocked the next job'
                assert len(attempts) == 10
                assert 'notice reservation cleanup' in caplog.text
            worker.stop()
            worker.thread.join(timeout=1)
            assert not worker.thread.is_alive(), 'reservation cleanup blocked shutdown'
    if not stop_early:
        assert db.notice_reserved(CHAT)
        backend.clock.advance(3601)
        assert not db.notice_reserved(CHAT)
        assert db.daily_counts(CHAT, backend.policy.resolved_timezone())['delivered'] == 0


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

            outages = db.get_meta('outages')
            db.upsert_session(model.Session('delayed', 'telegram', 'dm', 'delayed', 'delayed',
                                            paused_until=clock.now()+960))
            db.create_wake(model.Wake('delayed', 'A delayed reminder.', 'at', id='delayed',
                                      platform='telegram', chat_type='dm', armed_session_id='delayed',
                                      owner_requested=True, next_due_at=clock.now()))
            engine.tick()
            for _ in range(16):
                clock.advance(60)
                engine.tick()
            assert db.get_meta('outages') == outages
            delayed, = [text for text, key in ctx.calls if key == 'delayed']
            assert '(held by a pause)' in delayed
            assert 'This reminder was due ' not in delayed
            return trace, delayed
        finally:
            engine.close()


def materialize_gateway_baseline(destination):
    root = Path(__file__).resolve().parents[1]
    pin = 'c87874589518a539b6a40ea90c0482a329d607ab'
    destination = destination.resolve()

    def git(*args):
        try:
            return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.PIPE)
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            diagnostic = exc.stderr.decode(errors='replace').strip() if getattr(exc, 'stderr', None) else str(exc)
            message = f'v0.4.34 baseline is unavailable in this checkout: {diagnostic}'
            if any(os.environ.get(name, '').lower() == 'true' for name in ('CI', 'GITHUB_ACTIONS')):
                pytest.fail(message)
            pytest.skip(message)

    for raw_path in git('ls-tree', '-r', '--name-only', '-z', pin, '--', 'heartbeat2').split(b'\0'):
        if not raw_path:
            continue
        path = Path(os.fsdecode(raw_path))
        target = destination / path
        if (path.is_absolute() or '..' in path.parts or not path.is_relative_to('heartbeat2')
                or not target.resolve().is_relative_to(destination)):
            pytest.fail(f'v0.4.34 baseline has an unsafe path: {path!s}')
        content = git('show', f'{pin}:{path.as_posix()}')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def test_gateway_matches_v0434(tmp_path, clock, monkeypatch):
    materialize_gateway_baseline(tmp_path)
    spec = spec_from_file_location('hb_v0434', tmp_path / 'heartbeat2/__init__.py')
    baseline = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, baseline)
    spec.loader.exec_module(baseline)
    clock_pair = type(clock)(clock.now(), clock.monotonic()), clock
    previous, old_delayed = gateway_trace(spec.name, tmp_path / 'baseline', clock_pair[0], monkeypatch)
    current, delayed = gateway_trace('heartbeat2', tmp_path / 'current', clock_pair[1], monkeypatch)
    assert delayed == old_delayed
    assert len(current[-1][1]) >= 2
    for (status, calls, notices, tables), (old_status, old_calls, old_notices, old_tables) in zip(current, previous, strict=True):
        assert (status, calls, notices) == (old_status, old_calls, old_notices)
        for table, rows in tables.items():
            assert rows == old_tables[table], table


def test_gateway_gc_matches_v0434(tmp_path, monkeypatch):
    materialize_gateway_baseline(tmp_path)
    spec = spec_from_file_location('hb_gc_v0434', tmp_path / 'heartbeat2/__init__.py')
    baseline = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, baseline)
    spec.loader.exec_module(baseline)
    baseline_gc = import_module(f'{spec.name}.store').Store.gc
    assert sha256(getsource(Store.gc).encode()).digest() == sha256(getsource(baseline_gc).encode()).digest()


@pytest.mark.parametrize('ci_env', [None, 'CI', 'GITHUB_ACTIONS'])
def test_gateway_baseline_unavailable_policy(tmp_path, monkeypatch, ci_env):
    def missing(args, **kwargs):
        raise subprocess.CalledProcessError(128, args, stderr=b'fatal: not a valid object name')

    for name in ('CI', 'GITHUB_ACTIONS'):
        monkeypatch.delenv(name, raising=False)
    if ci_env:
        monkeypatch.setenv(ci_env, 'true')
    monkeypatch.setattr(subprocess, 'check_output', missing)
    error = pytest.fail.Exception if ci_env else pytest.skip.Exception
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as unavailable:
        materialize_gateway_baseline(tmp_path)
    assert isinstance(unavailable.value, error)
    assert 'v0.4.34 baseline' in str(unavailable.value)
    assert 'fatal: not a valid object name' in str(unavailable.value)


def test_gateway_baseline_uses_tree_and_blobs(tmp_path, monkeypatch):
    git = subprocess.check_output
    commands = []

    def observe(args, **kwargs):
        commands.append(args[1])
        return git(args, **kwargs)

    monkeypatch.setattr(subprocess, 'check_output', observe)
    materialize_gateway_baseline(tmp_path)
    assert commands[0] == 'ls-tree'
    assert commands[1:] and set(commands[1:]) == {'show'}
    assert (tmp_path / 'heartbeat2/__init__.py').is_file()


@pytest.mark.parametrize('path', [b'heartbeat2/../../escape', b'/escape'])
def test_gateway_baseline_rejects_escaping_path(tmp_path, monkeypatch, path):
    def list_tree(args, **kwargs):
        assert args[1] == 'ls-tree'
        return path + b'\0'

    monkeypatch.setattr(subprocess, 'check_output', list_tree)
    with pytest.raises(pytest.fail.Exception, match='unsafe path'):
        materialize_gateway_baseline(tmp_path)
