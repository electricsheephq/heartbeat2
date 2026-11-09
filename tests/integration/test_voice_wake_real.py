"""A voice wake end to end through the real Hermes loader, event bus, hooks, tool registry and command registry.

The bridge here is a small reference bridge written to docs/voice-bridge.md. Its "device" is module state: no audio,
socket or model call. It does not prove a real voice plugin's device authentication or delivery.
"""
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from test_real_loader import _load_hermes
from test_voice_bridge_real import make_home, runtime, drained

REFERENCE_BRIDGE = '''
import time

# One authenticated device: its canonical conversation id, and the run ids that are aliases of it.
CONVERSATION = 'conversation-1'
ALIASES = {'conversation-1', 'run-1', 'run-2'}
device = {'connected': True, 'busy': False, 'owner_text': None, 'refuse_next': 0}
injected = []


def register(ctx):
    unloaded = False

    def voice_target(session_id):
        return CONVERSATION if device['connected'] and session_id in ALIASES else None

    def owner_message(conversation_id, text):
        return conversation_id == CONVERSATION and text is not None and text == device['owner_text']

    def wake_ready(conversation_id):
        return conversation_id == CONVERSATION and device['connected'] and not device['busy']

    def inject_wake(conversation_id, text):
        if not wake_ready(conversation_id): return False
        if device['refuse_next']:
            device['refuse_next'] -= 1
            return False
        injected.append((conversation_id, text))
        return True

    payload = dict(version=1, registered_at=time.monotonic_ns(), voice_target=voice_target,
                   owner_message=owner_message, wake_ready=wake_ready, inject_wake=inject_wake)

    def announce(**_):
        if not unloaded: ctx.emit('heartbeat2_voice_bridge', payload)

    def on_unload():
        nonlocal unloaded
        unloaded = True
        ctx.emit('heartbeat2_voice_bridge', dict(version=1, registered_at=payload['registered_at'], withdrawn=True))

    ctx.subscribe('heartbeat2:voice_bridge_wanted', announce)
    ctx.on_unload(on_unload)
    announce()
'''
CONFIG = ('plugins:\n  enabled: [heartbeat2, bridge]\n  entries:\n    heartbeat2:\n      enabled: true\n'
          '      allow_gateway_injection: true\n      settings:\n        voice_bridge: bridge\n'
          '        quiet_hours: ""\n        timezone: UTC\n    bridge:\n      enabled: true\n')
KEY = 'heartbeat2:voice:conversation-1'


def test_voice_wake_through_the_real_loader(tmp_path, monkeypatch, clock):
    monkeypatch.setenv('HERMES_DISABLE_LAZY_INSTALLS', '1')
    home = tmp_path/'profile'
    monkeypatch.setenv('HERMES_HOME', str(home))
    _load_hermes()
    from hermes_cli.plugins import PluginManager, PluginContext
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from gateway.session_context import set_session_vars, clear_session_vars
    from tools.registry import registry as hermes_registry
    if not callable(getattr(PluginContext, 'emit', None)):
        pytest.skip('this Hermes pin has no event bus')
    make_home(home, 'bridge')
    (home/'plugins/bridge/__init__.py').write_text(REFERENCE_BRIDGE)
    (home/'config.yaml').write_text(CONFIG)
    token = set_hermes_home_override(home)
    manager = PluginManager(scope_key=str(home))
    try:
        manager.discover_and_load()
        drained(manager)
        bridge = manager._plugins['bridge'].module
        rt = runtime(manager)
        assert rt.voice_bridge() is not None
        # Stop the real timer so the fake clock drives explicit ticks.
        rt._stop.set()
        rt._wake.set()
        rt._thread.join(timeout=2)
        assert not rt._thread.is_alive()
        # An owner turn would restart that timer (pre_llm_call revives a stopped poller); keep it stopped.
        monkeypatch.setattr(rt, 'restart', lambda: None)
        rt.clock = clock
        gateway_calls = []
        served = NS(_served_profile_for_home=lambda path: 'default' if Path(path) == Path(manager.home_path) else None)
        manager.set_gateway_message_injector(served, lambda **kwargs: gateway_calls.append(kwargs) or True)
        engine = rt.get_engine()
        engine.paused = lambda: False
        engine.jitter = lambda: 0
        # Hermes knows no lineage between the device's run ids; only the bridge says they are one conversation.
        engine.compression_tip = lambda sid: None
        command = manager._plugin_commands['pulse']['handler']

        def session(sid, fn):
            # An API-server session as Hermes binds it: the route key and chat id are the run's own.
            tokens = set_session_vars(platform='api_server', chat_id=sid, chat_type='dm', session_id=sid,
                                      session_key='api:'+sid)
            try: return fn()
            finally: clear_session_vars(tokens)

        def tool(sid, args):
            return json.loads(hermes_registry.dispatch('pulse', args, session_id=sid))

        arm = {'action': 'create', 'note': 'The timer is ready.', 'trigger': {'kind': 'at', 'at': 'in 5m'}, 'owner_asked': True}
        # An API session the bridge does not vouch for keeps today's refusal, for the tool and for /pulse.
        assert session('stranger', lambda: tool('stranger', arm))['error'] == 'non_gateway'
        assert 'API-server' in session('stranger', lambda: command('off'))
        assert engine.store.get_session(KEY) is None

        def owner_turn():
            text = 'Remind me in five minutes.'
            bridge.device['owner_text'] = text
            manager.invoke_hook('pre_llm_call', session_id='run-1', turn_id='owner', platform='api_server', user_message=text)
            result = tool('run-1', arm)
            manager.invoke_hook('on_session_end', session_id='run-1', turn_id='owner', completed=True, failed=False, interrupted=False)
            return result
        armed = session('run-1', owner_turn)
        assert armed['armed'] and armed['wake']['owner_requested'], armed
        assert armed['wake']['session_key'] == KEY and armed['wake']['platform'] == 'api_server'
        wid = armed['wake']['id']
        assert session('run-1', lambda: command('status')).count('voice bridge: bridge (connected)') == 1

        # Due while the device is busy: held. Then idle, but the device refuses at the last moment: still held, no retry spent.
        clock.advance(301)
        bridge.device['busy'] = True
        engine.tick()
        occurrence = engine.store.list_occurrences(wid)[-1]
        assert occurrence.state == 'pending' and occurrence.error == 'busy' and bridge.injected == []
        bridge.device.update(busy=False, refuse_next=1)
        engine.tick()
        occurrence = engine.store.get_occurrence(wid, occurrence.dedupe_key)
        assert occurrence.state == 'pending' and occurrence.error == 'busy' and bridge.injected == []
        assert not engine.store.get_meta('inject_failures:'+occurrence.id)
        clock.advance(31)
        engine.tick()
        assert len(bridge.injected) == 1 and bridge.injected[0][0] == 'conversation-1'
        assert gateway_calls == []

        # The wake turn runs on a rotated run id, through the registered hooks and the registered tool.
        def wake_turn():
            manager.invoke_hook('pre_llm_call', session_id='run-2', turn_id='wake', platform='api_server',
                                user_message=bridge.injected[0][1])
            assert not engine.intake.flights['run-2'].invalid
            result = tool('run-2', {'action': 'respond', 'outcome': 'done', 'notify': False, 'summary': 'The timer is ready.'})
            assert result['ok'], result
            silenced = manager.invoke_hook('transform_llm_output', session_id='run-2', turn_id='wake', response_text='Chatty reply')
            manager.invoke_hook('on_session_end', session_id='run-2', turn_id='wake', completed=True, failed=False, interrupted=False)
            return silenced
        assert '[SILENT]' in session('run-2', wake_turn)
        occurrence = engine.store.get_occurrence(wid, occurrence.dedupe_key)
        assert occurrence.state == 'done' and not occurrence.delivered

        # The owner's controls reach the same conversation from a rotated run id.
        assert wid in session('run-2', lambda: command('list all'))
        assert not session('run-2', lambda: command('off')).startswith('Error')
        assert not engine.store.get_session(KEY).enabled
        assert session('run-2', lambda: tool('run-2', arm))['error'] == 'session_off'
        assert not session('run-1', lambda: command('on')).startswith('Error')
        assert engine.store.get_session(KEY).enabled
        assert [s.session_key for s in engine.store.list_sessions() if s.platform == 'api_server' and s.session_key.startswith('heartbeat2:voice:')] == [KEY]

        # A disconnected device is no voice session: arming is refused again.
        bridge.device['connected'] = False
        assert session('run-1', lambda: tool('run-1', arm))['error'] == 'non_gateway'
        assert gateway_calls == [] and len(bridge.injected) == 1
    finally:
        manager.unload()
        drained(manager)
        reset_hermes_home_override(token)
