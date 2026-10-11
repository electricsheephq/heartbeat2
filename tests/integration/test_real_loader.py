"""Real Hermes loader + fake gateway/session. No model or socket operations."""
import json
import shutil
import socket
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_real_loader_worker_registration_and_same_session_silence(tmp_path, monkeypatch, clock):
    home = tmp_path/'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME',str(home))
    monkeypatch.setenv('HERMES_DISABLE_LAZY_INSTALLS','1')
    pytest.importorskip('hermes_cli.plugins', reason='Hermes is not importable in this venv')
    from hermes_cli.plugins import PluginManager, PluginContext
    from hermes_cli.plugins_manifest import parse_manifest_file
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from gateway.session_context import set_session_vars, clear_session_vars

    plugin = home/'plugins/heartbeat2'
    shutil.copytree(ROOT,plugin,ignore=shutil.ignore_patterns('.git','.pytest_cache','__pycache__'))
    (home/'config.yaml').write_text('plugins:\n  entries:\n    heartbeat2:\n      enabled: true\n      allow_gateway_injection: true\n      settings:\n        quiet_hours: ""\n        timezone: UTC\n')
    seen = []
    register_tool = PluginContext.register_tool
    def tracked(self,*args,**kwargs):
        seen.append(threading.current_thread().name)
        return register_tool(self,*args,**kwargs)
    monkeypatch.setattr(PluginContext,'register_tool',tracked)
    token = set_hermes_home_override(home)
    manager = PluginManager(scope_key=str(home))
    try:
        manifest = parse_manifest_file(plugin/'plugin.yaml',plugin,'user','')
        assert manifest is not None
        with monkeypatch.context() as guard:
            guard.setattr(socket,'socket',lambda *a,**k: pytest.fail('loader attempted a socket'))
            manager._load_plugin(manifest)
        loaded = manager._plugins['heartbeat2']
        assert loaded.enabled, loaded.error
        assert seen and all(name.startswith('plugin-load:') for name in seen)
        assert 'pulse' in loaded.tools_registered
        assert 'pulse' in loaded.commands_registered
        registration = sys.modules[loaded.module.__name__+'.heartbeat2.registration']
        assert set(registration.HOOKS) <= set(loaded.hooks_registered)
        if hasattr(PluginContext,'register_skill'):
            assert any(entry['bare_name']=='heartbeat2-usage' for entry in manager._plugin_skills.values())
        runtime = registration._RUNTIMES[manager.scope_key]
        # Stop the real timer so the fake clock drives exactly one explicit tick.
        runtime._stop.set()
        runtime._wake.set()
        runtime._thread.join(timeout=2)
        assert not runtime._thread.is_alive()
        runtime.clock = clock
        calls = []
        def inject(**kwargs):
            calls.append(kwargs)
            return True
        manager.set_gateway_message_injector(object(),inject)
        engine = runtime.get_engine()
        engine.paused = lambda: False
        engine.compression_tip = lambda sid: sid
        engine.jitter = lambda: 0
        key = 'agent:telegram:dm:fixture'
        manager.invoke_hook('post_gateway_admission',session_key=key,platform='telegram',
                            source={'chat_type':'dm','chat_id':'fixture'},session_id='sid',text='Follow up')
        # Gateway commands get a bound HERMES_SESSION_KEY (no target argument).
        tokens=set_session_vars(platform='telegram',chat_id='fixture',chat_type='dm',session_key=key,session_id='sid')
        try:
            out = manager._plugin_commands['pulse']['handler']('add in 1m Check the report')
        finally:
            clear_session_vars(tokens)
        assert 'w_' in out, out
        full_schema = sys.modules[loaded.module.__name__+'.heartbeat2.tool'].SCHEMA
        assert full_schema['name'] == 'pulse' and full_schema['description']
        tool_schema = full_schema['parameters']
        assert 'respond' in tool_schema['properties']['action']['enum']
        assert not {'target','session','chat','platform','model'} & set(tool_schema['properties'])
        # The real loader must have put a valid definition into Hermes' global ToolRegistry.
        from tools.registry import registry as hermes_registry
        definition = hermes_registry.get_definitions({'pulse'}, quiet=True)[0]['function']
        assert definition['parameters']['required'] == ['action']
        clock.advance(60)
        engine.tick()
        assert len(calls) == 1 and calls[0]['session_key'] == key
        # Exercise the registered real tool handler with a gateway-bound identity.
        from tools.approval_context import set_current_session_key, reset_current_session_key
        session_token = set_current_session_key(key)
        try:
            manager.invoke_hook('pre_llm_call',session_id='sid',turn_id='turn',platform='telegram',user_message=calls[0]['content'])
            # Dispatch through Hermes' real registry, exactly as the agent loop does.
            result = json.loads(hermes_registry.dispatch('pulse',{'action':'respond','outcome':'no_change','notify':False,'summary':'nothing new'},session_id='sid'))
            assert result['ok'], result
        finally:
            reset_current_session_key(session_token)
        assert '[SILENT]' in manager.invoke_hook('transform_llm_output',session_id='sid',turn_id='turn',response_text='Chatty reply')
        manager.invoke_hook('on_session_end',session_id='sid',turn_id='turn',completed=True,failed=False,interrupted=False)
        engine.tick()
        assert len(calls) == 1
    finally:
        manager.unload()
        reset_hermes_home_override(token)
