"""SPEC-162 against the real profile-local Hermes event bus, with no delivery."""
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from test_real_loader import _load_hermes

ROOT = Path(__file__).resolve().parents[2]
BRIDGE = '''
import time
def register(ctx):
    global transport, announcement
    unloaded = False
    def voice_target(session_id): return ctx.plugin_id
    transport = voice_target
    announcement = data = dict(version=1, registered_at=time.monotonic_ns(),
        voice_target=voice_target, owner_message=lambda *a: False,
        wake_ready=lambda *a: False, inject_wake=lambda *a: False)
    def wanted(**kw):
        if not unloaded:
            ctx.emit('heartbeat2_voice_bridge', data)
    def on_unload():
        nonlocal unloaded
        unloaded = True
        ctx.emit('heartbeat2_voice_bridge', dict(
            version=1, registered_at=data['registered_at'], withdrawn=True))
    ctx.subscribe('heartbeat2:voice_bridge_wanted', wanted)
    ctx.emit('heartbeat2_voice_bridge', data)
    ctx.on_unload(on_unload)
'''


def make_home(home, bridge_id):
    plugin = home/'plugins/heartbeat2'
    plugin.mkdir(parents=True)
    for name in ('__init__.py', 'plugin.yaml'):
        shutil.copy2(ROOT/name, plugin/name)
    for name in ('heartbeat2', 'skills'):
        shutil.copytree(ROOT/name, plugin/name, ignore=shutil.ignore_patterns('__pycache__'))
    for name in (bridge_id, 'unconfigured'):
        folder = home/'plugins'/name
        folder.mkdir()
        (folder/'plugin.yaml').write_text(f'name: {name}\nversion: "0.0.1"\ndescription: test bridge\n')
        (folder/'__init__.py').write_text(BRIDGE)
    (home/'config.yaml').write_text(
        f'plugins:\n  enabled: [heartbeat2, {bridge_id}, unconfigured]\n'
        f'  entries:\n    heartbeat2:\n      enabled: true\n      settings:\n        voice_bridge: {bridge_id}\n'
        f'    {bridge_id}:\n      enabled: true\n    unconfigured:\n      enabled: true\n')


def runtime(manager):
    loaded = manager._plugins['heartbeat2']
    assert loaded.enabled, loaded.error
    registration = sys.modules[loaded.module.__name__+'.heartbeat2.registration']
    return registration._RUNTIMES[manager.scope_key]


def drained(manager):
    assert manager._wait_for_event_dispatch(5), 'event dispatch did not drain'


@pytest.mark.parametrize('bridge_first', [True, False])
def test_profiles_load_orders_and_forced_reload(tmp_path, monkeypatch, bridge_first):
    monkeypatch.setenv('HERMES_DISABLE_LAZY_INSTALLS', '1')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path/'bootstrap'))
    _load_hermes()
    from hermes_cli.plugins import PluginManager, PluginContext
    from hermes_cli.plugins_manifest import parse_manifest_file
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    if not callable(getattr(PluginContext, 'emit', None)):
        pytest.skip('this Hermes pin has no event bus')
    managers = []
    try:
        for index in range(2):
            home = tmp_path/f'profile-{index}'
            bridge_id = 'bridge'
            make_home(home, bridge_id)
            monkeypatch.setenv('HERMES_HOME', str(home))
            token = set_hermes_home_override(home)
            try:
                manager = PluginManager(scope_key=str(home))
                managers.append(manager)
                order = (bridge_id, 'heartbeat2') if bridge_first else ('heartbeat2', bridge_id)
                for plugin_id in (*order, 'unconfigured'):
                    plugin = home/'plugins'/plugin_id
                    manifest = parse_manifest_file(plugin/'plugin.yaml', plugin, 'user', '')
                    manager._load_plugin(manifest)
                    loaded = manager._plugins[plugin_id]
                    assert loaded.enabled, loaded.error
                drained(manager)
            finally:
                reset_hermes_home_override(token)
        for index, manager in enumerate(managers):
            bridge = manager._plugins['bridge'].module
            assert runtime(manager).voice_bridge().voice_target is bridge.transport
            assert runtime(manager).voice_bridge().voice_target('fixture') == 'bridge'

        manager = managers[0]
        old_runtime = runtime(manager)
        old_bridge = manager._plugins['bridge'].module
        old_function = old_bridge.transport
        stale = dict(old_bridge.announcement)
        other_view = runtime(managers[1]).voice_bridge()
        monkeypatch.setenv('HERMES_HOME', str(manager.home_path))
        token = set_hermes_home_override(manager.home_path)
        try:
            manager.discover_and_load(force=True)
            drained(manager)
            new_bridge = manager._plugins['bridge'].module
            new_runtime = runtime(manager)
            assert old_runtime.closed and old_runtime.voice_bridge() is None
            assert new_runtime is not old_runtime
            assert new_bridge.transport is not old_function
            assert new_runtime.voice_bridge().voice_target is new_bridge.transport
            # Simulate the old surviving subscriber answering after the new copy.
            ctx = PluginContext(manager._plugins['bridge'].manifest, manager)
            ctx.emit('heartbeat2_voice_bridge', stale)
            # A different namespace cannot supply a registration, even with a newer stamp.
            rogue = PluginContext(manager._plugins['unconfigured'].manifest, manager)
            rogue.emit('heartbeat2_voice_bridge', dict(stale, registered_at=new_bridge.announcement['registered_at']+1))
            drained(manager)
            assert new_runtime.voice_bridge().voice_target is new_bridge.transport
            assert runtime(managers[1]).voice_bridge() is other_view
        finally:
            reset_hermes_home_override(token)
    finally:
        for manager in reversed(managers):
            manager.unload()
            drained(manager)


@pytest.mark.parametrize('case', ['disabled', 'noncompliant_disabled', 'register_raises'])
def test_bridge_lifecycle_on_forced_reload(tmp_path, monkeypatch, case):
    monkeypatch.setenv('HERMES_DISABLE_LAZY_INSTALLS', '1')
    home = tmp_path/'profile'
    monkeypatch.setenv('HERMES_HOME', str(home))
    _load_hermes()
    from hermes_cli.plugins import PluginManager, PluginContext
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from gateway.session_context import set_session_vars, clear_session_vars
    if not callable(getattr(PluginContext, 'emit', None)):
        pytest.skip('this Hermes pin has no event bus')
    make_home(home, 'bridge')
    if case == 'noncompliant_disabled':
        # Deliberately violate the bridge contract: answer even after unload.
        source = BRIDGE.replace("        if not unloaded:\n", "        if True:\n")
        (home/'plugins/bridge/__init__.py').write_text(source)
    token = set_hermes_home_override(home)
    manager = PluginManager(scope_key=str(home))
    try:
        manager.discover_and_load()
        drained(manager)
        old_runtime = runtime(manager)
        old_function = old_runtime.voice_bridge().voice_target
        stale = dict(manager._plugins['bridge'].module.announcement)
        # Pin the valid bus ordering where withdrawals drain before the new
        # runtime subscribes. Otherwise late withdrawal can hide the zombie.
        unload = manager.unload
        def unload_and_drain():
            unload()
            drained(manager)
        monkeypatch.setattr(manager, 'unload', unload_and_drain)
        if case == 'register_raises':
            (home/'plugins/bridge/__init__.py').write_text(
                "def register(ctx):\n    raise RuntimeError('fixture register failure')\n")
        else:
            config = yaml.safe_load((home/'config.yaml').read_text())
            config['plugins']['enabled'].remove('bridge')
            config['plugins']['entries']['bridge']['enabled'] = False
            (home/'config.yaml').write_text(yaml.safe_dump(config))
        manager.discover_and_load(force=True)
        drained(manager)
        current = runtime(manager)
        assert old_runtime.closed and old_runtime.voice_bridge() is None
        assert current is not old_runtime
        assert not manager._plugins['bridge'].enabled
        if case == 'register_raises':
            assert manager._plugins['bridge'].error
            assert sys.modules[old_function.__module__].__dict__ is not old_function.__globals__
            # Failed registration removes the bridge's subscribers on this pin.
            # Replay a late old-copy announcement through the real namespaced bus
            # to prove the heartbeat2 defence independently of that cleanup.
            ctx = PluginContext(manager._plugins['bridge'].manifest, manager)
            ctx.emit('heartbeat2_voice_bridge', stale)
            drained(manager)
        if case == 'noncompliant_disabled':
            # Hermes retains the disabled module; liveness cannot detect this copy.
            assert sys.modules[old_function.__module__].__dict__ is old_function.__globals__
            assert current.voice_bridge().voice_target is old_function
            doc = (ROOT/'docs/voice-bridge.md').read_text()
            assert 'can stay connected until the gateway restarts' in doc
            assert 'disabling a bridge takes full effect at the next gateway restart' in doc
        else:
            assert current.voice_bridge() is None
        session_tokens = set_session_vars(platform='telegram', chat_id='fixture', chat_type='dm',
                                          session_key='agent:telegram:dm:fixture', session_id='sid')
        try:
            status = manager._plugin_commands['pulse']['handler']('status')
            connected = 'connected' if case == 'noncompliant_disabled' else 'not connected'
            assert f'voice bridge: bridge ({connected})' in status
        finally:
            clear_session_vars(session_tokens)
    finally:
        manager.unload()
        drained(manager)
        reset_hermes_home_override(token)
