"""Real Hermes event bus and local injector; fake notifier, no network."""
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from test_real_loader import _load_hermes
from test_voice_bridge_real import runtime, drained

ROOT=Path(__file__).resolve().parents[2]
NOTIFIER='''
import time
calls=[]
def register(ctx):
    global announcement
    stopped=False
    def send(target,text):
        calls.append((target,text))
        return {'ok':True,'status':'queued','message_id':'fixture'}
    announcement=dict(version=1,registered_at=time.monotonic_ns(),send=send)
    def offer(**_):
        if not stopped: ctx.emit('heartbeat2_notifier',announcement)
    def close():
        nonlocal stopped
        stopped=True
        ctx.emit('heartbeat2_notifier',dict(version=1,registered_at=announcement['registered_at'],withdrawn=True))
    ctx.subscribe('heartbeat2:notifier_wanted',offer)
    ctx.on_unload(close)
    offer()
'''


def make_home(home):
    plugin=home/'plugins/heartbeat2'
    plugin.mkdir(parents=True)
    for name in ('__init__.py','plugin.yaml'): shutil.copy2(ROOT/name,plugin/name)
    for name in ('heartbeat2','skills'):
        shutil.copytree(ROOT/name,plugin/name,ignore=shutil.ignore_patterns('__pycache__'))
    birdie=home/'plugins/birdie'
    birdie.mkdir()
    (birdie/'plugin.yaml').write_text('name: birdie\nversion: "0.0.1"\ndescription: fake notifier\n')
    (birdie/'__init__.py').write_text(NOTIFIER)
    (home/'config.yaml').write_text('''plugins:
  enabled: [heartbeat2, birdie]
  entries:
    heartbeat2:
      enabled: true
      allow_gateway_injection: true
      settings:
        notify_via: birdie:assistant
        quiet_hours: ""
        timezone: UTC
    birdie:
      enabled: true
''')


@pytest.mark.parametrize('birdie_first',[True,False])
def test_real_notifier_held_episode(tmp_path,monkeypatch,clock,birdie_first):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path/'bootstrap'))
    monkeypatch.setenv('HERMES_DISABLE_LAZY_INSTALLS','1')
    _load_hermes()
    from hermes_cli.plugins import PluginManager,PluginContext
    from hermes_cli.plugins_manifest import parse_manifest_file
    from hermes_constants import set_hermes_home_override,reset_hermes_home_override
    from gateway.run_plugin_injection import GatewayPluginInjectionMixin
    if not all(callable(getattr(PluginContext,n,None)) for n in ('emit','subscribe')):
        pytest.skip('this Hermes pin has no plugin event bus')
    if not callable(getattr(GatewayPluginInjectionMixin,'_plugin_local_session',None)):
        pytest.skip('this Hermes pin has no local injection')
    home=tmp_path/'profile'
    make_home(home)
    monkeypatch.setenv('HERMES_HOME',str(home))
    token=set_hermes_home_override(home)
    manager=PluginManager(scope_key=str(home))
    try:
        for name in (('birdie','heartbeat2') if birdie_first else ('heartbeat2','birdie')):
            folder=home/'plugins'/name
            manager._load_plugin(parse_manifest_file(folder/'plugin.yaml',folder,'user',''))
            assert manager._plugins[name].enabled,manager._plugins[name].error
        drained(manager)
        rt=runtime(manager)
        rt._stop.set(); rt._wake.set(); rt._thread.join(2)
        assert not rt._thread.is_alive()
        rt.clock=clock
        runner=NS(_served_profile_for_home=lambda h:'default' if Path(h)==home else None)
        manager.set_gateway_message_injector(runner,lambda **kw:False)
        e=rt.get_engine()
        e.paused=lambda:False
        e.compression_tip=lambda sid:sid
        e.jitter=lambda:0
        prefix=manager._plugins['heartbeat2'].module.__name__+'.heartbeat2.'
        model=sys.modules[prefix+'model']
        key='agent:main:local:dm:fixture'
        e.store.upsert_session(model.Session(key,'desktop','dm','fixture','sid',clock.now(),clock.now(),'episode'))
        w=e.store.create_wake(model.Wake(key,'Private work','at',platform='desktop',chat_type='dm',
            chat_id='fixture',armed_session_id='sid',next_due_at=clock.now(),expires_at=clock.now()+86400))
        e.tick()
        assert e.store.get_meta('held_since:'+e.store.list_occurrences(w.id)[0].id)
        birdie=manager._plugins['birdie'].module
        assert e.notifier().send is birdie.announcement['send']
        clock.advance(540); e.tick(); assert e.notifications.idle.wait(2)
        assert birdie.calls==[]
        clock.advance(60); e.tick(); assert e.notifications.idle.wait(2)
        e.tick(); assert e.notifications.idle.wait(2)
        assert len(birdie.calls)==1 and birdie.calls[0][0]=='assistant'
        assert manager.unload('birdie')
        drained(manager)
        assert rt.notifier() is None
        notifier=sys.modules[prefix+'notifier']
        assert 'not connected' in notifier.notifier_line(e,key)
    finally:
        manager.unload()
        drained(manager)
        reset_hermes_home_override(token)
