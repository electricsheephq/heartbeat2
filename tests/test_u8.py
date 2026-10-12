"""U8 review regressions: synthetic sessions, fake clocks, loopback probes only."""
from dataclasses import replace
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest
from test_engine import engine, arm, start, session, KEY
from test_commands import runtime
from test_r3 import _CONNECT, _DNS, _CREATE
from heartbeat2 import commands, probes
from heartbeat2.poller import Poller
from heartbeat2.policy import Policy
from heartbeat2.store import Store
from heartbeat2.schedule import local_day
from bench.run import load_scenarios
from bench.sim.runner import simulate


@pytest.mark.parametrize('reason,code,retryable', [('rate_limit',429,True),('auth',401,False)])
def test_R8_F1_retry_success_delivers_done(engine,reason,code,retryable):
    e=engine; w=arm(e); e.tick(); start(e)
    o,=e.store.list_occurrences(w.id)
    for retry in range(3):
        e.intake.api_request_error(session_id='sid',turn_id='turn',reason=reason,status_code=code,
                                  retryable=retryable,retry_count=retry,max_retries=3)
    assert e.store.get_occurrence(w.id,o.dedupe_key).state=='started'
    assert not e.intake.flights['sid'].invalid
    assert json.loads(e.store.get_meta('provider_health'))['step']==0
    e.intake.post_api_request(session_id='sid',turn_id='turn')
    assert not e.store.get_meta('provider_error:'+o.id)
    result=e.tool.handle({'action':'respond','outcome':'done','notify':True,'summary':'Ready.'},session_id='sid')
    assert result.get('ok'),result
    assert e.intake.transform_llm_output(session_id='sid',turn_id='turn',response_text='Ready.') is None
    e.intake.on_session_end(session_id='sid',turn_id='turn',completed=True)
    current=e.store.get_occurrence(w.id,o.dedupe_key)
    assert current.state=='done' and current.delivered and not current.error
    assert e.store.get_wake(w.id).status=='done' and e.store.get_wake(w.id).error_streak==0


@pytest.mark.parametrize('timeout', [None,800])
def test_R8_F1_error_only_agent_timeout_never_replays(engine,monkeypatch,timeout):
    if timeout is None: monkeypatch.delenv('HERMES_AGENT_TIMEOUT',raising=False)
    else: monkeypatch.setenv('HERMES_AGENT_TIMEOUT',str(timeout))
    limit=timeout or 1800
    e=engine; w=arm(e); e.tick(); start(e)
    o,=e.store.list_occurrences(w.id)
    e.intake.api_request_error(session_id='sid',turn_id='turn',reason='server_error',status_code=503)
    gate=json.loads(e.store.get_meta('provider_health'))
    e.clock.advance(limit-1); e.acks()
    assert e.store.get_occurrence(w.id,o.dedupe_key).state=='started'
    e.clock.advance(1); e.acks()
    current=e.store.get_occurrence(w.id,o.dedupe_key)
    assert current.state=='interrupted' and current.error=='provider_error:server'
    assert e.store.get_session(KEY).busy_since is None
    assert e.store.get_wake(w.id).status=='failed'
    assert json.loads(e.store.get_meta('provider_health'))==gate
    e.clock.advance(20000); e.tick()
    assert len(e.ctx.calls)==1


def test_R8_F1_outage_one_trial_per_backoff_step(engine):
    e=engine; e.policy=replace(e.policy,wake_spacing_minutes=0)
    w=arm(e,'every'); e.tick(); start(e)
    for step,delay in enumerate((300,900,3600,14400)):
        now=e.clock.now()
        e.intake.api_request_error(session_id='sid',turn_id='turn',reason='rate_limit',status_code=429)
        gate=json.loads(e.store.get_meta('provider_health'))
        assert gate['step']==step and gate['until']==now+delay
        # A later turn is an earlier, definitive failure settlement (G5).
        e.intake.pre_llm_call(session_id='sid',turn_id='next',platform='telegram',user_message='/pulse status')
        e.intake.on_session_end(session_id='sid',turn_id='next',completed=True)
        assert json.loads(e.store.get_meta('provider_health'))==gate
        session(e,last_wake_turn_at=None)
        arm(e,'every'); arm(e,'every')
        before=len(e.ctx.calls)
        e.clock.advance(delay-1); e.tick(); assert len(e.ctx.calls)==before
        e.clock.advance(1); e.tick(); e.tick()
        assert len(e.ctx.calls)==before+1
        assert e.store.list_occurrences(state='pending')
        trial,=e.store.list_occurrences(state='claimed')
        w=e.store.get_wake(trial.wake_id)
        start(e)
        # Park the earlier schedules while retaining the live trial.
        for old in e.store.list_wakes():
            if old.id!=w.id: e.store.cas_wake(old.id,old.status,status='cancelled')


@pytest.mark.parametrize('reason,code', [('timeout',None),('context_overflow',400),('model_not_found',404),
    ('unknown',None),('rate_limit',429),('quota_exhausted',429),('auth',401),('billing',402),('server_error',503),
    ('auth_permanent',401),('upstream_rate_limit',429),('upstream_blocked',403),('overloaded',503),
    ('ssl_cert_verification',None),('payload_too_large',413),('image_too_large',400),('image_corrupt',400),
    ('provider_policy_blocked',400),('content_policy_blocked',400),('model_entitlement',400),
    ('incomplete_response',None),('format_error',400),('role_alternation',400),('invalid_encrypted_content',400),
    ('multimodal_tool_content_unsupported',400),('reasoning_mandatory',400),('thinking_signature',400),
    ('long_context_tier',400),('oauth_long_context_beta_forbidden',400),('llama_cpp_grammar_pattern',400)])
def test_R8_F2_all_api_error_reasons_reach_watchers(engine,reason,code):
    e=engine; w=arm(e,'api_error',next_due_at=None)
    e.intake.pre_llm_call(session_id='sid',turn_id='human',platform='telegram',user_message='Hi')
    e.intake.api_request_error(session_id='sid',turn_id='human',reason=reason,status_code=code,task_id='task')
    e.tick()
    o,=e.store.list_occurrences(w.id)
    assert o.payload_json==[{'task_id':'task','reason':reason}]


def test_R8_F3_closed_window_holds_ladder_and_trial_advances(engine):
    e=engine; e.acquire()
    for i in range(4):
        e.intake.pre_llm_call(session_id='sid',turn_id=f'h{i}',platform='telegram',user_message='There?')
        e.intake.api_request_error(session_id='sid',turn_id=f'h{i}',reason='rate_limit',status_code=429)
        e.clock.advance(45)
    gate=json.loads(e.store.get_meta('provider_health'))
    assert gate['step']==0 and gate['until']==e.clock.now()+120
    e.clock.advance(120)
    e.provider_failure('rate_limit')
    gate=json.loads(e.store.get_meta('provider_health')); assert gate['step']==1
    gate['trial']='synthetic'; e.store.set_meta('provider_health',json.dumps(gate))
    e.provider_failure('auth')
    assert json.loads(e.store.get_meta('provider_health'))['step']==2


@pytest.mark.parametrize('tick_s',[1,5])
def test_R8_F4_F12_one_provider_deferral_no_checks(engine,tick_s):
    e=engine; e.acquire(); e.provider_failure('rate_limit'); w=arm(e)
    for _ in range(300//tick_s):
        e.tick(); e.clock.advance(tick_s)
    e.tick()
    row=e.store.get_ledger(local_day(e.clock.now(),e.policy.resolved_timezone()),w.id)
    assert row['provider_deferrals']==1 and row['gated_skips']==0
    assert len(e.ctx.calls)==1


@pytest.mark.parametrize('state',['pending','claimed'])
def test_R8_P1_pause_tick_resume_one_shot_fires_once(engine,state):
    e=engine; e.tick()
    if state=='pending': session(e,busy_since=e.clock.now())
    w=arm(e,creator='agent'); e.tick()
    o,=e.store.list_occurrences(w.id); assert o.state==state
    result=e.tool.handle({'action':'pause','id':w.id},session_id='sid'); assert result.get('ok'),result
    assert e.store.get_occurrence(w.id,o.dedupe_key).state=='superseded'
    session(e,busy_since=None,last_wake_turn_at=None)
    e.clock.advance(30); e.tick(); assert e.store.get_wake(w.id).status=='paused'
    result=e.tool.handle({'action':'resume','id':w.id},session_id='sid'); assert result.get('ok'),result
    e.tick(); start(e)
    assert e.tool.handle({'action':'respond','outcome':'done','notify':True,'summary':'Ready'},session_id='sid').get('ok')
    assert e.intake.transform_llm_output(session_id='sid',response_text='Ready') is None
    e.intake.on_session_end(session_id='sid',completed=True); e.tick()
    rows=e.store.list_occurrences(w.id)
    assert [o.state for o in rows]==['superseded','done']
    assert e.store.get_wake(w.id).fires==1 and rows[-1].delivered


def test_R8_P1_pause_keeps_started_turn(engine):
    e=engine; w=arm(e); e.tick(); start(e)
    assert e.tool.handle({'action':'pause','id':w.id},session_id='sid').get('ok')
    assert e.store.list_occurrences(w.id)[0].state=='started'


@pytest.mark.parametrize('trigger,future',[({},False),({'at':'in 1h'},True)])
def test_R8_P1_resume_uses_stored_due_and_fresh_key(engine,trigger,future):
    e=engine; due=e.clock.now()+720 if future else e.clock.now()
    w=arm(e,status='paused',next_due_at=due if future else None,trigger_json=trigger)
    e.store.add_occurrence(w.id,f't:{due:.17g}')
    assert e.store.cas_occurrence(w.id,f't:{due:.17g}','pending','superseded')
    result=e.tool.handle({'action':'resume','id':w.id},session_id='sid')
    assert result.get('ok'),result
    scheduled=e.store.get_wake(w.id).next_due_at
    assert due<scheduled<due+.01
    assert e.store.get_occurrence(w.id,f't:{scheduled:.17g}') is None


def test_R8_P1_gate_reply_cancels_with_owner_reason(engine):
    e=engine; e.acquire(); w=arm(e,cancel_on_reply=True)
    session(e,last_human_at=e.clock.now()+1)
    e.tick()
    current=e.store.get_wake(w.id)
    assert (current.status,current.status_reason)==('cancelled','owner_replied')
    assert not e.ctx.calls


@pytest.mark.parametrize('state,error,expected', [('skipped','unanswered','failed'),('skipped','wake_inactive','active'),
    ('interrupted','failed','failed'),('dead','ack_timeout','failed'),('missed','','failed')])
def test_R8_F8_latest_non_superseded_terminal(engine,state,error,expected):
    e=engine; e.acquire(); w=arm(e,next_due_at=None)
    for key,terminal in [('old','skipped'),('paused','superseded'),('latest',state)]:
        e.store.add_occurrence(w.id,key)
        if terminal in {'dead','interrupted'}:
            e.store.claim(w.id,key,epoch=e.epoch,pid=1)
            assert e.store.cas_occurrence(w.id,key,'claimed',terminal,attempt=1,error=error)
        else: assert e.store.cas_occurrence(w.id,key,'pending',terminal,error=error)
    e.terminal_one_shot(e.store.get_wake(w.id))
    assert e.store.get_wake(w.id).status==expected


@pytest.mark.parametrize('paused,live',[ (True,False),(False,True)])
def test_R8_F8_never_terminalizes_paused_or_live(engine,paused,live):
    e=engine; w=arm(e,next_due_at=None,status='paused' if paused else 'active')
    e.store.add_occurrence(w.id,'old',state='skipped')
    if live: e.store.add_occurrence(w.id,'live')
    e.terminal_one_shot(w)
    assert e.store.get_wake(w.id).status==w.status


@pytest.mark.parametrize('anchor,expected,note', [('9:00 pm','21:00','Call mom'),('9 pm','21:00','Call mom'),
    ('9:00 PM','21:00','Call mom'),('9 am','09:00','Call mom'),('09:00','09:00','amend docs')])
def test_R8_F7_spaced_meridiem_not_note(runtime,anchor,expected,note):
    text=commands.handle('add every 1d at '+anchor+' '+note,runtime)
    assert not text.startswith('Error'),text
    w,=runtime.get_engine().store.list_wakes()
    assert (w.trigger_json.get('at'),w.note)==(expected,note)


@pytest.mark.parametrize('skip_quiet',[True,False])
def test_R8_F9_restart_scope_is_occurrence(engine,skip_quiet):
    e=engine; w=arm(e,'every',next_due_at=e.clock.now()+3600)
    e.tick(); e.clock.advance(3900); e.close()
    e2=Poller(e.ctx,e.store,e.policy,e.clock,session_key_fn=lambda:KEY,paused=lambda:False,jitter=lambda:0,compression_tip=lambda sid:sid)
    try:
        if skip_quiet: e2.store.set_meta('quiet:'+KEY,json.dumps({'until':e.clock.now()+60}))
        e2.tick()
        if not skip_quiet:
            assert 'after restart' in e2.ctx.calls[-1][0]
            return
        assert e2.store.list_occurrences(w.id)[0].state=='skipped'
        due=e2.store.get_wake(w.id).next_due_at; e2.clock.advance(due+1-e2.clock.now())
        session(e2,busy_since=e2.clock.now()); e2.tick()
        e2.clock.advance(120); session(e2,busy_since=None); e2.tick()
        why=next(line for line in e2.ctx.calls[-1][0].splitlines() if line.startswith('Why'))
        assert 'held by a busy chat' in why and 'after restart' not in why
    finally: e2.close()


def test_R8_F10_probe_phase_window_and_stale_reader(engine):
    e=engine; e.tick(); reader_store=Store(e.store.path,e.clock)
    reader=Poller(e.ctx,reader_store,e.policy,e.clock)
    sampled=[]; guard=threading.Lock()
    for _ in range(5): arm(e,'command',trigger_json={'command':['git','status']})
    def probe(w):
        with guard:
            e.clock.advance(12); sampled.append(reader.status())
        return {'fire':False}
    e.probe_runner=probe
    try:
        e.tick()
        assert len(sampled)==5 and all(x['state']=='alive' for x in sampled),sampled
        assert not reader_store.get_meta('probing_until')
        reader_store.set_meta('probing_since',str(e.clock.now()))
        reader_store.set_meta('probing_epoch',e.epoch)
        reader_store.set_meta('probing_until',str(e.clock.now()+70))
        e.clock.advance(71); assert reader.status()['state']=='stalled'
    finally: reader.close(); reader_store.close()


@pytest.mark.parametrize('interval,cap,expected',[(172800,None,172800),(86400,None,86400),(80000,300,80000)])
def test_R8_F11_slow_never_shortens_watcher(runtime,interval,cap,expected):
    e=runtime.get_engine(); w=arm(e,'command',status='paused',status_reason='governor:error',
        trigger_json={'command':['git','status'],'check_every':interval},pacing_max_s=cap)
    text=commands.handle('slow '+w.id,runtime); current=e.store.get_wake(w.id)
    assert current.trigger_json['check_every']==expected and current.next_due_at==e.clock.now()+expected
    assert str(expected) in text


@pytest.mark.parametrize('phase',['headers','chunk'])
def test_R8_F5_url_timeout_reaps_worker(monkeypatch,phase):
    def connect(sock,address):
        assert address[0]=='127.0.0.1'; return _CONNECT(sock,address)
    def dns(host,*args,**kwargs):
        assert host=='127.0.0.1'; return _DNS(host,*args,**kwargs)
    monkeypatch.setattr(socket.socket,'connect',connect)
    monkeypatch.setattr(socket,'getaddrinfo',dns)
    monkeypatch.setattr(socket,'create_connection',_CREATE)
    listener=socket.socket(); listener.bind(('127.0.0.1',0)); listener.listen(); stop=threading.Event()
    def serve():
        conn,_=listener.accept()
        with conn:
            conn.recv(4096)
            try:
                conn.sendall(b'HTTP/1.1 200 OK\r\nX-Slow: ' if phase=='headers' else b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n')
                while not stop.wait(.02): conn.sendall(b'0')
            except OSError: pass
    thread=threading.Thread(target=serve,daemon=True); thread.start()
    monkeypatch.setattr(probes,'URL_DEADLINE_S',.2)
    w=SimpleNamespace(kind='url',trigger_json={'url':f'http://127.0.0.1:{listener.getsockname()[1]}/'},probe_state_json={},id='x')
    try:
        assert probes.check(w,policy=Policy(url_allow_private=True))['error']=='TimeoutError'
        end=time.monotonic()+1
        while time.monotonic()<end and any(x.name=='pulse-url' for x in threading.enumerate()): time.sleep(.02)
        assert not any(x.name=='pulse-url' for x in threading.enumerate())
    finally: stop.set(); listener.close(); thread.join(1)


@pytest.mark.parametrize('quiet,failed,stop,held,late',[(True,False,False,300,0),(True,True,False,300,1300),(False,True,True,15,1280)])
def test_R8_F14_measured_holds_preserve_lateness(tmp_path,quiet,failed,stop,held,late):
    sc=load_scenarios('positive-followup')[0]
    sc['arms'][0]['command']='add in 5m Check the task.'
    sc['settings']={'quiet_hours':'12:00-18:00' if quiet else '', 'timezone':'UTC'}
    sc['epoch']=1791806400; sc['duration']=2100
    sc['timeline']=([{'t':600,'event':'command','text':'quiet off'}] if quiet else [])
    if failed: sc['timeline'] += [{'t':0,'event':'injector','mode':'return_false'},{'t':1000,'event':'injector','mode':'ok'}]
    if stop: sc['timeline'] += [{'t':1400,'event':'estop','on':True},{'t':1415,'event':'estop','on':False}]
    r=simulate(sc,tmp_path); turn,=r['turns']
    assert (turn['deferred_s'],turn['lateness_s'])==(held,late),turn
    assert turn['due_t']<=turn['gates_opened_t']<=turn['inject_t']


@pytest.mark.parametrize('name',['positive-followup','duplicates-59703'])
def test_R8_F15_retry_only_observable_failure(tmp_path,name):
    result=simulate(load_scenarios(name)[0],tmp_path,baseline=True)
    occurrences=[t['occurrence'] for t in result['turns']]
    if name=='positive-followup': assert len(occurrences)==len(set(occurrences))
    else:
        assert result['metrics']['duplicate_deliveries']>0
        assert not [t for t in result['turns'] if t['t']<360]


def test_R8_F15_resends_preserve_model_result_and_count_messages(tmp_path):
    result=simulate(load_scenarios('resend-85743')[0],tmp_path,baseline=True)
    occurrences=[t['occurrence'] for t in result['turns']]
    assert len(occurrences)==len(set(occurrences))
    assert result['metrics']['resent_messages']>=20
    assert result['metrics']['message_attempts']>result['metrics']['model_wakes']
    retries=[m for m in result['message_attempts'] if m['retry']]
    assert retries and all(m['text']=='Still waiting.' for m in retries)


def test_R8_F13_existing_module_timer_unchanged():
    import inspect
    import test_bench_mutants as mutants
    assert '<2' not in inspect.getsource(mutants.test_R6_E1_E2_E3_mutant_gate)
    assert '<60' in inspect.getsource(mutants.mutant_budget)
