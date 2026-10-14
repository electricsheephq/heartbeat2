"""B3R4 regressions: synthetic state and fake transport only."""
from copy import deepcopy
from datetime import datetime
import json
import sqlite3

import pytest

from heartbeat2.model import Occurrence, Wake
from bench.soak.client import ClientError
from bench.soak.evidence import display, hermes_revision
from bench.soak.runner import Runner, epoch
from bench.soak.schedule import digest, smoke_schedule
from bench.soak.score import score
from test_b3_r3 import shape_rig
from test_b3_score import corpus


def smoke(r, c):
    r.schedule = smoke_schedule(r.schedule['start'])
    e = r.schedule['episodes'][0]
    r.episodes = [e]; c.e = e
    r.clock.wall = epoch(e['sends'][0]['at']) - 120
    return e


def declare(r):
    r.frozen = r.state()
    r.emit('declaration', start=r.schedule['start'], tz=r.schedule['tz'], seed=r.schedule['seed'],
           schedule_sha256=r.schedule['sha256'], frozen=r.frozen, mode=r.schedule.get('mode'),
           smoke_at=r.schedule.get('smoke_at'))


def restart(r, c):
    other = Runner(r.schedule, r.path, c, r.home, r.soak_dir, 'chat', clock=r.clock,
                   pulse=r.pulse, transcript=r.transcript, until_day=r.until_day)
    other.state = r.state
    return other


def test_install_stamp_and_legacy_revision(tmp_path):
    stamp = tmp_path/'install-stamp.json'
    for content, expected in [(json.dumps({'baseVersion':'0.21.7', 'displayVersion':'label', 'source':'git', 'commit':'b'*40}, indent=2), '0.21.7'),
                              (json.dumps({'source':'git'}), None), ('version = "1.2.3"\n', '1.2.3'), ('2.0\n', '2.0')]:
        stamp.write_text(content)
        assert hermes_revision(tmp_path, stamp)['hermes_version'] == expected


@pytest.mark.parametrize('problem,reason', [('timezone','timezone_mismatch'), ('session','hermes_session_missing'),
    ('pulse_session','pulse_session_missing'), ('day','not_fresh_calendar_day'),
    ('injection','gateway_injection_disabled'), ('version','hermes_version_missing'),
    ('commit','hermes_commit_missing'), ('pending','pending_occurrences'), ('usage','start_day_usage')])
def test_readonly_preconditions_send_nothing(tmp_path, problem, reason):
    r,c,store,e = shape_rig(tmp_path, 'N')
    original = r.state
    if problem in ('timezone','injection','version','commit'):
        field, value = {'timezone':('resolved_timezone','UTC'), 'injection':('allow_gateway_injection',False),
                        'version':('hermes_version',None), 'commit':('hermes_commit',None)}[problem]
        r.state = lambda: original() | {field:value}
    elif problem == 'session': r.transcript.current_session = lambda _: None
    elif problem == 'pulse_session': r.pulse.session = lambda _: None
    elif problem == 'day': r.clock.wall += 86400
    else:
        w = store.create_wake(Wake('chat', 'prior', 'at', status='done'))
        if problem == 'pending': store._insert_record('occurrences', Occurrence(wake_id=w.id, dedupe_key='pending', state='started'))
        else: store.add_ledger(r.schedule['start'], w.id, delivered=1)
    assert r.run() == 1
    assert not c.sent
    assert next(x for x in r.rows if x['kind']=='setup_failed')['reason'] == reason
    store.close()


@pytest.mark.parametrize('mode', ['soak','smoke'])
@pytest.mark.parametrize('status', ['active','paused','proposed'])
def test_preexisting_owner_wakes_refused_before_reset(tmp_path, mode, status):
    r,c,store,e = shape_rig(tmp_path, 'N')
    if mode == 'smoke': smoke(r,c)
    w = store.create_wake(Wake('chat','owner wake','at',status=status))
    assert r.run() == 1 and not c.sent
    failed = next(x for x in r.rows if x['kind']=='setup_failed')
    assert failed['reason'] == 'preexisting_wakes' and failed['wake_ids'] == [w.id]
    assert store.get_wake(w.id).status == status
    assert r.transcript.current_session('chat') == 's'
    store.close()


@pytest.mark.parametrize('builtin,mode,ok', [(True,'soak',True),(False,'soak',False),(False,'smoke',True),(True,'smoke',True)])
def test_start_day_usage_recorded(tmp_path, builtin, mode, ok):
    r,c,store,e = shape_rig(tmp_path,'N')
    if mode == 'smoke': e=smoke(r,c)
    w=store.create_wake(Wake('chat','prior','owner_silent',status='active' if builtin else 'done',trigger_json={'builtin':True} if builtin else {}))
    store.add_ledger(r.schedule['start'],w.id,fires=1,noops=1)
    assert r.run() == (0 if ok else 1)
    row=next(x for x in r.rows if x['kind'] in ('baseline_setup','setup_failed') and 'usage' in x)
    assert row['usage'][0]['wake_id']==w.id
    if not ok: assert not c.sent and row['reason']=='start_day_usage'
    store.close()


@pytest.mark.parametrize('kind,builtin_delivered,ok', [('M',2,True),('M',3,False),('N',5,True),('N',6,False),('C-reply',5,True),('R-rel',5,False)])
def test_start_day_budget_costs_and_margin(tmp_path,kind,builtin_delivered,ok):
    r,c,store,e=shape_rig(tmp_path,kind)
    # Isolate day-1 budgeting even for kinds originally placed on later days.
    r.episodes[0]['day']=r.schedule['episodes'][0]['day']=1
    r.schedule['sha256']=digest({k:v for k,v in r.schedule.items() if k!='sha256'})
    w=store.create_wake(Wake('chat','builtin','owner_silent',trigger_json={'builtin':True}))
    store.add_ledger(r.schedule['start'],w.id,delivered=builtin_delivered)
    declare(r)
    r.setup_send=lambda *a: False  # Probe ends before any fake setup send.
    assert not r.baseline()
    failures=[x for x in r.rows if x['kind']=='setup_failed']
    assert bool(failures) == (not ok)
    if not ok: assert failures[0]['reason']=='start_day_budget'
    assert not c.sent
    store.close()


def test_today_ground_truth_preflight_no_transport(tmp_path):
    for mode in ('smoke','soak'):
        r,c,store,e=shape_rig(tmp_path/mode,'N')
        if mode=='smoke': smoke(r,c)
        store.cas_session('chat',{'unanswered_count':0},unanswered_count=1)
        builtin=store.create_wake(Wake('chat','builtin','owner_silent',trigger_json={'builtin':True}))
        store.add_ledger(r.schedule['start'],builtin.id,fires=1,noops=1)
        owner=store.create_wake(Wake('chat','earlier one shots','at',status='done',owner_requested=True))
        store.add_ledger(r.schedule['start'],owner.id,delivered=7)
        declare(r)
        reached=[]; r.setup_send=lambda *a: reached.append(True) or False
        assert not r.baseline() and not c.sent
        row=next(x for x in r.rows if x['kind'] in ('preflight','setup_failed') and 'usage' in x)
        assert row['builtin_usage'][0]['wake_id']==builtin.id
        assert row['delivered_today']==7 and row['max_messages_per_day']==6
        if mode=='smoke': assert reached and row['kind']=='preflight'
        else: assert not reached and row['reason']=='start_day_usage'
        store.close()


def test_smoke_five_minute_window():
    s=smoke_schedule('2026-10-15'); e=s['episodes'][0]
    assert e['sends'][0]['text']=='remind me in 5 minutes to check the smoke receipt'
    assert epoch(e['due'][0])-epoch(e['sends'][0]['at'])==300
    assert epoch(e['latest_close'])-epoch(e['due'][0])==900


def test_poll_only_distinct_messages_and_edits(tmp_path):
    r,c,store,e=shape_rig(tmp_path,'N')
    for i in range(111): c.message(f'old {i}',False,r.clock.now()-86400+i)
    for _ in range(40): r.poll(); r.clock.sleep(30)
    c.messages[-1]=c.messages[-1]|{'text':'edited','edit_date':datetime.fromtimestamp(r.clock.now()).astimezone().isoformat()}
    r.poll()
    assert sum(x['kind']=='dm' for x in r.rows)==101
    assert len(r.dm_latest)==100
    store.close()


@pytest.mark.parametrize('kind,index,late,missed', [('R-rel',0,29,False),('M',0,29,False),('M',2,29,True),('M',2,30,False)])
def test_gate_deadline_is_m3_only(tmp_path,kind,index,late,missed):
    r,c,store,e=shape_rig(tmp_path,kind)
    r.clock.wall=epoch(e['sends'][0]['at']);r.tick_episode(e);r.clock.sleep(15);r.turns()
    turn=next(t for t in r.rows if t['kind']=='turn'); turn['key']=f'request:{index}'
    w=store.list_wakes('chat')[0]; r.clock.wall=w.next_due_at-late
    r.evidence(e)
    assert r.has('gate_missed',e['id'],w.id)==missed
    assert r.has('gate',e['id'],w.id)==(not missed)
    store.close()


def test_active_evidence_before_poll(tmp_path):
    r,c,store,e=shape_rig(tmp_path); seen=[]
    original_poll=r.poll; original_evidence=r.evidence
    def poll():
        if r.active: seen.append('poll')
        return original_poll()
    def evidence(e): seen.append('evidence');return original_evidence(e)
    r.poll=poll;r.evidence=evidence
    assert r.run()==0
    assert all(seen[i-1]=='evidence' for i,x in enumerate(seen) if x=='poll')
    store.close()


def test_unresolved_episode_turn_voids_continues_and_requeues(tmp_path):
    r,c,store,e=shape_rig(tmp_path); original=c.row
    def row(role,text,at,mid=None,calls=None,cid=None):
        if role=='user' and text==e['sends'][0]['text']:at+=180
        return original(role,text,at,mid,calls,cid)
    c.row=row
    assert r.run()==0
    assert any(x['kind']=='void' and x['reason']=='turn_unresolved' for x in r.rows)
    assert not r.has('setup_failed',None) and r.has('episode_end',e['id'])
    assert restart(r,c).run()==0
    r.requeue();assert next(x for x in r.rows if x['kind']=='requeue')['episodes'][0]['replaces']==e['id']
    store.close()


@pytest.mark.parametrize('applied', [False,True])
def test_cleanup_transient_client_error_recovers(tmp_path,applied):
    r,c,store,e=shape_rig(tmp_path);original=c.send; failed=[]
    def send(text):
        if text.startswith('/pulse cancel ') and not failed:
            failed.append(True)
            if applied: original(text)
            raise ClientError('synthetic transient')
        return original(text)
    c.send=send
    assert r.run()==0
    assert any(x['kind']=='void' and x['reason']=='cleanup_client_error' for x in r.rows)
    assert not r.has('setup_failed',None)
    assert all(w['status']!='active' for w in r.pulse.wakes('chat'))
    assert restart(r,c).run()==0
    store.close()


def test_resume_uncertain_cleanup_reverifies_without_resend(tmp_path):
    r,c,store,e=shape_rig(tmp_path);declare(r);assert r.baseline()
    r.clock.wall=epoch(e['sends'][0]['at']);r.tick_episode(e);r.clock.sleep(15);r.turns()
    r.emit('close',e['id'],close_at=r.clock.now()); w=store.list_wakes('chat')[0]
    r.emit('send_intent',e['id'],key='setup_cancel:'+w.id,text='/pulse cancel '+w.id,purpose='setup')
    r.emit('client_error',e['id'],operation='send',key='setup_cancel:'+w.id)
    store.cas_wake(w.id,w.status,status='cancelled')
    count=len(c.sent);resumed=restart(r,c)
    assert resumed.run()==0 and len(c.sent)==count
    assert not resumed.has('void',e['id'])
    assert resumed.has('cleanup_verified',e['id'])
    store.close()


@pytest.mark.parametrize('markup,plain', [('## Reminder\n*call* _now_','Reminder\ncall now'),
    ('###### **Reminder**\n*call the bank*','Reminder\ncall the bank'),
    ('# Receipt\n_call_ snake_case','Receipt\ncall snake_case')])
def test_adapter_header_italic_transforms(markup,plain):
    assert display(markup)==display(plain)
    assert display('snake_case')=='snake_case'


def test_late_start_distinguishes_overrun(tmp_path):
    r,c,store,e=shape_rig(tmp_path,'N')
    r.clock.wall=epoch(e['sends'][0]['at'])+121
    r.emit('runner_start',resumed=False)  # Process started late.
    r.tick_episode(e);assert next(x for x in r.rows if x['kind']=='void')['reason']=='runner_stopped'
    store.close()
    r,c,store,e=shape_rig(tmp_path/'overrun','N')
    declare(r);assert r.baseline();r.emit('runner_start',resumed=False)
    r.emit('episode_end','previous',snapshot=r.snapshot())
    r.clock.wall=epoch(e['sends'][0]['at'])+121
    r.tick_episode(e)
    assert next(x for x in r.rows if x['kind']=='void')['reason']=='previous_episode_overran'
    assert not any(text==e['sends'][0]['text'] for _,text in c.sent)
    r.requeue(); assert next(x for x in r.rows if x['kind']=='requeue')['episodes']
    store.close()


def test_day8_margin_total_delivery_cost(tmp_path):
    r,c,store,e=shape_rig(tmp_path,'N');s,_,_=corpus();r.schedule=s
    candidates=[deepcopy(x) for x in s['episodes'] if x['kind'] in ('W','S')][:6]
    r.schedule['episodes']=candidates
    for x in candidates:r.emit('void',x['id'],reason='client_error')
    r.frozen=r.state();r.requeue(); row=next(x for x in r.rows if x['kind']=='requeue')
    assert len(row['episodes'])==5 and len(row['incomplete'])==1
    store.close()


def test_smoke_unexercised_bars_and_old_history(tmp_path):
    r,c,store,e=shape_rig(tmp_path);smoke(r,c)
    c.message('old background bot message',False,r.clock.now()-86400)
    assert r.run()==0
    result=score(r.schedule,r.rows)
    assert all(result['bars'][f'B3-{i}']=='NOT_EXERCISED' for i in (4,5,6))
    assert result['bars']['B3-3']=='PASS'
    assert result['metrics']['unrequested_messages_per_day']=={}
    store.close()


def test_s_on_only_when_off(tmp_path):
    r,c,store,e=shape_rig(tmp_path,'S');assert r.run()==0
    assert not any(text=='/pulse on' for _,text in c.sent)
    store.close()


def test_setup_command_mismatch_reason(tmp_path):
    r,c,store,e=shape_rig(tmp_path,'N');original=c.send
    def send(text):
        m=original(text)
        if text=='/pulse quiet default':c.messages[-1]['text']='Error: unavailable'
        return m
    c.send=send
    assert r.run()==1
    assert next(x for x in r.rows if x['kind']=='setup_failed')['reason']=='command_reply_or_state_mismatch'
    store.close()


@pytest.mark.parametrize('condition,reason', [('session','session_not_rotated'),('quiet','quiet_window_mismatch'),
    ('unanswered','unanswered_not_zero'),('enabled','chat_disabled'),('dirty','dirty_wakes')])
def test_postsend_failure_reasons(tmp_path,condition,reason):
    r,c,store,e=shape_rig(tmp_path,'N');declare(r)
    original=r.setup_send
    def setup_send(*args):
        ok=original(*args)
        if args[1]=='quiet':
            if condition=='session':r.transcript.current_session=lambda _: 's'
            elif condition=='quiet':store.set_meta('quiet:chat',json.dumps({'range':'off'}))
            elif condition=='unanswered':store.cas_session('chat',{'unanswered_count':0},unanswered_count=1)
            elif condition=='enabled':store.cas_session('chat',{'enabled':True},enabled=False)
            else:
                store.create_wake(Wake('chat','setup addition','at'))
                r.pulse.wakes=lambda _: []  # Missed by cancel safety net, caught by final inventory below.
                original_state=r.pulse.baseline_state
                def baseline_state(*a):
                    r.pulse.wakes=lambda _: [w.__dict__ | {'trigger_json':json.dumps(w.trigger_json)} for w in store.list_wakes('chat')]
                    return original_state(*a)
                r.pulse.baseline_state=baseline_state
        if args[1]=='on' and condition=='enabled':store.cas_session('chat',{'enabled':True},enabled=False)
        return ok
    r.setup_send=setup_send
    assert not r.baseline()
    assert next(x for x in r.rows if x['kind']=='setup_failed')['reason']==reason
    assert c.sent
    store.close()


def test_smoke_without_started_episode_has_no_pass_bars(tmp_path):
    r,c,store,e=shape_rig(tmp_path);smoke(r,c);declare(r)
    result=score(r.schedule,r.rows)
    assert set(result['bars'].values())=={'NOT_EXERCISED'}
    assert result['status']=='INCOMPLETE'
    store.close()


@pytest.mark.parametrize('state', ['claimed','started'])
def test_smoke_pending_is_send_free_failure(tmp_path,state):
    r,c,store,e=shape_rig(tmp_path);smoke(r,c)
    w=store.create_wake(Wake('chat','prior','at',status='done'))
    store._insert_record('occurrences',Occurrence(wake_id=w.id,dedupe_key='pending',state=state))
    assert r.run()==1 and not c.sent
    assert next(x for x in r.rows if x['kind']=='setup_failed')['reason']=='pending_occurrences'
    store.close()


def test_blob_columns_are_json_safe(tmp_path):
    """Deployed transcripts carry BLOB columns; every reader row must serialize into the JSON log."""
    import sqlite3
    from bench.soak.evidence import Transcript
    from bench.soak.schedule import canonical
    path = tmp_path/'state.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL, display_identity BLOB)')
        db.execute("INSERT INTO messages VALUES (1,'s','user','hi',1.0,?)", (bytes(range(32)),))
    rows = Transcript(path).session_rows('s')
    assert rows[0]['display_identity'] == bytes(range(32)).hex()
    canonical(rows)


def test_session_meta_row_does_not_hold_the_turn_open(tmp_path):
    """Deployed Hermes writes one 'session_meta' row after a fresh session's first final answer."""
    import sqlite3
    from bench.soak.evidence import Transcript
    path = tmp_path/'state.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL, tool_calls TEXT)')
        db.executemany('INSERT INTO messages VALUES (?,?,?,?,?,?)', [
            (1,'s','user','remind me in 5 minutes to check',100.0,None), (2,'s','assistant','',105.0,'[{"id":"c","function":{"name":"pulse","arguments":"{}"}}]'),
            (3,'s','tool','{"ok": true}',106.0,None), (4,'s','assistant','Armed.',110.0,None), (5,'s','session_meta','',111.0,None)])
    assert Transcript(path, now=lambda: 125.0).turn_done('s', 100.0)
    assert not Transcript(path, now=lambda: 115.0).turn_done('s', 100.0)  # Still inside the quiet period.


def test_r5_w_late_baseline_rejected(tmp_path):
    for offset in (-1, 0, 1):
        r,c,store,e=shape_rig(tmp_path/str(offset),'W')
        try:
            r.clock.callback=lambda: None
            r.clock.wall=epoch(e['sends'][0]['at'])
            r.tick_episode(e);r.clock.sleep(15);r.turns()
            w=store.list_wakes('chat')[0]
            assert not r.has('baseline',e['id'])
            r.clock.wall=epoch(e['flip'])+1800+offset
            store.cas_wake(w.id,w.status,probe_state_json={'size':8})
            r.evidence(e)
            assert r.has('baseline',e['id'],w.id)==(offset<0)
            assert r.has('w_baseline_missing',e['id'])==(offset>=0)
        finally:
            store.close()


def test_r5_s_first_delivery_window(tmp_path):
    for delay in (7200, None, 7201):
        r,c,store,e=shape_rig(tmp_path/str(delay),'S')
        try:
            r.clock.callback=lambda: None
            start=epoch(e['sends'][0]['at']);r.clock.wall=start
            r.tick_episode(e);r.clock.sleep(15);r.turns()
            w=store.list_wakes('chat')[0]
            r.deliveries=lambda _: ([] if delay is None else
                [dict(visible_at=start+delay,message={'id':99},wake_id=w.id)])
            # Skip the deadline poll to reproduce a delivery first read late.
            r.clock.wall=start+(7200 if delay is None else delay+300)
            r.tick_episode(e)
            stops=[at for at,text in c.sent if text=='stop checking in on me']
            assert stops==([start+7500] if delay==7200 else [])
            assert r.has('s_no_first_delivery',e['id'])==(delay!=7200)
            if delay!=7200:
                assert r.close_at(e)==start+7200
            if delay is None:
                marker=next(x for x in r.rows if x['kind']=='s_no_first_delivery')
                assert epoch(marker['t'])==start+7200
        finally:
            store.close()


def test_r5_hard_failure_cleanup_and_deadline(tmp_path,monkeypatch):
    for stalled in (False, True):
        r,c,store,e=shape_rig(tmp_path/str(stalled))
        try:
            c.no_delivery=True
            builtin=store.create_wake(Wake('chat','builtin','owner_silent',trigger_json={'builtin':True}))
            descendants=[]
            def hard_failure(*args):
                if not r.active or not r.has('turn',e['id'],'request:0'):
                    return None
                if not descendants:
                    for status in ('paused','proposed'):
                        descendants.append(store.create_wake(Wake('chat','synthetic descendant','at',status=status)).id)
                return 'synthetic_hard_failure'
            monkeypatch.setattr('bench.soak.score.hard_failure',hard_failure)
            original=c.send
            def send(text):
                if stalled and text.startswith('/pulse cancel '):
                    c.sent.append((r.clock.now(),text))
                    return c.message(text,True)  # No reply or state change.
                return original(text)
            c.send=send
            assert r.run()==2
            failure=next(x for x in r.rows if x['kind']=='hard_fail')
            assert failure['reason']=='synthetic_hard_failure'
            created={w['id'] for w in r.wakes(e,all_created=True)}
            assert set(descendants)<created and builtin.id not in created
            cancels=[text.split()[-1] for _,text in c.sent if text.startswith('/pulse cancel ')]
            assert cancels and builtin.id not in cancels
            assert store.get_wake(builtin.id).status=='active'
            if stalled:
                incomplete=next(x for x in r.rows if x['kind']=='hard_fail_cleanup_incomplete')
                assert epoch(incomplete['t'])-epoch(failure['t'])==900
                assert {w['id'] for w in incomplete['wakes']}==created
                assert any(w['status']=='active' for w in incomplete['wakes'])
                assert not r.has('episode_end',e['id'])
            else:
                assert set(cancels)==created
                assert all(store.get_wake(wid).status=='cancelled' for wid in created)
                end=next(x for x in r.rows if x['kind']=='episode_end')
                assert r.rows.index(failure)<r.rows.index(end)
                assert r.has('cleanup_verified',e['id'])
                assert not r.has('hard_fail_cleanup_incomplete',e['id'])
        finally:
            store.close()


def test_r5_preflight_version_and_registered_settings(tmp_path):
    for setting,reason in [('timezone',None),('version','plugin_version_too_old'),
                           ('max_messages_per_day','nondefault_settings')]:
        r,c,store,e=shape_rig(tmp_path/setting,'N')
        try:
            if setting=='version':
                (r.home/'plugins/heartbeat2/plugin.yaml').write_text('version: 0.1.14\n')
            elif setting=='max_messages_per_day':
                config=r.home/'config.yaml'
                config.write_text(config.read_text().replace('timezone: Asia/Tokyo',
                    'timezone: Asia/Tokyo, max_messages_per_day: 5'))
            assert r.run()==(1 if reason else 0)
            if reason:
                failed=next(x for x in r.rows if x['kind']=='setup_failed')
                assert failed['reason']==reason and not c.sent
            else:
                assert r.has('preflight',None) and not r.has('setup_failed',None)
                assert c.sent
        finally:
            store.close()


def test_r5_post_close_owner_message_does_not_void(tmp_path):
    for closed,message_offset in [(False,31),(True,31),(True,29)]:
        r,c,store,e=shape_rig(tmp_path/f'{closed}-{message_offset}','N')
        try:
            start=epoch(e['sends'][0]['at']);r.clock.wall=start+30
            r.active=e
            if closed:
                r.emit('close',e['id'],close_at=r.clock.now())
            c.message('synthetic owner message',True,start+message_offset)
            r.clock.wall=start+60;r.poll()
            assert r.has('dm',e['id'])
            foreign=[x for x in r.rows if x['kind']=='void' and x['reason']=='foreign_owner_message']
            assert bool(foreign)==(not closed)
        finally:
            store.close()
