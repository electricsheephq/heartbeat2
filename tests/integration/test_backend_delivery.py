"""Real OS locks and pinned Hermes injection slots, driven without listeners or model calls."""
from base64 import b64decode
from contextlib import closing, contextmanager, nullcontext
import json
import multiprocessing
import os
from pathlib import Path
import socket
from threading import Lock
import time
from types import SimpleNamespace

import pytest

from conftest import FakeClock
from test_backend_wakes import ARM, CHAT, OTHER_WINDOW, arm_backend_wake, hosts
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
    calls, live, queued = [], {}, {}
    if backend:
        from tui_gateway import server
        server.install_tui_message_injector(manager)

        def enqueue(session, text, transport):
            key = session['session_key']
            calls.append((key, text))
            if session['attached']:
                assert key not in queued, 'Hermes already holds a wake prompt for this chat'
                queued[key] = text
                session['queued_prompt'] = {'text': text}

        server._enqueue_prompt = enqueue
        live = server._sessions
    else:
        def inject(*, session_key, content, **kwargs):
            calls.append((session_key, content))
            queued[session_key] = content
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
        pipe.send('ready')
        while True:
            op, args = pipe.recv()
            if op == 'exit':
                return
            result = None
            if op == 'open':
                import threading
                key, *attached = args
                live[key] = {'session_key': key, 'history_lock': threading.RLock(), 'running': True,
                             'attached': attached[0] if attached else True,
                             'inflight_turn': {'user': 'fixture turn'}}
            elif op == 'drop':
                queued.pop(args[0])
                if backend: live[args[0]].pop('queued_prompt')
            elif op == 'reload':
                rt.close()
                if args[0]:
                    # A Hermes directory-plugin reload also discards the module's epoch registry.
                    from importlib import reload
                    from heartbeat2 import poller
                    reload(poller)
                rt = Runtime(ctx, clock=clock)
                engine = rt.get_engine()
                engine.paused = lambda: False
            elif op in {'arm', 'command', 'start', 'finish', 'start_human', 'end_human'}:
                key, platform, *rest = args
                with bound(key, platform):
                    if op == 'arm':
                        result = engine.tool.handle(rest[0] if rest else ARM)
                    elif op == 'command':
                        result = rt.command(rest[0])
                    elif op == 'start':
                        if backend: live[key].pop('queued_prompt')
                        engine.intake.pre_llm_call(session_id=key, turn_id='wake-turn', user_message=queued.pop(key))
                    elif op == 'start_human':
                        engine.intake.pre_llm_call(session_id=key, turn_id='human-turn', user_message='Hello.')
                    elif op == 'end_human':
                        engine.intake.on_session_end(session_id=key, turn_id='human-turn', completed=True)
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
    monkeypatch.setenv('HERMES_AGENT_TIMEOUT', '1800')
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
            assert parent.poll(20), f'initialization timed out in process {proc.pid}'
            assert parent.recv() == 'ready'
            yield ask
        finally:
            try:
                if proc.is_alive():
                    parent.send(('exit', ()))
            finally:
                parent.close()
                proc.join(5)

    def rows(sql, *params):
        import sqlite3
        file = tmp_path / 'plugin-data/heartbeat2/pulse.db'
        with closing(sqlite3.connect(file)) as db:
            return db.execute(sql, params).fetchall()

    yield SimpleNamespace(serve=serve, rows=rows, wall=clock.now())
    for proc in processes:
        if proc.is_alive():
            proc.kill()
        proc.join()
    assert all(proc.exitcode == 0 for proc in processes), {
        proc.pid: proc.exitcode for proc in processes
    }


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


def test_backend_holder_delivers_until_it_closes(cluster):
    holder_chat = 'holder-chat'
    with cluster.serve(True) as peer:
        with cluster.serve(True) as holder:
            assert holder('acquire') and not peer('acquire')
            holder('open', holder_chat)
            peer('open', CHAT)
            assert OTHER_WINDOW not in holder('arm', holder_chat, '')['receipt']
            assert OTHER_WINDOW in peer('arm', CHAT, '')['receipt'].splitlines()
            assert holder('tick', 61) == [holder_chat]
            assert cluster.rows('SELECT state,error FROM occurrences JOIN wakes ON wakes.id=wake_id '
                                'WHERE session_key=?', CHAT) == [('pending', 'app_closed')]
            calls, status = peer('bootstrap_tick', 61)
            assert not calls
            assert status == {'state': 'alive', 'reason': 'scheduler lock held elsewhere'}
            assert OTHER_WINDOW in peer('command', CHAT, '', 'status').splitlines()
            holder('start', holder_chat, '')
            assert holder('finish', holder_chat, '')['ok']
        assert peer('tick', 5) == [CHAT]
        peer('start', CHAT, '')
        assert peer('finish', CHAT, '')['ok']
        assert peer('tick', 600) == [CHAT]
        assert cluster.rows('SELECT state FROM occurrences') == [('done',), ('done',)]


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
def test_backend_reacquires_and_preserves_own_turn(cluster, started):
    with cluster.serve(True) as owner, cluster.serve(True) as holder:
        assert owner('acquire')
        assert owner('arm', CHAT, '')['armed']
        owner('open', CHAT)
        assert owner('tick', 61) == [CHAT]
        if started:
            owner('start', CHAT, '')
        owner('release')
        assert holder('acquire')
        holder('release')
        assert owner('acquire')
        state = 'started' if started else 'claimed'
        assert cluster.rows('SELECT state,attempt FROM occurrences') == [(state, 1)]
        if started:
            assert cluster.rows('SELECT busy_since IS NOT NULL FROM sessions') == [(1,)]
        else:
            owner('start', CHAT, '')
        assert owner('finish', CHAT, '')['ok']
        assert owner('tick', 601) == [CHAT]
        assert cluster.rows('SELECT state,attempt FROM occurrences') == [('done', 1)]


def test_non_holder_probes_its_own_dropped_prompt(cluster):
    with cluster.serve(True) as owner, cluster.serve(True) as holder:
        assert owner('acquire')
        assert owner('arm', CHAT, '')['armed']
        owner('open', CHAT)
        assert owner('tick', 61) == [CHAT]
        owner('drop', CHAT)
        owner('release')  # Takes its drop observations even without the scheduler lock.
        assert holder('acquire')
        holder('open', CHAT)
        for advance in (91, 5, 601):
            assert holder('tick', advance) == []
            assert cluster.rows('SELECT state,error,attempt FROM occurrences') == [('claimed', '', 1)]
        assert owner('tick', 30) == [CHAT]
        assert cluster.rows('SELECT state,attempt FROM occurrences') == [('claimed', 1)]
        assert owner('tick', 5) == [CHAT]
        assert cluster.rows('SELECT state,error,attempt FROM occurrences') == [('pending', 'app_closed', 1)]
        holder('release')
        assert owner('tick', 5) == [CHAT, CHAT]
        owner('start', CHAT, '')
        assert owner('finish', CHAT, '')['ok']
        assert cluster.rows('SELECT state,attempt FROM occurrences') == [('done', 2)]


def test_non_holder_recovers_own_started_turn_at_ttl(cluster):
    with cluster.serve(True) as owner, cluster.serve(True) as holder:
        assert owner('acquire')
        assert owner('arm', CHAT, '')['armed']
        owner('open', CHAT)
        assert owner('tick', 61) == [CHAT]
        owner('start', CHAT, '')
        owner('release')
        assert holder('acquire')
        assert holder('tick', 20000) == []
        assert cluster.rows('SELECT state FROM occurrences') == [('started',)]
        assert owner('tick', 20000) == [CHAT]
        assert cluster.rows('SELECT state,error FROM occurrences') == [('interrupted', 'no_session_end')]
        assert cluster.rows('SELECT busy_since FROM sessions') == [(None,)]


def test_backend_restart_retries_false_accept_after_two_absent_reads(cluster):
    with cluster.serve(True) as original:
        assert original('acquire')
        assert original('arm', CHAT, '')['armed']
    with cluster.serve(True, cluster.wall + 61) as restarted:
        # Hermes accepts the registered session even though the agent is not attached.
        restarted('open', CHAT, False)
        assert restarted('tick', 0) == [CHAT]
        assert cluster.rows("SELECT value FROM meta WHERE key LIKE 'accepted:%'") == [('1',)]
        restarted('open', CHAT)
        assert restarted('tick', 29) == [CHAT]
        assert restarted('tick', 1) == [CHAT]
        assert cluster.rows('SELECT state,attempt FROM occurrences') == [('claimed', 1)]
        assert restarted('tick', 5) == [CHAT, CHAT]
        restarted('start', CHAT, '')
        assert restarted('finish', CHAT, '')['ok']
        assert restarted('tick', 601) == [CHAT, CHAT]
        assert cluster.rows('SELECT state,attempt FROM occurrences') == [('done', 2)]
        assert cluster.rows('SELECT fires,model_turns FROM wakes') == [(1, 1)]
        assert cluster.rows("SELECT value FROM meta WHERE key LIKE 'accepted:%'") == [('1',)]
        assert cluster.rows("SELECT value FROM meta WHERE key LIKE 'inject_failures:%'") == []


def test_backend_keeps_wake_queued_behind_busy_turn(cluster):
    with cluster.serve(True) as owner:
        owner('open', CHAT)
        assert owner('arm', CHAT, '')['armed']
        assert owner('tick', 61) == [CHAT]
        # A human turn won the admission race after the dispatch check.
        assert owner('tick', 1) == [CHAT]
        owner('start_human', CHAT, '')
        assert cluster.rows('SELECT busy_since IS NOT NULL FROM sessions') == [(1,)]
        assert owner('tick', 601) == [CHAT]
        assert cluster.rows('SELECT state,attempt FROM occurrences') == [('claimed', 1)]
        owner('end_human', CHAT, '')
        assert owner('tick', 1) == [CHAT]
        owner('start', CHAT, '')
        assert owner('finish', CHAT, '')['ok']
        assert owner('tick', 601) == [CHAT]
        assert cluster.rows('SELECT state,attempt FROM occurrences') == [('done', 1)]
        assert cluster.rows('SELECT fires,model_turns FROM wakes') == [(1, 1)]
        assert cluster.rows("SELECT value FROM meta WHERE key LIKE 'accepted:%'") == [('1',)]


@pytest.mark.parametrize('reload_module', [False, True], ids=['retained-module', 'reloaded-module'])
def test_non_holder_recovers_retired_busy_epoch(cluster, reload_module):
    with cluster.serve(True) as holder, cluster.serve(True) as owner:
        assert holder('acquire') and not owner('acquire')
        owner('open', CHAT)
        assert owner('arm', CHAT, '')['armed']
        owner('start_human', CHAT, '')
        busy, = cluster.rows('SELECT busy_epoch FROM sessions')
        assert busy[0]
        owner('reload', reload_module)
        assert not owner('acquire')
        assert holder('tick', 17999) == []
        assert owner('tick', 17999) == []
        assert cluster.rows('SELECT busy_epoch FROM sessions') == [busy]
        assert holder('tick', 1) == []
        assert cluster.rows('SELECT busy_epoch FROM sessions') == [busy]
        assert owner('tick', 1) == []
        assert cluster.rows('SELECT busy_epoch FROM sessions') == [(None,)]
        holder('release')
        assert owner('tick', 1) == [CHAT]
        owner('start', CHAT, '')
        assert owner('finish', CHAT, '')['ok']
        assert cluster.rows('SELECT state FROM occurrences') == [('done',)]


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


@pytest.mark.parametrize('queued_first', ['text', 'image', 'refusal', 'staged-vision'])
def test_tui_probe_observes_real_hermes_queue_and_drain(hosts, monkeypatch, caplog, tmp_path, queued_first):
    if os.environ.get('HEARTBEAT2_REQUIRE_HERMES') != '1':
        pytest.skip('requires a read-only Hermes pin')
    pytest.importorskip('tui_gateway')
    from tui_gateway import server
    from heartbeat2.tui_probe import queued_or_inflight

    _, backend = hosts
    wid = arm_backend_wake(backend, monkeypatch)
    session = {'session_key': CHAT, 'history_lock': Lock(), 'running': True,
               'inflight_turn': {'user': 'Previous turn'}, 'agent': None}
    sid = 'probe-window'
    monkeypatch.setattr(server, '_sessions', {sid: session})
    monkeypatch.setattr(server, '_ensure_session_db_row', lambda *args: True)
    monkeypatch.setattr(server, '_ensure_active_session_slot', lambda *args: None)
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda *args: False)
    monkeypatch.setattr(server, '_emit', lambda *args: None)
    monkeypatch.setattr(server, '_session_profile_runtime_scope', lambda *args: nullcontext())
    monkeypatch.setattr(server, '_routing_provenance_db', lambda *args: nullcontext())
    monkeypatch.setattr(server, '_reopen_routed_session_row', lambda *args: None)
    monkeypatch.setattr(backend.ctx, 'inject_message',
                        lambda text, *, session_key: server.inject_tui_session_message(
                            session_key=session_key, content=text))

    if queued_first == 'staged-vision':
        from agent import image_routing
        image = tmp_path / 'draft.png'
        image.write_bytes(b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAACklEQVR4nGNgAAIAAAUAAen63NgAAAAASUVORK5CYII='))
        session['attached_images'] = [str(image)]
        monkeypatch.setattr(image_routing, 'decide_image_input_mode', lambda *args, **kwargs: 'native')
        images = []
        admit = server._admit_prompt_turn

        def record_admitted_images(*args, **kwargs):
            admitted = admit(*args, **kwargs)
            images.extend(admitted[0])
            return admitted

        monkeypatch.setattr(server, '_admit_prompt_turn', record_admitted_images)
    if queued_first in {'text', 'image'}:
        with session['history_lock']:
            server._enqueue_prompt(session, 'Owner text', None,
                                   **({'image_paths': ['fixture.png']} if queued_first == 'image' else {}))
    backend.clock.advance(61)
    backend.tick()
    o, = backend.store.list_occurrences(wid)
    marker = f'⟦pulse o:{o.id} a:{o.attempt}'
    assert queued_or_inflight(CHAT, marker) is True
    with session['history_lock']:
        if queued_first == 'text':
            server._enqueue_prompt(session, 'Later owner text', None)
            text = session['queued_prompt']['text']
            assert text.startswith('Owner text\n\n') and text.endswith('Later owner text')
        elif queued_first == 'image':
            assert session['queued_prompt']['image_paths'] == ['fixture.png']
            assert marker in session['queued_prompts'][0]['text']
        elif queued_first == 'staged-vision':
            wake_prompt = session['queued_prompt']['text']
            assert not session['queued_prompt'].get('image_paths')
        session.update(running=False, inflight_turn=None)
    assert queued_or_inflight(CHAT, marker) is True

    if queued_first == 'refusal':
        with caplog.at_level('INFO'):
            assert server._drain_queued_prompt('fixture', sid, session)
        assert 'no agent attached' in caplog.text
        assert session['inflight_turn']['status'] == 'error'
        assert marker in session['inflight_turn']['user']
        assert queued_or_inflight(CHAT, marker) is False
        assert backend.store.get_occurrence(wid, o.dedupe_key).state == 'claimed'
        return

    # Hermes admits the real drained text. Exercise the plugin hook at the worker
    # boundary without launching a model turn.
    started = []

    def start_hook(target, *, name, session):
        text = session['inflight_turn']['user']
        assert queued_or_inflight(CHAT, marker) is True
        if marker in text:
            if queued_first == 'staged-vision':
                # The real admission consumes the staged image, and Hermes builds
                # the persisted hook argument from the native vision payload.
                assert session['attached_images'] == [] and images == [str(image)]
                parts = server._route_turn_images(session['agent'], wake_prompt, images)
                text = server._build_persist_user_message(wake_prompt, images, parts)
                assert [part['type'] for part in text] == ['text', 'image_url']
                assert '@image:' in text[0]['text'] and marker in text[0]['text']
            backend.intake.pre_llm_call(session_id=CHAT, turn_id='wake', user_message=text)
            started.append(backend.store.get_occurrence(wid, o.dedupe_key).state)
        session.update(running=False, inflight_turn=None)
        return object()

    session['agent'] = SimpleNamespace()
    monkeypatch.setattr(server, '_start_session_work', start_hook)
    assert server._drain_queued_prompt('fixture', sid, session)
    if queued_first == 'image':
        assert started == []
        assert server._drain_queued_prompt('fixture', sid, session)
    assert started == ['started']
    backend.intake.on_session_end(session_id=CHAT, turn_id='wake')
    assert backend.store.get_occurrence(wid, o.dedupe_key).state == 'done'
    assert backend.store.get_wake(wid).fires == 1
    if queued_first == 'staged-vision':
        for delay in (601, 5):
            backend.clock.advance(delay)
            backend.tick()
        assert backend.store.get_occurrence(wid, o.dedupe_key).attempt == 1
