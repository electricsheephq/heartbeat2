"""Closed-window notices: fake clock, isolated store, no delivery or network."""
from dataclasses import replace
import json
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest

from heartbeat2 import commands, policy
from heartbeat2.notifier import NotifierRegistry, notice_state, notifier_line
from heartbeat2.policy import Policy
from test_engine import KEY, engine, arm, pending, session, end
from test_app_sessions import bind
from test_v05_voice_bridge import Bus, Context
from heartbeat2.registration import Runtime, register


def offer(registry, stamp=1, **kw):
    data = dict(version=1, registered_at=stamp, send=lambda *a: {'ok': True}) | kw
    registry.announce(**data)
    return data


@pytest.mark.parametrize('value', ['Birdie:assistant', 'birdie:', ':assistant', 'birdie:a/b', 'birdie:a b',
    'a'*65+':b', 'a:'+'b'*65, True, 3, None])
def test_invalid_setting(value):
    with pytest.raises(ValueError): Policy.from_settings({'notify_via': value})


@pytest.mark.parametrize('value', ['', '  ', 'birdie:assistant', 'a_-:A.1_-'])
def test_valid_setting(value):
    assert Policy.from_settings({'notify_via':value}).notify_via == value.strip()


def test_registry_lifecycle(monkeypatch):
    r = NotifierRegistry()
    a = offer(r, 2)
    offer(r, 1)
    assert r.get().send is a['send']
    r.announce(version=1, registered_at=3, withdrawn=True)
    offer(r, 3); offer(r, 2)
    assert r.get() is None
    offer(r, 4)
    assert r.get()
    module = ModuleType('fixture_notifier')
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec('def send(*a): return {"ok": True}', module.__dict__)
    offer(r, 5, send=module.send)
    monkeypatch.setitem(sys.modules, module.__name__, ModuleType(module.__name__))
    assert r.get() is None
    r.close(); offer(r, 6)
    assert r.get() is None


@pytest.mark.parametrize('change', [{'version':True}, {'version':2}, {'registered_at':True},
    {'registered_at':0}, {'registered_at':1.1}, {'send':None}])
def test_registry_invalid_once(change, caplog):
    r = NotifierRegistry()
    offer(r, **change); offer(r, **change)
    assert r.get() is None and len(caplog.records) == 1


def held(e, monkeypatch, surface='desktop', count=1):
    monkeypatch.setattr(policy, 'local_injection_supported', lambda:True)
    session(e, platform=surface)
    e.policy = replace(e.policy, notify_via='birdie:assistant')
    e.ctx.accept = False
    calls = []
    r = NotifierRegistry()
    offer(r, send=lambda target,text: calls.append((target,text)) or {'ok':True})
    e.notifier = r.get
    wakes = []
    for _ in range(count):
        w = arm(e, platform=surface, title='PRIVATE TITLE')
        o = pending(e,w)
        e.dispatch(o,cache_line='')
        wakes.append((w,o))
    return calls,r,wakes


def settle(e):
    assert e.notifications.idle.wait(2), 'worker did not finish'


@pytest.mark.parametrize('surface,label',[('desktop','Desktop'),('tui','TUI')])
def test_episode_timing_accounting_text_and_reopen(engine, monkeypatch,surface,label):
    calls,r,wakes = held(engine,monkeypatch,surface=surface,count=3)
    engine.clock.advance(540); engine.notifications.tick(engine); settle(engine)
    assert calls == []
    engine.clock.advance(60); engine.notifications.tick(engine); settle(engine)
    assert len(calls) == 1 and calls[0][0] == 'assistant'
    text = calls[0][1]
    assert text.startswith(f"HeartBeat: 3 wake(s) in default's {label} chat have been waiting since ")
    assert len(text) <= 240 and 'PRIVATE' not in text and text.endswith('Open that chat to let them run.')
    assert engine.daily(KEY)['delivered'] == 1
    assert engine.store.get_session(KEY).unanswered_count == 0
    assert 'queued at ' in notice_state(engine,KEY)
    events = engine.store.connection.execute("SELECT payload_json FROM events WHERE kind='notify'").fetchall()
    assert len(events) == 1 and text not in events[0][0]
    engine.clock.advance(3600); engine.notifications.tick(engine); settle(engine)
    assert len(calls) == 1
    engine.ctx.accept = True
    w,o = wakes[0]
    engine.dispatch(o,cache_line='')
    assert engine.store.get_meta('notify_episode:'+KEY) is None
    engine.intake.pre_llm_call(session_id='sid',turn_id='turn',user_message=engine.ctx.calls[-1][0],platform=surface)
    end(engine,'progress',False,'Reopened.')
    engine.clock.advance(600)
    engine.ctx.accept = False
    # A fresh hold in this chat starts a fresh episode, including remaining wakes.
    w2 = arm(engine,platform=surface); o2 = pending(engine,w2)
    engine.dispatch(o2,cache_line='')
    engine.clock.advance(600); engine.notifications.tick(engine); settle(engine)
    assert len(calls) == 2


@pytest.mark.parametrize('gate', ['quiet', 'cap', 'unanswered', 'off', 'paused', 'messaging', 'blank', 'disconnected'])
def test_chat_gates(engine,monkeypatch,gate):
    calls,r,_ = held(engine,monkeypatch)
    if gate == 'quiet': engine.policy = replace(engine.policy,quiet_hours='00:00-23:59')
    if gate == 'cap': engine.policy = replace(engine.policy,max_messages_per_day=0)
    if gate == 'unanswered': session(engine,unanswered_count=100)
    if gate == 'off': session(engine,enabled=False)
    if gate == 'paused': session(engine,paused_until=engine.clock.now()+3600)
    if gate == 'messaging': session(engine,platform='telegram')
    if gate == 'blank': engine.policy = replace(engine.policy,notify_via='')
    if gate == 'disconnected': r.close()
    engine.clock.advance(600); engine.notifications.tick(engine); settle(engine)
    assert len(calls) == int(gate == 'unanswered')
    if gate in {'quiet','cap'}:
        if gate == 'quiet': engine.policy=replace(engine.policy,quiet_hours='')
        else:
            engine.policy=replace(engine.policy,max_messages_per_day=6)
            engine.clock.advance(86400)
        engine.notifications.tick(engine); settle(engine)
        assert len(calls) == 1


@pytest.mark.parametrize('error,label', [('no token for this profile','notifier not ready'),
    ('TimeoutError: timed out','may repeat'), ('refused','waiting until')])
def test_retries(engine,monkeypatch,error,label):
    calls,r,_ = held(engine,monkeypatch)
    def send(*a): calls.append(a); return {'ok':False,'error':error+'x'*150}
    offer(r,2,send=send)
    engine.clock.advance(600)
    for index,delay in enumerate([0,300,300,3600]):
        engine.clock.advance(delay); engine.notifications.tick(engine); settle(engine)
        assert len(calls) == index+1
        assert label in notice_state(engine,KEY)
        data=json.loads(engine.store.get_meta('notify_episode:'+KEY))
        assert len(data['error']) <= 120
        engine.clock.advance(1); engine.notifications.tick(engine); settle(engine)
        assert len(calls) == index+1
    engine.clock.advance(86400); engine.notifications.tick(engine); settle(engine)
    assert 'notify failed' in notice_state(engine,KEY) and len(calls) == 4


def test_raise_and_slow_send_do_not_block_tick(engine,monkeypatch):
    calls,r,_=held(engine,monkeypatch)
    entered=threading.Event(); release=threading.Event()
    def slow(*a):
        entered.set(); release.wait(5); raise RuntimeError('refused')
    offer(r,2,send=slow)
    engine.clock.advance(600)
    start=time.monotonic(); engine.tick()
    assert time.monotonic()-start < 1 and entered.wait(1)
    engine.tick()
    assert not engine.notifications.idle.is_set()
    release.set(); settle(engine)
    assert 'waiting until' in notice_state(engine,KEY)


def test_status_desk_and_gc(engine,monkeypatch):
    calls,r,wakes=held(engine,monkeypatch)
    bind(monkeypatch,'desktop')
    rt=SimpleNamespace(get_engine=lambda:engine)
    for cmd in ('status','desk'):
        text=commands.handle(cmd,rt)
        assert 'notify_via: birdie:assistant, connected' in text and 'waiting until' in text
        assert text.count('notify_via:') == 1
    for w,o in wakes: engine.store.cas_occurrence(w.id,o.dedupe_key,'pending','skipped')
    engine.notifications.tick(engine)
    assert engine.store.get_meta('notify_episode:'+KEY) is None


def test_registration_invalid_and_no_bus(tmp_path,monkeypatch,clock,caplog):
    monkeypatch.setattr(Runtime,'start',lambda s:None)
    for setting in ('Bad:value','birdie:assistant'):
        ctx=Context(tmp_path,Bus(),{'notify_via':setting})
        ctx.emit=None
        rt=register(ctx,clock=clock)
        try:
            e=rt.get_engine()
            assert ('invalid' if setting.startswith('Bad') else 'not connected') in notifier_line(e)
            rt.policy(); rt.policy()
        finally: rt.close()
    assert sum('notify_via ignored' in r.message for r in caplog.records) == 1
