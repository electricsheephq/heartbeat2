from copy import deepcopy
from datetime import date, datetime
import json
import re
import sqlite3
import pytest
from heartbeat2.model import Session, Wake, Occurrence
from heartbeat2.store import Store
from bench.soak.client import ClientError
from bench.soak.evidence import Transcript, Pulse
from bench.soak.runner import Runner, epoch
from bench.soak.schedule import build, digest


class FakeClock:
    def __init__(self, now): self.wall, self.callback = now, lambda: None
    def now(self): return self.wall
    def sleep(self, n): self.wall += n; self.callback()


class FakeClient:
    def __init__(self, clock, store, state, episode, directory):
        self.clock, self.store, self.state, self.e, self.directory = clock, store, state, episode, directory
        self.sent, self.messages, self.pending, self.deliveries = [], [], [], []
        self.sid = 's'
        self.mid, self.request_no, self.failed, self.no_delivery, self.stop_off = 0, 0, False, False, False
    def message(self, text, out, at=None):
        self.mid += 1
        m = dict(id=self.mid, date=datetime.fromtimestamp(at or self.clock.now()).astimezone().isoformat(), edit_date=None, out=out, text=text)
        self.messages.append(m); return m
    def row(self, role, text, at, mid=None, calls=None, cid=None):
        with sqlite3.connect(self.state) as db:
            db.execute('INSERT INTO messages (session_id,role,content,timestamp,tool_calls,tool_name,tool_call_id,platform_message_id) VALUES (?,?,?,?,?,?,?,?)',
                       (self.sid, role, text, at, json.dumps(calls) if calls else None, 'pulse' if role == 'tool' else None, cid, str(mid) if mid else None))
    def send(self, text):
        if self.failed == 'send': raise ClientError('fake')
        now = self.clock.now(); m = self.message(text, True); self.sent.append((now, text))
        if text.startswith('/'):
            if text == '/new': reply = '⚠️ **Confirm /new** — reply `/approve` to approve once'
            elif text == '/approve':
                self.sid = 'fresh'
                with sqlite3.connect(self.state) as db:
                    db.execute("UPDATE sessions SET ended_at=? WHERE id='s'", (now,))
                    db.execute('INSERT OR IGNORE INTO sessions VALUES (?,?,?,?)', (self.sid,'chat',now,None))
                self.store.cas_session('chat', {'unanswered_count':self.store.get_session('chat').unanswered_count}, unanswered_count=0)
                reply = '✨ Session reset! Starting fresh.\n\nModel: fixture'
            elif text == '/pulse quiet default':
                self.store.set_meta('quiet:chat','{}'); reply = 'Quiet hours follow your settings again: 22:00-08:00'
            elif text.startswith('/pulse cancel '):
                w=self.store.get_wake(text.split()[-1]);self.store.cas_wake(w.id,w.status,status='cancelled')
                reply=w.id+' cancelled · fixture wake'
            elif text == '/pulse on':
                self.store.cas_session('chat',{'enabled':self.store.get_session('chat').enabled},enabled=True);reply='HeartBeat 2.0 on for this chat.'
            else: raise AssertionError(text)
            self.message(reply,False,now+1);return m
        self.row('user',text,now,m['id']+9000);result=None
        if text == 'stop checking in on me':
            if self.stop_off: self.store.cas_session('chat', {'enabled': True}, enabled=False)
            else:
                for w in self.store.list_wakes('chat', status='active'): self.store.cas_wake(w.id, 'active', status='cancelled')
        elif text in ('thanks', 'all good, no need'):
            self.store.cas_session('chat', {'unanswered_count': self.store.get_session('chat').unanswered_count}, unanswered_count=0)
            for w in self.store.list_wakes('chat', status='active'):
                if w.cancel_on_reply: self.store.cas_wake(w.id, 'active', status='cancelled')
        elif not text.startswith('/') and self.e['kind'] != 'N':
            k = self.e['kind']; n = self.request_no; self.request_no += 1
            due = now+3600 if k == 'S' else epoch(self.e['flip'])+300 if k == 'W' else now+([20,40,60][n]*60) if k == 'M' else epoch(self.e['due'][n])
            term = self.e['tasks'][n]['term'] if self.e['tasks'] else 'DONE' if k == 'W' else 'check'
            w = Wake(session_key='chat', armed_session_id='s', note=term, kind='every' if k == 'S' else 'file' if k == 'W' else 'at',
                     next_due_at=due, cancel_on_reply=k.startswith('C'), owner_requested=True, eff_interval_s=3600,
                     trigger_json={'check_every': 300, 'pattern': '^DONE$', 'path': str(self.directory/(self.e.get('job', '')+'.status'))})
            w = self.store.create_wake(w); self.pending.append((w.id, due, term))
            result = dict(ok=True, wake=json.loads(json.dumps(w.__dict__)), receipt='Fires once.')
        if result:
            calls = [dict(id=str(m['id']), function=dict(name='pulse', arguments='{"action":"create"}'))]
            self.row('assistant', '', now+1, calls=calls); self.row('tool', json.dumps(result), now+2, cid=str(m['id']))
        self.row('assistant', 'Set.' if result else 'Okay.', now+3)
        self.message('Set.' if result else 'Okay.', False, now+3)
        return m
    def tick(self):
        now = self.clock.now()
        for wid, due, term in list(self.pending):
            w = self.store.get_wake(wid)
            if w.kind == 'file' and not w.probe_state_json and now >= w.created_at+300:
                self.store.cas_wake(wid, w.status, probe_state_json={'size': 8})
            if self.no_delivery or now < due or w.status != 'active': continue
            text = term+' reminder' if self.e['tasks'] else 'DONE' if w.kind == 'file' else 'Checking in?'
            self.row('user',f'⟦pulse o:{wid}/t:{due} a:1⟧ fixture wake',due-.2)
            self.row('assistant','',due+.1)
            self.row('assistant', text, due+1)
            self.deliveries.append(self.message(text, False, due+1))
            self.store._insert_record('occurrences', Occurrence(wake_id=wid, dedupe_key='one', state='done', started_at=due, ended_at=due+2, delivered=True, summary=text, payload_json=[{'message': 'DONE'}]))
            self.store.cas_wake(wid, 'active', next_due_at=due+3600 if w.kind == 'every' else None)
            session = self.store.get_session('chat'); self.store.cas_session('chat', {'unanswered_count': session.unanswered_count}, unanswered_count=session.unanswered_count+1)
            self.pending.remove((wid, due, term))
    def read(self, after_id, limit):
        if self.failed == 'read': raise ClientError('fake')
        return [m for m in self.messages if after_id is None or m['id'] > after_id][-limit:]


def rig(tmp_path, kind):
    s = build(date(2026, 10, 15), 'Asia/Tokyo'); e = deepcopy(next(e for e in s['episodes'] if e['kind'] == kind))
    s['episodes'] = [e]; s['start'] = datetime.fromisoformat(e['sends'][0]['at']).date().isoformat(); s['sha256'] = digest({k: v for k, v in s.items() if k != 'sha256'})
    home = tmp_path/'profile'; plugin = home/'plugins/heartbeat2'; plugin.mkdir(parents=True)
    (plugin/'plugin.yaml').write_text('version: 0.1.15\n')
    (home/'config.yaml').write_text('model: {default: fake, provider: fake}\nplugins: {entries: {heartbeat2: {enabled: true, allow_gateway_injection: true, timezone: Asia/Tokyo}}}\n')
    state = home/'state.db'
    with sqlite3.connect(state) as db:
        db.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY,session_key TEXT,started_at REAL,ended_at REAL)')
        db.execute("INSERT INTO sessions VALUES ('s','chat',0,NULL)")
        db.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL, tool_calls TEXT, tool_name TEXT, tool_call_id TEXT, platform_message_id TEXT)')
    clock = FakeClock(epoch(e['sends'][0]['at'])-120)
    store = Store(tmp_path/'pulse.db', clock); store.upsert_session(Session(session_key='chat', current_session_id='s'))
    client = FakeClient(clock, store, state, e, tmp_path/'jobs'); clock.callback = client.tick
    runner = Runner(s, tmp_path/'run.jsonl', client, home, tmp_path/'jobs', 'chat', clock=clock,
                    pulse=Pulse(store.path), transcript=Transcript(state, now=clock.now), until_day=e['day'])
    from bench.soak.evidence import frozen_state
    runner.hermes_version_file = tmp_path/'version.txt'; runner.hermes_version_file.write_text('1.0\n')
    runner.state = lambda: dict(frozen_state(home), hermes_version=runner.hermes_version_file.read_text().strip() if runner.hermes_version_file else None, hermes_commit='a'*40)
    return runner, client, store, e


@pytest.mark.parametrize('kind', ['R-clock', 'R-rel', 'W', 'C-reply', 'C-silent', 'M', 'S', 'N'])
def test_episode_actions_and_evidence(tmp_path, kind):
    r, c, store, e = rig(tmp_path, kind)
    assert r.run() == 0
    requests = [row for row in r.rows if row['kind'] == 'send' and row['purpose'] == 'request']
    assert [row['message']['text'] for row in requests] == [s['text'].replace('{soak_dir}', str(r.soak_dir)) for s in e['sends']]
    turns = [row for row in r.rows if row['kind'] == 'turn']
    assert len(turns)+len([x for x in r.rows if x['kind']=='command_reply']) == len(c.sent)
    assert all('pulse_results' in t and 'snapshot' in t and t['final_text'] for t in turns)
    if kind != 'M': assert [epoch(row['t']) for row in requests] == [epoch(s['at']) for s in e['sends']]
    else:
        for previous, current in zip(requests, requests[1:]):
            turn = next(row for row in turns if row['message_id'] == previous['message']['id'])
            assert epoch(current['t']) == epoch(turn['t'])+60
    thanks = [t for t, text in c.sent if text == 'thanks']
    assert len(thanks) == (0 if kind in ('C-reply', 'S', 'N') else 1)
    if thanks:
        dm = c.deliveries[-1]
        assert 60 <= thanks[0]-epoch(dm['date']) <= 300+30
    if kind == 'M':
        gate = next(row for row in r.rows if row['kind'] == 'gate' and row['request_index'] == 2)
        assert epoch(gate['t']) <= gate['due']-30
        assert gate['session']['unanswered_count'] == 2
        assert next(row for row in r.rows if row['kind'] == 'm3_owner_check')['owner_after'] == []
    if kind == 'W':
        assert (r.soak_dir/(e['job']+'.status')).read_bytes() == b'RUNNING\nDONE\n'
        assert next(row for row in r.rows if row['kind'] == 'baseline')['first_probe_at'] < epoch(e['flip'])
    if kind == 'C-reply': assert any(text == 'all good, no need' and t == epoch(e['reply_at']) for t, text in c.sent)
    if kind == 'S':
        stop = next(t for t, text in c.sent if text == 'stop checking in on me')
        first = epoch(c.deliveries[0]['date'])
        assert 300 <= stop-first <= 330
        assert r.clock.now() >= stop+7200
    store.close()


def test_s_timeout_and_cleanup_order(tmp_path):
    r, c, store, e = rig(tmp_path/'timeout', 'S'); c.no_delivery = True
    assert r.run() == 0
    assert r.has('s_no_first_delivery', e['id'])
    assert not any(text == 'stop checking in on me' for _, text in c.sent)
    assert any(text.startswith('/pulse cancel ') for _, text in c.sent)
    store.close()
    r, c, store, e = rig(tmp_path/'off', 'S'); c.stop_off = True
    assert r.run() == 0
    texts = [text for _, text in c.sent]
    assert next(i for i, t in enumerate(texts) if t.startswith('/pulse cancel ')) < texts.index('/pulse on')
    assert r.pulse.session('chat')['enabled']
    setup = [row['action'] for row in r.rows if row['kind'] == 'setup']
    assert setup.index('archive') < setup.index('cancel') < setup.index('on') < setup.index('verify_enabled')
    store.close()


@pytest.mark.parametrize('reason', ['foreign_owner_message', 'gateway_restart', 'client_outage_5m'])
def test_voids(tmp_path, reason):
    r, c, store, e = rig(tmp_path, 'R-rel')
    original = c.tick
    def tick():
        original()
        if r.clock.now() < epoch(e['sends'][0]['at'])+30: return
        if reason == 'foreign_owner_message' and not any(m['text'] == 'foreign' for m in c.messages): c.message('foreign', True)
        elif reason == 'gateway_restart': (r.home/'gateway-starts.log').write_text('1\n2\n')
        elif reason == 'client_outage_5m': c.failed = 'read'
    r.clock.callback = tick
    assert r.run() == 0  # Amendment 17: live cleanup proof survives missing command replies.
    assert not r.has('setup_failed', None) and r.has('episode_end', e['id'])
    assert all(w['status'] == 'cancelled' for w in r.pulse.wakes('chat'))
    assert any(row['kind'] == 'void' and row['reason'] == reason for row in r.rows)
    if reason == 'gateway_restart': assert any(row['kind'] == 'gateway_starts' and row['new'] == [1.0, 2.0] for row in r.rows)
    store.close()


def test_abort_resume_requeue_hours_and_dryrun(tmp_path, capsys):
    r, c, store, e = rig(tmp_path/'abort', 'N')
    r.clock.callback = lambda: (r.home/'plugins/heartbeat2/plugin.yaml').write_text('version: 0.1.16\n')
    assert r.run() == 3
    assert r.rows[-1]['kind'] in ('snapshot', 'abort')
    store.close()
    r, c, store, e = rig(tmp_path/'resume', 'R-rel')
    r.frozen = r.state()
    r.emit('declaration', start=r.schedule['start'], tz=r.schedule['tz'], seed=r.schedule['seed'], schedule_sha256=r.schedule['sha256'], frozen=r.frozen)
    r.frozen = r.state(); assert r.baseline()
    r.clock.wall = epoch(e['sends'][0]['at'])
    r.emit('episode_start', e['id'], schedule=e, session=r.pulse.session('chat'))
    r.send(e, 'request:0', e['sends'][0]['text'], 'request')
    restarted = Runner(r.schedule, r.path, c, r.home, r.soak_dir, 'chat', clock=r.clock, pulse=r.pulse, transcript=r.transcript, until_day=e['day'])
    restarted.state = r.state
    assert restarted.run() == 0
    assert sum(text == e['sends'][0]['text'] for _, text in c.sent) == 1
    assert any(row['kind'] == 'void' and row['reason'] == 'runner_stopped' for row in restarted.rows)
    restarted.requeue(); replacements = next(row['episodes'] for row in restarted.rows if row['kind'] == 'requeue')
    assert len(replacements) == 1 and replacements[0]['day'] == 8
    assert datetime.fromisoformat(replacements[0]['sends'][0]['at']).strftime('%H:%M') == '08:30'
    before = r.path.read_bytes(); sends = len(c.sent)
    assert r.run(dry_run=True) == 0
    assert r.path.read_bytes() == before and len(c.sent) == sends and capsys.readouterr().out
    r.clock.wall = datetime.fromisoformat(e['sends'][0]['at']).replace(hour=7, minute=59).timestamp()
    r.send(e, 'early', 'hello')
    assert r.has('hours_guard', e['id'], 'early') and len(c.sent) == sends
    store.close()


def test_send_failure_fixed_w_window_and_replacement(tmp_path):
    r, c, store, e = rig(tmp_path/'send', 'N')
    original = c.send
    def fail_request(text):
        if text == e['sends'][0]['text']: raise ClientError('fake')
        return original(text)
    c.send = fail_request
    assert r.run() == 0
    assert any(row['kind'] == 'void' and row['reason'] == 'client_error' for row in r.rows)
    store.close()
    r, c, store, e = rig(tmp_path/'watch', 'W')
    extra = deepcopy(next(x for x in build(date(2026, 10, 15), 'UTC')['episodes'] if x['kind'] == 'N'))
    extra['day'] = e['day']; extra['id'] = 'later'
    extra['sends'][0]['at'] = datetime.fromtimestamp(epoch(e['latest_close'])+300).astimezone().isoformat()
    extra['latest_close'] = datetime.fromtimestamp(epoch(extra['sends'][0]['at'])+600).astimezone().isoformat()
    r.episodes.append(extra)
    r.emit('episode_start', e['id'], schedule=e, session=r.pulse.session('chat'))
    r.active = e; r.send(e, 'request:0', e['sends'][0]['text'], 'request')
    r.clock.sleep(15); r.turns()
    w = store.list_wakes('chat')[0]
    store.cas_wake(w.id, 'active', probe_state_json={'size': 8})
    # Stored check interval is evidence from turn end, so change the fake's archived row too.
    for row in r.rows:
        if row['kind'] == 'turn':
            row['snapshot']['wakes'][-1]['trigger_json'] = json.dumps(dict(check_every=1800, path=str(r.soak_dir/(e['job']+'.status')), pattern='^DONE$'))
    r.evidence(e)
    shifts = [row for row in r.rows if row['kind'] == 'shift']
    assert not shifts
    assert epoch(e['latest_close']) == epoch(e['flip'])+1800
    assert epoch(next(x for x in r.episodes if x['id'] == 'later')['sends'][0]['at']) == epoch(extra['sends'][0]['at'])
    r.void('client_error'); r.requeue()
    replacement = next(row['episodes'][0] for row in r.rows if row['kind'] == 'requeue')
    assert replacement['job'] == e['job'] and replacement['path'] in replacement['sends'][0]['text']
    store.close()


def test_s_resume_archives_and_restores_chat(tmp_path):
    from bench.soak.evidence import frozen_state
    r, c, store, e = rig(tmp_path, 'S')
    r.emit('declaration', start=r.schedule['start'], tz=r.schedule['tz'], seed=r.schedule['seed'], schedule_sha256=r.schedule['sha256'], frozen=r.state())
    r.frozen = r.state(); assert r.baseline()
    r.clock.wall = epoch(e['sends'][0]['at'])
    r.emit('episode_start', e['id'], schedule=e, session=r.pulse.session('chat'))
    r.send(e, 'request:0', e['sends'][0]['text'], 'request')
    store.cas_session('chat', {'enabled': True}, enabled=False)
    resumed = Runner(r.schedule, r.path, c, r.home, r.soak_dir, 'chat', clock=r.clock, pulse=r.pulse, transcript=r.transcript, until_day=e['day'])
    resumed.state = r.state
    assert resumed.run() == 0
    texts = [text for _, text in c.sent]
    assert texts.count('check on me every hour today') == 1
    assert any(text.startswith('/pulse cancel ') for text in texts)
    assert '/pulse on' in texts and resumed.pulse.session('chat')['enabled']
    assert resumed.has('resume_cleanup', e['id'])
    store.close()
