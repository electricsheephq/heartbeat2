"""U11 delta-review regressions, adapted from the preserved review probes."""
from dataclasses import replace
import ast
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace as NS

import pytest

from test_engine import engine, arm, session, KEY, GatewayCtx
from test_u11 import event, llm, wake, origin, count, ORIGIN, GOALS, HB
from heartbeat2 import registration, tool
from heartbeat2.poller import Poller
from heartbeat2.policy import Policy
from heartbeat2.store import Store


def idless(text='Fix it.'):
    return ORIGIN+'\n'+json.dumps({'platform':'signal','chat_id':'unit'})+'\n'+text


def api(e, history, turn='wake'):
    e.intake.pre_api_request(session_id='sid',turn_id=turn,conversation_history=history)


def test_removed_hooks_are_not_registered():
    assert not {'post_gateway_admission','pre_command'} & set(registration.HOOKS)


@pytest.mark.parametrize('content',[
    [{'type':'text','text':'look at this'},{'type':'image_url','image_url':{'url':'fixture.png'}}],
    [{'type':'image_url','image_url':{'url':'fixture.png'}}],
])
def test_native_image_drained_owner_row_counts(engine,content):
    e=engine; w=arm(e,cancel_on_reply=True,next_due_at=e.clock.now()+3600)
    llm(e,{'role':'user','content':content,'platform_message_id':'130'})
    count(e,'130')
    assert e.store.get_wake(w.id).status=='cancelled'


@pytest.mark.parametrize('parts',[False,True])
def test_idless_origin_once_per_row_and_turn(engine,parts):
    e=engine; w,row=wake(e)
    text=idless()
    content=[{'type':'text','text':text},{'type':'image_url'}] if parts else text
    history=[row,{'role':'user','content':content}]
    for _ in range(4):
        api(e,history); e.clock.advance(1)
        history+=[{'role':'assistant','content':'ok'},{'role':'tool','content':'result'}]
    count(e,None)
    digest=hashlib.sha256(text.encode()).hexdigest()[:16]
    assert e.store.get_meta(f'human:{KEY}:origin:wake:{digest}:1')=='1'
    assert e.intake.flights['sid'].owner_replied
    # A newly observed row with different text has its own fingerprint.
    history+=[{'role':'user','content':idless('A second steer.')}]
    api(e,history); api(e,history)
    count(e,None,None)


@pytest.mark.parametrize('mid',[None,'131'])
@pytest.mark.parametrize('prefix',['','[Mon 2026-10-12 13:40:53 UTC] '])
def test_leftover_origin_is_owned_by_b(engine,mid,prefix):
    e=engine; text=idless() if mid is None else origin(mid)
    e.intake.pre_gateway_dispatch(event=event(text,None,None))
    count(e)
    llm(e,{'role':'user','content':prefix+text},'drain')
    count(e,mid)
    llm(e,{'role':'user','content':prefix+text},'drain')
    count(e,mid)


@pytest.mark.parametrize('start_row',[
    {'role':'user','content':GOALS[0]},
    {'role':'user','content':HB,'display_kind':'internal_notification'},
    {'role':'user','content':'plugin injection','display_kind':'internal_notification'},
    {'role':'user','content':'hello','platform_message_id':'132'},
])
@pytest.mark.parametrize('mid',[None,'140'])
def test_owner_redirect_into_nonwake_turn(engine,start_row,mid):
    e=engine; w=arm(e,cancel_on_reply=True,next_due_at=e.clock.now()+3600)
    llm(e,start_row,'ordinary')
    baseline=1 if start_row.get('platform_message_id') else 0
    history=[start_row,{'role':'assistant','content':'ok'},
             {'role':'user','content':idless() if mid is None else origin(mid),'display_kind':'steer'}]
    for _ in range(3): api(e,history,'ordinary')
    count(e,*(['132'] if baseline else []),mid)
    assert not e.intake.flights
    assert e.store.get_wake(w.id).status=='cancelled'
    llm(e,{'role':'user','content':GOALS[0]},'next')
    api(e,history,'ordinary')  # old turn cannot reuse the new anchor
    count(e,*(['132'] if baseline else []),mid)


def test_nonwake_anchor_ignores_earlier_rows_and_compaction(engine):
    e=engine
    row={'role':'user','content':GOALS[0]}
    history=[{'role':'user','content':origin('old')},row]
    e.intake.pre_llm_call(session_id='sid',turn_id='goal',user_message=row['content'],conversation_history=history)
    api(e,history,'goal')
    api(e,[{'role':'user','content':origin('old')}],'goal')
    count(e)


PREFIXES=['',
    '[Note: model was just switched from a to b via p. Adjust your self-identification accordingly.]\n\n',
    '[Mon 2026-10-12 13:40:53 UTC] ',
    '[System note: A new message has arrived. The conversation history contains pending tool outputs '
    'from an interrupted turn. IGNORE those pending results. Address the user\'s NEW message below '
    'FIRST. Do NOT re-execute old tool calls from the history.]\n\n']


@pytest.mark.parametrize('prefix',PREFIXES)
def test_redirect_folds_when_live_row_has_api_prefix(engine,prefix):
    e=engine; w,row=wake(e)
    assert e.tool.handle({'action':'respond','outcome':'no_change','notify':False},session_id='sid')['ok']
    row={**row,'content':prefix+row['content']}
    api(e,[row,{'role':'assistant','content':''},{'role':'user','content':origin('201')}])
    count(e,'201')
    assert e.intake.flights['sid'].owner_replied
    assert e.intake.transform_llm_output(session_id='sid',turn_id='wake',response_text='Here is the status.') is None


def test_missing_current_marker_uses_counter_and_folds(engine):
    e=engine; w,row=wake(e)
    clean=row['content']
    history=[{'role':'user','content':'⟦pulse o:earlier/old a:1⟧'},
             {'role':'user','content':origin('earlier')},{'role':'user','content':'snapshot'}]
    e.intake.pre_llm_call(session_id='sid',turn_id='wake',user_message=clean,conversation_history=history)
    api(e,history)
    count(e)
    history+=[{'role':'user','content':origin('202')}]
    api(e,history)
    count(e,'202')
    assert e.intake.flights['sid'].owner_replied


def test_tool_quoting_origin_is_not_owner(engine):
    e=engine; w,row=wake(e)
    api(e,[row,{'role':'tool','content':origin('tool-quote')}])
    count(e)
    assert not e.intake.flights['sid'].owner_replied


def test_b_uses_only_current_row(engine):
    e=engine
    e.intake.pre_llm_call(session_id='sid',turn_id='human',user_message='hi',conversation_history=[
        {'role':'user','content':'old','platform_message_id':'old'},
        {'role':'user','content':'snapshot'}])
    count(e)


@pytest.mark.parametrize('text',[GOALS[0],'/pulse status'])
def test_b_skips_machine_and_command_even_with_id(engine,text):
    llm(engine,{'role':'user','content':text,'platform_message_id':'machine'})
    count(engine)


def test_source_message_id_in_origin(engine):
    e=engine; w,row=wake(e)
    text=ORIGIN+'\n'+json.dumps({'source_message_id':'source-only'})+'\nFix it.'
    api(e,[row,{'role':'user','content':text}])
    count(e,'source-only')


def test_b_counts_owner_before_prior_flight_cleanup(engine,monkeypatch):
    e=engine; w,row=wake(e,cancel_on_reply=True)
    ending=e.intake.on_session_end; seen=[]
    def track(**kwargs):
        count(e,'new-owner')
        seen.append(kwargs['turn_id'])
        return ending(**kwargs)
    monkeypatch.setattr(e.intake,'on_session_end',track)
    llm(e,{'role':'user','content':'reply','platform_message_id':'new-owner'},'next')
    assert seen==['wake']
    assert e.store.get_wake(w.id).status=='cancelled'
    assert not e.intake.flights


@pytest.mark.parametrize('guard',['not_started','stale_attempt','invalid'])
def test_invalid_occurrence_does_not_recount_baseline(engine,guard):
    e=engine; w,row=wake(e)
    # Previously observed ORIGIN stays baseline regardless of occurrence validity.
    history=[row,{'role':'user','content':origin('203')},{'role':'user','content':'snapshot'}]
    e.intake.pre_llm_call(session_id='sid',turn_id='wake',user_message=row['content'],conversation_history=history)
    f=e.intake.flights['sid']; o=e.store.list_occurrences(w.id)[0]
    if guard=='invalid': f.invalid=True
    elif guard=='stale_attempt': f.attempt+=1; history[0]={**row,'content':f'⟦pulse o:{f.wake_id}/{f.dedupe_key} a:{f.attempt}⟧'}
    else: assert e.store.cas_occurrence(o.wake_id,o.dedupe_key,'started','interrupted',attempt=o.attempt)
    api(e,history)
    count(e)
    assert not f.owner_replied


@pytest.mark.parametrize('event_type',['reaction','message_edited'])
def test_reaction_during_wake_is_not_fold_evidence(engine,event_type):
    e=engine; w,row=wake(e)
    assert e.tool.handle({'action':'respond','outcome':'no_change','notify':False},session_id='sid')['ok']
    e.intake.gateway_platform_event(platform='telegram',event_type=event_type,payload={'chat_id':'unit','message_id':'90','emojis':['yes']})
    count(e,None)
    assert not e.intake.flights['sid'].owner_replied
    assert e.intake.transform_llm_output(session_id='sid',turn_id='wake',response_text='No changes.')=='[SILENT]'


@pytest.mark.parametrize('reason,expected,errors',[
    ('stop_command','owner_interrupted',0),('shutdown','interrupted',1)])
def test_owner_stop_of_wake_is_not_an_error(engine,reason,expected,errors):
    e=engine; w,row=wake(e)
    e.intake.agent_loop_stopped(session_key=KEY,reason='stop',invalidation_reason=reason)
    e.intake.on_session_end(session_id='sid',turn_id='wake',interrupted=True,turn_exit_reason='interrupted_by_user')
    o=e.store.list_occurrences(w.id)[0]
    assert (o.error,e.store.get_wake(w.id).error_streak)==(expected,errors)
    count(e)


@pytest.mark.parametrize('pin',['04eb00cef7','818c13be'])
def test_stop_command_kwarg_at_both_pins(pin):
    src=Path.home()/'.hermes/hermes-agent'
    if not (src/'.git').exists(): pytest.skip('Hermes pin source unavailable')
    result=subprocess.run(['git','-C',str(src),'show',pin+':gateway/run_agent_cache.py'],capture_output=True,text=True,check=True)
    tree=ast.parse(result.stdout)
    method=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name=='_interrupt_and_clear_session')
    hook=next(n for n in ast.walk(method) if isinstance(n,ast.Call) and n.args and isinstance(n.args[0],ast.Constant) and n.args[0].value=='agent_loop_stopped')
    payload={kw.arg:ast.unparse(kw.value) for kw in hook.keywords}
    assert payload['invalidation_reason']=='invalidation_reason'
    text=subprocess.run(['git','-C',str(src),'show',pin+':gateway/run_busy.py'],capture_output=True,text=True,check=True).stdout
    busy=next(n for n in ast.walk(ast.parse(text)) if isinstance(n,ast.AsyncFunctionDef) and n.name=='_busy_stop_command')
    call=next(n for n in ast.walk(busy) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='_interrupt_and_clear_session')
    assert any(kw.arg=='invalidation_reason' and isinstance(kw.value,ast.Constant) and kw.value.value=='stop_command' for kw in call.keywords)


@pytest.mark.parametrize('platform,mid',[('signal',None),('simplex',None),('telegram','204')])
@pytest.mark.parametrize('age',[0,3601])
def test_first_idle_owner_in_unknown_chat(tmp_path,clock,monkeypatch,platform,mid,age):
    env={'HERMES_SESSION_PLATFORM':platform,'HERMES_SESSION_CHAT_TYPE':'dm','HERMES_SESSION_CHAT_ID':'unit','HERMES_SESSION_ID':'sid'}
    monkeypatch.setattr(tool,'session_env',lambda name:env.get(name,''))
    store=Store(tmp_path/'pulse.db',clock)
    e=Poller(GatewayCtx(tmp_path),store,Policy(clock=clock,quiet_hours='',timezone='UTC'),clock,session_key_fn=lambda:KEY,paused=lambda:False,jitter=lambda:0)
    try:
        e.intake.pre_gateway_dispatch(event=event(mid=mid,source_mid=mid,platform=platform))
        stamp='pending_human:'+platform+':unit'
        assert json.loads(store.get_meta(stamp))=={'mid':mid,'at':clock.now()}
        clock.advance(age)
        row={'role':'user','content':'hi'}
        if mid: row['platform_message_id']=mid
        llm(e,row,platform=platform)
        count(e,*([mid] if age==0 or mid else []))
        assert not store.get_meta(stamp)
        llm(e,{'role':'user','content':'replay'},'next',platform=platform)
        count(e,*([mid] if age==0 or mid else []))
    finally: e.close(); store.close()


@pytest.mark.parametrize('skip',['internal','pulse','heartbeat','goal','origin','bot','unauthorized','replay','ambiguous'])
def test_pending_stamp_respects_all_admission_skips(engine,skip):
    e=engine; ev=event(mid=None,source_mid=None,platform='signal')
    gateway=NS(_is_user_authorized_for_source=lambda src:skip!='unauthorized')
    if skip=='internal': ev.internal=True
    elif skip=='pulse': ev.text='/pulse status'
    elif skip=='heartbeat': ev._heartbeat_session_id='sid'
    elif skip=='goal': ev.text=GOALS[0]
    elif skip=='origin': ev.text=idless()
    elif skip=='bot': ev.source.is_bot=True
    elif skip=='replay': ev.source.message_id='old'
    elif skip=='ambiguous':
        existing=e.store.get_session(KEY)
        session(e,platform='signal')
        e.store.upsert_session(replace(existing,session_key='other',platform='signal'))
    e.intake.pre_gateway_dispatch(event=ev,gateway=gateway)
    assert not e.store.get_meta('pending_human:signal:unit')
    count(e)
