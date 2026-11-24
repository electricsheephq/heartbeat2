"""Real OS locks and pinned Hermes injection slots, driven without listeners or model calls."""
from contextlib import contextmanager
import json
import multiprocessing
import os
from pathlib import Path
import socket
import time
from types import SimpleNamespace

import pytest

from conftest import FakeClock
from test_backend_wakes import ARM, CHAT
from test_engine import KEY, Runner


def child(pipe, home, backend, wall):
    os.environ.update(HOME=str(home), HERMES_HOME=str(home), HERMES_DISABLE_LAZY_INSTALLS='1',
                      HERMES_DISABLE_TELEMETRY='1')
    os.chdir(home)

    def no_network(*args, **kwargs):
        raise AssertionError('Backend fixture must not access the network')

    socket.socket.connect = socket.socket.connect_ex = socket.socket.sendto = no_network
    socket.create_connection = socket.getaddrinfo = no_network
    from gateway.session_context import clear_session_vars, set_session_vars
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
    from tools.approval_context import reset_current_session_key, set_current_session_key
    from heartbeat2.registration import Runtime

    manager = get_plugin_manager()
    manager._discovered = True
    ctx = PluginContext(PluginManifest(name='heartbeat2', key='heartbeat2', source='user'), manager)
    calls, live = [], {}
    if backend:
        from tui_gateway import server
        server.install_tui_message_injector(manager)

        def enqueue(session, text, transport):
            calls.append((session['session_key'], text))

        server._enqueue_prompt = enqueue
        live = server._sessions
    else:
        def inject(*, session_key, content, **kwargs):
            calls.append((session_key, content))
            return True  # Detect even an attempt to route a foreign key to the gateway slot.

        manager.set_gateway_message_injector(Runner(home), inject)

    @contextmanager
    def bound(key, platform):
        token = set_current_session_key(key)
        tokens = set_session_vars(platform=platform, source='desktop' if backend else 'telegram',
                                  session_key=key, session_id=key,
                                  chat_type='' if backend else 'dm', chat_id='' if backend else 'fixture')
        try:
            yield
        finally:
            clear_session_vars(tokens)
            reset_current_session_key(token)

    clock = FakeClock(wall=wall)
    rt = Runtime(ctx, clock=clock)
    engine = rt.get_engine()
    engine.paused = lambda: False
    try:
        while True:
            op, args = pipe.recv()
            if op == 'exit':
                return
            result = None
            if op == 'open':
                import threading
                key, = args
                live[key] = {'session_key': key, 'history_lock': threading.RLock(), 'running': True}
            elif op in {'arm', 'command', 'start', 'finish'}:
                key, platform, *rest = args
                with bound(key, platform):
                    if op == 'arm':
                        result = engine.tool.handle(rest[0] if rest else ARM)
                    elif op == 'command':
                        result = rt.command(rest[0])
                    elif op == 'start':
                        engine.intake.pre_llm_call(session_id=key, turn_id='wake-turn', user_message=calls[-1][1])
                    else:
                        result = engine.tool.handle({'action': 'respond', 'outcome': 'done',
                                                     'notify': True, 'summary': 'Task checked.'}, session_id=key)
                        engine.intake.on_session_end(session_id=key, turn_id='wake-turn')
            elif op == 'tick':
                clock.advance(args[0])
                engine.tick()
                result = [key for key, _ in calls]
            elif op == 'bootstrap_tick':
                clock.advance(args[0])
                rt.start()
                deadline = time.monotonic() + 5
                while rt._last_tick_monotonic != clock.monotonic():
                    assert time.monotonic() < deadline, rt.status()
                    time.sleep(.01)
                result = [key for key, _ in calls], rt.status()
                rt._stop.set()
                rt._wake.set()
                rt._thread.join(5)
            elif op in {'acquire', 'release'}:
                result = getattr(engine, op)()
            else:
                raise AssertionError(op)
            pipe.send(result)
    finally:
        rt.close()


@pytest.fixture
def cluster(tmp_path, clock, monkeypatch):
    if os.environ.get('HEARTBEAT2_REQUIRE_HERMES') != '1':
        pytest.skip('requires a read-only Hermes pin')
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE', '1')
    (tmp_path / 'config.yaml').write_text(json.dumps({'plugins': {'entries': {'heartbeat2': {
        'allow_gateway_injection': True, 'settings': {'quiet_hours': '', 'timezone': 'UTC'}}}}}))
    mp = multiprocessing.get_context('spawn')
    processes = []

    @contextmanager
    def serve(backend, wall=None):
        parent, worker = mp.Pipe()
        proc = mp.Process(target=child, args=(worker, tmp_path, backend, wall or clock.now()))
        proc.start()
        processes.append(proc)
        worker.close()

        def ask(op, *args):
            parent.send((op, args))
            assert parent.poll(20), f'{op} timed out in process {proc.pid}'
            return parent.recv()

        try:
            yield ask
        finally:
            if proc.is_alive():
                parent.send(('exit', ()))
            parent.close()
            proc.join(5)

    def rows(sql, *params):
        import sqlite3
        file = tmp_path / 'plugin-data/heartbeat2/pulse.db'
        with sqlite3.connect(file) as db:
            return db.execute(sql, params).fetchall()

    yield SimpleNamespace(serve=serve, rows=rows, wall=clock.now())
    for proc in processes:
        if proc.is_alive():
            proc.kill()
            proc.join()
        assert proc.exitcode == 0


@pytest.mark.parametrize('backend_first', [False, True])
def test_gateway_and_backend_share_home(cluster, backend_first):
    with cluster.serve(backend_first) as first, cluster.serve(not backend_first) as second:
        gateway, backend = (second, first) if backend_first else (first, second)
        assert first('acquire') and second('acquire')
        # Still one gateway scheduler, even if another backend holds its own lock.
        with cluster.serve(False) as other_gateway:
            assert not other_gateway('acquire')
        assert gateway('arm', KEY, 'telegram')['armed']
        gateway('arm', KEY, '')  # an empty bound platform cannot change this key's host
        assert backend('arm', CHAT, '')['armed']
        assert cluster.rows("SELECT value FROM meta WHERE key=?", 'host:' + KEY) == []
        backend('open', CHAT)
        assert gateway('tick', 61) == [KEY]
        assert backend('tick', 61) == [CHAT]
        backend('start', CHAT, '')
        assert backend('finish', CHAT, '')['ok']


def test_non_holder_delivers_after_foreign_host_miss(cluster):
    with cluster.serve(True) as holder, cluster.serve(True) as owner:
        assert holder('acquire') and not owner('acquire')
        assert owner('arm', CHAT, '')['armed']
        owner('open', CHAT)
        assert holder('tick', 61) == []
        assert cluster.rows('SELECT state,error FROM occurrences') == [('pending', 'app_closed')]
        calls, status = owner('bootstrap_tick', 61)
        assert calls == [CHAT]
        assert status == {'state': 'alive', 'reason': 'delivery only; scheduler lock held elsewhere'}
        assert holder('tick', 5) == []
        assert owner('tick', 5) == [CHAT]
        assert cluster.rows('SELECT state,attempt FROM occurrences') == [('claimed', 2)]


@pytest.mark.parametrize('started', [False, True])
def test_takeover_preserves_live_backend_claim(cluster, started):
    with cluster.serve(True) as owner, cluster.serve(True) as successor:
        assert owner('acquire')
        assert 'Fires once' in owner('command', CHAT, '', 'add in 1m Check the task.')
        owner('open', CHAT)
        assert owner('tick', 61) == [CHAT]
        if started:
            owner('start', CHAT, '')
        owner('release')
        assert successor('acquire')
        assert successor('tick', 20061) == []  # past the claimed and started ack thresholds
        state = 'started' if started else 'claimed'
        assert cluster.rows('SELECT state,attempt FROM occurrences') == [(state, 1)]
        if not started:
            owner('start', CHAT, '')
        assert owner('finish', CHAT, '')['ok']
        assert cluster.rows('SELECT state,attempt FROM occurrences') == [('done', 1)]


@pytest.mark.parametrize('started', [False, True])
def test_non_holder_takeover_preserves_own_turn(cluster, started):
    with cluster.serve(True) as holder, cluster.serve(True) as owner:
        assert holder('acquire') and not owner('acquire')
        assert owner('arm', CHAT, '')['armed']
        owner('open', CHAT)
        assert holder('tick', 61) == []
        assert owner('tick', 61) == [CHAT]
        if started:
            owner('start', CHAT, '')
        holder('release')
        assert owner('acquire')
        state = 'started' if started else 'claimed'
        assert cluster.rows('SELECT state,attempt FROM occurrences') == [(state, 2)]
        if started:
            assert cluster.rows('SELECT busy_since IS NOT NULL FROM sessions') == [(1,)]
        else:
            owner('start', CHAT, '')
        assert owner('finish', CHAT, '')['ok']
        assert owner('tick', 601) == [CHAT]
        assert cluster.rows('SELECT state,attempt FROM occurrences') == [('done', 2)]


def test_non_holder_recovers_dropped_prompt(cluster):
    with cluster.serve(True) as holder, cluster.serve(True) as owner:
        assert holder('acquire') and not owner('acquire')
        assert owner('arm', CHAT, '')['armed']
        owner('open', CHAT)
        assert holder('tick', 61) == []
        assert owner('tick', 61) == [CHAT]
        # Hermes drops the queued prompt without running the start hook.
        assert holder('tick', 599) == []
        assert owner('tick', 599) == [CHAT]
        assert cluster.rows('SELECT state FROM occurrences') == [('claimed',)]
        assert holder('tick', 2) == []
        assert cluster.rows('SELECT state FROM occurrences') == [('claimed',)]
        assert owner('tick', 2) == [CHAT, CHAT]
        owner('start', CHAT, '')
        assert owner('finish', CHAT, '')['ok']
        assert cluster.rows('SELECT state FROM occurrences') == [('done',)]


def test_non_holder_recovers_own_started_turn_at_ttl(cluster):
    with cluster.serve(True) as holder, cluster.serve(True) as owner:
        assert holder('acquire') and not owner('acquire')
        assert owner('arm', CHAT, '')['armed']
        owner('open', CHAT)
        assert holder('tick', 61) == []
        assert owner('tick', 61) == [CHAT]
        owner('start', CHAT, '')
        assert holder('tick', 20000) == []
        assert cluster.rows('SELECT state FROM occurrences') == [('started',)]
        assert owner('tick', 20000) == [CHAT]
        assert cluster.rows('SELECT state,error FROM occurrences') == [('interrupted', 'no_session_end')]
        assert cluster.rows('SELECT busy_since FROM sessions') == [(None,)]


def test_backend_absence_and_recurring_catch_up(cluster):
    with cluster.serve(False) as gateway:
        assert gateway('acquire')
        with cluster.serve(True) as backend:
            assert backend('acquire')
            assert backend('arm', CHAT, '')['armed']
            assert 'then every 15m' in backend('command', 'repeating-chat', '', 'add every 15m Check the task.')
        elapsed = 1861  # Two recurring slots elapsed while the backend was down.
        assert gateway('tick', elapsed) == []
        assert cluster.rows('SELECT status FROM wakes') == [('active',), ('active',)]
        assert cluster.rows('SELECT state FROM occurrences') == []
        assert cluster.rows("SELECT key FROM meta WHERE key LIKE 'notice:%'") == []
        with cluster.serve(True, cluster.wall + elapsed) as backend:
            for key in (CHAT, 'repeating-chat'):
                backend('open', key)
            assert sorted(backend('tick', 0)) == [CHAT, 'repeating-chat']
            assert sorted(backend('tick', 5)) == [CHAT, 'repeating-chat']
        assert cluster.rows('SELECT state,attempt FROM occurrences WHERE state != ?', 'skipped') == [
            ('claimed', 1), ('claimed', 1)]
