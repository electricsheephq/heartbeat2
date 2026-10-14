"""Final pre-registration and runner-amendment regressions, using fakes only."""
from copy import deepcopy
from datetime import date, datetime
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import pytest
from heartbeat2.model import Wake
from bench.soak import schedule
from bench.soak.evidence import frozen_state
from bench.soak.runner import epoch, Runner
from bench.soak.score import score, matching
from test_b3_runner import rig
from test_b3_score import corpus


@pytest.fixture(autouse=True)
def bounded_fake_clock(monkeypatch):
    from test_b3_runner import FakeClock
    sleep=FakeClock.sleep;counts={}
    def bounded(self,n):
        counts[id(self)]=counts.get(id(self),0)+1
        assert counts[id(self)] < 6000, 'runner did not finish within the fake-day budget'
        return sleep(self,n)
    monkeypatch.setattr(FakeClock,'sleep',bounded)


def test_exact_rng_order_and_round_up(monkeypatch):
    real = random.Random
    streams = {}
    class Traced:
        def __init__(self, seed): self.rng = real(seed); self.calls = streams.setdefault(seed, [])
        def sample(self, values, n): self.calls.append(('sample', list(values), n)); return self.rng.sample(values, n)
        def shuffle(self, values): self.calls.append(('shuffle', list(values))); return self.rng.shuffle(values)
        def choice(self, values): self.calls.append(('choice', list(values))); return self.rng.choice(values)
        def randint(self, a, b): self.calls.append(('randint', a, b)); return self.rng.randint(a, b)
    monkeypatch.setattr(schedule.random, 'Random', Traced)
    s = schedule.build(date(2026, 10, 15), 'Asia/Tokyo')
    g = streams['20261014:0']
    assert [c[0] for c in g] == ['sample', 'choice', 'choice', 'shuffle', 'shuffle', 'choice']
    for day in range(1, 8):
        es = [e for e in s['episodes'] if e['day'] == day]
        calls = streams[f'20261014:{day}']; assert calls[0][0] == 'shuffle' and 'S' not in calls[0][1]
        expected = [calls[0]]
        rng = real(f'20261014:{day}'); kinds = list(calls[0][1]); rng.shuffle(kinds)
        for i, e in enumerate(es):
            kind = e['kind']
            if kind == 'R-clock':
                raw = rng.randint(20, 90); expected.append(('randint', 20, 90))
                assert epoch(e['due'][0])-epoch(e['sends'][0]['at']) == (raw+(-(datetime.fromisoformat(e['sends'][0]['at']).minute+raw))%5)*60
            elif kind == 'R-rel': rng.choice([15,30,45,60]); expected.append(('choice', [15,30,45,60]))
            elif kind == 'W': rng.randint(10,30); expected.append(('randint',10,30))
            elif kind.startswith('C'): rng.choice([30,45,60]); expected.append(('choice',[30,45,60]))
            if i < len(es)-1: rng.randint(5,30); expected.append(('randint',5,30))
        assert calls == expected
    rng = real('20261014:0'); rng.sample(range(1,8),4)
    for _ in range(2): rng.choice(['R-clock','R-rel'])
    tasks = list(schedule.TASKS); rng.shuffle(tasks)
    texts = list(schedule.N_TEXTS); rng.shuffle(texts); texts.append(rng.choice(texts))
    assert [e['sends'][0]['text'] for e in s['episodes'] if e['kind']=='N'] == texts


def test_hashseed_process_determinism():
    code = "from bench.soak.schedule import build,canonical; from datetime import date; print(canonical(build(date(2026,10,15),'Asia/Tokyo')))"
    results = [subprocess.run([sys.executable, '-B', '-c', code], env=dict(os.environ, PYTHONHASHSEED=seed), capture_output=True, text=True, check=True).stdout for seed in ('1','98765')]
    assert results[0] == results[1]


@pytest.mark.parametrize('kind', ['W','R-rel','C-silent'])
def test_cleanup_every_episode(tmp_path, kind):
    r,c,store,e = rig(tmp_path,kind)
    assert r.run() == 0
    assert not r.pulse.wakes('chat') or all(w['status'] != 'active' for w in r.pulse.wakes('chat'))
    setup = [x['action'] for x in r.rows if x['kind']=='setup']
    assert 'archive' in setup and 'verify_enabled' in setup
    if kind != 'C-silent': assert 'cancel' in setup
    store.close()


def test_fresh_baseline_wakes_appearing_during_setup_quiet_and_archive(tmp_path):
    r,c,store,e = rig(tmp_path,'N')
    original = c.send
    def send(text):
        m = original(text)
        if text == '/approve':
            for status in ('active','proposed','paused'):
                store.create_wake(Wake(session_key='chat',kind='at',note='old',id='old-'+status,status=status))
        return m
    c.send = send
    store.set_meta('quiet:chat', json.dumps({'range':'off'}))
    store.cas_session('chat',{'enabled':True}, enabled=False, unanswered_count=2)
    assert r.run() == 0
    texts = [text for _,text in c.sent]
    assert texts.index('/new') < texts.index('/pulse quiet default') < texts.index(e['sends'][0]['text'])
    assert all(store.get_wake('old-'+status).status == 'cancelled' for status in ('active','proposed','paused'))
    baseline = next(row for row in r.rows if row['kind']=='baseline_setup')
    assert baseline['effective_quiet'] == '22:00-08:00'
    assert baseline['session']['unanswered_count']==0 and baseline['session']['enabled']
    assert baseline['transcript_sha256'] == schedule.digest(baseline['starting_transcript'])
    assert baseline['session_id'] != baseline['old_session_id']
    store.close()


@pytest.mark.parametrize('problem', ['injection','quiet','used_day','wrong_date','revision'])
def test_baseline_fails_closed(tmp_path, problem):
    r,c,store,e = rig(tmp_path,'N')
    if problem == 'injection':
        p=r.home/'config.yaml'; p.write_text(p.read_text().replace('allow_gateway_injection: true','allow_gateway_injection: false'))
    elif problem == 'quiet':
        p=r.home/'config.yaml'; p.write_text(p.read_text().replace('timezone: Asia/Tokyo','timezone: Asia/Tokyo, quiet_hours: "21:00-08:00"'))
    elif problem == 'wrong_date': r.clock.wall += 86400
    elif problem == 'revision': r.hermes_version_file = None
    else:
        store.create_wake(Wake(session_key='chat',kind='at',note='old',id='old'))
        store.add_ledger(datetime.fromtimestamp(r.clock.now(),r.zone).date().isoformat(),'old',delivered=1)
    assert r.run() == 1
    assert r.has('setup_failed', None)
    assert not any(text==e['sends'][0]['text'] for _,text in c.sent)
    store.close()


def test_w_flip_waits_for_recorded_baseline_and_fixed_window(tmp_path):
    r,c,store,e=rig(tmp_path,'W')
    r.clock.wall=epoch(e['sends'][0]['at']); r.active=e; r.tick_episode(e); r.clock.sleep(15); r.turns()
    w=store.list_wakes('chat')[0]
    r.clock.wall=epoch(e['flip'])+120
    store.cas_wake(w.id,'active',probe_state_json={'size':8})
    r.tick_episode(e)
    assert not r.has('file_write',e['id'],'flip')
    baseline=next(x for x in r.rows if x['kind']=='baseline')
    r.clock.sleep(60); r.tick_episode(e)
    flip=next(x for x in r.rows if x['kind']=='file_write' and x.get('key')=='flip')
    assert epoch(flip['t'])==epoch(baseline['t'])+60
    assert not r.has('shift',e['id'])
    assert r.has('w_window',e['id'])
    window=next(x for x in r.rows if x['kind']=='w_window')
    assert window['latest_close']==epoch(flip['t'])+1800
    store.close()


def test_w_no_baseline_is_capture_failure_not_void(tmp_path):
    r,c,store,e=rig(tmp_path,'W'); r.clock.callback=lambda:None
    assert r.run()==0
    assert r.has('w_baseline_missing',e['id']) and not r.has('void',e['id'])
    assert not r.has('file_write',e['id'],'flip')
    store.close()
    s,log,v=corpus()
    for e in [e for e in s['episodes'] if e['kind']=='W'][:2]:
        log.append(dict(kind='w_baseline_missing',episode=e['id'],t=e['latest_close']))
    assert score(s,log,v)['bars']['B3-1']=='FAIL'


def test_m_serialization_and_timeout(tmp_path):
    r,c,store,e=rig(tmp_path/'serial','M')
    assert r.run()==0
    sends=[x for x in r.rows if x['kind']=='send' and x['purpose']=='request']
    for previous,current in zip(sends,sends[1:]):
        turn=next(x for x in r.rows if x['kind']=='turn' and x['message_id']==previous['message']['id'])
        assert epoch(current['t'])==epoch(turn['t'])+60
    store.close()
    r,c,store,e=rig(tmp_path/'timeout','M')
    r.transcript.turn_done=lambda *a:False
    r.clock.wall=epoch(e['sends'][0]['at']); r.tick_episode(e); r.clock.wall += 300; r.tick_episode(e)
    assert r.has('void',e['id'])
    assert next(x for x in r.rows if x['kind']=='void')['reason']=='m_arming_timeout'
    assert len(c.sent)==1
    store.close()


def test_reply_closed_window_is_logged_and_not_sent(tmp_path):
    r,c,store,e=rig(tmp_path,'R-rel')
    r.deliveries=lambda e:[dict(visible_at=epoch(e['latest_close'])+1,message={'id':99})]
    r.clock.wall=epoch(e['latest_close'])+400
    r.replies(e)
    assert not c.sent and r.has('reply_skip',e['id'],'thanks:99')
    store.close()


def test_day8_preserves_texts_offsets_order_caps_and_single_replacement(tmp_path):
    r,c,store,e=rig(tmp_path,'N'); r.schedule=schedule.build(date(2026,10,15),'UTC')
    originals=[x for x in r.schedule['episodes'] if x['kind']=='W']
    for x in originals:r.emit('void',x['id'],reason='client_error')
    r.requeue(); row=next(x for x in r.rows if x['kind']=='requeue'); replacements=row['episodes']
    assert len(replacements)==5 and len(row['incomplete'])==2
    rng=random.Random('20261014:8')
    for i,(old,new) in enumerate(zip(originals,replacements)):
        assert new['id']==old['id']+'-r' and new['sends'][0]['text']==old['sends'][0]['text']
        assert epoch(new['flip'])-epoch(new['sends'][0]['at'])==epoch(old['flip'])-epoch(old['sends'][0]['at'])
        if i<len(replacements)-1:
            assert epoch(replacements[i+1]['sends'][0]['at'])-epoch(new['latest_close'])==rng.randint(5,30)*60
    for x in replacements:r.emit('void',x['id'],reason='client_error')
    r.requeue();assert len([x for x in r.rows if x['kind']=='requeue'])==1
    store.close()


@pytest.mark.parametrize('hour,minute,allowed',[(7,59,False),(8,0,True),(21,59,True),(22,0,False)])
def test_tokyo_guard_before_every_send(tmp_path,hour,minute,allowed):
    r,c,store,e=rig(tmp_path,'N')
    from zoneinfo import ZoneInfo
    r.zone=ZoneInfo('UTC')
    r.clock.wall=datetime(2026,10,15,hour,minute,tzinfo=ZoneInfo('Asia/Tokyo')).timestamp()
    r.send(e,'probe','probe')
    assert bool(c.sent)==allowed
    assert r.has('hours_guard',e['id'],'probe')== (not allowed)
    store.close()


def test_s_matching_all_recurring_bounds():
    s,log,v=corpus();e=next(e for e in s['episodes'] if e['kind']=='S')
    t=next(x for x in log if x['kind']=='turn' and x['episode']==e['id'] and x['key']=='request:0')
    w=t['snapshot']['wakes'][-1];sent=epoch(e['sends'][0]['at'])
    assert matching(w,e)
    for delta in (3299,6901): assert not matching(w|{'next_due_at':sent+delta},e)
    assert not matching(w|{'max_fires':2},e)
    assert not matching(w|{'expires_at':w['next_due_at']+7199},e)
    assert matching(w|{'max_fires':3,'expires_at':w['next_due_at']+7200},e)


def test_w_interval_term_and_all_delivery_packet_evidence():
    s,log,v=corpus()
    for e in [e for e in s['episodes'] if e['kind']=='W']:
        t=next(x for x in log if x['kind']=='turn' and x['episode']==e['id'] and x['key']=='request:0');w=t['snapshot']['wakes'][-1]
        assert not matching(w|{'trigger_json':w['trigger_json']|{'check_every':1501}},e,path=w['trigger_json']['path'])
        wt=next(x for x in log if x['kind']=='wake_turn' and x['occurrence']['wake_id']==w['id'])
        dm=next(x for x in log if x['kind']=='dm' and x['message']['text']=='DONE' and x['episode']==e['id'])
        wt['final_text']=dm['message']['text']=e['job']+' completed'
    for wt in [x for x in log if x['kind']=='wake_turn']:
        wt['rows'].insert(0,dict(role='tool',tool_name='read_file',content='observed value',tool_call_id='a'))
    result=score(s,log,v);assert result['bars']['B3-8']=='PASS'
    packets=[p for p in result['packets'] if p['id'].startswith('d-')]
    assert all('event_payloads' in p and p['tool_results'][0]['result']=='observed value' for p in packets)
    wt=next(x for x in log if x['kind']=='wake_turn');wt.pop('rows')
    assert score(s,log,v)['bars']['B3-8']=='INCOMPLETE'


def test_c_cancel_snapshot_precedes_cleanup():
    s,log,v=corpus();t=next(x for x in log if x['kind']=='turn' and x['key']=='cancel_reply')
    w=next(w for w in t['snapshot']['wakes'] if w['id'].startswith(t['episode']))
    w['status']='active'
    assert score(s,log,v)['bars']['B3-4']=='FAIL'
    t['snapshot']['wakes'].remove(w)
    assert score(s,log,v)['bars']['B3-4']=='INCOMPLETE'


def test_absolute_owner_requested_minimum_and_fail_precedence():
    s,log,v=corpus();ts=[x for x in log if x['kind']=='turn' and x['purpose']=='request' and any(w['owner_requested'] for w in x['snapshot']['wakes'])]
    matching_flags=[]
    for t in ts:
        old={w['id'] for w in t['before']['wakes']}
        for w in t['snapshot']['wakes']:
            if w['id'] not in old and w['kind']=='at':matching_flags.append(w)
    assert len(matching_flags)==21
    matching_flags[0]['owner_requested']=0;assert score(s,log,v)['bars']['B3-1']=='PASS'
    matching_flags[1]['owner_requested']=0;assert score(s,log,v)['bars']['B3-1']=='FAIL'
    s,log,v=corpus();t=next(x for x in log if x['kind']=='turn' and x['key']=='stop')
    next(w for w in t['snapshot']['wakes'] if w['id'].startswith(t['episode']))['status']='active'
    log=[x for x in log if x['kind']!='gate']
    log.append(dict(kind='abort',episode=None,t=t['t'],reason='frozen_state_changed'))
    assert score(s,log,v)['status']=='FAIL'


def test_hermes_revision_and_entry_are_frozen(tmp_path,monkeypatch):
    plugin=tmp_path/'plugins/heartbeat2';plugin.mkdir(parents=True)
    (plugin/'plugin.yaml').write_text('version: 0.1.15\n')
    cfg=tmp_path/'config.yaml';cfg.write_text('plugins: {entries: {heartbeat2: {allow_gateway_injection: true}}}\n')
    version=tmp_path/'version.txt';version.write_text('1.0\n')
    monkeypatch.setattr(subprocess,'run',lambda *a,**kw:subprocess.CompletedProcess(a,0,'a'*40+'\n'))
    a=frozen_state(tmp_path,tmp_path,version)
    assert a['hermes_version']=='1.0' and a['hermes_commit']=='a'*40 and a['allow_gateway_injection'] is True
    version.write_text('1.1\n');assert frozen_state(tmp_path,tmp_path,version)!=a
    version.write_text('1.0\n');cfg.write_text(cfg.read_text().replace('true','false'))
    b=frozen_state(tmp_path,tmp_path,version);assert b['settings_sha256']!=a['settings_sha256']


def test_runtime_revision_change_aborts(tmp_path,monkeypatch):
    r,c,store,e=rig(tmp_path,'N')
    r.clock.callback=lambda:r.hermes_version_file.write_text('changed\n')
    assert r.run()==3
    assert any(x['kind']=='abort' and x['reason']=='frozen_state_changed' for x in r.rows)
    store.close()


def test_runner_reads_only_configured_chat_and_readonly_sqlite(tmp_path,monkeypatch):
    from heartbeat2.model import Session
    from bench.soak.evidence import Pulse,Transcript
    import sqlite3
    r,c,store,e=rig(tmp_path,'N')
    store.upsert_session(Session(session_key='other',current_session_id='other'))
    store.create_wake(Wake(session_key='other',kind='at',note='private',id='other-wake'))
    opens=[];connect=sqlite3.connect
    def tracked(path,*a,**kw):opens.append((path,kw));return connect(path,*a,**kw)
    monkeypatch.setattr(sqlite3,'connect',tracked)
    pulse=Pulse(store.path);transcript=Transcript(r.home/'state.db')
    assert all('?mode=ro' in path and kw['uri'] for path,kw in opens)
    snap=pulse.snapshot('chat')
    assert [s['session_key'] for s in snap['sessions']]==['chat']
    assert not any(w['id']=='other-wake' for w in snap['wakes'])
    for reader in (pulse,transcript):
        with pytest.raises(sqlite3.OperationalError):reader.connection.execute('DELETE FROM sessions')
        reader.close()
    store.close()


def test_runner_has_no_profile_writes_or_hermes_invocation():
    import ast
    from bench.soak import runner,evidence,client
    for module in (runner,evidence,client):
        source=Path(module.__file__).read_text();tree=ast.parse(source)
        assert not any(isinstance(node,ast.Constant) and isinstance(node.value,str) and (node.value=='hermes' or '.hermes' in node.value) for node in ast.walk(tree))
    assert not any(isinstance(node,ast.Attribute) and node.attr in ('execute','executescript') for node in ast.walk(ast.parse(Path(runner.__file__).read_text())))


def test_cleanup_refusal_cannot_leave_an_active_wake(tmp_path):
    r,c,store,e=rig(tmp_path,'W');r.clock.wall=epoch(e['sends'][0]['at'])
    r.active=e;r.send(e,'request:0',e['sends'][0]['text'],'request');r.clock.sleep(15);r.turns()
    w=store.list_wakes('chat')[0]
    r.emit('send_intent',e['id'],key='setup_cancel:'+w.id,text='/pulse cancel '+w.id,purpose='setup')
    for n in (1,2): r.emit('send_intent',e['id'],key=f'setup_cancel:{w.id}:retry:{n}',text='/pulse cancel '+w.id,purpose='setup')
    assert r.cleanup(e) is False and r.has('setup_failed',None)
    assert next(x for x in r.rows if x['kind']=='setup_failed')['reason']=='cleanup_retry_exhausted'
    store.close()


def test_m_owner_after_gate_is_an_immediate_hard_failure():
    from bench.soak.score import hard_failure
    s,log,v=corpus();next(r for r in log if r['kind']=='m3_owner_check')['owner_after']=[123]
    assert hard_failure(log)=='B3-6'


def test_m_running_turn_void_finishes_cleanup_and_requeues(tmp_path):
    r,c,store,e=rig(tmp_path,'M');done=r.transcript.turn_done
    def stuck_request(sid,after_ts):
        if any(row['role']=='user' and row['timestamp']==after_ts and row['content'].startswith('remind me') for row in r.transcript.session_rows(sid)):return False
        return done(sid,after_ts)
    r.transcript.turn_done=stuck_request
    assert r.run()==0
    assert r.has('void',e['id']) and r.has('partial_arming',e['id'],'request:0')
    assert not r.has('turn',e['id'],'request:0') and r.has('episode_end',e['id'])
    assert sum(text.startswith('remind me') for _,text in c.sent)==1
    assert all(w['status']!='active' for w in r.pulse.wakes('chat'))
    r.requeue();replacement=next(row['episodes'][0] for row in r.rows if row['kind']=='requeue')
    assert replacement['id']==e['id']+'-r' and replacement['kind']=='M'
    store.close()
