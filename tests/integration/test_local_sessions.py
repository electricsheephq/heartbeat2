"""Real local-session admissions and agent turns; only model I/O is synthetic.

No native client or listener is started. The repository's network-denial fixture
also applies here. Old Hermes pins without local plugin injection skip cleanly.
"""
import asyncio
from contextlib import asynccontextmanager
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace as NS

import pytest


MODEL = 'local-session-fixture'
ANSWER = 'The local wake completed.'
ARM = {'action': 'create', 'note': 'local-session fixture', 'owner_asked': True,
       'trigger': {'kind': 'at', 'at': 'in 1 minute'}}
REFUSALS = {
    'cli': "Wakes can't run in the Hermes CLI: it doesn't show plugin turns. Use the Desktop app, the TUI or a messaging chat.",
    'api_server': "Wakes can't run in API-server sessions yet: Hermes drops plugin turns there. Arm it from the Desktop app, the TUI or a messaging chat.",
}


def _load_hermes():
    try:
        return importlib.import_module('hermes_cli.plugins')
    except ImportError:
        if os.environ.get('HEARTBEAT2_REQUIRE_HERMES') == '1':
            pytest.fail('HEARTBEAT2_REQUIRE_HERMES=1 but Hermes is not importable')
        pytest.skip('Hermes is not importable in this venv')


def _chunk(*, content=None, tool_calls=None, finish_reason=None):
    # Same OpenAI stream shape as Hermes tests/agent/test_streaming.py.
    delta = NS(content=content, tool_calls=tool_calls, reasoning_content=None, reasoning=None)
    return NS(choices=[NS(index=0, delta=delta, finish_reason=finish_reason)],
              model=MODEL, usage=None)


async def _wait(predicate, what):
    deadline = asyncio.get_running_loop().time() + 25
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, 'Timed out waiting for ' + what
        await asyncio.sleep(.01)


@asynccontextmanager
async def _rig(tmp_path, monkeypatch, clock):
    home = tmp_path/'home'
    home.mkdir()
    host_home = tmp_path/'host-home'
    host_home.mkdir()
    monkeypatch.setenv('HOME', str(host_home))
    monkeypatch.setattr(Path, 'home', lambda: host_home)
    for name, value in {'HERMES_HOME': home, 'HF_HOME': tmp_path/'cache',
                        'XDG_CACHE_HOME': tmp_path/'cache', 'TMPDIR': tmp_path,
                        'HERMES_DISABLE_LAZY_INSTALLS': '1',
                        'HERMES_DISABLE_TELEMETRY': '1'}.items():
        monkeypatch.setenv(name, str(value))
    plugins = _load_hermes()
    from heartbeat2 import policy, registration
    if not policy.local_injection_supported():
        pytest.skip('Hermes lacks local-session plugin injection')

    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_state import SessionDB
    from run_agent import AIAgent

    (home/'config.yaml').write_text(json.dumps({
        'model': {'default': MODEL, 'provider': 'custom', 'context_length': 131072,
                  'base_url': 'http://127.0.0.1:9/v1', 'api_key': 'fixture-only'},
        'plugins': {'entries': {'heartbeat2': {'allow_gateway_injection': True,
                   'settings': {'quiet_hours': '', 'timezone': 'UTC'}}}},
        'display': {'streaming': True}, 'tools': {'tool_search': {'enabled': 'off'}},
        'auxiliary': {'title_generation': {'enabled': False}},
        'model_context_length': 32768,
    }))
    token = set_hermes_home_override(home)
    runtime = db = manager = runner = authority = None
    try:
        runner = GatewayRunner(GatewayConfig(sessions_dir=home/'sessions'))
        db = SessionDB(home/'state.db')
        runner.session_store._db = runner._session_db = db
        authority = await initialize_session_authority(
            runner, profile_id=str(home), instance_id='local-fixture', db=db)
        # Resolve the real profile manager used by the real agent hook/tool paths.
        manager = plugins.get_plugin_manager()
        manager._discovered = True  # Register this plugin only, without unrelated discovery.
        ctx = plugins.PluginContext(
            plugins.PluginManifest(name='heartbeat2', key='heartbeat2', source='user'), manager)
        runner._gateway_loop = asyncio.get_running_loop()
        runner._running = True
        manager.set_gateway_message_injector(runner, runner._schedule_plugin_message_injection)

        # Step the actual poller explicitly; never race a background timer.
        monkeypatch.setattr(registration.Runtime, 'start', lambda self: None)
        clock.wall = 1794820000.0
        runtime = registration.register(ctx, clock=clock)
        monkeypatch.setattr(runtime, 'needs_restart', lambda: False)
        monkeypatch.setattr(runtime, 'restart', lambda: None)
        engine = runtime.get_engine()
        monkeypatch.setattr(engine, 'signal', lambda: None)
        rig = NS(home=home, runner=runner, authority=authority, db=db, ctx=ctx,
                 engine=engine, clock=clock, creates=[], responses=[], hooks=[])

        original_hook = runtime.hook
        def observe_hook(name, **kwargs):
            if name == 'pre_llm_call':
                rig.hooks.append((kwargs.get('session_id'), kwargs.get('platform'),
                                  kwargs.get('user_message', '')))
            return original_hook(name, **kwargs)
        monkeypatch.setattr(runtime, 'hook', observe_hook)

        def provider_create(**kwargs):
            messages = kwargs['messages']
            text = str(next((m.get('content', '') for m in reversed(messages)
                             if m.get('role') == 'user'), ''))
            wake = '⟦pulse o:' in text
            assert wake or 'arm local wake' in text, text
            if messages[-1].get('role') == 'tool':
                result = json.loads(messages[-1]['content'])
                (rig.responses if wake else rig.creates).append(result)
                return iter([_chunk(content=ANSWER if wake else 'Arming checked.'),
                             _chunk(finish_reason='stop')])
            args = ({'action': 'respond', 'outcome': 'progress', 'notify': True,
                     'summary': ANSWER} if wake else ARM)
            call = NS(index=0, id='local-fixture-call',
                      function=NS(name='pulse', arguments=json.dumps(args)))
            return iter([_chunk(tool_calls=[call]), _chunk(finish_reason='tool_calls')])

        original_init = AIAgent.__init__
        def safe_init(self, *args, **kwargs):
            kwargs.update(skip_context_files=True, skip_memory=True)
            original_init(self, *args, **kwargs)
            self.compression_enabled = False
            self.save_trajectories = False
            self._cached_system_prompt = 'Use pulse to create a wake or respond to one.'
        monkeypatch.setattr(AIAgent, '__init__', safe_init)
        client = NS(chat=NS(completions=NS(create=provider_create)))
        monkeypatch.setattr(AIAgent, '_create_request_openai_client', lambda *a, **k: client)
        monkeypatch.setattr(AIAgent, '_close_request_openai_client', lambda *a, **k: None)
        monkeypatch.setattr(runner, '_resolve_session_agent_runtime', lambda **kwargs: (
            MODEL, {'api_key': 'fixture-only', 'base_url': 'http://127.0.0.1:9/v1',
                    'provider': 'custom', 'api_mode': 'chat_completions'}))
        yield rig
    finally:
        # Finish/cancel fixture-owned FIFO work before closing its SQLite handle.
        if authority is not None:
            tasks = []
            for live in authority.sessions.values():
                if live.retire_timer is not None:
                    live.retire_timer.cancel()
                if live.task is not None and not live.task.done():
                    live.task.cancel()
                    tasks.append(live.task)
            for handle in authority.orphan_closes.values():
                handle.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        if manager is not None:
            manager.clear_gateway_message_injector(runner)
            manager.unload()
        if runtime is not None:
            runtime.close()
        if runner is not None:
            runner._running = False
        if db is not None:
            db.close()
        reset_hermes_home_override(token)


def _viewer(rig):
    from gateway.owner_identity import principal_subject
    from gateway.session_contract import SessionActor
    subject = principal_subject({'user_id': 'test-owner', 'profile_id': str(rig.home)})
    return SessionActor(subject, str(rig.home), frozenset({
        'session:create', 'session:submit', 'session:read', 'session:control'}), 'viewer')


def _rows(rig, sid):
    from hermes_state_runtime import list_session_admissions
    return list_session_admissions(rig.db, session_id=sid, pending_only=False)


async def _settled(rig, sid, count):
    def done():
        rows = _rows(rig, sid)
        live = rig.authority.sessions.get(sid)
        idle = live is None or live.task is None or live.task.done()
        return len(rows) == count and all(row['status'] == 'terminal' for row in rows) and idle
    await _wait(done, 'terminal local admissions')
    assert all(row['outcome'] == 'completed' for row in _rows(rig, sid))


async def _open(rig, surface):
    from gateway.session_local import create_local_session
    ref = create_local_session(rig.authority, _viewer(rig), {
        'request_id': surface, 'source': 'gui' if surface == 'desktop' else surface,
        'model': MODEL, 'cwd': str(rig.home), 'toolsets': ['heartbeat2']})
    attached = await rig.authority.attach(_viewer(rig), ref)
    return ref, attached


async def _arm(rig, ref):
    from gateway.session_contract import Submission
    count = len(_rows(rig, ref.session_id))
    before = len(rig.creates)
    await rig.authority.submit(_viewer(rig), Submission(
        f'arm-{count}', ref, {'text': 'arm local wake'}, 'queue'))
    await _settled(rig, ref.session_id, count + 1)
    assert len(rig.creates) == before + 1
    return rig.creates[-1]


async def _pending(rig, wid):
    # Like the probe, advance only this wake's deadline; keep all dispatch gates.
    assert rig.engine.store.cas_wake(wid, 'active', next_due_at=rig.clock.now() - 1)
    await asyncio.to_thread(rig.engine.times)
    occurrences = rig.engine.store.list_occurrences(wid)
    assert len(occurrences) == 1 and occurrences[0].state == 'pending'
    return occurrences[0]


async def _deliver(rig, ref, occurrence, events):
    before = len(_rows(rig, ref.session_id))
    event_start = len(events)
    await asyncio.to_thread(rig.engine.dispatch, occurrence, cache_line='')
    await _settled(rig, ref.session_id, before + 1)
    current = rig.engine.store.get_occurrence(occurrence.wake_id, occurrence.dedupe_key)
    assert current.state == 'done' and current.outcome == 'progress'
    assert current.delivered is True
    assert rig.responses[-1]['ok'] is True
    assert any(event['params']['type'] == 'message.complete'
               and event['params']['payload'].get('text') == ANSWER
               for event in events[event_start:])
    assert any(row['role'] == 'assistant' and row.get('content') == ANSWER
               for row in rig.db.get_messages(ref.session_id))


@pytest.mark.parametrize('surface', ['desktop', 'tui'])
def test_real_local_session_wake_and_reopen(tmp_path, monkeypatch, clock, surface):
    async def run():
        async with _rig(tmp_path, monkeypatch, clock) as rig:
            ref, attached = await _open(rig, surface)
            live = rig.authority.sessions[ref.session_id]
            key = live.route
            events = []
            live.event_stream.observers.add(events.append)
            armed = await _arm(rig, ref)
            assert armed.get('armed') is True, armed
            assert rig.engine.store.get_session(key).platform == surface
            assert any(sid == ref.session_id and platform == surface
                       for sid, platform, _ in rig.hooks)
            occurrence = await _pending(rig, armed['wake']['id'])
            await _deliver(rig, ref, occurrence, events)

            # A second real tool creation waits while its actual authority entry is retired.
            clock.advance(601)
            armed = await _arm(rig, ref)
            assert armed.get('armed') is True, armed
            occurrence = await _pending(rig, armed['wake']['id'])
            live.event_stream.observers.discard(events.append)
            await rig.authority.detach(_viewer(rig), attached.subscription_id,
                                       end_reason='fixture_window_closed')
            await _wait(lambda: ref.session_id not in rig.authority.sessions, 'local retirement')
            before = len(_rows(rig, ref.session_id))
            held_at = clock.now()
            for attempt in range(2):
                if attempt:
                    clock.advance(61)
                await asyncio.to_thread(rig.engine.dispatch, occurrence, cache_line='')
                occurrence = rig.engine.store.get_occurrence(occurrence.wake_id, occurrence.dedupe_key)
                assert occurrence.state == 'pending' and occurrence.error == 'app_closed'
                assert not rig.engine.store.get_meta('inject_failures:' + occurrence.id)
                assert float(rig.engine.store.get_meta('held_since:' + occurrence.id)) == held_at
                assert float(rig.engine.store.get_meta('not_before:' + occurrence.id)) == clock.now() + 60
                assert len(_rows(rig, ref.session_id)) == before

            await rig.authority.attach(_viewer(rig), ref)
            restored = rig.authority.sessions[ref.session_id]
            assert restored.route == key
            restored.event_stream.observers.add(events.append)
            clock.advance(61)
            await _deliver(rig, ref, occurrence, events)
    asyncio.run(run())


@pytest.mark.parametrize('surface', ['cli', 'api_server'])
def test_real_local_session_refusal(tmp_path, monkeypatch, clock, surface):
    async def run():
        async with _rig(tmp_path, monkeypatch, clock) as rig:
            if surface == 'cli':
                ref, _ = await _open(rig, surface)
                result = await _arm(rig, ref)
            else:
                from gateway.session_api import bind_api_session
                from gateway.session_context import set_session_vars, clear_session_vars
                ref = bind_api_session(rig.authority, 'api-fixture')
                key = rig.authority.sessions[ref.session_id].route
                def call():
                    tokens = set_session_vars(platform='api_server', chat_id=ref.session_id,
                        chat_type='dm', session_id=ref.session_id, session_key=key)
                    try:
                        return json.loads(rig.ctx.dispatch_tool('pulse', ARM, session_id=ref.session_id))
                    finally:
                        clear_session_vars(tokens)
                result = await asyncio.to_thread(call)
                assert _rows(rig, ref.session_id) == []
            assert result['error'] == 'non_gateway', result
            assert result['detail'] == REFUSALS[surface]
            key = rig.authority.sessions[ref.session_id].route
            assert rig.engine.store.list_wakes(key) == []
    asyncio.run(run())
