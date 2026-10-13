"""Owner-decided U2 review regressions, using offline gateway fixtures."""
from dataclasses import asdict, replace
import json
import os
import sys
from types import SimpleNamespace as NS

import pytest

from test_engine import admit, fold, engine, arm, start, end, session, KEY, GatewayCtx
from heartbeat2 import governor
from heartbeat2.model import Session
from heartbeat2.poller import Poller
from heartbeat2.registration import Runtime, register


def inbound(mid='m1', *, bot=False, internal=False, chat='unit'):
    return NS(internal=internal, message_id=mid, text='owner reply',
              source=NS(platform=NS(value='telegram'), chat_type='dm', chat_id=chat, is_bot=bot))


def reply(e, **args):
    return e.tool.handle(dict(action='respond', outcome='needs_attention', notify=True, **args), session_id='sid')


def test_R2_G1_registered_contract_and_results(engine, monkeypatch):
    e=engine
    monkeypatch.setattr(Runtime, 'start', lambda self: None)
    r=register(e.ctx, clock=e.clock)
    r._engine=e
    try:
        spec=e.ctx.tools['pulse']
        schema=spec['schema']
        assert set(schema)=={'name','description','parameters'}
        assert schema['name']=='pulse'
        assert all(action in schema['description'] for action in schema['parameters']['properties']['action']['enum'])
        result=spec['handler']({'action':'create','note':'test','trigger':{'kind':'every','every':'2m'}},session_id='sid')
        assert isinstance(result,str)
        assert json.loads(result)['clamped']['every']
        assert json.loads(spec['handler']({'action':'create'},session_id='sid'))['error']
        r._engine=None
        monkeypatch.setattr(r,'get_engine',lambda: (_ for _ in ()).throw(ValueError('fixture')))
        assert json.loads(spec['handler']({'action':'list'}))['error']=='engine_unavailable'
    finally:
        r._engine=None
        r.close()


def test_R2_G1_real_Hermes_registry(engine, tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path/'hermes'))
    monkeypatch.syspath_prepend(os.environ.get('HERMES_SRC') or os.path.expanduser('~/.hermes/hermes-agent'))
    registry_module=pytest.importorskip('tools.registry')
    monkeypatch.setattr(Runtime,'start',lambda self: None)
    r=register(engine.ctx,clock=engine.clock)
    r._engine=engine
    try:
        reg=registry_module.ToolRegistry()
        reg.register(name='pulse',**engine.ctx.tools['pulse'])
        definition=reg.get_definitions({'pulse'},quiet=True)[0]['function']
        assert definition['parameters']['required']==['action']
        assert definition['description']
        result=json.loads(reg.dispatch('pulse',{'action':'create','note':'real registry',
                          'trigger':{'kind':'every','every':'2m'}},session_id='sid'))
        assert result['ok'] and result['clamped']['every']
        assert json.loads(reg.dispatch('pulse',{'action':'wat'},session_id='sid'))['error']=='unknown_action'
    finally:
        r._engine=None
        r.close()


@pytest.mark.parametrize('child_signal',['platform','context'])
def test_R2_G2_child_never_touches_parent(engine, monkeypatch, child_signal):
    e=engine
    e.intake.pre_llm_call(session_id='sid',turn_id='parent',user_message='hello',platform='telegram')
    w=arm(e,'every')
    governor.pause(e,w,'noop')
    e.notice(KEY,'owner notice')
    before=asdict(e.store.get_session(KEY))
    if child_signal=='context':
        monkeypatch.setitem(sys.modules,'agent.delegation_context',NS(is_delegated_child_context=lambda: True))
    assert e.intake.pre_llm_call(session_id='child',turn_id='c1',user_message='work',
                               platform='subagent' if child_signal=='platform' else 'telegram',parent_session_id='sid') is None
    e.intake.post_api_request(session_id='child',turn_id='c1',usage={'input_tokens':1})
    e.intake.on_session_end(session_id='child',turn_id='c1',completed=True)
    assert asdict(e.store.get_session(KEY))==before
    assert 'child' not in e.intake.bindings and 'child' not in e.intake.turns
    assert e.store.get_meta('card:'+w.id)=='noop'
    assert 'owner notice' in e.store.get_meta('notice:'+KEY)


def test_R2_G2_parent_id_alone_is_main_turn(engine):
    engine.clock.advance(1)
    engine.intake.pre_gateway_dispatch(event=inbound())
    engine.intake.pre_llm_call(session_id='branch',turn_id='main',user_message='hello',
                             platform='telegram',parent_session_id='sid')
    assert engine.store.get_session(KEY).current_session_id=='branch'
    assert engine.store.get_session(KEY).last_human_at==engine.clock.now()


@pytest.mark.parametrize('guard',['untrusted','depth','child','babysit','owner'])
def test_R2_G2_child_inherits_wake_limits(engine, guard):
    e=engine
    w=arm(e,'every',depth=2 if guard=='depth' else 0)
    if guard=='untrusted':
        e.store.add_occurrence(w.id,'e:test',payload=[{'message':'untrusted'}])
        e.acquire(); e.dispatch(e.store.get_occurrence(w.id,'e:test'))
    else: e.tick()
    start(e)
    if guard=='child':
        assert e.tool.handle({'action':'create','note':'child','trigger':{'kind':'at','at':'+1h'}},session_id='sid')['ok']
    if guard=='owner':
        owner=arm(e,'every',creator='user',next_due_at=e.clock.now()+3600)
        args={'action':'pause','id':owner.id}
        expected='owner_item'
    else:
        trigger={'kind':'at','at':'+1h'} if guard in {'depth','child'} else {'kind':'every','every':'1h'}
        args={'action':'create','note':'child','trigger':trigger}
        if guard=='babysit': args['babysit']=True
        expected={'untrusted':'untrusted_rearm','depth':'rearm_depth','child':'rearm_child','babysit':'invalid_request'}[guard]
    assert e.tool.handle(args,session_id='delegate-child')['error']==expected


def test_R2_G2_rotation_follows_all_hooks(engine):
    e=engine
    w=arm(e,'every')
    e.tick(); start(e)
    e.compression_tip=lambda sid: 'tip' if sid=='sid' else sid
    result=e.tool.handle({'action':'respond','outcome':'progress','notify':False,'scratch':'saved'},session_id='tip')
    assert result['ok']
    e.intake.post_api_request(session_id='tip',turn_id='turn',usage={'input_tokens':4,'output_tokens':2})
    assert e.intake.transform_llm_output(response_text='chatty',session_id='tip',turn_id='turn')=='[SILENT]'
    e.intake.on_session_end(session_id='tip',turn_id='turn',completed=True)
    o=e.store.list_occurrences(w.id)[0]
    assert (o.state,o.tokens_in,o.tokens_out)==('done',4,2)
    assert e.store.get_session(KEY).busy_since is None
    assert e.store.get_wake(w.id).armed_session_id=='tip'
    assert e.store.get_wake(w.id).scratch=='saved'


def test_R2_G3_running_reply_counts_once_and_is_never_silent(engine, monkeypatch):
    e=engine
    a=arm(e); e.tick(); start(e); end(e,'progress',True,'first alert')
    e.clock.advance(601)
    b=arm(e,'every',cancel_on_reply=True); e.tick(); start(e)
    assert e.tool.handle({'action':'respond','outcome':'no_change','notify':False},session_id='sid')['ok']
    before=e.store.get_session(KEY)
    e.clock.advance(1)
    assert e.intake.pre_gateway_dispatch(event=inbound()) is None
    after=e.store.get_session(KEY)
    assert after.last_human_at>before.last_human_at and after.episode_id!=before.episode_id
    assert after.unanswered_count==0 and e.store.get_wake(a.id).engaged==1
    assert e.store.get_wake(b.id).status=='cancelled'
    fold(e)
    assert e.intake.transform_llm_output(response_text='answer to owner',session_id='sid',turn_id='turn') is None
    e.intake.on_session_end(session_id='sid',turn_id='turn',completed=True)
    assert not e.store.list_occurrences(b.id)[0].delivered
    assert e.store.get_session(KEY).unanswered_count==0
    monkeypatch.setattr('heartbeat2.tool.session_env',lambda name: 'm1' if name=='HERMES_SESSION_MESSAGE_ID' else '')
    e.intake.pre_llm_call(session_id='sid',turn_id='queued',user_message='owner reply',platform='telegram',
        conversation_history=[{'role':'user','content':'owner reply','platform_message_id':'m1'}])
    assert e.store.get_session(KEY).episode_id==after.episode_id
    assert len([ev for ev in e.store.pending_events() if ev['kind']=='human_message'])==1


def test_R2_G3_queued_without_admission_and_owner_interrupt(engine, monkeypatch):
    e=engine
    w=arm(e); e.tick(); start(e)
    e.clock.advance(1)
    e.intake.on_session_end(session_id='sid',turn_id='turn',interrupted=True,turn_exit_reason='interrupted_by_user')
    assert e.store.get_wake(w.id).error_streak==0
    session(e,unanswered_count=2)
    monkeypatch.setattr('heartbeat2.tool.session_env',lambda name: 'queued' if name=='HERMES_SESSION_MESSAGE_ID' else '')
    row={'role':'user','content':'queued reply','platform_message_id':'queued'}
    e.intake.pre_llm_call(session_id='sid',turn_id='q1',user_message='queued reply',platform='telegram',conversation_history=[row])
    assert e.store.get_session(KEY).unanswered_count==0
    episode=e.store.get_session(KEY).episode_id
    e.intake.pre_llm_call(session_id='sid',turn_id='q1',user_message='queued reply',platform='telegram',conversation_history=[row])
    assert e.store.get_session(KEY).episode_id==episode


def test_R2_G3_ambiguous_chat_does_not_count(engine):
    e=engine
    e.store.upsert_session(Session('other',platform='telegram',chat_id='unit',chat_type='dm'))
    before=e.store.get_session(KEY)
    e.clock.advance(1)
    assert e.intake.pre_gateway_dispatch(event=inbound()) is None
    assert e.store.get_session(KEY)==before


@pytest.mark.parametrize('action',['cancel','pause'])
@pytest.mark.parametrize('before_respond',[False,True])
def test_R2_G4_self_deescalation_keeps_verdict(engine, action, before_respond):
    e=engine
    w=arm(e,'every'); e.tick(); start(e)
    if not before_respond: assert reply(e,summary='completed')['ok']
    assert e.tool.handle({'action':action,'id':w.id},session_id='sid')['ok']
    if before_respond: assert reply(e,summary='completed')['ok']
    assert e.intake.transform_llm_output(response_text='required alert',session_id='sid',turn_id='turn') is None
    e.intake.on_session_end(session_id='sid',turn_id='turn',completed=True)
    assert e.store.list_occurrences(w.id)[0].delivered


def test_R2_G5_new_turn_finishes_failed_flight(engine):
    e=engine
    w=arm(e); e.tick(); start(e)
    e.intake.pre_llm_call(session_id='sid',turn_id='replacement',user_message='new turn',platform='telegram')
    o=e.store.list_occurrences(w.id)[0]
    assert (o.state,o.force_silent,o.error)==('interrupted',True,'failed')
    assert e.store.get_wake(w.id).error_streak==1


@pytest.mark.parametrize('wake',[False,True])
def test_R2_G5_liveness_TTL_and_API_activity(engine, monkeypatch, wake):
    e=engine
    monkeypatch.setenv('HERMES_AGENT_TIMEOUT','800')
    e.acquire()
    if wake:
        w=arm(e); e.tick(); start(e)
    else: e.intake.pre_llm_call(session_id='sid',turn_id='turn',user_message='hello',platform='telegram')
    e.clock.advance(7900)
    e.intake.post_api_request(session_id='sid',turn_id='turn')
    e.clock.advance(200)
    e.acks()
    assert e.store.get_session(KEY).busy_since is not None
    e.clock.advance(8001)
    e.acks()
    assert e.store.get_session(KEY).busy_since is None
    if wake:
        o=e.store.list_occurrences(w.id)[0]
        assert (o.state,o.error,o.force_silent)==('interrupted','no_session_end',True)
        assert not e.store.list_occurrences(w.id,state='pending')


def test_R2_G6_live_reconcile_preserves_started_and_busy(engine):
    e=engine
    w=arm(e); e.tick(); start(e); reply(e,summary='alert')
    e.release()
    replacement=Poller(e.ctx,e.store,e.policy,e.clock,paused=lambda:False)
    replacement.process_start=False
    try:
        assert replacement.acquire()
        assert e.store.list_occurrences(w.id)[0].state=='started'
        assert e.store.get_session(KEY).busy_since is not None
        assert e.intake.transform_llm_output(response_text='alert',session_id='sid') is None
    finally: replacement.close()


def test_R2_G6_wall_jump_alone_does_not_restart(engine):
    e=engine
    e.tick()
    e.store.set_meta('last_tick_monotonic',str(e.clock.monotonic()))
    restarts=[]
    e.restart=lambda:restarts.append(True)
    e.clock.wall+=400
    e.intake.pre_llm_call(session_id='sid',turn_id='human',user_message='hello',platform='telegram')
    assert restarts==[]


@pytest.mark.parametrize('action',['create','update'])
def test_R2_G7_invalid_duration_refused(engine, action):
    e=engine
    args={'action':action,'note':'invalid','trigger':{'kind':'owner_silent','after':'1 month'}}
    if action=='update': args['id']=arm(e).id
    result=e.tool.handle(args,session_id='sid')
    assert result['error']=='invalid_request'
    assert not any(w.trigger_json.get('after')=='1 day' for w in e.store.list_wakes())


@pytest.mark.parametrize('kind',['owner_silent','file','every'])
def test_R2_G7_bad_wake_isolated(engine, kind):
    e=engine
    bad=arm(e,kind,trigger_json={'after':'bad','check_every':'bad','at':'bad'})
    good=arm(e)
    e.tick()
    assert len(e.ctx.calls)==1
    assert e.store.list_occurrences(good.id)[0].state=='claimed'
    assert e.store.get_wake(bad.id).error_streak==1


def test_R2_G7_lowered_lifetime_expires_one_wake(engine):
    e=engine
    e.clock.wall-=3*86400
    bad=arm(e,'every',expires_at=e.clock.now()+14*86400)
    e.clock.wall+=3*86400
    good=arm(e)
    e.policy=replace(e.policy,max_lifetime_days=2)
    e.tick()
    assert len(e.ctx.calls)==1 and e.store.list_occurrences(good.id)[0].state=='claimed'
    expired=e.store.get_wake(bad.id)
    assert expired.status=='expired' and expired.status_reason


def test_R2_G7_bootstrap_death_before_first_tick_recovers(tmp_path, clock, monkeypatch):
    ctx=GatewayCtx(tmp_path)
    r=Runtime(ctx,clock=clock,bootstrap_interval=.01,tick_interval=.01)
    e=r.get_engine()
    e.store.upsert_session(Session(KEY,'telegram','dm','unit','sid'))
    arm(e)
    original=Poller.acquire
    monkeypatch.setattr(Poller,'acquire',lambda self: (_ for _ in ()).throw(ValueError('fixture')))
    try:
        r.start(); r._thread.join(timeout=1)
        assert not r._thread.is_alive()
        assert e.status()['reason']=='ValueError'
        monkeypatch.setattr(Poller,'acquire',original)
        r.hook('pre_llm_call',session_id='sid',turn_id='human',user_message='hi',platform='telegram')
        assert r._thread.is_alive()
        assert ctx.injected.wait(timeout=1)
    finally: r.close()


@pytest.mark.parametrize('status',['done','cancelled','orphaned','expired','failed'])
def test_R2_G8_terminal_cannot_pause_or_revive(engine, status):
    w=arm(engine,'every',status=status)
    result=engine.tool.handle({'action':'pause','id':w.id},session_id='sid')
    assert result['error']=='not_pausable'
    governor.pause(engine,w,'noop')
    assert engine.store.get_wake(w.id).status==status


def test_R2_G8_resume_preserves_governor_state(engine):
    e=engine
    w=arm(e,'every',status='paused',noop_streak=4,ignored_streak=2,error_streak=1,eff_interval_s=14400,
          trigger_json={'kind':'every','every':'1h'})
    e.store.set_meta('ratio_since:'+w.id,'123')
    assert e.tool.handle({'action':'resume','id':w.id},session_id='sid')['ok']
    resumed=e.store.get_wake(w.id)
    assert (resumed.noop_streak,resumed.ignored_streak,resumed.error_streak,resumed.eff_interval_s)==(4,2,1,14400)
    assert e.store.get_meta('ratio_since:'+w.id)=='123'
    assert e.tool.handle({'action':'resume','id':w.id},session_id='sid')['error']=='not_resumable'


def test_R2_G8_governor_resume_needs_owner(engine):
    e=engine
    w=arm(e,'every',status='paused',status_reason='governor:noop',noop_streak=6)
    arm(e); e.tick(); start(e)
    assert e.tool.handle({'action':'resume','id':w.id},session_id='sid')['error']=='needs_owner'
    assert e.store.get_wake(w.id).noop_streak==6


@pytest.mark.parametrize('answer',['keep','slow'])
def test_R2_G8_owner_answer_ratio_and_backoff(engine, answer):
    e=engine
    e.policy=replace(e.policy,noop_pause_after=100,ignored_pause_after=100,noop_slowdown_after=100)
    w=arm(e,'every',noop_streak=12,ignored_streak=2,error_streak=1)
    for i in range(12):
        key=f't:history{i}'
        e.store.add_occurrence(w.id,key); e.store.claim(w.id,key,epoch=e.epoch,pid=1)
        e.store.start(w.id,key,attempt=1); e.store.finish(w.id,key,attempt=1,outcome='no_change')
    governor.pause(e,w,'ratio')
    before=e.store.get_wake(w.id)
    governor.owner_answer(e,before,answer)
    after=e.store.get_wake(w.id)
    if answer=='keep':
        assert (after.noop_streak,after.ignored_streak,after.error_streak)==(0,0,0)
        governor.apply(e,after)
        assert e.store.get_wake(w.id).status=='active'
        # The ratio window starts again, rather than disabling the rule.
        for i in range(12):
            e.clock.advance(1)
            key=f't:new{i}'
            e.store.add_occurrence(w.id,key); e.store.claim(w.id,key,epoch=e.epoch,pid=1)
            e.store.start(w.id,key,attempt=1); e.store.finish(w.id,key,attempt=1,outcome='no_change')
            governor.apply(e,e.store.get_wake(w.id))
            assert e.store.get_wake(w.id).status==('paused' if i==11 else 'active')
    else:
        assert (after.noop_streak,after.ignored_streak,after.error_streak)==(12,2,1)
        assert after.eff_interval_s>before.eff_interval_s
        assert not e.store.get_meta('ratio_since:'+w.id)


def test_R2_G9_long_deferral_coalesces(engine):
    e=engine
    w=arm(e,'every')
    e.acquire()
    session(e,paused_until=e.clock.now()+12*3600)
    for _ in range(12):
        e.tick(); e.clock.advance(3600)
    assert len(e.store.list_occurrences(w.id,state='pending'))==1
    e.tick(); start(e); end(e,'progress',True,'one after deferral')
    e.clock.advance(601); e.tick()
    assert len(e.ctx.calls)==1


@pytest.mark.parametrize('change',['off','threshold'])
def test_R2_G10_checkin_current_settings(engine, change):
    e=engine
    e.policy=replace(e.policy,owner_silent_checkin=True)
    e.tick()
    e.policy=replace(e.policy,owner_silent_checkin=False) if change=='off' else replace(e.policy,checkin_after_hours=72)
    e.clock.advance(25*3600); e.tick()
    assert not e.ctx.calls
    if change=='threshold':
        e.clock.advance(48*3600); e.tick()
        assert len(e.ctx.calls)==1


@pytest.mark.parametrize('transition',['done','new'])
def test_R2_G10_checkin_survives_episode_and_new(engine, transition):
    e=engine
    e.policy=replace(e.policy,owner_silent_checkin=True)
    e.tick()
    if transition=='done':
        e.clock.advance(25*3600); e.tick(); start(e); end(e,'done',True,'check in')
        expected=2
    else:
        e.intake.on_session_reset(old_session_id='sid',new_session_id='sid-new')
        assert not e.store.get_meta('notice:'+KEY)
        expected=1
    admit(e,'new-human')
    e.clock.advance(5*86400); e.tick()
    assert len(e.ctx.calls)==expected
    sid='sid-new' if transition=='new' else 'sid'
    start(e,sid=sid)
    assert e.tool.handle({'action':'respond','outcome':'no_change','notify':False},session_id=sid)['ok']
    e.intake.on_session_end(session_id=sid,turn_id='turn',completed=True)
    e.clock.advance(86400); e.tick()
    assert len(e.ctx.calls)==expected


@pytest.mark.parametrize('summary',[None,'','  '])
def test_R2_G11_empty_summary_never_dedupes(engine, summary):
    e=engine
    w=arm(e,'every')
    for n in range(2):
        e.tick(); start(e)
        assert reply(e,**({} if summary is None else {'summary':summary}))['ok']
        assert e.intake.transform_llm_output(response_text=f'alert {n}',session_id='sid') is None
        e.intake.on_session_end(session_id='sid',turn_id='turn',completed=True)
        e.clock.advance(3600)
    assert e.store.get_wake(w.id).delivered==2


@pytest.mark.parametrize('internal',[False,True])
def test_R2_G12_real_bot_source_never_counts(engine, internal):
    e=engine
    w=arm(e,cancel_on_reply=True)
    before=e.store.get_session(KEY)
    e.clock.advance(1)
    e.intake.pre_gateway_dispatch(event=inbound(bot=not internal,internal=internal))
    assert e.store.get_session(KEY)==before
    assert e.store.get_wake(w.id).status=='active'


def test_R2_G12_scratch_clamp_visible(engine):
    e=engine
    w=arm(e); e.tick(); start(e)
    result=reply(e,scratch='x'*5000)
    assert result['clamped']['scratch']
    e.intake.on_session_end(session_id='sid',turn_id='turn',completed=True)
    assert len(e.store.get_wake(w.id).scratch)==4000


def test_R2_G7_bad_probe_result_isolated(engine):
    e=engine
    bad=arm(e,'file')
    good=arm(e)
    e.probe_runner=lambda w: {'fire':True,'state':object()}
    e.tick()
    assert len(e.ctx.calls)==1
    assert e.store.list_occurrences(good.id)[0].state=='claimed'
    assert e.store.get_wake(bad.id).error_streak==1
