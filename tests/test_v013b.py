"""Resume-hook shapes and bounded review-follow-up regressions."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from heartbeat2 import cache, commands
from heartbeat2.intake import Intake
from heartbeat2.model import Session
from heartbeat2.schedule import local_day
from heartbeat2.store import Store
from test_engine import engine, arm, pending, session, start, admit, KEY

# Identical gateway/run.py:1030 build_resume_recovery_note at all three pins:
# v0.21.6 818c13be, v0.21.7 71d34bad, main 04eb00ce.
NOTE = ("[System note: The previous turn was interrupted by a gateway restart; "
        "the gateway is now back online. Any restart/shutdown command in the history "
        "has already run — do NOT re-execute or verify it. Report to the user that "
        "the session was restored successfully and ask what they would like to do next. "
        "Do NOT re-execute old tool calls — skip any unfinished work from the conversation history.]")


def interrupted(engine, **changes):
    w = arm(engine, **changes)
    engine.tick()
    start(engine)
    o, = engine.store.list_occurrences(w.id)
    engine.interrupt(o, 'restart')
    engine.intake = Intake(engine)  # New process; no flights or owner admission.
    return engine.store.get_wake(w.id), engine.store.get_occurrence(w.id, o.dedupe_key)


def replay(engine, w, o, note=NOTE, history=None, turn='resumed'):
    text = f'⟦pulse o:{w.id}/{o.dedupe_key} a:{o.attempt}⟧\nWake instructions'
    # V013 pre_llm_call: six settled rows, interrupted USER, internal note USER.
    if history is None:
        history = [{'role':'user', 'content':'owner'}, {'role':'assistant','content':'reply'},
                   {'role':'user','content':'old wake'}, {'role':'assistant','content':''},
                   {'role':'tool','content':'{}'}, {'role':'assistant','content':'done'},
                   {'role':'user','content':text},
                   {'role':'user','content':note,'display_kind':'internal_notification'}]
    return engine.intake.pre_llm_call(session_id='sid', turn_id=turn, platform='telegram',
        is_first_turn=False, user_message=note, conversation_history=history)


@pytest.mark.parametrize('pin', ['v0.21.6', 'v0.21.7', 'main'])
def test_resume_pin_shaped_sequence_silent_accounted(engine, monkeypatch, pin):
    w, o = interrupted(engine)
    before = engine.daily(KEY)['model_turns']
    replay(engine, w, o)
    replay(engine, w, o)  # A duplicate hook is not another model turn.
    assert engine.intake.transform_llm_output(session_id='sid', turn_id='resumed', response_text='Restored') == '[SILENT]'
    assert engine.tool.handle({'action':'respond','outcome':'progress','notify':True}, session_id='sid')['error'] == 'not_in_flight'
    engine.intake.pre_api_request(session_id='sid', turn_id='resumed', conversation_history=[
        {'role':'user', 'content':f'⟦pulse o:{w.id}/{o.dedupe_key} a:1⟧\n\n'+NOTE}])
    engine.intake.post_api_request(session_id='sid', turn_id='resumed', platform='telegram', api_call_count=1)
    engine.intake.on_session_end(session_id='sid', turn_id='resumed', completed=True, failed=False,
                                 interrupted=False, turn_exit_reason='text_response(finish_reason=stop)')
    assert engine.store.get_occurrence(w.id, o.dedupe_key) == o
    changed = engine.store.get_wake(w.id)
    assert changed.fires == w.fires and changed.delivered == 0 and changed.model_turns == w.model_turns+1
    assert engine.daily(KEY)['model_turns'] == before+1
    assert engine.store.get_ledger(local_day(engine.clock.now(), engine.policy.resolved_timezone()), w.id)['model_turns'] == before+1
    monkeypatch.setattr(commands, 'session_env', lambda n: KEY if n == 'HERMES_SESSION_KEY' else '')
    assert 'resumed_after_restart' in commands.handle('why '+w.id, SimpleNamespace(get_engine=lambda:engine))


@pytest.mark.parametrize('owner_note', [NOTE, NOTE+'\n\nOwner asks a real question'], ids=['note-owner','note-plus-owner'])
def test_resume_owner_dispatch_never_silenced(engine, owner_note):
    w, o = interrupted(engine)
    admit(engine, 'owner-note', owner_note)
    replay(engine, w, o, note=owner_note)
    assert engine.intake.transform_llm_output(session_id='sid', response_text='Owner reply') is None
    assert len([r for r in engine.store.pending_events() if r['kind'] == 'human_message']) == 1
    assert engine.store.get_wake(w.id).model_turns == w.model_turns


@pytest.mark.parametrize('note', [NOTE, NOTE+'\n\nOwner asks a question'], ids=['note-only','note-plus-owner'])
def test_resume_pending_human_plus_owner_text_counted_once(engine, note):
    w, o = interrupted(engine)
    # New session intake: dispatch has no session yet and writes pending_human.
    engine.store.connection.execute('DELETE FROM sessions WHERE session_key=?', (KEY,))
    source = SimpleNamespace(platform=SimpleNamespace(value='telegram'),chat_type='dm',chat_id='unit',is_bot=False)
    gateway = SimpleNamespace(_session_key_for_source=lambda _:KEY)
    engine.intake.pre_gateway_dispatch(event=SimpleNamespace(internal=False, source=source,
        message_id='pending-owner', text='Owner asks a question'), gateway=gateway)
    engine.store.upsert_session(Session(KEY,'telegram','dm','unit','sid'))
    replay(engine, w, o, note=note)
    replay(engine, w, o, note=note)
    assert engine.intake.transform_llm_output(session_id='sid', response_text='Owner reply') is None
    events = engine.store.connection.execute("SELECT COUNT(*) FROM events WHERE kind='human_message'").fetchone()[0]
    assert events == 1


@pytest.mark.parametrize('case', ['claimed','started','no_marker','foreign_key','foreign_sid','assistant_after','owner_suffix','attempt_mismatch'])
def test_resume_nonmatching_conditions_unchanged(engine, case):
    w, o = interrupted(engine)
    history = None
    note = NOTE
    if case in {'claimed','started'}:
        engine.store.connection.execute('UPDATE occurrences SET state=? WHERE wake_id=?',(case,w.id))
    elif case == 'no_marker':
        history = [{'role':'user','content':'ordinary task'}, {'role':'user','content':NOTE,'display_kind':'internal_notification'}]
    elif case == 'foreign_key':
        engine.store.connection.execute('UPDATE wakes SET session_key=? WHERE id=?',('other',w.id))
    elif case == 'foreign_sid':
        engine.store.cas_wake(w.id,w.status,armed_session_id='other-sid')
    elif case == 'assistant_after':
        history = [{'role':'user','content':f'⟦pulse o:{w.id}/{o.dedupe_key} a:1⟧'},
                   {'role':'assistant','content':'already answered'}, {'role':'user','content':NOTE}]
    elif case == 'owner_suffix': note += '\nOwner text without dispatch'
    elif case == 'attempt_mismatch': o = replace(o, attempt=o.attempt+1)
    replay(engine,w,o,note=note,history=history)
    assert engine.intake.transform_llm_output(session_id='sid', response_text='Normal') is None
    assert engine.store.get_wake(w.id).model_turns == w.model_turns


@pytest.mark.parametrize('edit,field', [({'until':{'expires':'in 2d'}},'expires_at'),
    ({'until':{'max_fires':2}},'max_fires'), ({'until':{'owner_replies':True}},'cancel_on_reply'), ({'babysit':True},'babysit')])
def test_agent_lifetime_edit_revokes_owner_status(engine, edit, field):
    w = arm(engine,'every',creator='user',trigger_json={'kind':'every','every':'1h'})
    result = engine.tool.handle({'action':'update','id':w.id,**edit}, session_id='sid')
    assert result['ok'] and engine.store.get_wake(w.id).agent_edited


@pytest.mark.parametrize('field,value', [('expires_at',None),('max_fires',2),('cancel_on_reply',True),('babysit',True)])
def test_owner_command_lifetime_edit_preserves_owner_status(engine, monkeypatch, field, value):
    prepare = engine.tool.prepare
    # /pulse exposes no arbitrary lifetime edit syntax. Exercise its shared
    # prepare seam, keeping the actual owner command and persisted fields.
    def edited(wake, args, **kwargs):
        result = prepare(wake,args,**kwargs)
        return replace(result,wake=replace(result.wake,**{field:value}))
    monkeypatch.setattr(engine.tool,'prepare',edited)
    monkeypatch.setattr(commands,'session_env',lambda n: KEY if n=='HERMES_SESSION_KEY' else '')
    assert 'active' in commands.handle('add every 1h Owner wake',SimpleNamespace(get_engine=lambda:engine))
    w, = engine.store.list_wakes(KEY)
    assert getattr(w,field)==value and not w.agent_edited


@pytest.mark.parametrize('usage',[{}, {'completion_tokens':5}, SimpleNamespace(output_tokens=5)])
def test_empty_usage_unpriced_without_cache_classification(engine, monkeypatch, usage):
    session(engine,last_prompt_tokens=1234)
    w=arm(engine); engine.tick(); start(engine)
    calls=[]
    monkeypatch.setattr(cache,'cost',lambda *a,**k: calls.append(k) or 0)
    engine.intake.post_api_request(session_id='sid',turn_id='turn',usage=usage,
        started_at=engine.clock.now(),provider='openai',model='gpt-6.1-sol')
    o,=engine.store.list_occurrences(w.id)
    assert (o.api_calls,o.unpriced_calls,o.warm_wakes,o.cold_wakes,o.cost_microusd)==(1,1,0,0,0)
    assert engine.store.get_session(KEY).last_prompt_tokens==1234 and not calls


def test_pricing_ttl_uses_call_route_mode_platform(engine, monkeypatch):
    session(engine,last_route='old|old|old',last_cache_api_mode='old-mode',last_cache_touch_at=engine.clock.now()+10)
    arm(engine); engine.tick(); start(engine)
    ttls=[]; costs=[]
    def ttl(route,**kwargs):
        ttls.append((route,kwargs['api_mode'],kwargs['platform'])); return 3600,'test'
    monkeypatch.setattr(cache,'cache_ttl_for',ttl)
    monkeypatch.setattr(cache,'cost',lambda *a,**k: costs.append(k['ttl']) or .1)
    engine.intake.post_api_request(session_id='sid',turn_id='turn',usage={'input_tokens':100},
        started_at=engine.clock.now(),provider='new',model='claude-new',base_url='https://new.example',api_mode='new-mode')
    assert ttls[-1]==('new|claude-new|https://new.example','new-mode','telegram') and costs==[3600]


def test_cache_observation_only_after_cas_success(engine, monkeypatch):
    session(engine,last_route='route',last_cache_touch_at=engine.clock.now()-100)
    before=engine.store.get_session(KEY)
    monkeypatch.setattr(engine.store,'cas_session',lambda *a,**k:False)
    cache.touch(engine.store,before,started_at=engine.clock.now(),route='route',prompt=100,read=100)
    assert not engine.store.get_meta('cache_routes')


def test_cache_routes_read_modify_write_is_transactional(engine, monkeypatch):
    session(engine,last_route='route',last_cache_touch_at=engine.clock.now()-100)
    checks=[]
    get=engine.store.get_meta; set_=engine.store.set_meta
    def get_meta(key):
        if key=='cache_routes': checks.append(engine.store.connection.in_transaction)
        return get(key)
    def set_meta(key,value):
        if key=='cache_routes': checks.append(engine.store.connection.in_transaction)
        return set_(key,value)
    monkeypatch.setattr(engine.store,'get_meta',get_meta); monkeypatch.setattr(engine.store,'set_meta',set_meta)
    cache.touch(engine.store,engine.store.get_session(KEY),started_at=engine.clock.now(),route='route',prompt=100,read=100)
    assert checks==[True,True]


@pytest.mark.parametrize('creator,expected',[('user','cold'),('agent','warm')])
def test_first_wake_cache_age_owner_only(engine, monkeypatch, creator, expected):
    session(engine,last_route='openai|gpt-6.1|api.openai.com',last_cache_touch_at=engine.clock.now()-1700)
    monkeypatch.setattr(cache,'cost',lambda *a,**k:None)
    w=arm(engine,'every',creator=creator,created_in='command' if creator=='user' else 'human_turn')
    assert '5m '+expected in engine.cache_line(w,seconds=300)


def test_probe_failed_checks_cas_has_no_effect(engine, monkeypatch):
    w=arm(engine,'url',trigger_json={'kind':'url','url':'https://example.com'})
    cas=engine.store.cas_wake
    monkeypatch.setattr(engine.store,'cas_wake',lambda *a,**k:False if 'checks' in k else cas(*a,**k))
    engine._probe_result(w,{'fire':True,'identity':'changed','state':{'new':True}})
    assert not engine.store.list_occurrences(w.id)
    assert not engine.store.get_ledger(local_day(engine.clock.now(),engine.policy.resolved_timezone()),w.id)
    assert engine.store.get_wake(w.id).probe_state_json is None
