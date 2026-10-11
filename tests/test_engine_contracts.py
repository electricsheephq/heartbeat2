"""Additional supported-path coverage beneath the named invariants."""
from dataclasses import fields, replace
from datetime import datetime
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from test_engine import engine, arm, session, start, end, GatewayCtx, KEY
from heartbeat2.model import Session
from heartbeat2.policy import Policy


def test_permission_matrix_and_proposals(engine):
    e = engine
    args = dict(action='create', note='n', trigger={'kind':'at','at':'+1h'})
    e.policy = replace(e.policy, agent_can_create='ask_first')
    result = e.tool.handle(args, session_id='sid')
    assert result['ok'] and result['wake']['status']=='proposed'
    wid = result['wake']['id']
    assert e.tool.handle(dict(action='resume',id=wid),session_id='sid')['error']=='needs_owner_ok'
    assert e.tool.handle(dict(action='update',id=wid,note='updated'),session_id='sid')['wake']['status']=='proposed'
    e.policy = replace(e.policy,agent_can_create='no',enabled=False)
    assert e.tool.handle(dict(action='pause',id=wid))['error']=='not_pausable'
    assert e.tool.handle(dict(action='cancel',id=wid))['ok']
    assert e.tool.handle(dict(action='off'))['ok']
    e.policy = replace(e.policy,enabled=True,agent_can_create='yes')
    assert e.tool.handle(args)['error']=='session_off'


def test_owner_items_and_cross_session_are_fenced(engine):
    e = engine
    w = arm(e,creator='user')
    e.tick(); start(e)
    assert e.tool.handle(dict(action='cancel',id=w.id),session_id='sid')['error']=='owner_item'
    other = e.store.create_wake(replace(w,id='w_other',session_key='other'))
    assert e.tool.handle(dict(action='pause',id=other.id),session_id='sid')['error']=='wrong_session'
    end(e)
    assert e.tool.handle(dict(action='pause',id=w.id),session_id='sid')['error']=='not_pausable'
    active=arm(e,'every',creator='user')
    assert e.tool.handle(dict(action='pause',id=active.id),session_id='sid')['ok']


def test_respond_last_wins_usage_success_only_scratch(engine):
    e = engine
    w = arm(e,'self',pacing_min_s=3600,pacing_max_s=7200)
    assert e.tool.handle(dict(action='respond',outcome='done',notify=True))['error']=='not_in_flight'
    e.tick(); start(e)
    assert e.tool.handle(dict(action='respond',outcome='progress',notify=True,summary='first',scratch='discard',next_check='1m'),session_id='sid')['ok']
    assert e.tool.handle(dict(action='respond',outcome='no_change',notify=False,summary='last',scratch='keep',next_check='2h'),session_id='sid')['ok']
    e.intake.post_api_request(session_id='sid',usage={'prompt_tokens':12,'completion_tokens':7})
    e.intake.transform_llm_output(response_text='chatty',session_id='sid')
    e.intake.on_session_end(session_id='sid',completed=True,failed=False)
    o=e.store.list_occurrences(w.id)[0]
    assert (o.summary,o.tokens_in,o.tokens_out)==('last',12,7)
    assert e.store.get_wake(w.id).scratch=='keep'
    assert e.store.get_wake(w.id).next_due_at==e.clock.now()+7200
    e.clock.advance(7200); e.tick(); start(e)
    e.tool.handle(dict(action='respond',outcome='progress',notify=True,scratch='failed'),session_id='sid')
    e.intake.on_session_end(session_id='sid',failed=True,completed=False)
    assert e.store.get_wake(w.id).scratch=='keep'
    assert e.store.list_occurrences(w.id)[-1].state=='interrupted'


def test_stale_ack_and_owner_reply_force_silent(engine):
    e = engine
    w = arm(e,cancel_on_reply=True)
    e.tick()
    text=e.ctx.calls[-1][0]
    e.clock.advance(1)
    e.intake.post_gateway_admission(session_key=KEY,platform='telegram',source={'chat_type':'dm','chat_id':'unit'},message_id='h',text='reply')
    start(e,message=text)
    assert e.intake.transform_llm_output(response_text='stale',session_id='sid')=='[SILENT]'
    e.intake.on_session_end(session_id='sid',completed=True)
    assert e.store.get_wake(w.id).status=='cancelled'
    assert not e.store.list_occurrences(w.id)[0].delivered


def test_human_presence_limits_and_commands_keep_owner_state(engine):
    e=engine
    arm(e)
    assert 'armed' in e.intake.pre_llm_call(session_id='sid',user_message='human')['context']
    e.intake.on_session_end(session_id='sid',completed=True)
    session(e,enabled=False,paused_until=e.clock.now()+100,first_notice_sent=True,unanswered_count=2)
    e.intake.post_gateway_admission(session_key=KEY,platform='telegram',source={'chat_type':'dm','chat_id':'unit'},message_id='h',text='hello')
    s=e.store.get_session(KEY)
    assert not s.enabled and s.first_notice_sent and s.paused_until
    assert s.unanswered_count==0
    e.policy=replace(e.policy,max_active=2)
    ctx=e.intake.pre_llm_call(session_id='sid',user_message='human')
    assert 'limits changed' in ctx['context']
    assert 'limits changed' not in e.intake.pre_llm_call(session_id='sid',user_message='again')['context']
    before=e.store.get_session(KEY).last_human_at
    e.clock.advance(10)
    assert e.intake.pre_command(surface='gateway',session_key=KEY,platform='telegram') is None
    assert e.store.get_session(KEY).last_human_at>before


def test_event_scope_filter_coalesce_and_deadline(engine):
    e=engine
    w=arm(e,'reaction',next_due_at=None,trigger_json={'emojis':['yes']})
    e.store.add_event('reaction','no',{'emojis':['no']},session_key=KEY)
    e.store.add_event('reaction','wrong',{'emojis':['yes']},session_key='other')
    e.store.add_event('reaction','yes1',{'emojis':['yes']},session_key=KEY)
    e.store.add_event('reaction','yes2',{'emojis':['yes']},session_key=KEY)
    session(e,busy_since=e.clock.now())
    e.tick()
    occ=e.store.list_occurrences(w.id)
    assert len(occ)==1 and len(occ[0].payload_json)==2
    assert not e.store.pending_events()
    d=arm(e,'file',next_due_at=None,deadline_at=e.clock.now()-1,trigger_json={'path':'/tmp/fake','check_every':60})
    e.tick()
    assert 'deadline passed' in str(e.store.list_occurrences(d.id)[0].payload_json)
    e.tick()
    assert len(e.store.list_occurrences(d.id))==1


def test_probe_stub_injected_results_fingerprint_and_pool(engine):
    e=engine
    w=arm(e,'file',next_due_at=e.clock.now(),trigger_json={'path':'/tmp/fake','check_every':60})
    e.probe_runner=lambda wake: {'fire':False,'state':{'size':1}}
    e.tick()
    assert not e.ctx.calls and e.store.get_wake(w.id).gated_skips==1
    assert e.store.get_wake(w.id).probe_state_json=={'size':1}
    e.clock.advance(60)
    e.probe_runner=lambda wake: {'fire':True,'identity':'v2','message':'changed','state':{'size':2}}
    e.tick()
    assert len(e.ctx.calls)==1
    assert e.store.get_wake(w.id).probe_state_json=={'size':2}
    start(e); end(e,'progress',True)
    e.clock.advance(600); e.tick()
    assert len(e.ctx.calls)==1


def test_governor_value_reset_self_floor_and_babysit(engine):
    e=engine
    w=arm(e,'self',pacing_min_s=3600,pacing_max_s=7200)
    for _ in range(3):
        e.tick(); start(e); end(e,next_check='1h')
        e.clock.advance(e.store.get_wake(w.id).eff_interval_s)
    assert e.store.get_wake(w.id).eff_interval_s==7200
    e.tick(); start(e); end(e,'progress',True,summary='useful')
    assert e.store.get_wake(w.id).noop_streak==0
    assert e.store.get_wake(w.id).eff_interval_s==3600
    session(e,last_wake_turn_at=None,unanswered_count=0)
    b=arm(e,'every',babysit=True)
    e.tick(); start(e); end(e,'needs_attention',True,summary='found')
    assert e.store.get_wake(b.id).status=='done'


def test_governor_ignored_engagement_and_ratio(engine):
    from heartbeat2 import governor
    e=engine
    e.policy=replace(e.policy,ignored_pause_after=1,engagement_window_hours=1)
    w=arm(e,'every')
    e.tick(); start(e); end(e,'progress',True)
    e.clock.advance(3601); e.tick()
    assert e.store.get_wake(w.id).status_reason=='governor:ignored'
    # The ratio rule is distinct from the consecutive-noop threshold.
    e.policy=replace(e.policy,noop_pause_after=50)
    r=arm(e,'every')
    for n in range(12):
        e.store.add_occurrence(r.id,f't:ratio{n}')
        e.store.claim(r.id,f't:ratio{n}',epoch='test',pid=1)
        e.store.start(r.id,f't:ratio{n}',attempt=1)
        e.store.finish(r.id,f't:ratio{n}',attempt=1,outcome='no_change',force_silent=True)
        governor.finish(e,e.store.get_wake(r.id),e.store.get_occurrence(r.id,f't:ratio{n}'))
    assert e.store.get_wake(r.id).status_reason=='governor:ratio'


def test_quiet_one_shot_defers_then_fires(engine):
    e=engine
    e.clock.wall=datetime.fromisoformat('2025-10-12T23:00:00+00:00').timestamp()
    e.policy=replace(e.policy,quiet_hours='22:00-08:00')
    w=arm(e)
    e.tick()
    assert not e.ctx.calls
    o=e.store.list_occurrences(w.id)[0]
    assert o.state=='pending' and o.error=='quiet_hours'
    e.clock.advance(9*3600); e.tick()
    assert len(e.ctx.calls)==1


def test_gateway_event_mapping_fail_open_and_manifest(engine):
    e=engine
    w=arm(e,'reaction',next_due_at=None)
    assert e.intake.gateway_platform_event(platform='telegram',event_type='reaction',payload={'chat_id':'unit','message_id':'1','emojis':['yes']}) is None
    e.tick()
    assert len(e.ctx.calls)==1
    assert e.intake.post_gateway_admission(source='malformed') is None
    import yaml
    data=yaml.safe_load((Path(__file__).resolve().parents[1]/'plugin.yaml').read_text())
    assert data['manifest_version']==2 and data['kind']=='standalone'
    config=data['config_schema']
    expected={f.name for f in fields(Policy)}-{'clock','profile_timezone'}
    assert set(config)==expected
    p=Policy()
    for key, value in config.items():
        assert value['default']==getattr(p,key) and value['label'] and value['description']
    assert config['agent_can_create']['choices']==['yes','ask_first','no']
