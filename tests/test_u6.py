"""U6 red-first regressions; local synthetic data and loopback only."""
from dataclasses import replace
import json
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from test_engine import engine, arm, start, session, KEY
from test_commands import runtime
from test_r3 import observer, _CONNECT, _DNS, _CREATE
from heartbeat2 import commands, probes, registration
from heartbeat2.model import Wake, Occurrence, Session
from heartbeat2.poller import Poller
from heartbeat2.store import Store
from heartbeat2.wake_text import render_wake
from bench.run import load_scenarios
from bench.sim.runner import simulate
from heartbeat2.intake import Intake


def terminal_occurrence(e,w,cause):
    e.store.add_occurrence(w.id,'t:1')
    if cause=='missed':
        e.store.cas_occurrence(w.id,'t:1','pending','missed')
    else:
        e.store.claim(w.id,'t:1',epoch=e.epoch,pid=1)
        e.store.cas_occurrence(w.id,'t:1','claimed',cause,attempt=1)


@pytest.mark.parametrize('cause', ['interrupted','dead','missed'])
def test_R6_L1_terminal_unsuccessful_one_shot(runtime,cause):
    e=runtime.get_engine(); w=arm(e,next_due_at=None)
    terminal_occurrence(e,w,cause)
    e.tick(); current=e.store.get_wake(w.id)
    assert current.status=='failed' and cause in current.status_reason
    assert cause in commands.handle('list',runtime)
    assert cause in commands.handle('why '+w.id,runtime)
    recurring=arm(e,'every',next_due_at=e.clock.now()+3600)
    terminal_occurrence(e,recurring,cause); e.tick()
    assert e.store.get_wake(recurring.id).status=='active'
    assert e.store.get_wake(recurring.id).next_due_at is not None


@pytest.mark.parametrize('late,restart', [(0,False),(1,False),(59,False),(60,False),(3600,True)])
def test_R6_L2_natural_snapshot(clock,late,restart):
    w=Wake('s','Task','at',id='w_test',created_at=clock.now()); o=Occurrence(w.id,'t:1')
    kw={'after_restart':True} if restart else {}
    text=render_wake(w,o,Session('s'),clock=clock,late_by_s=late,**kw)
    assert 'fire 1 of 1' in text and 'unknown ago' not in text
    assert ('late by' in text)==(late>=60)
    assert ('after restart' in text)==restart


@pytest.mark.parametrize('reason,code,klass', [('rate_limit',429,'rate_limit'),('quota_exhausted',429,'quota'),('auth',403,'auth'),('billing',402,'billing'),('server_error',503,'server')])
def test_R6_L3_provider_settlement_and_profile_gate(runtime,reason,code,klass):
    e=runtime.get_engine(); w=arm(e,'every'); e.tick(); start(e)
    rt=registration.Runtime(e.ctx,clock=e.clock); rt._engine=e
    assert rt.hook('api_request_error',session_id='sid',turn_id='turn',platform='telegram',reason=reason,
                   status_code=code,retry_count=0,max_retries=5,error={'type':'SyntheticProviderError','message':'private fixture'}) is None
    o=e.store.list_occurrences(w.id)[0]
    assert o.state=='started' and e.store.get_meta('provider_error:'+o.id)==klass
    assert e.store.get_wake(w.id).error_streak==0
    e.intake.on_session_end(session_id='sid',turn_id='turn',failed=True)
    o=e.store.list_occurrences(w.id)[0]
    assert o.state=='interrupted' and o.error=='provider_error:'+klass and o.force_silent
    assert e.store.get_wake(w.id).error_streak==1
    other=arm(e); e.tick()
    assert len(e.ctx.calls)==1
    assert e.store.list_occurrences(other.id)[0].error=='provider_unavailable'
    gate=json.loads(e.store.get_meta('provider_health'))
    assert gate['until']==e.clock.now()+300
    assert 'provider_unavailable' in commands.handle('status',runtime)
    assert 'deferrals' in commands.handle('ledger',runtime)
    # The same physical turn's retries cannot advance the ladder again.
    rt.hook('api_request_error',session_id='sid',turn_id='turn',reason=reason,status_code=code,error={'type':'SyntheticProviderError'})
    assert json.loads(e.store.get_meta('provider_health'))==gate
    # Any successful human model call clears the profile gate, without usage.
    e.intake.post_api_request(session_id='human-sid',turn_id='human-turn')
    e.clock.advance(600); e.tick()
    assert len(e.ctx.calls)==2
    assert not e.store.get_meta('provider_health')


def test_R6_L3_backoff_steps_and_g5_fallback(engine):
    e=engine
    for delay in (300,900,3600,14400,14400):
        w=arm(e,'every'); e.tick(); start(e)
        e.intake.api_request_error(session_id='sid',turn_id='turn',reason='rate_limit',status_code=429)
        gate=json.loads(e.store.get_meta('provider_health'))
        assert gate['until']-e.clock.now()==delay
        assert e.store.list_occurrences(w.id)[0].state=='started'
        e.clock.advance(1800); e.acks()
        assert e.store.list_occurrences(w.id)[0].error=='provider_error:rate_limit'
        assert json.loads(e.store.get_meta('provider_health'))==gate
        e.store.cas_wake(w.id,e.store.get_wake(w.id).status,status='cancelled')
        e.clock.advance(delay+600)
    # Provider identity stored with a flight is consumed by the new-turn path.
    w=arm(e,'every'); e.tick(); start(e)
    o=e.store.list_occurrences(w.id)[0]
    e.store.set_meta('provider_error:'+o.id,'auth')
    e.intake.pre_llm_call(session_id='sid',turn_id='new',platform='telegram',user_message='Hello')
    assert e.store.get_occurrence(w.id,o.dedupe_key).error=='provider_error:auth'


def test_R6_L4_E8_E14_shared_db_liveness(runtime):
    e=runtime.get_engine(); e.tick()
    e.runtime_status=lambda:{'state':'not running','reason':'bootstrap thread stopped'}
    assert e.status()['state']=='alive'
    store=Store(e.store.path,e.clock)
    reader=Poller(e.ctx,store,e.policy,e.clock)
    w=arm(e,'command',trigger_json={'command':['git','status']})
    observed=[]
    def probe(wake):
        e.clock.advance(25)
        observed.append(reader.status())
        observed.append(commands.handle('status',runtime))
        return {'fire':False}
    e.probe_runner=probe; e.tick()
    assert observed[0]=={'state':'alive','reason':'checking probes'}
    assert 'poller alive' in observed[1]
    assert not store.get_meta('probing_since')
    e.clock.advance(40); assert reader.status()['state']=='stalled'
    reader.close(); store.close()


@pytest.mark.parametrize('result',[{'fire':False,'state':{'old':1}},{'fire':True,'state':{'old':1},'message':'changed'},{'error':'TimeoutError'}])
def test_R6_E9_retarget_during_probe(engine,result):
    e=engine; w=arm(e,'file',trigger_json={'path':'old'})
    def probe(original):
        now=e.store.get_wake(w.id)
        e.store.cas_wake(w.id,'active',expected_version=now.version,trigger_json={'path':'new'},probe_state_json=None)
        return result
    e.probe_runner=probe; e.tick()
    current=e.store.get_wake(w.id)
    assert current.probe_state_json is None and current.error_streak==0
    assert not e.store.list_occurrences(w.id)


@pytest.mark.parametrize('flag',['-wc','-vw','-wRowner/repo','-vp'])
def test_R6_E10_absolute_gh_flags(flag):
    assert not probes.command_allowed(['/opt/homebrew/bin/gh','pr','view','1',flag],'/opt/homebrew/bin/gh pr view')


@pytest.mark.parametrize('interval,expected',[(300,600),(80000,86400)])
def test_R6_E11_E17_slow_probe_cadence(runtime,interval,expected):
    e=runtime.get_engine(); w=arm(e,'command',status='paused',status_reason='governor:error',trigger_json={'command':['git','status'],'check_every':interval})
    text=commands.handle('slow '+w.id,runtime); current=e.store.get_wake(w.id)
    assert current.trigger_json['check_every']==expected
    assert current.next_due_at==e.clock.now()+expected
    assert str(expected) in text


def test_R6_E11_slow_one_shot_refused(runtime):
    e=runtime.get_engine(); w=arm(e,status='paused',status_reason='governor:error')
    assert 'slow applies to recurring wakes and watchers' in commands.handle('slow '+w.id,runtime)
    assert e.store.get_wake(w.id).status=='paused'


@pytest.mark.parametrize('anchor,expected',[('9:00pm','21:00'),('9pm','21:00'),('9am','09:00'),('9:00','09:00'),('09:00','09:00')])
def test_R6_E12_clock_anchor(runtime,anchor,expected):
    e=runtime.get_engine(); text=commands.handle('add every 1d at '+anchor+'\nCheck release',runtime)
    assert not text.startswith('Error'),text
    w=e.store.list_wakes()[0]; assert w.trigger_json.get('at')==expected and w.note=='Check release'


@pytest.mark.parametrize('tail',['9:5 note','9:00pm','9am','25pm note'])
def test_R6_E12_bad_anchor_never_note(runtime,tail):
    assert commands.handle('add every 1d at '+tail,runtime).startswith('Error')
    assert not runtime.get_engine().store.list_wakes()


def test_R6_E15_command_no_shell_and_deadline(tmp_path,monkeypatch):
    assert probes.COMMAND_DEADLINE_S==30.0
    seen=[]; real=probes.subprocess.Popen
    def spy(argv,**kw):
        seen.append(kw); return real(argv,**kw)
    monkeypatch.setattr(probes.subprocess,'Popen',spy)
    # Direct runner exercises argv boundaries, including literal shell syntax.
    rc,body=probes._run_command([sys.executable,'-c','import sys,json;print(json.dumps(sys.argv[1:]))','a b','$HOME;echo x'],tmp_path)
    assert seen[0]['shell'] is False
    assert rc==0 and json.loads(body)==['a b','$HOME;echo x']


@pytest.mark.parametrize('phase',['headers','chunk'])
def test_R6_E7_E13_url_wall_deadline(engine,monkeypatch,phase):
    def connect(sock,address):
        assert address[0]=='127.0.0.1'; return _CONNECT(sock,address)
    def dns(host,*args):
        assert host=='127.0.0.1'; return _DNS(host,*args)
    monkeypatch.setattr(socket.socket,'connect',connect)
    monkeypatch.setattr(socket,'getaddrinfo',dns)
    monkeypatch.setattr(socket,'create_connection',_CREATE)
    listener=socket.socket(); listener.bind(('127.0.0.1',0)); listener.listen()
    stopped=threading.Event()
    def serve():
        conn,_=listener.accept()
        with conn:
            conn.recv(4096)
            prefix=b'HTTP/1.1 200 OK\r\nX-Slow: ' if phase=='headers' else b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'
            try:
                conn.sendall(prefix)
                for _ in range(30):
                    if stopped.wait(.05):break
                    conn.sendall(b'0')
                conn.sendall(b'\r\n\r\n')
            except OSError:pass
    thread=threading.Thread(target=serve,daemon=True); thread.start()
    monkeypatch.setattr(probes,'URL_DEADLINE_S',.25)
    e=engine; w=arm(e,'url',trigger_json={'url':f'http://127.0.0.1:{listener.getsockname()[1]}/'})
    arm(e)
    before=time.monotonic()
    try:
        e.tick(); elapsed=time.monotonic()-before
        assert elapsed<.75,elapsed
        assert e.store.get_wake(w.id).error_streak==1
        assert len(e.ctx.calls)==1
    finally:
        stopped.set(); listener.close(); thread.join(1)


def test_R6_E1_nonterminal_coalescing_verdicts():
    for name in ('sleep-62294','hermes-scale-zero'):
        assert load_scenarios(name)[0]['model_policy']['default'][0]['outcome']=='progress'


def test_R6_E2_real_stale_claim_contention(tmp_path):
    result=simulate(load_scenarios('duplicates-59703')[0],tmp_path)
    races=[x for x in result['trace'] if x['event']=='stale_claim']
    assert races and all(not x['claimed'] for x in races)
    assert sum(x['event']=='standby_rebuilt' for x in result['trace'])==2


def test_R6_E3_estop_due_window(tmp_path,monkeypatch):
    monkeypatch.setattr(Poller,'paused_unused',None,raising=False)
    # Removing every estop seam must produce an observable forbidden delivery.
    original=Poller.__init__
    def init(self,*args,**kw):
        kw['paused']=lambda:False; original(self,*args,**kw)
    monkeypatch.setattr(Poller,'__init__',init)
    assert simulate(load_scenarios('estop-i19')[0],tmp_path)['failures']


def test_R6_E4_baseline_duplicates_and_resend(tmp_path):
    duplicate=simulate(load_scenarios('duplicates-59703')[0],tmp_path/'dup',baseline=True)
    resend=simulate(load_scenarios('resend-85743')[0],tmp_path/'resend',baseline=True)
    assert duplicate['metrics']['duplicate_deliveries']>0
    assert resend['metrics']['resent_messages']>=20
    occurrences=[t['occurrence'] for t in resend['turns']]
    assert len(occurrences)==len(set(occurrences))


def test_R6_E5_lateness_excludes_gates(tmp_path):
    result=simulate(load_scenarios('busy-external-64016')[0],tmp_path)
    turn=next(x for x in result['turns'] if x['ref']=='external')
    assert turn['deferred_s']==600 and turn['lateness_s']==0
    assert result['metrics']['latency_sample_count']==len(result['turns'])
    stopped=simulate(load_scenarios('estop-i19')[0],tmp_path/'estopped')['turns'][0]
    assert stopped['deferred_s']==1400 and stopped['lateness_s']==0


def test_R6_E6_baseline_comparable_paths(tmp_path):
    for name in ('busy-external-64016','compaction-2935','restart-78187','uptime-59710','positive-ci','hermes-drained'):
        result=simulate(load_scenarios(name)[0],tmp_path/name,baseline=True)
        assert result['turns'],name
    ci=simulate(load_scenarios('positive-ci')[0],tmp_path/'ci',baseline=True)
    assert not ci['failures']


def test_R6_E16_silenced_human_reply_fails_gate(tmp_path,monkeypatch):
    real=Intake.transform_llm_output
    def silent(self,**kw):
        real(self,**kw); return '[SILENT]'
    monkeypatch.setattr(Intake,'transform_llm_output',silent)
    failures=simulate(load_scenarios('hermes-drained')[0],tmp_path)['failures']
    assert any('prompted_deliveries' in f for f in failures)


def test_R6_L3_TTL_provider_identity(engine):
    e=engine; w=arm(e,'every'); e.tick(); start(e)
    o=e.store.list_occurrences(w.id)[0]; e.store.set_meta('provider_error:'+o.id,'server')
    e.clock.advance(18001); e.acks()
    current=e.store.get_occurrence(w.id,o.dedupe_key)
    assert current.state=='interrupted' and current.error=='provider_error:server'
    assert e.store.get_wake(w.id).error_streak==1
    assert json.loads(e.store.get_meta('provider_health'))['until']==e.clock.now()+300


def test_R6_L3_single_profile_recovery_trial(engine):
    e=engine; e.acquire(); e.provider_failure('auth'); e.clock.advance(300)
    # Two conversations, one profile: only one recovery model turn is admitted.
    for key in (KEY,'other'):
        if key!='other': w=arm(e)
        else:
            e.store.upsert_session(Session(key,'telegram','dm','other','other-sid'))
            e.store.create_wake(Wake(key,'Task','at',platform='telegram',chat_type='dm',chat_id='other',armed_session_id='other-sid',next_due_at=e.clock.now()))
    e.tick(); assert len(e.ctx.calls)==1
    assert len(e.store.list_occurrences(state='pending'))==1
    gate=json.loads(e.store.get_meta('provider_health')); assert gate.get('trial')
    # A trial which never starts cannot keep the gate wedged forever.
    o=e.store.list_occurrences(state='claimed')[0]
    e.store.cas_occurrence(o.wake_id,o.dedupe_key,'claimed','dead',attempt=o.attempt)
    e.tick(); assert len(e.ctx.calls)==2


def test_R6_L2_engine_restart_wording(engine):
    e=engine; w=arm(e,next_due_at=e.clock.now()+60)
    e.tick(); session(e,busy_since=e.clock.now()); e.clock.advance(3600); e.tick()
    session(e,busy_since=None); e.tick()
    assert 'held by a busy chat' in e.ctx.calls[-1][0] and 'after restart' not in e.ctx.calls[-1][0]


def test_R6_E9_ABA_retarget_generation(engine):
    e=engine; w=arm(e,'file',trigger_json={'path':'old'})
    def check(original):
        for path in ('new','old'):
            current=e.store.get_wake(w.id)
            e.store.cas_wake(w.id,'active',expected_version=current.version,trigger_json={'path':path})
        return {'fire':True,'message':'stale','state':{'old':1}}
    e.probe_runner=check; e.tick()
    assert not e.store.list_occurrences(w.id)
    assert e.store.get_wake(w.id).probe_state_json is None
