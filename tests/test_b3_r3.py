"""Public-safe deployed-shape fixture and SPEC-B3R3 regressions. No network."""
from copy import deepcopy
from datetime import datetime
import json
import re
import sqlite3
import pytest
from heartbeat2.model import Wake
from bench.soak.runner import epoch
from bench.soak.schedule import digest
from bench.soak.score import attribute, hard_failure, matching, score, text_matches
from test_b3_runner import FakeClient, rig
from test_b3_score import corpus


class ShapeClient(FakeClient):
    """Different id spaces, command-only replies, stale pulse session, real wake turns."""
    def row(self, role, text, at, mid=None, calls=None, cid=None):
        if calls:
            calls=deepcopy(calls)
            for call in calls:
                if call['function']['name']=='pulse':
                    call['function']=dict(name='tool_call',arguments=json.dumps({'calls':[dict(name='pulse',arguments=json.loads(call['function']['arguments']))]}))
        super().row(role, text, at, mid+9000 if mid and role == 'user' else None, calls, cid)

    def message(self, text, out, at=None):
        if not out:
            text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
            for token in ('**', '__', '`', '~~'): text = text.replace(token, '')
        return super().message(text, out, at)

    def send(self, text):
        if not text.startswith('/'):
            m = super().send(text)
            with sqlite3.connect(self.state) as db:
                db.execute("UPDATE messages SET content='**Set.**' WHERE session_id=? AND content='Set.'", (self.sid,))
            return m
        now = self.clock.now(); m = self.message(text, True); self.sent.append((now, text))
        if text == '/new': reply = '⚠️ **Confirm /new**\nApprove Once — reply `/approve`'
        elif text == '/approve':
            self.sid = 'fresh'
            with sqlite3.connect(self.state) as db:
                db.execute("UPDATE sessions SET ended_at=? WHERE id='s'", (now,))
                db.execute('INSERT INTO sessions VALUES (?,?,?,?)', (self.sid, 'chat', now, None))
            self.store.cas_session('chat', {'unanswered_count':self.store.get_session('chat').unanswered_count}, unanswered_count=0)
            reply = '✨ Session reset! Starting fresh.\n\nModel: fixture'
        elif text == '/pulse quiet default':
            self.store.set_meta('quiet:chat', '{}')
            reply = 'Quiet hours follow your settings again: 22:00-08:00'
        elif text == '/pulse on':
            self.store.cas_session('chat', {'enabled':self.store.get_session('chat').enabled}, enabled=True)
            reply = 'HeartBeat 2.0 on for this chat.'
        elif text.startswith('/pulse cancel '):
            w = self.store.get_wake(text.split()[-1]); self.store.cas_wake(w.id, w.status, status='cancelled')
            reply = w.id+' cancelled · synthetic wake'
        else: raise AssertionError(text)
        self.message(reply, False, now+1)
        return m



def shape_rig(tmp_path, kind='R-rel'):
    r, old, store, e = rig(tmp_path, kind)
    c = ShapeClient(r.clock, store, old.state, e, old.directory)
    r.client = c; r.clock.callback = c.tick
    return r, c, store, e


def test_deployed_shape_end_to_end(tmp_path):
    r, c, store, e = shape_rig(tmp_path)
    from bench.soak.schedule import smoke_schedule
    r.schedule=smoke_schedule(r.schedule['start']);e=r.schedule['episodes'][0];r.episodes=[e];c.e=e
    r.clock.wall=epoch(e['sends'][0]['at'])-120
    assert r.run() == 0
    assert [s for _, s in c.sent][:3] == ['/new', '/approve', '/pulse quiet default']
    assert not any('always' in s for _, s in c.sent)
    assert r.pulse.session('chat')['current_session_id'] == 's'
    turns = [x for x in r.rows if x['kind'] == 'turn']
    assert turns and all(x['session_id'] == 'fresh' for x in turns)
    assert next(t for t in turns if t['key']=='request:0')['pulse_results'][0]['result']['ok']
    assert all(str(x['message_id']) != x['owner']['platform_message_id'] for x in turns)
    assert len([x for x in r.rows if x['kind'] == 'command_reply']) == 4
    deliveries, unresolved = attribute(r.rows)
    assert len(deliveries) == 1 and unresolved == []
    assert deliveries[0]['rows'][0]['content'].startswith('⟦pulse')
    assert deliveries[0]['final_text']
    # Same scorer path; smoke scoring has one registered episode and no invented judge verdict.
    result = score(r.schedule, r.rows)
    assert result['bars']['B3-3'] == 'PASS'
    assert result['bars']['B3-7'] == result['bars']['B3-8'] == 'PENDING'
    assert len(result['packets']) == 2
    store.close()


@pytest.mark.parametrize('markup,display', [('**bold** __under__ `id` ~~gone~~', 'bold under id gone'),
    ('[label](https://example.test/a)\n```text\nhello\n```', 'label hello')])
def test_display_equality(markup, display):
    assert text_matches(display, markup)
    assert not text_matches(display[:3], markup)


def test_owner_text_time_and_session_not_id(tmp_path):
    r,c,store,e = shape_rig(tmp_path)
    r.clock.wall = epoch(e['sends'][0]['at']); m = c.send(e['sends'][0]['text'])
    owner = r.transcript.owner('s', m['text'], epoch(m['date']))
    assert owner and owner['platform_message_id'] != str(m['id'])
    assert r.transcript.owner('fresh', m['text'], epoch(m['date'])) is None
    assert r.transcript.owner('s', m['text'], epoch(m['date'])-121) is None
    assert r.transcript.owner('s', m['text'], epoch(m['date'])+6) is None
    store.close()


def test_command_mismatch_fails_closed(tmp_path):
    r,c,store,e = shape_rig(tmp_path, 'N'); original = c.send
    def send(text):
        m = original(text)
        if text == '/pulse quiet default': c.messages[-1]['text'] = 'Error: unavailable'
        return m
    c.send = send
    assert r.run() == 1
    assert next(x for x in r.rows if x['kind']=='setup_failed')['reason']=='command_reply_or_state_mismatch'
    assert not any(text == e['sends'][0]['text'] for _,text in c.sent)
    store.close()


@pytest.mark.parametrize('kind', ['M', 'R-rel'])
def test_gate_planned_45_seconds_and_m3_refuses_late(tmp_path, kind):
    r,c,store,e = rig(tmp_path,kind)
    r.clock.wall = epoch(e['sends'][0]['at']); r.tick_episode(e); r.clock.sleep(15); r.turns()
    w=store.list_wakes('chat')[0]
    due=w.next_due_at+1.3
    for t in r.by_episode[e['id']]:
        if t['kind']=='turn':
            next(x for x in t['snapshot']['wakes'] if x['id']==w.id)['next_due_at']=due
            if kind=='M': t['key']='request:2'
    r.clock.wall=due-44; r.evidence(e)
    gate=next(x for x in r.rows if x['kind']=='gate')
    assert epoch(gate['t']) <= due-30
    r.rows.remove(gate); r.index.discard(('gate',e['id'],w.id))
    r.clock.wall=due-29; r.evidence(e)
    assert len([x for x in r.rows if x['kind']=='gate']) == (0 if kind=='M' else 1)
    assert r.has('gate_missed',e['id'],w.id)==(kind=='M')
    store.close()


@pytest.mark.parametrize('kind,marker', [('S','s_no_first_delivery'),('W','w_baseline_missing')])
def test_close_latches_before_late_delivery_or_baseline(tmp_path,kind,marker):
    r,c,store,e = rig(tmp_path,kind); c.no_delivery=True; r.clock.callback=lambda:None
    r.clock.wall=epoch(e['sends'][0]['at']); r.tick_episode(e); r.clock.sleep(15); r.turns()
    r.clock.wall=epoch(e['sends'][0]['at'])+7200 if kind=='S' else epoch(e['flip'])+1800
    r.tick_episode(e); assert r.has(marker,e['id']) and r.has('close',e['id'])
    locked=r.close_at(e); w=store.list_wakes('chat')[0]
    store.cas_wake(w.id,w.status,probe_state_json={'size':8})
    r.deliveries=lambda e:[dict(visible_at=r.clock.now(),message={'id':99},wake_id=w.id)]
    r.clock.sleep(15);r.turns();r.tick_episode(e);r.clock.sleep(400);r.tick_episode(e)
    assert r.close_at(e)==locked
    assert not any(text in ('thanks','stop checking in on me') for _,text in c.sent)
    assert not r.has('file_write',e['id'],'flip')
    store.close()


def test_s_extra_wake_is_precision_only_and_before_stop_is_capture():
    s,logs,v=corpus(); t=next(x for x in logs if x['kind']=='turn' and x['key']=='stop')
    request=next(x for x in logs if x['kind']=='turn' and x['episode']==t['episode'] and x['key']=='request:0')
    request['snapshot']['wakes'].append(dict(id='extra',status='done',kind='at',note='extra'))
    t['snapshot']['wakes'].append(dict(id='extra',status='done',kind='at',note='extra'))
    pre=next(x for x in logs if x['kind']=='before_stop' and x['episode']==t['episode'])
    pre['wakes'].append(dict(id='extra',status='done',next_due_at=None))
    assert hard_failure(logs) is None
    result=score(s,logs,v);assert result['bars']['B3-2']=='FAIL' and result['bars']['B3-5']=='PASS'
    s,logs,v=corpus()
    for pre in [x for x in logs if x['kind']=='before_stop']: pre['wakes'][0]['next_due_at']=epoch(pre['t'])+7201
    result=score(s,logs,v);assert result['bars']['B3-1']=='FAIL' and result['bars']['B3-5']=='PASS'
    assert hard_failure(logs) is None


def test_w_missing_baseline_excluded_from_delivery():
    s,logs,v=corpus(); e=next(e for e in s['episodes'] if e['kind']=='W')
    logs=[x for x in logs if not (x['kind']=='wake_turn' and x['occurrence']['wake_id']==e['id']+'-0') and not (x['kind'] in ('dm','baseline','file_write') and x['episode']==e['id'] and (x.get('key')=='flip' or x['kind'] in ('baseline','dm')))]
    # Preserve its ordinary arming reply.
    at=epoch(e['sends'][0]['at'])+1
    logs.append(dict(kind='dm',episode=e['id'],t=e['sends'][0]['at'],message=dict(id=9900,date=datetime.fromtimestamp(at).astimezone().isoformat(),edit_date=None,out=False,text='Set.')))
    for r in logs:
        if r.get('snapshot'):r['snapshot']['occurrences']=[o for o in r['snapshot']['occurrences'] if o['wake_id']!=e['id']+'-0']
    logs.append(dict(kind='w_baseline_missing',episode=e['id'],t=e['latest_close']))
    result=score(s,logs,v)
    assert result['bars']['B3-1']==result['bars']['B3-3']=='PASS'
    assert result['bars']['B3-8']!='INCOMPLETE'


@pytest.mark.parametrize('before', [True, False])
def test_void_order_for_precision_and_missing_delivery(before):
    s,logs,v=corpus(); t=next(x for x in logs if x['kind']=='turn' and not x['pulse_results'] and x['purpose']=='request')
    t['snapshot']['wakes'].append(dict(id='extra',kind='at',status='active',note='extra'))
    at=epoch(t['t'])+(1 if before else -1)
    logs.append(dict(kind='void',episode=t['episode'],t=datetime.fromtimestamp(at).astimezone().isoformat(),reason='client_error'))
    result=score(s,logs,v);assert (result['bars']['B3-2']=='FAIL')==before
    s,logs,v=corpus();e=next(e for e in s['episodes'] if e['kind'].startswith('R'))
    logs=[x for x in logs if not (x['kind']=='wake_turn' and x['occurrence']['wake_id']==e['id']+'-0') and not (x['kind']=='dm' and x['episode']==e['id'] and x['message']['text'] not in ('Set.','Okay.'))]
    for r in logs:
        if r.get('snapshot'):r['snapshot']['occurrences']=[o for o in r['snapshot']['occurrences'] if o['wake_id']!=e['id']+'-0']
    at=epoch(e['latest_close'])+(1 if before else -1)
    logs.append(dict(kind='void',episode=e['id'],t=datetime.fromtimestamp(at).astimezone().isoformat(),reason='gateway_restart'))
    assert (score(s,logs,v)['bars']['B3-3']=='FAIL')==before


def test_stop_set_wording_is_not_arming_claim():
    s,logs,v=corpus()
    for t in [x for x in logs if x['kind']=='turn' and x['key']=='stop']:
        old=t['final_text'];t['final_text']="Done, I've cancelled the hourly check-in I set up for you."
        t['rows'][-1]['content']=t['final_text']
        for dm in logs:
            if dm['kind']=='dm' and dm['episode']==t['episode'] and dm['message']['text']==old:dm['message']['text']=t['final_text']
    assert score(s,logs,v)['bars']['B3-7']=='PASS'


@pytest.mark.parametrize('cron,good', [('0 * * * *',True),('30 * * * *',True),('0 9-21 * * *',True),('0 */2 * * *',False),('0,30 * * * *',False),('0 * * * 1',False),('0 9-10 * * *',False)])
def test_hourly_cron_across_observation(cron,good):
    s,logs,v=corpus();e=next(e for e in s['episodes'] if e['kind']=='S')
    w=next(x for x in logs if x['kind']=='turn' and x['episode']==e['id'] and x['key']=='request:0')['snapshot']['wakes'][-1]
    assert matching(w|{'kind':'cron','trigger_json':{'cron':cron}},e)==good


def test_late_start_void_and_requeue_without_send(tmp_path):
    r,c,store,e=rig(tmp_path,'R-clock');r.clock.wall=epoch(e['sends'][0]['at'])+7200
    r.tick_episode(e)
    assert not c.sent and r.has('void',e['id']) and r.has('episode_end',e['id'])
    r.requeue();assert next(x for x in r.rows if x['kind']=='requeue')['episodes'][0]['replaces']==e['id']
    store.close()


def test_day8_counts_all_deliveries(tmp_path):
    r,c,store,e=rig(tmp_path,'N');s,_,_=corpus();r.schedule=s
    candidates=[x for x in s['episodes'] if x['kind'] in ('R-clock','R-rel','M','W','S','C-silent')][:8]
    for x in candidates:r.emit('void',x['id'],reason='client_error')
    r.frozen=r.state()|{'max_messages_per_day':4};r.requeue()
    row=next(x for x in r.rows if x['kind']=='requeue')
    expected=lambda x:3 if x['kind']=='M' else 1
    assert sum(expected(x) for x in row['episodes'])<=3
    assert row['incomplete']
    store.close()


@pytest.mark.parametrize('field',['log','soak_dir'])
def test_profile_output_paths_refused(tmp_path,field):
    from bench.soak.runner import Runner
    r,c,store,e=rig(tmp_path,'N')
    args=dict(log=r.path,soak_dir=r.soak_dir);args[field]=r.home/'forbidden'
    with pytest.raises(ValueError,match='profile'):Runner(r.schedule,args['log'],c,r.home,args['soak_dir'],'chat',pulse=r.pulse,transcript=r.transcript)
    store.close()


def test_m_replies_by_request_not_ordinal(tmp_path):
    r,c,store,e=rig(tmp_path,'M')
    r.clock.wall=epoch(e['sends'][0]['at']);r.tick_episode(e);r.clock.sleep(15);r.turns()
    w=store.list_wakes('chat')[0];r.clock.wall=w.next_due_at+400
    r.deliveries=lambda e:[dict(wake_id=w.id,visible_at=w.next_due_at,message={'id':i}) for i in range(3)]
    r.replies(e);assert not any(text=='thanks' for _,text in c.sent)
    store.close()


def test_poll_chat_cursor_with_bounded_edit_overlap(tmp_path):
    r,c,store,e=rig(tmp_path,'N');calls=[]
    c.read=lambda after,limit:calls.append((after,limit)) or []
    r.last_seen=100000;r.poll()
    assert calls==[(100000,100),(None,100)]
    store.close()


@pytest.mark.parametrize('schedule_tz,plugin_tz', [('UTC','Asia/Tokyo'),('Asia/Tokyo','UTC')])
def test_setup_timezone_fails_closed(tmp_path,schedule_tz,plugin_tz):
    r,c,store,e=shape_rig(tmp_path,'N')
    r.schedule['tz']=schedule_tz;r.schedule['sha256']=digest({k:v for k,v in r.schedule.items() if k!='sha256'})
    original=r.state;r.state=lambda:original()|{'resolved_timezone':plugin_tz}
    assert r.run()==1 and not any(text==e['sends'][0]['text'] for _,text in c.sent)
    assert next(x for x in r.rows if x['kind']=='setup_failed')['reason']=='timezone_mismatch'
    store.close()


def test_cleanup_wake_created_inside_wake_turn_and_paused_proposed(tmp_path):
    r,c,store,e=shape_rig(tmp_path);original=c.tick;made=[]
    def tick():
        original()
        if c.deliveries and not made:
            for status in ('paused','proposed'):
                made.append(store.create_wake(Wake(session_key='chat',kind='at',note='synthetic descendant',status=status,created_at=r.clock.now(),parent_wake_id=c.pending[0][0] if c.pending else None)).id)
    r.clock.callback=tick
    assert r.run()==0
    assert all(store.get_wake(wid).status=='cancelled' for wid in made) and made
    store.close()


def test_blank_delivery_remains_unresolved():
    s,logs,v=corpus();dm=next(x for x in logs if x['kind']=='dm' and x['message']['text'] not in ('Set.','Okay.','Stopped.'))
    dm['message']['text']=''
    deliveries,unresolved=attribute(logs)
    assert not any(d['message']['id']==dm['message']['id'] for d in deliveries)
    assert any(u['message']['id']==dm['message']['id'] for u in unresolved)
    assert score(s,logs,v)['bars']['B3-3']=='INCOMPLETE'


def test_m_turn_timeout_stays_five_minutes(tmp_path):
    r,c,store,e=rig(tmp_path,'M');r.transcript.turn_done=lambda *a:False
    r.clock.wall=epoch(e['sends'][0]['at']);r.tick_episode(e)
    r.clock.wall+=299;r.tick_episode(e);assert not r.has('void',e['id'])
    r.clock.wall+=1;r.tick_episode(e);assert r.has('void',e['id'])
    store.close()


def test_late_m3_gate_is_incomplete():
    s,logs,v=corpus();gate=next(r for r in logs if r['kind']=='gate' and r.get('request_index')==2)
    gate['t']=datetime.fromtimestamp(gate['due']-29).astimezone().isoformat()
    assert score(s,logs,v)['bars']['B3-6']=='INCOMPLETE'


def test_every_arming_assistant_text_needs_display_evidence():
    s,logs,v=corpus();turn=next(r for r in logs if r['kind']=='turn' and r['key']=='request:0')
    turn['rows'].insert(0,dict(role='assistant',timestamp=turn['owner']['timestamp']+.5,content='Preparing your request.'))
    assert score(s,logs,v)['bars']['B3-7']=='INCOMPLETE'


def test_arming_boundary_is_delivery_not_execution_start():
    s,logs,v=corpus();wake_turn=next(r for r in logs if r['kind']=='wake_turn')
    eid=wake_turn['occurrence']['wake_id'].rsplit('-',1)[0]
    reply=next(r for r in logs if r['kind']=='dm' and r['episode']==eid and r['message']['text']=='Set.')
    reply['message']['date']=datetime.fromtimestamp(wake_turn['occurrence']['started_at']+1).astimezone().isoformat()
    assert not any(u['message']['id']==reply['message']['id'] for u in attribute(logs)[1])
    assert score(s,logs,v)['bars']['B3-7']=='PASS'


def test_occurrence_marker_is_exact_not_sql_wildcard(tmp_path):
    r,c,store,e=shape_rig(tmp_path);r.clock.wall=epoch(e['sends'][0]['at']);c.send(e['sends'][0]['text'])
    wid,due,_=c.pending[0];c.row('user',f'⟦pulse o:{wid.replace("_","X")}/t:{due} a:1⟧',due)
    c.row('assistant','wrong wake',due+1)
    assert r.transcript.occurrence_turn('s',dict(wake_id=wid,started_at=due,ended_at=due+2))==[]
    store.close()


def test_attribution_work_is_window_bounded():
    s,logs,v=corpus();reads=[]
    class CountedTurn(dict):
        def get(self,key,*args):
            if key=='owner':reads.append(key)
            return super().get(key,*args)
        def __getitem__(self,key):
            if key=='owner':reads.append(key)
            return super().__getitem__(key)
    logs=[CountedTurn(r) if r['kind']=='turn' else r for r in logs]
    deliveries,unresolved=attribute(logs)
    assert len(deliveries)==26 and not unresolved
    assert len(reads) <= 20*sum(r['kind']=='turn' for r in logs)


def test_smoke_cli_fake_transport_only(tmp_path,monkeypatch):
    from bench.soak import runner
    r,c,store,e=shape_rig(tmp_path)
    schedule_path=tmp_path/'schedule.json';schedule_path.write_text(json.dumps(r.schedule))
    log=tmp_path/'smoke'/'run.jsonl'
    actual=runner.Runner
    class FixedDatetime(datetime):
        @classmethod
        def now(cls,tz=None):return cls.fromtimestamp(r.clock.now(),tz)
    def make(s,*args,**kwargs):
        c.e=s['episodes'][0]
        result=actual(s,*args,clock=r.clock,pulse=r.pulse,transcript=r.transcript,**kwargs)
        result.state=r.state
        return result
    monkeypatch.setattr(runner,'datetime',FixedDatetime)
    monkeypatch.setattr(runner,'CommandClient',lambda *a:c)
    monkeypatch.setattr(runner,'Runner',make)
    monkeypatch.setattr('sys.argv',['runner','--smoke','--schedule',str(schedule_path),'--log',str(log),
        '--client-cmd','never-invoked','--peer','synthetic-bot','--profile-home',str(r.home),
        '--soak-dir',str(r.soak_dir),'--chat-key','chat'])
    assert runner.main()==0
    result=json.loads((log.parent/'smoke-score.json').read_text())
    assert result['bars']['B3-3']=='PASS' and result['status']=='PENDING'
    assert sum('remind me in 5 minutes' in text for _,text in c.sent)==1
    assert any(text.startswith('/pulse cancel ') for _,text in c.sent)
    store.close()


def test_nested_dispatch_error_and_receipt_are_archived(tmp_path):
    r,c,store,e=shape_rig(tmp_path);now=r.clock.now()
    c.row('user','synthetic request',now,mid=123)
    malformed={'calls':[{'arguments':{'action':'create'}},'name']}
    c.row('assistant','',now+1,calls=[dict(id='bad',function=dict(name='tool_call',arguments=json.dumps(malformed)))])
    c.row('tool',json.dumps({'error':'invalid dispatch'}),now+2,cid='bad')
    c.row('assistant','',now+3,calls=[dict(id='good',function=dict(name='pulse',arguments=json.dumps({'action':'create'})))])
    c.row('tool',json.dumps({'ok':True,'wake':{'id':'synthetic-wake'},'receipt':'Fires once.'}),now+4,cid='good')
    c.row('assistant','**Set.**',now+5)
    results=r.transcript.pulse_results('s',now)
    assert len(results)==2 and results[0]['result']['error']=='invalid dispatch'
    assert results[0]['dispatch']=='tool_call'
    assert results[1]['args']=={'action':'create'} and results[1]['result']['receipt']=='Fires once.'
    store.close()
