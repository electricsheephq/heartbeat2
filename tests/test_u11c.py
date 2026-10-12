"""U11c regressions adapted from the U11b review probes."""
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace as NS
import pytest
from test_engine import engine, arm, session, KEY, GatewayCtx, admit
from test_u11 import event, llm, wake, origin, count, ORIGIN, GOALS
from heartbeat2 import tool
from heartbeat2.poller import Poller
from heartbeat2.policy import Policy
from heartbeat2.store import Store

AUTH = NS(_is_user_authorized_for_source=lambda src: True)
STEER_OPEN = '[OUT-OF-BAND USER MESSAGE — the user sent this while you were working; it is not a tool result]'
STEER_CLOSE = '[/OUT-OF-BAND USER MESSAGE]'


def humans(e, key=KEY):
    return e.store.connection.execute(
        "SELECT COUNT(*) FROM events WHERE kind='human_message' AND session_key=?", (key,)).fetchone()[0]


def hermes_origin(text, platform, mid=None):
    # gateway/run_busy.py _steer_text_with_origin: None values dropped; Signal has no ids.
    data = {'platform': platform, 'chat_id': 'unit', 'chat_type': 'dm', 'user_id': 'fixture-owner'}
    if mid: data.update(message_id=mid, source_message_id=mid)
    return (ORIGIN + '\n' + json.dumps(data, ensure_ascii=True) +
            '\nDo not guess a reply destination when these fields are insufficient.\n\n' + text)


def steer_row(text):
    # agent/prompt_builder.py steer_user_row
    return {'role': 'user', 'content': STEER_OPEN + '\n' + text + '\n' + STEER_CLOSE, 'display_kind': 'steer'}


# ---- 1. /steer on an id-less platform: A counts the command, C counts its ORIGIN row again ----

@pytest.mark.parametrize('platform,mid', [('telegram', '301'), ('signal', None), ('simplex', None)])
@pytest.mark.parametrize('turn', ['wake', 'human'])
def test_slash_steer_counted_once(engine, platform, mid, turn):
    e = engine
    if turn == 'wake':
        w, row = wake(e)
        session(e, platform=platform)
    else:
        row = {'role': 'user', 'content': 'do the thing'}
        if mid: row['platform_message_id'] = '300'
        llm(e, row, 'human', platform=platform)
    session(e, platform=platform)
    before = humans(e)
    # Busy /steer: adapter bypass -> _dispatch_inline_reply -> _handle_message -> A, then the
    # priority lane's _busy_steer_command steers the ORIGIN-prefixed args.
    e.intake.pre_gateway_dispatch(event=event('/steer Fix it.', mid=mid, source_mid=mid, platform=platform),
                                  gateway=AUTH)
    assert humans(e) == before
    history = [row, {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c1'}]},
               {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'},
               steer_row(hermes_origin('Fix it.', platform, mid))]
    for _ in range(2):
        e.intake.pre_api_request(session_id='sid', turn_id=turn, conversation_history=history)
    assert humans(e) == before + 1, f'one /steer counted {humans(e) - before} times on {platform}'


# ---- 2. Pre-API steer inserted mid-history (Hermes scans the whole transcript for a tool row) ----

@pytest.mark.parametrize('via_command', [False, True])
def test_setup_phase_steer_into_wake_is_seen(engine, via_command):
    e = engine
    w = arm(e, 'every'); e.tick()
    text = e.ctx.calls[-1][0]
    wake_row = {'role': 'user', 'content': text, 'display_kind': 'internal_notification'}
    older = [{'role': 'user', 'content': 'earlier ask', 'platform_message_id': '10'},
             {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c0'}]},
             {'role': 'tool', 'tool_call_id': 'c0', 'content': 'old result'},
             {'role': 'assistant', 'content': 'done earlier'}]
    history = older + [wake_row]
    e.intake.pre_llm_call(session_id='sid', turn_id='wake', user_message=text,
                          conversation_history=list(history), platform='telegram')
    assert e.tool.handle({'action': 'respond', 'outcome': 'no_change', 'notify': False}, session_id='sid')['ok']
    if via_command:
        e.intake.pre_gateway_dispatch(event=event('/steer Stop and tell me X', mid='501', source_mid='501'),
                                      gateway=AUTH)
    # agent/turn_iteration_prep.py prepare_iteration -> _inject_steer_after_newest_tool_result:
    # newest tool row anywhere in messages; the wake turn has none yet, so index 3 (before the wake row).
    history.insert(3, steer_row(hermes_origin('Stop and tell me X', 'telegram', '501')))
    e.intake.pre_api_request(session_id='sid', turn_id='wake', conversation_history=history)
    assert humans(e) == 1, 'owner steer never counted'
    assert e.intake.flights['sid'].owner_replied, 'steer not folded'
    out = e.intake.transform_llm_output(session_id='sid', turn_id='wake', response_text='X is 42.')
    assert out is None, 'reply to the owner steer is silenced'


# ---- 3. A maps by (platform, chat_id) only: a DM thread/topic message is credited to a sibling ----

def test_dm_thread_message_counts_under_its_own_session(tmp_path, clock, monkeypatch):
    keys = {'current': KEY}
    thread_key = KEY + ':thread9'
    env = {'HERMES_SESSION_PLATFORM': 'telegram', 'HERMES_SESSION_CHAT_TYPE': 'dm',
           'HERMES_SESSION_CHAT_ID': 'unit', 'HERMES_SESSION_ID': 'sid9'}
    monkeypatch.setattr(tool, 'session_env', lambda name: env.get(name, ''))
    from heartbeat2.model import Session
    store = Store(tmp_path / 'pulse.db', clock)
    store.upsert_session(Session(KEY, 'telegram', 'dm', 'unit', 'sid', clock.now(), clock.now(), 'episode'))
    e = Poller(GatewayCtx(tmp_path), store, Policy(clock=clock, quiet_hours='', timezone='UTC'), clock,
               session_key_fn=lambda: keys['current'], paused=lambda: False, jitter=lambda: 0,
               compression_tip=lambda sid: sid)
    try:
        main_wake = arm(e, cancel_on_reply=True, next_due_at=clock.now() + 3600)
        # Owner's first message in a NEW thread of the same DM (Mattermost root_id / Telegram DM topic).
        e.intake.pre_gateway_dispatch(event=event('in the thread', mid='401', source_mid='401'), gateway=NS(_session_key_for_source=lambda source: thread_key,
                   _is_user_authorized_for_source=lambda source: True))
        assert json.loads(store.get_meta('pending_human:key:'+thread_key))['mid']=='401'
        keys['current'] = thread_key
        e.intake.pre_llm_call(session_id='sid9', turn_id='t9', user_message='in the thread', platform='telegram',
                              conversation_history=[{'role': 'user', 'content': 'in the thread',
                                                     'platform_message_id': '401'}])
        assert humans(e, thread_key) == 1
        assert humans(e, KEY) == 0, 'thread message also counted under the main DM session'
        assert e.store.get_wake(main_wake.id).status == 'active', "main session's cancel_on_reply wake cancelled"
    finally:
        e.close(); store.close()


# ---- 4. Owner redirect into an invalid (superseded) wake turn: counted, but its reply is silenced ----

def test_redirect_into_invalid_wake_reply_not_silenced(engine):
    e = engine
    w = arm(e, 'every', cancel_on_reply=True); e.tick()
    text = e.ctx.calls[-1][0]
    # Owner activity after claim, before the injected turn starts -> flight invalid at B.
    o = e.store.list_occurrences(w.id)[-1]
    session(e, last_human_at=(o.claimed_at or e.clock.now()) + 1)
    row = {'role': 'user', 'content': text, 'display_kind': 'internal_notification'}
    llm(e, row, 'wake')
    assert e.intake.flights['sid'].invalid
    e.intake.pre_api_request(session_id='sid', turn_id='wake', conversation_history=[
        row, {'role': 'assistant', 'content': ''}, {'role': 'user', 'content': hermes_origin('What is X?', 'telegram', '601')}])
    assert humans(e) == 1
    out = e.intake.transform_llm_output(session_id='sid', turn_id='wake', response_text='X is 42.')
    assert out is None, 'answer to the owner redirect silenced'


# ---- 5. Mid-turn compaction re-indexes an id-less steer row ----

def test_idless_steer_not_recounted_after_midturn_compaction(engine):
    e = engine
    session(e, platform='signal')
    w, row = wake(e)
    steer = steer_row(hermes_origin('Fix it.', 'signal'))
    big = [{'role': 'user', 'content': 'old %d' % i} for i in range(6)]
    history = big + [row, {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c1'}]},
                     {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'}, steer]
    e.intake.pre_api_request(session_id='sid', turn_id='wake', conversation_history=history)
    assert humans(e) == 1
    compacted = [{'role': 'user', 'content': '[CONTEXT COMPACTION] summary'}, row,
                 {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c1'}]},
                 {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'}, steer]
    e.intake.pre_api_request(session_id='sid', turn_id='wake', conversation_history=compacted)
    assert humans(e) == 1, 'id-less steer counted again after compaction re-indexed it'


# ---- 1b. /steer that misses the last tool batch becomes the leftover follow-up turn (B) ----

@pytest.mark.parametrize('platform,mid', [('telegram', '311'), ('signal', None)])
def test_slash_steer_leftover_counted_once(engine, platform, mid):
    e = engine
    # A leftover steer exists only when /steer reached a busy session.
    session(e, platform=platform, busy_since=e.clock.now())
    e.intake.pre_gateway_dispatch(event=event('/steer Fix it.', mid=mid, source_mid=mid, platform=platform),
                                  gateway=AUTH)
    assert humans(e) == 0
    # gateway/run_turn.py _run_agent_drain_pending: leftover steer -> eventless next turn.
    llm(e, {'role': 'user', 'content': hermes_origin('Fix it.', platform, mid)}, 'followup', platform=platform)
    assert humans(e) == 1, f'one /steer counted {humans(e)} times on {platform}'

def api(e, history, turn='wake'):
    e.intake.pre_api_request(session_id='sid', turn_id=turn, conversation_history=list(history))


def wake_row(e):
    text = e.ctx.calls[-1][0]
    return {'role': 'user', 'content': text, 'display_kind': 'internal_notification'}


# P-A (pre-existing race variant; see test_goal_loop_redirect_silence.py for the U11b regression):
# owner redirect into a wake turn whose flight is invalid (cancel_on_reply wake, owner
# replied between claim and turn start). Hermes still runs the injected turn; the owner then
# redirects (busy interrupt mode, model streaming) and the model answers the correction in
# this turn. The answer is silenced because invalid flights never fold and transform returns
# [SILENT] for invalid flights.
def test_owner_redirect_into_invalid_wake_turn_is_not_silenced(engine):
    e = engine
    w = arm(e, 'every', cancel_on_reply=True)
    e.tick()                                     # claim + inject
    row = wake_row(e)
    e.clock.advance(5)
    # msg1: idle owner message admitted before the wake turn starts (A counts it).
    e.intake.pre_gateway_dispatch(event=event('quick question', 'm1', 'm1'))
    count(e, 'm1')
    # The queued wake turn starts; its occurrence was superseded by owner_replied -> invalid.
    llm(e, row, 'wake')
    f = e.intake.flights['sid']
    assert f.invalid
    # msg2 arrives while the wake turn is streaming -> busy redirect row (no A).
    history = [row, {'role': 'assistant', 'content': ''},
               {'role': 'user', 'content': origin('m2'), 'api_content': 'scaffold'}]
    api(e, history)
    count(e, 'm1', 'm2')                         # counted via the anchor fallback
    out = e.intake.transform_llm_output(session_id='sid', turn_id='wake',
                                        response_text='Sure - the answer to your correction is 42.')
    assert out is None, 'owner-directed answer in an invalid wake turn was silenced: %r' % out


# Control: the same redirect into a VALID notify=false wake is not silenced (P11).
def test_owner_redirect_into_valid_wake_after_priority_lane_count(engine):
    e = engine
    w = arm(e, 'every', cancel_on_reply=True)
    e.tick()
    row = wake_row(e)
    llm(e, row, 'wake')
    assert e.tool.handle({'action': 'respond', 'outcome': 'no_change', 'notify': False}, session_id='sid')['ok']
    # Priority lane: A counts m3 first (dedupe hit at C), then C sees the redirect row.
    e.intake.pre_gateway_dispatch(event=event('fix', 'm3', 'm3'))
    count(e, 'm3')
    assert not e.intake.flights['sid'].owner_replied
    api(e, [row, {'role': 'assistant', 'content': ''}, {'role': 'user', 'content': origin('m3')}])
    count(e, 'm3')
    assert e.intake.flights['sid'].owner_replied
    assert e.intake.transform_llm_output(session_id='sid', turn_id='wake', response_text='ok') is None

def b(e, turn, history):
    return e.intake.pre_llm_call(session_id='sid', task_id='sid', turn_id=turn, platform='telegram',
                                 user_message=history[-1]['content'], conversation_history=list(history))


def c(e, turn, history):
    e.intake.pre_api_request(session_id='sid', turn_id=turn, conversation_history=list(history))


def end(e, turn, text):
    out = e.intake.transform_llm_output(session_id='sid', turn_id=turn, response_text=text)
    e.intake.on_session_end(session_id='sid', turn_id=turn, completed=True)
    return out


def test_owner_answer_in_wake_turn_after_goal_redirect_is_delivered(engine):
    e = engine
    e.tick()                                         # gateway already running (process-start reconcile done)
    goal1 = [{'role': 'user', 'content': GOALS[0] + '\nGoal: ship it'}]
    b(e, 'goal1', goal1)
    end(e, 'goal1', 'step 1 done')                   # idle gap between goal continuations
    assert e.store.get_session(KEY).busy_since is None
    w = arm(e, 'every', cancel_on_reply=True)        # "nudge me unless I reply"
    e.tick()                                         # claimed + injected in the idle gap
    wake_text = e.ctx.calls[-1][0]
    e.clock.advance(1)
    # The next goal continuation (already enqueued by Hermes) wins the session; the wake queues.
    hist = goal1 + [{'role': 'assistant', 'content': 'step 1 done'},
                    {'role': 'user', 'content': GOALS[0] + '\nGoal: ship it'}]
    b(e, 'goal2', hist)
    e.clock.advance(5)
    hist += [{'role': 'assistant', 'content': ''},
             {'role': 'user', 'content': origin('m1'), 'api_content': 'scaffold'}]  # owner redirect #1
    c(e, 'goal2', hist)
    end(e, 'goal2', 'answer to m1')
    # Queued wake drains.
    hist += [{'role': 'assistant', 'content': 'answer to m1'},
             {'role': 'user', 'content': wake_text, 'display_kind': 'internal_notification'}]
    b(e, 'wake', hist)
    e.clock.advance(5)
    hist += [{'role': 'assistant', 'content': ''},
             {'role': 'user', 'content': origin('m2'), 'api_content': 'scaffold'}]  # owner redirect #2
    c(e, 'wake', hist)
    out = e.intake.transform_llm_output(session_id='sid', turn_id='wake',
                                        response_text='Here is the answer to your second message.')
    assert out is None, 'model answer to owner message m2 was silenced: %r' % out

def test_assistant_echo_of_token_does_not_hide_redirect(engine):
    e=engine; w,row=wake(e)
    assert e.tool.handle({'action':'respond','outcome':'no_change','notify':False},session_id='sid')['ok']
    token=row['content'].split('\n',1)[0].split(' [',1)[0]
    history=[row,{'role':'assistant','content':''},{'role':'user','content':origin('301')},
             {'role':'assistant','content':'Handling '+token+' now.'}]
    e.intake.pre_api_request(session_id='sid',turn_id='wake',conversation_history=history)
    count(e,'301')
    assert e.intake.flights['sid'].owner_replied
    assert e.intake.transform_llm_output(session_id='sid',turn_id='wake',response_text='Answer to owner') is None

def test_owner_redirect_into_stale_cancel_on_reply_wake_is_silenced(engine):
    e=engine
    w=arm(e,cancel_on_reply=True)
    e.tick()                                  # claimed + injected; Hermes FIFOs it behind the owner's turn
    text=e.ctx.calls[-1][0]
    e.clock.advance(1); admit(e,'m1','Any news?')   # owner's message m1 (its own turn runs first)
    count(e,'m1')
    wake_row={'role':'user','content':text,'display_kind':'internal_notification'}
    hist=[{'role':'user','content':'Any news?','platform_message_id':'m1'},{'role':'assistant','content':'Checking.'},wake_row]
    e.intake.pre_llm_call(session_id='sid',turn_id='wake',user_message=text,platform='telegram',conversation_history=hist)
    f=e.intake.flights['sid']
    assert f.invalid                          # stale: owner replied after claim
    # Owner sends m2 during the stale wake turn; interrupt-mode redirect appends an ORIGIN row
    e.clock.advance(1); admit(e,'m2','Actually, what about Bob?')
    hist2=hist+[{'role':'assistant','content':''},{'role':'user','content':origin('m2')}]
    e.intake.pre_api_request(session_id='sid',turn_id='wake',conversation_history=hist2)
    count(e,'m1','m2')
    out=e.intake.transform_llm_output(session_id='sid',turn_id='wake',response_text='Bob finished the report at 3pm.')
    print('transform ->',out,'owner_replied',f.owner_replied)
    assert out is None, 'answer to the owner m2 is silenced'


@pytest.mark.parametrize('parts',[False,True])
def test_counter_baseline_multiplicity_and_compaction(engine,parts):
    e=engine; w,row=wake(e)
    text=hermes_origin('same steer','signal')
    content=[{'type':'text','text':text},{'type':'image_url'}] if parts else text
    steer={'role':'user','content':content,'display_kind':'steer'}
    # An identical earlier row is baseline, rather than a new owner message.
    history=[steer,{'role':'tool','content':'old'},row]
    e.intake.pre_llm_call(session_id='sid',turn_id='wake',user_message=row['content'],conversation_history=history)
    fp=hashlib.sha256(text.encode()).hexdigest()[:16]
    assert e.intake.anchors['sid']==('wake',Counter({fp:1}))
    api(e,history); count(e)
    assert not e.intake.flights['sid'].owner_replied
    history[2:2]=[steer,steer]
    for _ in range(5): api(e,history)
    count(e,None,None)
    # Ordinals continue after the baseline copy, so each new copy has its own key.
    assert e.store.get_meta(f'human:{KEY}:origin:wake:{fp}:2')=='1'
    assert e.store.get_meta(f'human:{KEY}:origin:wake:{fp}:3')=='1'
    # Compaction moves the copies; Counter subtraction must not recount them.
    for _ in range(4): api(e,[row,steer,steer,steer])
    count(e,None,None)
    assert e.intake.anchors['sid'][1]==Counter({fp:3})


def test_leftover_idless_b_baseline_dedupes_c(engine):
    e=engine; text=hermes_origin('leftover','signal'); row={'role':'user','content':text}
    llm(e,row,'leftover',platform='signal')
    fp=hashlib.sha256(text.encode()).hexdigest()[:16]
    count(e,None)
    assert e.store.get_meta(f'human:{KEY}:origin:leftover:{fp}:1')=='1'
    for _ in range(4): api(e,[{'role':'tool','content':'repair'},row],'leftover')
    count(e,None)


@pytest.mark.parametrize('role',['tool','assistant'])
def test_origin_and_token_quotes_are_not_owner_rows(engine,role):
    e=engine; w,row=wake(e)
    api(e,[row,{'role':role,'content':origin('quoted')+'\n'+row['content']}])
    count(e)
    assert not e.intake.flights['sid'].owner_replied
    api(e,[row,{'role':'user','content':origin('real')},
           {'role':role,'content':row['content']}])
    count(e,'real')
    assert e.intake.flights['sid'].owner_replied


@pytest.mark.parametrize('guard',['no_anchor','anchor_turn','flight_turn'])
def test_counter_requires_turn_anchor_and_folds_only_same_flight(engine,guard):
    e=engine; w,row=wake(e)
    if guard=='no_anchor': e.intake.anchors.clear()
    elif guard=='anchor_turn': e.intake.anchors['sid']=('another',Counter())
    else: e.intake.flights['sid'].turn_id='another'
    api(e,[row,{'role':'user','content':origin('new')}])
    count(e,*(['new'] if guard=='flight_turn' else []))
    assert not e.intake.flights['sid'].owner_replied


@pytest.mark.parametrize('guard',['invalid','not_started','stale_attempt'])
def test_new_origin_folds_invalid_and_stale_flights(engine,guard):
    e=engine; w,row=wake(e); f=e.intake.flights['sid']; o=e.store.list_occurrences(w.id)[0]
    if guard=='invalid': f.invalid=True
    elif guard=='not_started': assert e.store.cas_occurrence(w.id,o.dedupe_key,'started','interrupted',attempt=o.attempt)
    else: f.attempt+=1
    assert e.intake.transform_llm_output(session_id='sid',turn_id='wake',response_text='machine')=='[SILENT]'
    api(e,[row,{'role':'user','content':origin('redirect')}])
    count(e,'redirect')
    assert f.owner_replied
    assert e.intake.transform_llm_output(session_id='sid',turn_id='wake',response_text='answer') is None


@pytest.mark.parametrize('text',['/steer Fix it.','/s Fix it.','/StEeR@bot Fix it.','/S@bot','/steer'])
@pytest.mark.parametrize('platform',['telegram','signal'])
def test_a_steer_alias_skip(engine,text,platform):
    e=engine; session(e,platform=platform,busy_since=e.clock.now())
    mid='steer-id' if platform=='telegram' else None
    e.intake.pre_gateway_dispatch(event=event(text,mid,mid,platform))
    count(e)
    llm(e,{'role':'user','content':hermes_origin('Fix it.',platform,mid)},'leftover',platform=platform)
    count(e,mid)


@pytest.mark.parametrize('text',['/status','/steering','/something'])
def test_a_nonsteer_commands_still_count(engine,text):
    engine.intake.pre_gateway_dispatch(event=event(text))
    count(engine,'101')


@pytest.mark.parametrize('existing',[False,True])
@pytest.mark.parametrize('age',[0,3601])
def test_resolved_session_key_pending_and_existing(engine,existing,age):
    e=engine; ev=event(mid=None,source_mid=None)
    key=KEY if existing else KEY+':topic'
    gateway=NS(_session_key_for_source=lambda source:key,_is_user_authorized_for_source=lambda source:True)
    e.intake.pre_gateway_dispatch(event=ev,gateway=gateway)
    stamp='pending_human:key:'+key
    if existing:
        count(e,None)
        assert not e.store.get_meta(stamp)
    else:
        assert json.loads(e.store.get_meta(stamp))=={'mid':None,'at':e.clock.now()}
        assert not e.store.get_meta('pending_human:telegram:unit')
        count(e)
        e.clock.advance(age); e.tool.session_key_fn=lambda:key
        llm(e,{'role':'user','content':'hello'},'topic')
        assert humans(e,key)==(1 if age==0 else 0)
        assert not e.store.get_meta(stamp)
        llm(e,{'role':'user','content':'hello'},'next')
        assert humans(e,key)==(1 if age==0 else 0)


def test_resolver_failure_uses_legacy_match(engine):
    def broken(source): raise RuntimeError('fixture failure')
    engine.intake.pre_gateway_dispatch(event=event(),gateway=NS(_session_key_for_source=broken))
    count(engine,'101')


def test_baseline_drain_finishes_fire_before_owner_answer(tmp_path,monkeypatch):
    from bench.baseline.heartbeat1 import Heartbeat1
    from bench.sim.gateway import FakeGateway
    from bench.sim.runner import simulate
    from bench.run import load_scenarios
    calls=[]; send=Heartbeat1.send; human=FakeGateway.human
    def track_send(self,*args,**kwargs):
        calls.append('fire-send'); result=send(self,*args,**kwargs); calls.append('fire-finished'); return result
    def track_human(self,*args,**kwargs):
        if kwargs.get('drained'): calls.append('drain')
        return human(self,*args,**kwargs)
    monkeypatch.setattr(Heartbeat1,'send',track_send)
    monkeypatch.setattr(FakeGateway,'human',track_human)
    result=simulate(load_scenarios('hermes-drained')[0],tmp_path,baseline=True)
    assert calls[:3]==['fire-send','fire-finished','drain']
    prompted=[d for d in result['deliveries'] if d['prompted']]
    assert len(prompted)==4
    assert prompted[0]['text']=='Here is your requested update.'
    assert result['metrics']['delivered_messages']==8
    assert result['metrics']['message_attempts']==8


@pytest.mark.parametrize('pin',['04eb00cef7','818c13be'])
def test_gateway_resolver_matches_tool_session_key_at_both_pins(pin):
    src=Path.home()/'.hermes/hermes-agent'
    if not (src/'.git').exists(): pytest.skip('Hermes pin source unavailable')
    def read(file): return subprocess.check_output(['git','-C',str(src),'show',pin+':'+file],text=True)
    run=ast.parse(read('gateway/run.py'))
    resolver=next(n for n in ast.walk(run) if isinstance(n,ast.FunctionDef) and n.name=='_session_key_for_source')
    binding=next(n for n in ast.walk(run) if isinstance(n,ast.FunctionDef) and n.name=='_set_session_env')
    # Execute source-derived key builder, resolver and ContextVar binding, with
    # source identity discovery stubbed (no pinned identity in these ordinary DMs).
    build=ast.parse(read('gateway/session.py'))
    nodes=[n for n in build.body if isinstance(n,ast.FunctionDef) and n.name in {'build_session_key','_session_key_namespace'}]
    resolver=ast.fix_missing_locations(ast.parse(ast.unparse(resolver)).body[0])
    resolver.body=[n for n in resolver.body if not isinstance(n,ast.ImportFrom)]
    context={}; exec(compile(read('gateway/session_context.py'),'hermes_session_context','exec'),context)
    context['_runtime_cwd']=lambda *args:None
    context['Platform']=NS(WHATSAPP='whatsapp',SLACK='slack')
    context['identity_of']=lambda source:None
    exec('from __future__ import annotations\n'+ast.unparse(ast.Module(body=nodes+[resolver],type_ignores=[])),context)
    # Bind the gateway context's session_key directly, as the actual tool path does.
    call=next(n for n in ast.walk(binding) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='set_session_vars')
    assert next(ast.unparse(k.value) for k in call.keywords if k.arg=='session_key')=='context.session_key'
    keys=[]
    for thread in ('','thread-fixture'):
        source=NS(chat_type='dm',chat_id='fixture-chat',thread_id=thread,platform=NS(value='telegram'),profile='')
        key=context['_session_key_for_source'](NS(session_store=None,config=NS()),source)
        assert key==context['build_session_key'](source)
        context['set_session_vars'](session_key=key,chat_id=source.chat_id,thread_id=thread)
        assert context['get_session_env']('HERMES_SESSION_KEY')==key
        keys.append(key)
        context['reset_session_vars']()
    assert keys==['agent:main:telegram:dm:fixture-chat','agent:main:telegram:dm:fixture-chat:thread-fixture']
    commands=read('hermes_cli/commands.py')
    assert 'CommandDef("steer"' in commands and 'aliases=("s",)' in commands


# ---- U11c verifier regressions (D1 idle /steer, D2 compaction rewrite, D3 ordinals) ----

# agent/context_compressor.py:501-506 (identical text at 04eb00cef7 and 818c13be)
END = "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"
HDR = "[PRIOR CONTEXT — for reference only; not a new message]"
DELIM = "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]"


def idless(text):
    return ORIGIN + '\n' + json.dumps({'platform': 'signal', 'chat_id': 'unit', 'chat_type': 'dm'}) + \
        '\nDo not guess a reply destination when these fields are insufficient.\n\n' + text


def merged_into_tail(row, summary='Summary of earlier work.'):
    # ContextCompressor._merge_summary_into_tail_row, non-force_user_leading branch (in place).
    out = dict(row)
    out['content'] = HDR + '\n' + row['content'] + '\n\n' + DELIM + '\n\n' + summary + '\n\n' + END
    out['_compressed_summary'] = True
    return out


def b_turn(e, turn, history, platform='telegram'):
    return e.intake.pre_llm_call(session_id='sid', task_id='sid', turn_id=turn, platform=platform,
                                 user_message=history[-1]['content'], conversation_history=list(history))


# D1: idle /steer (and the starting/no-agent queue fallback) on an id-less platform.
# Hermes gateway/run_inbound.py:963-973 (both pins): idle /steer rewrites event.text to the payload and
# falls through as an ordinary turn; no ORIGIN row is ever produced. A has already skipped '/steer'.
@pytest.mark.parametrize('text', ['/steer Fix it.', '/s Fix it.'])
@pytest.mark.parametrize('platform', ['signal', 'simplex'])
def test_d1_idle_steer_on_idless_platform_counts_once(engine, text, platform):
    e = engine; session(e, platform=platform)
    w = arm(e, cancel_on_reply=True, next_due_at=e.clock.now() + 3600, platform=platform)
    e.intake.pre_gateway_dispatch(event=event(text, mid=None, source_mid=None, platform=platform), gateway=AUTH)
    llm(e, {'role': 'user', 'content': 'Fix it.'}, 'idle', platform=platform)   # Hermes row: payload, no id
    assert humans(e) == 1, f'idle {text!r} from {platform} counted {humans(e)} times'
    assert e.store.get_wake(w.id).status == 'cancelled', 'cancel_on_reply wake survives the owner reply'


# D2a: mid-turn compaction merges the summary into the first tail row (an OLD owner redirect).
# Telegram id: the count dedupes, but C folds the wake -> notify=false output is un-silenced.
def test_d2a_compaction_rewrite_does_not_fold_wake(engine):
    e = engine
    e.intake.count(KEY, 'r_old')                         # counted in its own earlier turn
    w = arm(e, 'every'); e.tick()
    wrow = {'role': 'user', 'content': e.ctx.calls[-1][0], 'display_kind': 'internal_notification'}
    r_old = {'role': 'user', 'content': origin('r_old')}
    hist = [{'role': 'user', 'content': 'start', 'platform_message_id': '1'}, {'role': 'assistant', 'content': 'a0'},
            r_old, {'role': 'assistant', 'content': 'a1'},
            {'role': 'user', 'content': 'later', 'platform_message_id': '2'}, {'role': 'assistant', 'content': 'a2'}, wrow]
    b_turn(e, 'wake', hist)
    assert e.tool.handle({'action': 'respond', 'outcome': 'no_change', 'notify': False}, session_id='sid')['ok']
    tools = [{'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c1'}]}, {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'}]
    api(e, hist + tools, 'wake')
    compacted = hist[:2] + [merged_into_tail(r_old)] + hist[3:] + tools + tools
    api(e, compacted, 'wake')
    count(e, 'r_old')
    assert not e.intake.flights['sid'].owner_replied, 'compaction rewrite folded the wake'
    assert e.intake.transform_llm_output(session_id='sid', turn_id='wake', response_text='No changes.') == '[SILENT]'


# D2b: same rewrite in a machine (goal) turn on Signal: recount cancels a cancel_on_reply wake.
def test_d2b_compaction_rewrite_idless_goal_turn_no_recount(engine):
    e = engine; session(e, platform='signal')
    r_old = {'role': 'user', 'content': idless('Remind me later.')}
    b_turn(e, 'old', [r_old], platform='signal')              # counted once as a leftover turn earlier
    assert humans(e) == 1
    e.intake.on_session_end(session_id='sid', turn_id='old', completed=True)
    w = arm(e, cancel_on_reply=True, next_due_at=e.clock.now() + 3600, platform='signal')
    hist = [{'role': 'user', 'content': 'start'}, {'role': 'assistant', 'content': 'a0'},
            r_old, {'role': 'assistant', 'content': 'a1'}, {'role': 'user', 'content': GOALS[0] + '\nGoal: ship'}]
    b_turn(e, 'goal', hist, platform='signal')
    tools = [{'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c1'}]}, {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'}]
    api(e, hist + tools, 'goal')
    api(e, hist[:2] + [merged_into_tail(r_old)] + hist[3:] + tools + tools, 'goal')
    assert humans(e) == 1, 'compaction rewrite recounted an old id-less owner row in a goal turn'
    assert e.store.get_wake(w.id).status == 'active', 'machine turn cancelled a cancel_on_reply wake'


# D3: two identical id-less owner steers in ONE turn, observed by different pre_api_request calls.
def test_d3_duplicate_idless_steer_across_calls_counts_twice(engine):
    e = engine; session(e, platform='signal'); w, row = wake(e)
    s = {'role': 'user', 'content': '[OUT-OF-BAND USER MESSAGE — x]\n' + idless('yes') + '\n[/OUT-OF-BAND USER MESSAGE]',
         'display_kind': 'steer'}
    hist = [row, {'role': 'assistant', 'content': ''}, {'role': 'tool', 'content': 'r1'}, s]
    api(e, hist, 'wake')
    hist += [{'role': 'assistant', 'content': ''}, {'role': 'tool', 'content': 'r2'}, dict(s)]
    api(e, hist, 'wake')
    assert humans(e) == 2, f'counted {humans(e)}'
