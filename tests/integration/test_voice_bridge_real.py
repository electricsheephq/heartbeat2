"""SPEC-162 against the real profile-local Hermes event bus, with no delivery."""
import shutil
import sys
from pathlib import Path

import pytest

from test_real_loader import _load_hermes

ROOT = Path(__file__).resolve().parents[2]
BRIDGE = '''
import time
def register(ctx):
    global transport, announcement
    def voice_target(session_id): return ctx.plugin_id
    transport = voice_target
    announcement = data = dict(version=1, registered_at=time.monotonic_ns(),
        voice_target=voice_target, owner_message=lambda *a: False,
        wake_ready=lambda *a: False, inject_wake=lambda *a: False)
    ctx.subscribe('heartbeat2:voice_bridge_wanted',
        lambda **kw: ctx.emit('heartbeat2_voice_bridge', data))
    ctx.emit('heartbeat2_voice_bridge', data)
    ctx.on_unload(lambda: ctx.emit('heartbeat2_voice_bridge', dict(
        version=1, registered_at=data['registered_at'], withdrawn=True)))
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
