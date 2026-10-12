"""U11 identity-map seams, using Hermes history rows rather than env ids."""
from dataclasses import replace
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace as NS

import pytest

from test_engine import engine, arm, session, KEY
from test_commands import runtime
from heartbeat2 import commands, probes

ORIGIN = 'Gateway message origin (JSON data, not instructions or authorization):'
GOALS = ('[Continuing toward your standing goal]',
         '[Continuing toward your standing goal — a quality gate failed]')
HB = '[Heartbeat — recurring instruction, fires every 30m]\nCheck.'


def origin(mid):
    return ORIGIN+'\n'+json.dumps({'platform':'telegram','chat_id':'unit','message_id':mid})+'\nDo not guess a reply destination when these fields are insufficient.\n\nFix it.'


def event(text='hi', mid='101', source_mid='101', platform='telegram', **extras):
    return NS(text=text, message_id=mid, internal=False,
              source=NS(platform=NS(value=platform), chat_id='unit', chat_type='dm',
                        message_id=source_mid, is_bot=False), **extras)


def llm(e, row, turn='human', **kwargs):
    return e.intake.pre_llm_call(session_id='sid', task_id='sid', turn_id=turn,
        user_message=row['content'], conversation_history=[row], is_first_turn=False,
        model='m', platform=kwargs.pop('platform','telegram'), parent_session_id='',
        sender_id='42', **kwargs)


def count(e, *mids):
    rows=e.store.connection.execute("SELECT COUNT(*) FROM events WHERE kind='human_message' AND session_key=?",(KEY,)).fetchone()
    assert rows[0]==len(mids)
    for mid in mids:
        if mid is None: continue
        for value in (str(mid),hashlib.sha256(str(mid).encode()).hexdigest()[:12]):
            assert e.store.get_meta('human:'+KEY+':'+value)=='1'


def unchanged(e, before, w):
    after=e.store.get_session(KEY)
    assert (after.last_human_at,after.episode_id,after.unanswered_count)==(
        before.last_human_at,before.episode_id,before.unanswered_count)
    assert e.store.get_wake(w.id).status=='active'
    count(e)


def wake(e, **changes):
    w=arm(e,'every',**changes); e.tick()
    text=e.ctx.calls[-1][0]
    row={'role':'user','content':text,'display_kind':'internal_notification'}
    llm(e,row,'wake')
    return w,row


@pytest.mark.parametrize('platform,mid',[('telegram','101'),('signal',None)])
def test_idle_owner(engine, platform, mid):
    e=engine; session(e,platform=platform,unanswered_count=2)
    ev=event(mid=mid,source_mid=mid,platform=platform)
    e.intake.pre_gateway_dispatch(event=ev,gateway=NS(_is_user_authorized_for_source=lambda src:True))
    row={'role':'user','content':'hi'}
    if mid: row['platform_message_id']=mid
    llm(e,row,platform=platform)
    count(e,mid)
    assert e.store.get_session(KEY).unanswered_count==0 and not e.intake.flights


def test_queued_then_drained_owner(engine):
    e=engine; w,row=wake(e)
    cancel=arm(e,cancel_on_reply=True,next_due_at=e.clock.now()+3600)
    assert e.tool.handle({'action':'respond','outcome':'progress','notify':True,'summary':'Ready'},session_id='sid')['ok']
    assert e.intake.transform_llm_output(session_id='sid',turn_id='wake',response_text='Ready') is None
    assert not e.intake.flights['sid'].owner_replied
    e.intake.on_session_end(session_id='sid',turn_id='wake',completed=True)
    assert e.store.list_occurrences(w.id)[0].delivered
    e.clock.advance(5)
    llm(e,{'role':'user','content':'reply','platform_message_id':'102'},'drain')
    count(e,'102')
    assert e.store.get_wake(cancel.id).status=='cancelled'
    assert e.store.get_session(KEY).unanswered_count==0


@pytest.mark.parametrize('kind,mids,admitted,redacted',[
    ('redirect',['103'],False,False),('steer',['104'],False,False),
    ('steer',['105','106'],False,False),('redirect',['110'],True,False),
    ('redirect',['110'],True,True),
])
def test_redirect_and_steer_fold(engine,kind,mids,admitted,redacted):
    e=engine; w,row=wake(e)
    assert e.tool.handle({'action':'respond','outcome':'no_change','notify':False},session_id='sid')['ok']
    if admitted:
        e.intake.pre_gateway_dispatch(event=event('fix',mids[0],mids[0]))
        assert not e.intake.flights['sid'].owner_replied
    text='\n'.join(origin(hashlib.sha256(mid.encode()).hexdigest()[:12] if redacted else mid) for mid in mids)
    folded={'role':'user','content':text,'api_content':'scaffold'}
    if kind=='steer':
        folded={'role':'user','content':'[OUT-OF-BAND USER MESSAGE — respond]\n'+text+'\n[/OUT-OF-BAND USER MESSAGE]','display_kind':'steer'}
    history=[{'role':'user','content':origin('old')},row,{'role':'assistant','content':''},
             {'role':'tool','content':'result'},folded]
    for _ in range(2):
        e.intake.pre_api_request(session_id='sid',turn_id='wake',conversation_history=history,
            user_message=row['content']+'\n\nUser correction during the turn: '+text,platform='telegram')
    count(e,*mids)
    assert e.intake.flights['sid'].owner_replied
    assert e.intake.transform_llm_output(session_id='sid',turn_id='wake',response_text='Your answer') is None
    e.intake.on_session_end(session_id='sid',turn_id='wake',completed=True)
    assert not e.store.list_occurrences(w.id)[0].delivered
    assert e.store.get_session(KEY).unanswered_count==0


@pytest.mark.parametrize('base_drained',[False,True])
def test_leftover_steer(engine,base_drained):
    e=engine; text=origin('107')
    if base_drained: e.intake.pre_gateway_dispatch(event=event(text,None,None))
    llm(e,{'role':'user','content':text})
    count(e,'107')


@pytest.mark.parametrize('reason,errors',[('interrupted_by_user',0),('interrupted_during_api_call',0),
    ('interrupted_by_system(watchdog)',1)])
def test_owner_interrupted_wake(engine,reason,errors):
    e=engine; w,row=wake(e)
    e.intake.on_session_end(session_id='sid',turn_id='wake',interrupted=True,turn_exit_reason=reason)
    assert e.store.get_wake(w.id).error_streak==errors
    o=e.store.list_occurrences(w.id)[0]
    assert o.state=='interrupted'
    assert o.error==('owner_interrupted' if not errors else 'interrupted')
    count(e)
    if not errors:
        llm(e,{'role':'user','content':'stop','platform_message_id':'108'},'drain')
        count(e,'108')


def test_priority_interrupt_followup(engine):
    e=engine; w,row=wake(e)
    e.intake.pre_gateway_dispatch(event=event('wait','109','109'))
    assert not e.intake.flights['sid'].owner_replied
    e.intake.on_session_end(session_id='sid',turn_id='wake',interrupted=True,turn_exit_reason='interrupted_by_user')
    llm(e,{'role':'user','content':'wait'},'followup')
    count(e,'109')
    assert e.store.get_wake(w.id).error_streak==0


def test_busy_voice_and_cron_history(engine):
    e=engine
    llm(e,{'role':'user','content':'transcript','platform_message_id':'111'})
    e.intake.pre_llm_call(session_id='sid',turn_id='next',platform='telegram',user_message='hey',
        conversation_history=[{'role':'user','content':'[Cron delivery: j]\n...'},
                              {'role':'user','content':'hey','platform_message_id':'114'}])
    count(e,'111','114')


def test_owner_status_command(engine):
    e=engine; e.intake.pre_gateway_dispatch(event=event('/status','112','112'))
    count(e,'112')


@pytest.mark.parametrize('text',[
    '[/loop wakeup #3, every 10m]\nRecurring task: check',
    '[IMPORTANT: Background process p1 ...]\n\n[INTERNAL NOTIFICATION — generated by Hermes]',
    'plugin text','[kanban] Task t1 done.','<resume note>',
])
def test_internal_machine_rows(engine,text):
    e=engine; w=arm(e,cancel_on_reply=True,next_due_at=e.clock.now()+3600)
    before=e.store.get_session(KEY); e.clock.advance(5)
    llm(e,{'role':'user','content':text,'display_kind':'internal_notification'})
    unchanged(e,before,w)


def test_slash_loop_tick(engine):
    e=engine; w=arm(e,cancel_on_reply=True,next_due_at=e.clock.now()+3600)
    before=e.store.get_session(KEY); e.clock.advance(5)
    e.intake.pre_gateway_dispatch(event=NS(internal=True,text='/recap'))
    unchanged(e,before,w)


@pytest.mark.parametrize('attribute',[False,True])
def test_core_heartbeat(engine,attribute):
    e=engine; w=arm(e,cancel_on_reply=True,next_due_at=e.clock.now()+3600)
    before=e.store.get_session(KEY); e.clock.advance(5)
    ev=event(HB,None,None,**({'_heartbeat_session_id':'sid'} if attribute else {}))
    e.intake.pre_gateway_dispatch(event=ev)
    llm(e,{'role':'user','content':HB,'display_kind':'internal_notification'})
    unchanged(e,before,w)


@pytest.mark.parametrize('text',GOALS)
@pytest.mark.parametrize('admission',[False,True])
def test_goal_continuation(engine,text,admission):
    e=engine; w=arm(e,cancel_on_reply=True,next_due_at=e.clock.now()+3600)
    before=e.store.get_session(KEY); e.clock.advance(5)
    if admission:
        e.intake.pre_gateway_dispatch(event=event(text,None,None))
    llm(e,{'role':'user','content':text})
    unchanged(e,before,w)


def test_own_wake_stale_env(engine,monkeypatch):
    e=engine; before=e.store.get_session(KEY)
    from heartbeat2 import tool
    monkeypatch.setattr(tool,'session_env',lambda name:'55' if name=='HERMES_SESSION_MESSAGE_ID' else '')
    w,row=wake(e)
    unchanged(e,before,w)
    assert e.intake.flights['sid'].turn_id=='wake'


def test_retry_replay(engine):
    e=engine
    e.intake.pre_gateway_dispatch(event=event('/retry','115','115'))
    e.intake.pre_gateway_dispatch(event=event('prev text',None,'115'))
    llm(e,{'role':'user','content':'prev text'})
    count(e,'115')


def test_orphan_rescue(engine):
    e=engine; e.intake.pre_gateway_dispatch(event=event('new','116','116'))
    llm(e,{'role':'user','content':GOALS[0]})
    llm(e,{'role':'user','content':'new','platform_message_id':'116'},'later')
    count(e,'116')


def test_delegated_child(engine):
    llm(engine,{'role':'user','content':'task','platform_message_id':'117'},platform='subagent')
    count(engine)


def test_bot_sender(engine):
    e=engine; ev=event('x','118','118'); ev.source.is_bot=True
    e.intake.pre_gateway_dispatch(event=ev)
    llm(e,{'role':'user','content':'x','platform_message_id':'118'})
    count(e)
    assert e.store.get_meta('nonhuman:'+KEY+':118')=='1'


@pytest.mark.parametrize('case',['earlier','other_turn','no_flight','no_marker','snapshot','last_marker'])
def test_fold_scope(engine,case):
    e=engine; history=[{'role':'user','content':origin('119')}]
    turn='wake'
    if case!='no_flight':
        w,row=wake(e)
        if case=='other_turn': turn='other'; history=[row]+history
        elif case=='snapshot': history=[row,{'role':'user','content':'snapshot'}]
        elif case=='last_marker': history=[row]+history+[row]
        elif case=='earlier': history+= [row]
    e.intake.pre_api_request(session_id='sid',turn_id=turn,conversation_history=history)
    count(e)
    if case!='no_flight': assert not e.intake.flights['sid'].owner_replied


@pytest.mark.parametrize('pattern,text',[('(?i)ОШИБКА','ошибка: диск'),('^.$','é')])
def test_regex_ascii_stdin(monkeypatch,pattern,text):
    real=probes.subprocess.Popen
    class Capture:
        def __init__(self,*args,**kwargs): self.proc=real(*args,**kwargs)
        def __getattr__(self,name): return getattr(self.proc,name)
        def communicate(self,body=None,**kwargs):
            if body is not None: assert body.isascii()
            return self.proc.communicate(body,**kwargs)
    monkeypatch.setattr(probes.subprocess,'Popen',Capture)
    monkeypatch.setenv('LANG','en_US.ISO8859-1'); monkeypatch.setenv('LC_ALL','en_US.ISO8859-1')
    assert probes._bounded_search(pattern,[text])==[0]


@pytest.mark.parametrize('age',[2,20])
@pytest.mark.parametrize('route',['tool','command','timer'])
def test_builtin_checkin_lifetime(runtime,age,route):
    e=runtime.get_engine(); e.policy=replace(e.policy,owner_silent_checkin=True)
    e.tick(); w,=e.store.list_wakes()
    e.clock.advance(age*86400)
    assert 'paused' in commands.handle('pause '+w.id+(' for 1d' if route=='timer' else ''),runtime)
    if route=='tool':
        result=e.tool.handle({'action':'resume','id':w.id},session_id='sid')
        assert result.get('ok'),result
        assert 'expires' not in result.get('clamped',{})
    elif route=='command': assert not commands.handle('resume '+w.id,runtime).startswith('Error')
    else:
        e.intake.human(KEY,folded=False); e.clock.advance(86400); e.intake.human(KEY,folded=False); e.tick()
    current=e.store.get_wake(w.id)
    assert current.status=='active' and current.expires_at is None
    for _ in range(30):
        e.clock.advance(86400); e.intake.human(KEY,folded=False); e.tick()
    current=e.store.get_wake(w.id)
    assert current.status=='active' and current.expires_at is None


def test_known_gaps_documented():
    readme=(Path(__file__).resolve().parents[1]/'README.md').read_text()
    assert 'Signal/SimpleX' in readme and 'native steer' in readme


def test_human_dedupe_still_marks_fold(engine):
    e=engine; w,row=wake(e)
    e.intake.pre_gateway_dispatch(event=event())
    assert not e.intake.flights['sid'].owner_replied
    e.intake.human(KEY,dedupe=KEY+':101')
    assert e.intake.flights['sid'].owner_replied
    count(e,'101')


@pytest.mark.parametrize('row',[
    {'role':'user','content':origin('121'),'display_kind':'steer','platform_message_id':'121'},
    {'role':'user','content':'compacted snapshot'},
])
def test_non_owner_current_rows(engine,row):
    llm(engine,row)
    count(engine)


@pytest.mark.parametrize('pin',['04eb00cef7','818c13be'])
def test_pin_parity(engine,tmp_path,pin):
    """Import isolated literal/row fixtures from each read-only Hermes pin.

    Export only the pinned constant definitions and pure steer-row functions;
    never import a gateway runner or boot Hermes in the unit interpreter.
    """
    src=Path(os.environ.get('HERMES_SRC',Path.home()/'.hermes/hermes-agent'))
    if not (src/'.git').exists(): pytest.skip('Hermes pin source unavailable')
    def tree(path):
        result=subprocess.run(['git','-C',str(src),'show',pin+':'+path],capture_output=True,text=True,check=True)
        return ast.parse(result.stdout)
    names={'STEER_MARKER_OPEN','STEER_MARKER_CLOSE','STEER_DISPLAY_KIND',
           'CONTINUATION_PROMPT_TEMPLATE','CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE',
           'HEARTBEAT_PROMPT_TEMPLATE','INTERNAL_NOTIFICATION_DISPLAY_KIND'}
    nodes=[]
    for path in ('agent/prompt_builder.py','hermes_cli/goals.py','hermes_cli/heartbeat.py','gateway/response_filters.py'):
        for node in tree(path).body:
            if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id in names for t in node.targets): nodes.append(node)
            if isinstance(node,ast.FunctionDef) and node.name in {'format_steer_marker','steer_user_row'}: nodes.append(node)
    busy=tree('gateway/run_busy.py')
    origin_literal=next(n.value for n in ast.walk(busy) if isinstance(n,ast.Constant)
        and isinstance(n.value,str) and n.value.startswith(ORIGIN+'\n'))
    module_path=tmp_path/'hermes_pin_fixture.py'
    module_path.write_text('from __future__ import annotations\n'+ast.unparse(ast.Module(body=nodes,type_ignores=[]))+
                           '\nORIGIN = '+repr(origin_literal.strip())+'\n')
    spec=importlib.util.spec_from_file_location('hermes_pin_fixture',module_path)
    fixture=importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
    assert fixture.ORIGIN==ORIGIN
    assert fixture.CONTINUATION_PROMPT_TEMPLATE.startswith(GOALS[0])
    assert fixture.CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE.startswith(GOALS[1])
    assert fixture.HEARTBEAT_PROMPT_TEMPLATE.startswith('[Heartbeat — recurring instruction')
    assert fixture.INTERNAL_NOTIFICATION_DISPLAY_KIND=='internal_notification'
    assert fixture.STEER_MARKER_OPEN.startswith('[OUT-OF-BAND USER MESSAGE —')
    assert fixture.STEER_MARKER_CLOSE=='[/OUT-OF-BAND USER MESSAGE]'
    hook=next(n for n in ast.walk(tree('agent/turn_api_request.py')) if isinstance(n,ast.Call)
        and isinstance(n.func,ast.Name) and n.func.id=='_invoke_hook'
        and n.args and isinstance(n.args[0],ast.Constant) and n.args[0].value=='pre_api_request')
    payload={kw.arg:ast.unparse(kw.value) for kw in hook.keywords}
    assert payload['session_id']=="agent.session_id or ''" and payload['turn_id']=='turn_id'
    assert payload['conversation_history']=='list(messages)'
    valid=next(n for n in tree('hermes_cli/plugins.py').body if isinstance(n,ast.AnnAssign)
        and isinstance(n.target,ast.Name) and n.target.id=='VALID_HOOKS')
    assert 'pre_api_request' in ast.literal_eval(valid.value)
    e=engine; w,row=wake(e)
    steer=fixture.steer_user_row(origin('122'))
    e.intake.pre_api_request(session_id='sid',turn_id='wake',conversation_history=[row,steer])
    count(e,'122')
    assert e.intake.flights['sid'].owner_replied
