"""v0.1.2 acceptance: deterministic gateway/storage proof, no model or network."""
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import importlib.util
import json
import subprocess
import sys
import types
import pytest

from heartbeat2 import commands
from heartbeat2.model import Wake
from heartbeat2.schedule import local_day
from test_engine import engine, arm, pending, session, start, admit, KEY


@pytest.fixture
def hermes_helpers(monkeypatch):
    """Unit stub; real helpers exercised separately at both pinned trees."""
    for name in ('agent','agent.agent_runtime_helpers','agent.prompt_caching','agent.session_source'):
        monkeypatch.setitem(sys.modules,name,types.ModuleType(name))
    helpers=sys.modules['agent.agent_runtime_helpers']
    helpers.raw='1h'
    helpers._raw_cache_ttl_from_config=lambda default: helpers.raw
    helpers.cache_ttl_means_disabled=lambda raw: raw in ('off',None,False)
    helpers.configured_cache_ttl=lambda: helpers.raw if helpers.raw in ('5m','1h') else None
    helpers.anthropic_prompt_cache_policy=lambda route: ('claude' in route.model and route.api_mode=='anthropic_messages',True)
    caching=sys.modules['agent.prompt_caching']
    caching.effective_cache_ttl=lambda ttl,**kwargs: ttl or '5m'
    caching.auto_cache_ttl_for_source=lambda source: '5m' if source in {'cron','subagent','oneshot','webhook','kanban','api','tool','batch'} else '1h'
    sys.modules['agent.session_source'].session_source_for=lambda platform: platform or 'cli'
    return helpers


@pytest.mark.parametrize('creator', ['agent', 'user'])
def test_mute_holds_watchers(engine, creator):
    w = arm(engine, 'subagent_done', creator=creator, agent_edited=creator=='user')
    session(engine, unanswered_count=2)
    o = pending(engine, w, 'e:match')
    assert engine.gate(w, o, engine.store.get_session(KEY))[:2] == ('held_unanswered', True)
    engine.dispatch(o)
    held = engine.store.get_occurrence(w.id, o.dedupe_key)
    assert held.state == 'pending' and held.error == 'held_unanswered'
    assert held.matched_at == held.held_since == engine.clock.now()
    assert not engine.ctx.calls


@pytest.mark.parametrize('kind,creator,trigger', [('every','agent',{}), ('owner_silent','user',{'builtin':True})])
def test_mute_drops_nudges(engine, kind, creator, trigger):
    engine.policy = replace(engine.policy, owner_silent_checkin=True)
    session(engine, unanswered_count=2, last_human_at=engine.clock.now()-90000)
    w = arm(engine, kind, creator=creator, trigger_json=trigger)
    o = pending(engine, w)
    engine.dispatch(o)
    assert engine.store.get_occurrence(w.id, o.dedupe_key).error == 'unanswered'
    assert engine.store.get_occurrence(w.id, o.dedupe_key).state == 'skipped'


@pytest.mark.parametrize('creator,expected', [('user','claimed'), ('agent','skipped')])
def test_caps_owner_at_bypasses_agent_at_drops(engine, creator, expected):
    engine.policy = replace(engine.policy, max_model_wakes_per_day=0, max_messages_per_day=0)
    w = arm(engine, creator=creator)
    engine.tick()
    assert engine.store.list_occurrences(w.id)[0].state == expected
    assert len(engine.ctx.calls) == int(creator == 'user')


@pytest.mark.parametrize('date,hours', [('2026-03-08T00:30:00',22.5), ('2026-11-01T00:30:00',24.5)])
@pytest.mark.parametrize('cap', ['max_model_wakes_per_day', 'max_messages_per_day'])
def test_watcher_cap_holds_to_local_day_dst(engine, date, hours, cap):
    engine.clock.wall = datetime.fromisoformat(date).replace(tzinfo=ZoneInfo('America/New_York')).timestamp()
    engine.policy = replace(engine.policy, timezone='America/New_York', **{cap:1})
    w = arm(engine, 'subagent_done')
    engine.ledger(w, **{'model_turns' if cap=='max_model_wakes_per_day' else 'delivered':1})
    o = pending(engine, w, 'e:match')
    reason, defer, until = engine.gate(w, o, engine.store.get_session(KEY))
    assert (reason, defer) == ('held_daily_cap', True)
    assert until is None
    engine.dispatch(o)
    assert not engine.store.get_meta('not_before:'+o.id)
    engine.clock.advance(hours*3600-1)
    engine.dispatch(engine.store.get_occurrence(w.id, o.dedupe_key))
    assert not engine.ctx.calls
    engine.clock.advance(1)
    engine.dispatch(engine.store.get_occurrence(w.id, o.dedupe_key))
    assert len(engine.ctx.calls) == 1


def test_held_matches_coalesce_and_owner_reply_releases_once(engine):
    session(engine, unanswered_count=2)
    w = arm(engine, 'subagent_done', noop_streak=4)
    first = engine.clock.now()
    for n in range(3):
        engine.store.add_event('subagent_done', f'match{n}', {'summary':str(n)}, session_key=KEY)
        engine.tick()
        engine.clock.advance(1)
    rows = engine.store.list_occurrences(w.id)
    assert len(rows) == 1 and rows[0].held_matches == 3
    assert rows[0].matched_at == first and rows[0].payload_json == [{'summary':'2'}]
    assert engine.store.get_wake(w.id).noop_streak == 4
    assert not engine.ctx.calls
    admit(engine, text='thanks')
    engine.tick()
    engine.tick()
    assert len(engine.ctx.calls) == 1
    assert '3 matches merged' in engine.ctx.calls[0][0] and 'Held since' in engine.ctx.calls[0][0]


@pytest.mark.parametrize('ending', ['expires_at','max_fires'])
def test_expired_held_preserves_match(engine, ending):
    session(engine, unanswered_count=2)
    w = arm(engine, 'subagent_done', expires_at=engine.clock.now()+10)
    o = pending(engine, w, 'e:match')
    engine.dispatch(o)
    first = engine.store.get_occurrence(w.id, o.dedupe_key).matched_at
    if ending == 'expires_at': engine.clock.advance(11)
    else: engine.store.cas_wake(w.id,'active',max_fires=1,fires=1)
    engine.tick()
    result = engine.store.get_occurrence(w.id, o.dedupe_key)
    assert result.error == 'expired_held' and result.state in {'skipped','superseded'}
    assert result.matched_at == first and not result.delivered
    assert not engine.ctx.calls


@pytest.mark.parametrize('failed,api_calls', [(False,0), (True,0), (True,2), (False,2)])
def test_no_verdict_and_model_cap_refunds_only_zero_call_provider_failure(engine, failed, api_calls):
    w = arm(engine)
    engine.tick()
    start(engine)
    for _ in range(api_calls):
        engine.intake.post_api_request(session_id='sid',turn_id='turn',usage={'prompt_tokens':10})
    if failed:
        engine.intake.api_request_error(session_id='sid',turn_id='turn',reason='server_error',status_code=503)
    engine.intake.on_session_end(session_id='sid',turn_id='turn',completed=not failed,failed=failed)
    counted = int(not failed or api_calls > 0)
    assert engine.daily(KEY)['model_turns'] == counted
    assert engine.store.get_wake(w.id).model_turns == counted
    assert engine.store.get_wake(w.id).no_verdict == 1
    row = engine.store.get_ledger(local_day(engine.clock.now(),'UTC'),w.id)
    assert row['no_verdict'] == 1
    assert row['model_turns'] == counted
    engine.intake.on_session_end(session_id='sid',turn_id='turn',completed=True)
    assert engine.store.get_wake(w.id).no_verdict == 1


def test_no_fire_probe_checks_are_not_gate_skips(engine):
    w = arm(engine, 'file', trigger_json={'check_every':60})
    engine.probe_runner = lambda _: {'fire':False,'state':{'offset':0}}
    engine.tick()
    result = engine.store.get_wake(w.id)
    row = engine.store.get_ledger(local_day(engine.clock.now(),'UTC'), w.id)
    assert result.gated_skips == 0 and row.get('gated_skips',0) == 0
    assert result.checks == row['checks'] == 1


def test_hold_visibility_and_last_ten_occurrences(engine, monkeypatch):
    monkeypatch.setattr(commands,'session_env',lambda name: KEY if name=='HERMES_SESSION_KEY' else '')
    runtime = SimpleNamespace(get_engine=lambda:engine)
    session(engine, unanswered_count=2)
    w = arm(engine, 'subagent_done')
    for n in range(11): engine.store.add_occurrence(w.id, f'old:{n}',state='skipped')
    o = pending(engine,w,'e:hold')
    engine.dispatch(o)
    output = commands.handle('why '+w.id, runtime)
    assert 'old:0' not in output and 'old:1 ' not in output
    for text in ('e:hold','pending','held: waiting for your reply','matched','held-since','delivered no'):
        assert text in output
    assert 'Held items: 1' in commands.handle('status',runtime)
    assert 'no_verdict' in commands.handle('ledger',runtime)


@pytest.mark.parametrize('setting,platform,ttl', [('5m','telegram',300),('1h','telegram',3600),('auto','telegram',3600),('auto','cron',300),('off','telegram',None)])
def test_claude_ttl_resolution(hermes_helpers, setting, platform, ttl):
    from heartbeat2.cache import cache_ttl_for
    hermes_helpers.raw=setting
    assert cache_ttl_for('anthropic|claude-opus-5.5|api.anthropic.com',platform=platform,api_mode='anthropic_messages')[0] == ttl


@pytest.mark.parametrize('route,ttl', [('openai|gpt-6.1-sol|api.openai.com',1800),('openai|gpt-5.6-sol|api.openai.com',1800),('openai|gpt-5.5|api.openai.com',None),('zai|glm-5.3|api.z.ai',None)])
def test_openai_and_unknown_ttls(route, ttl):
    from heartbeat2.cache import cache_ttl_for
    assert cache_ttl_for(route)[0] == ttl


def test_learned_ttl_same_session_same_route_bounded(engine):
    from heartbeat2 import cache
    route='zai|glm-5.3|api.z.ai'
    t=engine.clock.now()
    def touch(at, read, route=route):
        cache.touch(engine.store,engine.store.get_session(KEY),started_at=at,route=route,prompt=100,read=read)
    touch(t,0)
    touch(t+1620,99)
    assert cache.cache_ttl_for(route,store=engine.store)==(None,'unknown')
    assert 'warm seen ≤27m' in cache.line(engine.store,engine.store.get_session(KEY),t+1620)
    touch(t+1620+8340,0)
    assert cache.cache_ttl_for(route,store=engine.store)==(1620,'learned')
    touch(t+1620+8340+1000,0)
    assert cache.cache_ttl_for(route,store=engine.store)==(None,'unknown')
    touch(t+20000,99,'other|glm|other.example')
    assert 'other|glm|other.example' not in json.loads(engine.store.get_meta('cache_routes'))
    touch(t+19999,0)
    assert engine.store.get_session(KEY).last_cache_touch_at == t+20000


def test_hermes_import_missing_falls_through(engine, monkeypatch):
    from heartbeat2.cache import cache_ttl_for
    monkeypatch.setitem(sys.modules,'agent.agent_runtime_helpers',None)
    route='anthropic|claude-test|api.anthropic.com'
    engine.store.set_meta('cache_routes',json.dumps({route:{'max_warm_gap':100,'min_cold_gap':200}}))
    assert cache_ttl_for(route,store=engine.store)==(100,'learned')


@pytest.mark.parametrize('seconds,expected', [(3419,3419),(3420,3420),(3600,3420),(4500,3420),(4501,4501)])
def test_snap_boundaries(seconds, expected):
    from heartbeat2.cache import interval
    from heartbeat2.schedule import next_every
    w=Wake('unit','task','every')
    assert interval(w,seconds,3600,900)==expected
    assert next_every(0,seconds,cache_ttl=3600,minimum_s=900)==expected


@pytest.mark.parametrize('creator,kind,trigger', [('user','every',{}),('agent','cron',{'cron':'0 * * * *'}),('agent','every',{'at':'09:00'})])
def test_owner_cron_anchor_never_snapped(engine, hermes_helpers, creator, kind, trigger):
    from heartbeat2.cache import interval, touch
    touch(engine.store,engine.store.get_session(KEY),started_at=engine.clock.now(),route='anthropic|claude-opus-5.5|api.anthropic.com',prompt=100,read=100,api_mode='anthropic_messages')
    w=Wake(KEY,'task',kind,creator=creator,trigger_json=trigger,base_interval_s=3600,eff_interval_s=3600)
    assert interval(w,3600,3600,900)==3600
    assert engine.timing(w)['cache_ttl'] is None
    assert engine.policy.clamp(w,cache_ttl=3600).wake.eff_interval_s==3600


@pytest.mark.parametrize('jitter', [-1,-.5,0,.5,1])
def test_cache_jitter_downward_and_floors_win(jitter):
    from heartbeat2.schedule import next_every
    from heartbeat2.cache import interval
    w=Wake('unit','task','self',pacing_min_s=3500)
    assert interval(w,3600,3600,900)==3600
    assert 3500<=next_every(0,3600,cache_ttl=3600,minimum_s=3500,jitter_unit=jitter)<=3600
    assert next_every(0,3000,cache_ttl=3600,jitter_unit=jitter)<=3000
    assert next_every(0,5000,cache_ttl=3600,jitter_unit=jitter)==5000*(1+.1*jitter)


def test_governor_warm_ladder_and_cold_regime(engine, hermes_helpers):
    from heartbeat2 import cache, governor
    engine.policy=replace(engine.policy,agent_ungated_floor_minutes=15)
    cache.touch(engine.store,engine.store.get_session(KEY),started_at=engine.clock.now(),route='anthropic|claude-opus-5.5|api.anthropic.com',prompt=100,read=100,api_mode='anthropic_messages')
    w=arm(engine,'every',base_interval_s=900,eff_interval_s=900)
    sequence=[]
    for n in range(6):
        engine.store.cas_wake(w.id,'active',noop_streak=n)
        governor.apply(engine,engine.store.get_wake(w.id))
        sequence.append(engine.store.get_wake(w.id).eff_interval_s/60)
    engine.store.cas_wake(w.id,'active',noop_streak=6)
    governor.apply(engine,engine.store.get_wake(w.id))
    assert sequence[:5]==[15,15,15,30,57]
    assert sequence[5]==57 and engine.store.get_wake(w.id).status=='paused'
    cold=arm(engine,'every',base_interval_s=7200,eff_interval_s=7200,noop_streak=3)
    governor.apply(engine,cold)
    assert engine.store.get_wake(cold.id).eff_interval_s==14400


def test_cache_line_bounded_appended_and_pricing_failure_omits(engine, hermes_helpers, monkeypatch):
    from heartbeat2 import cache
    cache.touch(engine.store,engine.store.get_session(KEY),started_at=engine.clock.now()-3120,route='anthropic|claude-opus-5.5|api.anthropic.com',prompt=120000,read=120000,api_mode='anthropic_messages')
    monkeypatch.setattr(cache,'cost',lambda *a,**k: .05 if k.get('read') else .99)
    w=arm(engine)
    text=engine.cache_line(w)
    assert len(text)<=220 and len(text.split())<=40 and 'warm' in text and '$0.99' in text
    engine.tick()
    assert engine.ctx.calls[-1][0].splitlines()[-1] == text
    monkeypatch.setattr(cache,'cost',lambda *a,**k: (_ for _ in ()).throw(ValueError('pricing')))
    assert engine.cache_line(w)==''


def test_usage_split_and_human_cache_touch(engine, monkeypatch):
    from heartbeat2 import cache
    monkeypatch.setattr(cache,'cost',lambda *a,**k: .125)
    route=dict(provider='openai',model='gpt-6.1-sol',base_url='https://user:unused@api.openai.com/v1?ignored=1',api_mode='codex_responses')
    usage=dict(prompt_tokens=100,output_tokens=2,cache_read_tokens=70,cache_write_tokens=20)
    engine.intake.pre_llm_call(session_id='sid',turn_id='human',user_message='Hello',platform='telegram')
    engine.intake.post_api_request(session_id='sid',turn_id='human',usage=usage,started_at=engine.clock.now()-52,**route)
    s=engine.store.get_session(KEY)
    assert s.last_route == 'openai|gpt-6.1-sol|api.openai.com'
    assert s.last_cache_touch_at==engine.clock.now()-52 and s.last_cache_hit_ratio==.7 and s.last_prompt_tokens==100
    assert s.last_activity_at==engine.clock.now()
    engine.intake.on_session_end(session_id='sid',turn_id='human',completed=True)
    w=arm(engine)
    engine.tick(); start(engine)
    for n in range(2): engine.intake.post_api_request(session_id='sid',turn_id='turn',usage=usage,started_at=engine.clock.now()+n,**route)
    engine.intake.on_session_end(session_id='sid',turn_id='turn',completed=True)
    o=engine.store.list_occurrences(w.id)[0]
    row=engine.store.get_ledger(local_day(engine.clock.now(),'UTC'),w.id)
    for record in (o,engine.store.get_wake(w.id)):
        assert (record.tokens_in,record.cache_read_tokens,record.cache_write_tokens,record.tokens_out)==(200,140,40,4)
        assert record.warm_wakes==1 and record.cold_wakes==0 and record.cost_microusd==250000
    assert row['cache_read_tokens']==140 and row['cache_write_tokens']==40
    assert 'warm 1 / cold 0' in cache.display(row) and '$0.25' in cache.display(row)


def test_store_additive_migration_from_v010(tmp_path, clock):
    from dataclasses import fields
    from heartbeat2.model import Occurrence, Session
    from heartbeat2.store import Store, CACHE_FIELDS
    package=tmp_path/'v010'; package.mkdir()
    (package/'__init__.py').write_text('')
    for name in ('store','model','schedule','clock'):
        (package/(name+'.py')).write_bytes(subprocess.check_output(['git','show',f'06c188b:heartbeat2/{name}.py']))
    spec=importlib.util.spec_from_file_location('v012_old_fixture',package/'__init__.py',submodule_search_locations=[str(package)])
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    oldstore=__import__(spec.name+'.store',fromlist=['Store'])
    oldmodel=__import__(spec.name+'.model',fromlist=['Wake'])
    path=tmp_path/'old.db'
    with oldstore.Store(path,clock) as old:
        w=old.create_wake(oldmodel.Wake('unit','old note','at'))
        old.add_occurrence(w.id,'old:match');old.upsert_session(oldmodel.Session('unit'))
        old.claim(w.id,'old:match',epoch='v010',pid=1);old.start(w.id,'old:match',attempt=1)
        old.finish(w.id,'old:match',attempt=1,tokens_in=100)
        old.add_ledger('2026-10-13',w.id,tokens_in=100,fires=1)
    with Store(path,clock) as upgraded:
        for table,cls in (('wakes',Wake),('occurrences',Occurrence),('sessions',Session)):
            columns={r[1] for r in upgraded.connection.execute(f'PRAGMA table_info({table})')}
            assert {f.name for f in fields(cls)}<=columns
        assert CACHE_FIELDS|{'checks','no_verdict'}<={r[1] for r in upgraded.connection.execute('PRAGMA table_info(ledger)')}
        assert upgraded.get_wake(w.id).note=='old note' and upgraded.get_occurrence(w.id,'old:match').state=='done'
        assert upgraded.get_occurrence(w.id,'old:match').tokens_in==100 and upgraded.get_occurrence(w.id,'old:match').api_calls==1
        assert upgraded.get_ledger('2026-10-13',w.id)['tokens_in']==100 and upgraded.get_meta('schema_version')=='1'
    with Store(path,clock) as reopened: assert reopened.get_wake(w.id).note=='old note'


def test_pending_burst_becomes_one_hold_and_keeps_latest(engine):
    session(engine,unanswered_count=2)
    w=arm(engine,'subagent_done')
    for n in range(45): engine.store.add_event('subagent_done',f'burst{n}',{'summary':str(n)},session_key=KEY)
    engine.tick()
    o,=engine.store.list_occurrences(w.id,state='pending')
    assert o.held_matches==45 and o.payload_json==[{'summary':'44'}]
    assert engine.store.get_wake(w.id).noop_streak==0


def test_daily_hold_releases_with_unchanged_cap_at_midnight(engine):
    engine.policy=replace(engine.policy,max_messages_per_day=1)
    engine.clock.wall=datetime.fromisoformat('2026-03-08T23:59:00+00:00').timestamp()
    w=arm(engine,'subagent_done')
    engine.ledger(w,delivered=1)
    o=pending(engine,w,'e:held')
    engine.dispatch(o)
    assert not engine.ctx.calls
    engine.clock.advance(60)
    engine.dispatch(engine.store.get_occurrence(w.id,o.dedupe_key))
    assert len(engine.ctx.calls)==1 and engine.policy.max_messages_per_day==1


def test_cache_tool_consumers_and_owner_result(engine, hermes_helpers, monkeypatch):
    from heartbeat2 import cache
    monkeypatch.setattr(cache,'cost',lambda *a,**kw: None)
    cache.touch(engine.store,engine.store.get_session(KEY),started_at=engine.clock.now(),route='anthropic|claude-opus-5.5|api.anthropic.com',prompt=100,read=100,api_mode='anthropic_messages')
    created=engine.tool.handle({'action':'create','note':'Task','trigger':{'kind':'self','every':'1h'}})
    assert created['wake']['eff_interval_s']==3420 and created['wake']['next_due_at']==engine.clock.now()+3420
    assert 'warm' in created['cache']
    updated=engine.tool.handle({'action':'update','id':created['wake']['id'],'trigger':{'kind':'self','every':'75m'}})
    assert updated['wake']['eff_interval_s']==3420 and 'warm' in updated['cache']
    assert 'cache' in engine.tool.handle({'action':'list'})['wakes'][0]
    engine.clock.advance(3420);engine.tick();start(engine)
    verdict=engine.tool.handle({'action':'respond','outcome':'no_change','notify':False,'next_check':'1h'},session_id='sid')
    assert verdict['ok']
    o=engine.store.list_occurrences(created['wake']['id'])[0]
    assert o.next_check_s==3420
    owner=Wake(KEY,'task','every',creator='user',base_interval_s=3600,eff_interval_s=3600,trigger_json={'every':'1h'})
    candidate=engine.tool.prepare(owner,{})
    assert candidate.wake.eff_interval_s==3600 and 'cold' in engine.cache_line(owner,seconds=3600)


@pytest.mark.parametrize('setting,platform,tip', [('5m','telegram',True),('auto','cron',True),('auto','telegram',False)])
def test_status_exact_cache_tip(engine, hermes_helpers, monkeypatch, setting, platform, tip):
    from heartbeat2 import cache
    hermes_helpers.raw=setting
    cache.touch(engine.store,engine.store.get_session(KEY),started_at=engine.clock.now(),route='anthropic|claude-opus-5.5|api.anthropic.com',prompt=100,read=0,api_mode='anthropic_messages')
    # Auto source is independent of the messaging destination, just as in Hermes.
    monkeypatch.setattr(sys.modules['agent.session_source'],'session_source_for',lambda _: platform)
    monkeypatch.setattr(commands,'session_env',lambda name: KEY if name=='HERMES_SESSION_KEY' else '')
    out=commands.handle('status',SimpleNamespace(get_engine=lambda:engine))
    exact='your Claude cache lasts 5 min, so every wake is cold; set `prompt_caching: {cache_ttl: auto}` (1h for chat) in config.yaml'
    assert (exact in out)==tip
