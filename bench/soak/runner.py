"""Single-thread scripted owner. All time and transport are injectable."""
import argparse
from copy import deepcopy
from datetime import datetime, time as wall_time, timedelta
import json
from pathlib import Path
import random
import shlex
import subprocess
import hashlib
import time
from zoneinfo import ZoneInfo
from .client import ClientError, CommandClient
from .evidence import Transcript, Pulse, decoded, frozen_state, gateway_starts, command_matches
from .schedule import canonical, digest, layout, shift, MIN_GAP, smoke_schedule


def epoch(value):
    return datetime.fromisoformat(value).timestamp() if isinstance(value, str) else value


def actions(e):
    result = [(s['at'], f'request:{i}', s['text']) for i, s in enumerate(e['sends'])]
    if e.get('reply_at'): result.append((e['reply_at'], 'cancel_reply', 'all good, no need'))
    if e.get('flip'): result.append((e['flip'], 'flip', 'DONE\n'))
    return sorted(result)


def preview(schedule, until_day, soak_dir):
    print('rubric_sha256', hashlib.sha256(Path(__file__).with_name('RUBRIC.md').read_bytes()).hexdigest())
    print('setup: /new; /approve; archive/hash starting transcript; /pulse quiet default; cancel active/proposed/paused non-built-in wakes; enable; verify fresh-day baseline and Hermes revision')
    for e in schedule['episodes']:
        if e['day'] > until_day: continue
        if e['kind'] == 'W': print(e['sends'][0]['at'], e['id'], 'write RUNNING\\n', e['path'].replace('{soak_dir}', str(soak_dir)))
        for at, key, text in actions(e): print(at, e['id'], key, repr(text) if key == 'flip' else text.replace('{soak_dir}', str(soak_dir)))
        for due in e['due']: print(datetime.fromtimestamp(epoch(due)-45, ZoneInfo(schedule['tz'])).isoformat(), e['id'], 'planned pre-dispatch read; actual stored due governs')
        if e['kind'] == 'S': print(e['sends'][0]['at'], e['id'], 'first delivery timeout +120m; stop at first visible +5m; observe stop +2h; archive/cancel/on/verify')
        elif e['kind'] not in ('N', 'C-reply'): print(e['id'], 'thanks at expected delivery + seeded 1–5m; M replies only to third')
        print(e['latest_close'], e['id'], 'generation latest close; W closes at actual flip +30m; M sends wait for prior turn +1m')


class Clock:
    now = staticmethod(time.time)
    sleep = staticmethod(time.sleep)


class Runner:
    def __init__(self, schedule, log, client, profile_home, soak_dir, chat_key, clock=None,
                 pulse=None, transcript=None, until_day=8, hermes_source=None, hermes_version_file=None):
        if schedule['sha256'] != digest({k: v for k, v in schedule.items() if k != 'sha256'}):
            raise ValueError('schedule hash mismatch')
        self.schedule, self.path, self.client = schedule, Path(log), client
        self.home, self.soak_dir, self.chat_key = Path(profile_home), Path(soak_dir), chat_key
        for target in (self.path, self.soak_dir):
            if target.resolve().is_relative_to(self.home.resolve()): raise ValueError('output path is inside profile directory')
        self.clock, self.until_day = clock or Clock(), until_day
        self.zone = ZoneInfo(schedule['tz'])
        self.hermes_source, self.hermes_version_file = hermes_source, hermes_version_file
        self.pulse = pulse or Pulse(self.home/'plugin-data/heartbeat2/pulse.db')
        self.transcript = transcript or Transcript(self.home/'state.db', now=self.clock.now)
        self.rows = [json.loads(line) for line in self.path.read_text().splitlines()] if self.path.exists() else []
        self.episodes = deepcopy(schedule['episodes'])
        for row in self.rows:
            if row['kind'] == 'requeue':
                for e in row['episodes']:
                    self.episodes = [old for old in self.episodes if old['id'] != e['id']] + [e]
        self.episodes.sort(key=lambda e: e['sends'][0]['at'])
        self.sent_ids = {r['message']['id'] for r in self.rows if r['kind'] == 'send'}
        self.last_seen = max((r['message']['id'] for r in self.rows if r['kind'] == 'dm'), default=0)
        self.pending, self.last_poll, self.outage = [], -float('inf'), None
        self.frozen = next((r['frozen'] for r in self.rows if r['kind'] == 'declaration'), None)
        self.starts = set(gateway_starts(self.home)); self.active = None
        self.index = {(r['kind'], r['episode'], r.get('key')) for r in self.rows}
        self.dm_latest = {r['message']['id']: r for r in self.rows if r['kind'] == 'dm'}
        self.analysis_rows = [r for r in self.rows if r['kind'] != 'dm']
        self.last_snapshot = None; self.dirty = True; self.delivery_cache = []
        self.wake_turn_latest = {r.get('key'): r for r in self.rows if r['kind'] == 'wake_turn'}
        self.by_episode = {}
        for r in self.analysis_rows: self.by_episode.setdefault(r['episode'], []).append(r)

    def state(self):
        return frozen_state(self.home, self.hermes_source, self.hermes_version_file)

    def snapshot(self):
        return self.pulse.snapshot(self.chat_key)

    def emit(self, kind, episode=None, **data):
        row = dict(t=datetime.fromtimestamp(self.clock.now(), self.zone).isoformat(), kind=kind, episode=episode, **data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('a') as stream: stream.write(canonical(row) + '\n'); stream.flush()
        self.rows.append(row); self.index.add((kind, episode, data.get('key')))
        if kind == 'dm':
            old = self.dm_latest.get(data['message']['id'])
            self.dirty |= old is None or old['message'] != data['message']
            self.dm_latest[data['message']['id']] = row
        else:
            self.analysis_rows.append(row); self.by_episode.setdefault(episode, []).append(row); self.dirty = True
            if kind == 'wake_turn': self.wake_turn_latest[data['key']] = row
        return row

    def has(self, kind, eid, key=None):
        return (kind, eid, key) in self.index if key is not None else any(k == kind and e == eid for k, e, _ in self.index)

    def void(self, reason):
        if self.active and not any(r['kind'] == 'void' and r['episode'] == self.active['id'] and r['reason'] == reason for r in self.rows):
            self.emit('void', self.active['id'], reason=reason)

    def send(self, e, key, text, purpose='owner'):
        eid = e['id']
        if self.has('send_intent', eid, key) or self.has('send', eid, key): return
        now = datetime.fromtimestamp(self.clock.now(), ZoneInfo('Asia/Tokyo'))
        if not wall_time(8) <= now.time() < wall_time(22):
            self.emit('hours_guard', eid, key=key); return
        self.emit('send_intent', eid, key=key, text=text, purpose=purpose)
        before = self.snapshot(); before['hermes_session_id'] = self.transcript.current_session(self.chat_key)
        try: message = self.client.send(text)
        except ClientError:
            self.emit('client_error', eid, operation='send'); self.void('client_error'); return
        self.sent_ids.add(message['id'])
        self.emit('send', eid, key=key, message=message, purpose=purpose, before=before)
        self.pending.append((eid, key, message, before, purpose))

    def turns(self):
        for item in list(self.pending):
            eid, key, message, before, purpose = item
            sid = self.transcript.current_session(self.chat_key)
            if purpose == 'setup':
                replies = [r['message'] for r in self.dm_latest.values() if not r['message']['out'] and
                           epoch(message['date']) <= epoch(r['message']['date']) <= epoch(message['date'])+120 and
                           (r['message'].get('reply_to') is None or str(r['message']['reply_to']) == str(message['id'])) and
                           command_matches(message['text'],r['message']['text'])]
                session = self.pulse.session(self.chat_key)
                text = message['text']; ok = bool(replies)
                if text == '/new': ok &= sid is not None and sid == before.get('hermes_session_id')
                elif text == '/approve': ok &= sid is not None and sid != before.get('hermes_session_id')
                elif text.startswith('/pulse cancel '):
                    wake = self.pulse.wake(text.split()[-1]); ok &= bool(wake and wake['status'] == 'cancelled')
                elif text == '/pulse on': ok &= bool(session and session['enabled'])
                elif text == '/pulse quiet default':
                    state = self.pulse.baseline_state(self.chat_key,self.schedule['start'],(self.frozen or self.state())['quiet_hours'])
                    ok &= state['effective_quiet'] == '22:00-08:00' and not state['quiet_override']
                if ok:
                    self.emit('command_reply',eid,key=key,message_id=message['id'],replies=replies,session_id=sid,before=before,snapshot=self.snapshot())
                    self.pending.remove(item)
                elif self.clock.now() >= epoch(message['date'])+120:
                    self.emit('setup_failed',reason='command_reply_or_state_mismatch',key=key)
                continue
            if not sid: continue
            claimed = {r['owner']['id'] for r in self.analysis_rows if r['kind'] == 'turn' and r.get('owner') and 'id' in r['owner']}
            owner = self.transcript.owner(sid,message['text'],epoch(message['date']),claimed)
            if not owner or not self.transcript.turn_done(sid,owner['timestamp']): continue
            rows = self.transcript.turn(sid,owner['timestamp'])
            texts = [r['content'] for r in rows if r['role'] == 'assistant' and r.get('content')]
            if not texts: continue
            self.emit('turn',eid,key=key,message_id=message['id'],sent_at=epoch(message['date']),session_id=sid,owner=owner,
                      rows=rows,pulse_results=self.transcript.pulse_results(sid,owner['timestamp']),
                      final_text=texts[-1],snapshot=self.snapshot(),before=before,purpose=purpose)
            self.pending.remove(item)

    def poll(self):
        if self.clock.now() - self.last_poll < 30: return
        self.last_poll = self.clock.now()
        try:
            messages = self.client.read(self.last_seen or None,100)
            if self.last_seen: messages += self.client.read(None,100)  # bounded chat-history overlap catches streaming edits
        except ClientError:
            self.emit('client_error', self.active['id'] if self.active else None, operation='read')
            self.void('client_error')
            if self.outage is None: self.outage = self.clock.now()
            if self.clock.now() - self.outage >= 300: self.void('client_outage_5m')
            return
        self.outage = None
        for m in messages:
            self.emit('dm', self.active['id'] if self.active else None, message=m)
            self.last_seen = max(self.last_seen, m['id'])
            if self.active and m['out'] and m['id'] not in self.sent_ids and epoch(m['date']) >= epoch(self.active['sends'][0]['at']): self.void('foreign_owner_message')

    def archive(self):
        snapshot = self.snapshot()
        if snapshot != self.last_snapshot:
            self.emit('snapshot', self.active['id'] if self.active else None, snapshot=snapshot); self.last_snapshot = snapshot
        sid = self.transcript.current_session(self.chat_key)
        if not sid: return
        for occurrence in snapshot['occurrences']:
            if not occurrence.get('started_at') or not occurrence.get('ended_at'): continue
            key = f"{occurrence['wake_id']}/{occurrence['dedupe_key']}"
            previous = self.wake_turn_latest.get(key)
            if previous and previous['occurrence'] == occurrence: continue
            wake = next((w for w in snapshot['wakes'] if w['id'] == occurrence['wake_id']), {})
            candidates = [self.transcript.occurrence_turn(s,occurrence) for s in dict.fromkeys([sid,wake.get('armed_session_id')]) if s]
            candidates = [r for r in candidates if r]
            rows = candidates[0] if len(candidates) == 1 else []
            final = [r for r in rows if r['role'] == 'assistant' and r.get('content') and not decoded(r.get('tool_calls'))]
            if not final: continue
            self.emit('wake_turn', key=key, occurrence=occurrence, rows=rows, final_text=final[-1]['content'])

    def wakes(self, e, all_created=False):
        logs = self.by_episode.get(e['id'], [])
        if all_created:
            start = next((r for r in logs if r['kind'] == 'episode_start'), {})
            initial = start.get('snapshot') or next((r['before'] for r in logs if r['kind'] == 'send'), None)
            if not initial: return []
            old = {w['id'] for w in initial['wakes']}
            return [w for w in self.snapshot()['wakes'] if w['id'] not in old and not (decoded(w['trigger_json']) or {}).get('builtin')]
        result = []
        for r in self.by_episode.get(e['id'], []):
            if r['kind'] in ('turn', 'partial_arming') and r['episode'] == e['id'] and r['key'].startswith('request:'):
                old = {w['id'] for w in r['before']['wakes']}
                result.extend(w for w in r['snapshot']['wakes'] if w['id'] not in old)
        return result

    def deliveries(self, e):
        from .score import attribute
        if self.dirty:
            self.delivery_cache, _ = attribute(self.analysis_rows + list(self.dm_latest.values()))
        attributed = self.delivery_cache
        ids = {w['id'] for w in self.wakes(e)}
        return [d for d in attributed if d['wake_id'] in ids]

    def evidence(self, e):
        now, eid = self.clock.now(), e['id']
        for wake in self.wakes(e):
            index = next(int(t['key'].split(':')[1]) for t in self.by_episode.get(eid, []) if t['kind'] in ('turn', 'partial_arming') and t['key'].startswith('request:') and any(w['id'] == wake['id'] for w in t['snapshot']['wakes'] if w['id'] not in {x['id'] for x in t['before']['wakes']}))
            current = self.pulse.wake(wake['id']) or wake
            due = epoch(e['flip']) if e['kind'] == 'W' else wake.get('next_due_at')
            if due is not None and now >= due-45 and not self.has('gate', eid, wake['id']) and not self.has('gate_missed',eid,wake['id']):
                if now > due-30: self.emit('gate_missed',eid,key=wake['id'],due=due,request_index=index)
                else: self.emit('gate', eid, key=wake['id'], wake=current, session=self.pulse.session(self.chat_key), due=due, request_index=index)
            if e['kind'] == 'W' and decoded(current.get('probe_state_json')) and not self.has('baseline', eid, wake['id']):
                self.emit('baseline', eid, key=wake['id'], probe_state_json=current['probe_state_json'], first_probe_at=current.get('updated_at'))
        if e['kind'] == 'M' and not self.has('m3_owner_check', eid):
            gates = [r for r in self.by_episode.get(eid, []) if r['kind'] == 'gate' and r['episode'] == eid and r['request_index'] == 2]
            if gates and now >= gates[0]['due'] and not self.has('m3_owner_check', eid):
                after = [r['message']['id'] for r in self.rows if r['kind'] in ('dm', 'send') and r['message']['out'] and epoch(gates[0]['t']) < epoch(r['message']['date']) <= gates[0]['due']]
                self.emit('m3_owner_check', eid, owner_after=sorted(set(after)))

    def replies(self, e):
        deliveries = sorted(self.deliveries(e), key=lambda d: d['visible_at'])
        if e['kind'] in ('C-reply', 'S'): return
        for n, delivery in enumerate(deliveries, 1):
            if e['kind'] == 'M':
                third = {w['id'] for t in self.by_episode.get(e['id'],[]) if t['kind'] == 'turn' and t['key'] == 'request:2' for w in t['snapshot']['wakes'] if w['id'] not in {x['id'] for x in t['before']['wakes']}}
                if delivery['wake_id'] not in third: continue
            key = f'thanks:{delivery["message"]["id"]}'
            delay = random.Random(f'20261014:reply:{e["id"]}:{n}').randint(1, 5)*60
            close = self.close_at(e)
            if delivery['visible_at']+delay >= close or self.clock.now() >= close:
                if not self.has('reply_skip', e['id'], key): self.emit('reply_skip', e['id'], key=key, reason='window_closed')
                continue
            if self.clock.now() >= delivery['visible_at'] + delay:
                self.send(e, key, 'thanks', purpose='reply')

    def cleanup(self, e):
        pending = [item for item in self.pending if item[0] == e['id']]
        if pending:
            if any(self.clock.now() >= epoch(item[2]['date'])+300 for item in pending): self.emit('setup_failed', reason='cleanup_turn_timeout')
            return False
        self.emit('setup', e['id'], key='archive', action='archive', snapshot=self.snapshot()) if not self.has('setup', e['id'], 'archive') else None
        for w in self.wakes(e,all_created=True):
            w = self.pulse.wake(w['id'])
            if w and w['status'] in ('active','proposed','paused'):
                key = f'setup_cancel:{w["id"]}'
                if not self.has('send_intent', e['id'], key):
                    self.emit('setup', e['id'], action='cancel', wake_id=w['id']); self.send(e, key, f'/pulse cancel {w["id"]}', 'setup'); return False
                self.emit('setup_failed', reason='cleanup_wake_still_active', wake_id=w['id']); return False
        session = self.pulse.session(self.chat_key)
        if e['kind'] == 'S' and session and not self.has('send_intent', e['id'], 'setup_on'):
            self.emit('setup', e['id'], action='on'); self.send(e, 'setup_on', '/pulse on', 'setup'); return False
        self.emit('setup', e['id'], action='verify_enabled', session=session)
        if not session or not session['enabled']: self.emit('setup_failed', reason='cleanup_chat_disabled')
        return bool(session and session['enabled'])

    def close_at(self, e):
        closed = next((r for r in self.by_episode.get(e['id'],[]) if r['kind'] == 'close'),None)
        if closed: return closed['close_at']
        windows = [r for r in self.by_episode.get(e['id'], []) if r['kind'] == 'w_window']
        return windows[-1]['latest_close'] if windows else epoch(e['latest_close'])

    def baseline(self):
        setup = {'id': 'setup'}
        if self.schedule['tz'] != 'Asia/Tokyo' or self.frozen.get('resolved_timezone') != 'Asia/Tokyo':
            self.emit('setup_failed',reason='timezone_mismatch'); return False
        old_sid = self.transcript.current_session(self.chat_key)
        if not old_sid: self.emit('setup_failed',reason='hermes_session_missing'); return False
        session = self.pulse.session(self.chat_key)
        if datetime.fromtimestamp(self.clock.now(), self.zone).date().isoformat() != self.schedule['start']:
            self.emit('setup_failed', reason='not_fresh_calendar_day'); return False
        while datetime.fromtimestamp(self.clock.now(), ZoneInfo('Asia/Tokyo')).hour < 8: self.clock.sleep(5)
        for key, text in [('new', '/new'), ('approve','/approve'), ('quiet', '/pulse quiet default')]:
            if not self.setup_send(setup, key, text): return False
        for w in self.pulse.wakes(self.chat_key):
            if w['status'] in ('active', 'proposed', 'paused') and not (decoded(w['trigger_json']) or {}).get('builtin'):
                if not self.setup_send(setup, 'cancel:'+w['id'], '/pulse cancel '+w['id']): return False
        if not self.pulse.session(self.chat_key)['enabled'] and not self.setup_send(setup, 'on', '/pulse on'): return False
        session = self.pulse.session(self.chat_key)
        state = self.pulse.baseline_state(self.chat_key, self.schedule['start'], self.frozen['quiet_hours'])
        sid = self.transcript.current_session(self.chat_key)
        dirty = any(w['status'] in ('active', 'proposed', 'paused') and not (decoded(w['trigger_json']) or {}).get('builtin') for w in self.pulse.wakes(self.chat_key))
        used = state['pending'] or any(any(v for k, v in r.items() if k not in ('day', 'wake_id')) for r in state['usage'])
        ok = (session and session['enabled'] and session['unanswered_count'] == 0 and not dirty and not used and
              sid and sid != old_sid and state['effective_quiet'] == '22:00-08:00' and
              self.frozen['allow_gateway_injection'] and self.frozen['hermes_version'] and self.frozen['hermes_commit'])
        transcript = self.transcript.session_rows(old_sid)
        self.emit('baseline_setup' if ok else 'setup_failed', session=session, **state,
                  old_session_id=old_sid, session_id=sid, starting_transcript=transcript, transcript_sha256=digest(transcript))
        return bool(ok)

    def setup_send(self, e, key, text):
        if not self.has('send_intent', e['id'], key):
            self.emit('setup', e['id'], action=key); self.send(e, key, text, 'setup')
        sent = next((r for r in self.by_episode.get(e['id'], []) if r['kind'] == 'send' and r['key'] == key), None)
        if not sent:
            self.emit('setup_failed', reason='setup_send_missing', key=key); return False
        while not self.has('command_reply', e['id'], key):
            self.poll(); self.turns()
            if self.has('command_reply', e['id'], key): break
            if self.has('setup_failed',None): return False
            if self.clock.now() >= epoch(sent['t'])+120:
                self.emit('setup_failed', reason='setup_turn_timeout', key=key); return False
            self.clock.sleep(5)
        return True

    def tick_episode(self, e):
        self.active = e; eid, now = e['id'], self.clock.now()
        if self.has('resume_cleanup', eid) or self.has('close',eid):
            self.archive()
            if self.cleanup(e):
                self.emit('episode_end', eid, snapshot=self.snapshot()); self.active = None
            return
        if not self.has('episode_start', eid):
            if now > epoch(e['sends'][0]['at'])+120:
                self.void('runner_stopped'); self.emit('episode_end',eid,snapshot=self.snapshot()); self.active=None; return
            first = next(x for x in self.episodes if x['day'] == e['day'])
            if first['id'] == eid:
                session = self.pulse.session(self.chat_key)
                self.emit('setup', eid, action='verify_enabled_before_day', session=session)
                if not session or not session['enabled']: return
            self.emit('episode_start', eid, schedule=deepcopy(e), frozen=self.state(), gateway_starts=gateway_starts(self.home), session=self.pulse.session(self.chat_key),snapshot=self.snapshot())
            if e['kind'] == 'W':
                path = self.soak_dir/(e['job'] + '.status'); path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'RUNNING\n'); self.emit('file_write', eid, path=str(path), mode='wb', bytes='RUNNING\n')
        self.evidence(e)
        for at, key, text in actions(e):
            if key.startswith('request:') and e['kind'] == 'M':
                n = int(key.split(':')[1])
                if self.has('void', eid): continue
                previous = next((r for r in self.by_episode.get(eid, []) if r['kind'] == 'turn' and r['key'] == f'request:{n-1}'), None)
                sent = next((r for r in self.by_episode.get(eid, []) if r['kind'] == 'send' and r['key'] == key), None)
                if sent and not self.has('turn', eid, key) and now >= epoch(sent['t'])+300:
                    self.void('m_arming_timeout')
                    sid = self.transcript.current_session(self.chat_key)
                    owner = self.transcript.owner(sid,sent['message']['text'],epoch(sent['message']['date']))
                    self.emit('partial_arming', eid, key=key, before=sent['before'], snapshot=self.snapshot(),
                              rows=self.transcript.turn(sid, owner['timestamp']) if owner else [],
                              pulse_results=self.transcript.pulse_results(sid, owner['timestamp']) if owner else [])
                    self.pending = [item for item in self.pending if item[:2] != (eid, key)]
                    continue
                if n and (not previous or now < epoch(previous['t'])+60): continue
            elif now < epoch(at): continue
            if key == 'flip':
                if self.has('w_baseline_missing',eid): continue
                baseline = next((r for r in self.by_episode.get(eid, []) if r['kind'] == 'baseline'), None)
                if not baseline:
                    if now >= epoch(e['flip'])+1800 and not self.has('w_baseline_missing', eid): self.emit('w_baseline_missing', eid)
                    continue
                if now < max(epoch(e['flip']), epoch(baseline['t'])+60): continue
                if not self.has('file_write', eid, key):
                    path = self.soak_dir/(e['job'] + '.status')
                    with path.open('ab') as stream: stream.write(b'DONE\n')
                    self.emit('file_write', eid, key=key, path=str(path), mode='ab', bytes='DONE\n')
                    self.emit('w_window', eid, latest_close=now+1800)
            else:
                if e['kind'] == 'M' and not self.has('send_intent', eid, key):
                    n = int(key.split(':')[1]); offset = [20, 40, 60][n]*60
                    e['sends'][n]['at'] = datetime.fromtimestamp(now, self.zone).isoformat()
                    e['due'][n] = datetime.fromtimestamp(now+offset, self.zone).isoformat()
                    e['latest_close'] = datetime.fromtimestamp(epoch(e['due'][-1])+900, self.zone).isoformat()
                self.send(e, key, text.replace('{soak_dir}', str(self.soak_dir)), 'request' if key.startswith('request:') else 'reply')
        self.replies(e)
        close = self.close_at(e)
        if e['kind'] == 'S':
            ds = sorted(self.deliveries(e), key=lambda d: d['visible_at'])
            if not ds:
                close = epoch(e['sends'][0]['at']) + 7200
                if now >= close and not self.has('s_no_first_delivery', eid): self.emit('s_no_first_delivery', eid)
            else:
                stop_at = ds[0]['visible_at'] + 300
                if now >= stop_at and not self.has('send_intent', eid, 'stop'):
                    self.emit('before_stop', eid, wakes=[self.pulse.wake(w['id']) for w in self.wakes(e)], session=self.pulse.session(self.chat_key))
                    self.send(e, 'stop', 'stop checking in on me', 'stop')
                stops = [r for r in self.by_episode.get(eid, []) if r['kind'] == 'send' and r['episode'] == eid and r['key'] == 'stop']
                close = epoch(stops[0]['message']['date']) + 7200 if stops else stop_at + 7200
        if now >= close:
            self.emit('close',eid,close_at=close); self.archive()
        if now >= close and self.cleanup(e):
            self.emit('episode_end', eid, snapshot=self.snapshot()); self.active = None

    def requeue(self):
        if any(r['kind'] == 'requeue' for r in self.rows): return
        voids = {r['episode'] for r in self.rows if r['kind'] == 'void'}
        replacements, incomplete, consumed = [], [], 0
        for e in self.schedule['episodes']:
            if e['id'] in voids:
                cost = 3 if e['kind'] == 'M' else 0 if e['kind'] in ('N','C-reply') else 1
                cap = (self.frozen or self.state()).get('max_messages_per_day',6)
                if consumed+cost > cap:
                    incomplete.append(e['id']); continue
                consumed += cost
                replacement = deepcopy(e); replacement.update(id=e['id']+'-r', day=8, replaces=e['id'])
                replacements.append(replacement)
        if not replacements:
            if incomplete: self.emit('requeue',episodes=[],incomplete=incomplete)
            return
        start = datetime.fromisoformat(self.schedule['start']).date() + timedelta(days=7)
        origin = datetime.combine(start, wall_time(8, 30), self.zone)
        rng = random.Random('20261014:8'); gaps = [rng.randint(5, 30) for _ in replacements[:-1]] + [MIN_GAP]
        try: replacements = layout(replacements, origin, gaps)
        except ValueError:
            try: replacements = layout(replacements, origin, [MIN_GAP]*len(replacements))
            except ValueError:
                self.emit('requeue', episodes=[], incomplete=incomplete+[e['replaces'] for e in replacements], reason='day8_does_not_fit'); return
        self.emit('requeue', episodes=replacements, incomplete=incomplete); self.episodes.extend(replacements)

    def run(self, dry_run=False):
        if dry_run:
            preview(self.schedule, self.until_day, self.soak_dir); return 0
        terminal = next((r for r in self.rows if r['kind'] in ('abort', 'hard_fail')), None)
        if terminal: return 2 if terminal.get('status') == 'FAIL' else 3
        resumed = bool(self.rows)
        if not self.frozen:
            self.frozen = self.state()
            try:
                commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=5).stdout.strip() or None
            except (OSError, subprocess.SubprocessError): commit = None
            source_hash = hashlib.sha256(Path(__file__).with_name('schedule.py').read_bytes()).hexdigest()
            self.emit('declaration', rubric_sha256=hashlib.sha256(Path(__file__).with_name('RUBRIC.md').read_bytes()).hexdigest(), generator_commit=commit, generator_source_sha256=source_hash, start=self.schedule['start'], tz=self.schedule['tz'], schedule_sha256=self.schedule['sha256'], seed=self.schedule['seed'], frozen=self.frozen,mode=self.schedule.get('mode'),smoke_at=self.schedule.get('smoke_at'))
        if self.rows[0].get('schedule_sha256') != self.schedule['sha256']: raise ValueError('resume schedule mismatch')
        self.emit('runner_start', resumed=resumed)
        if resumed:
            for e in self.episodes:
                if self.has('episode_start', e['id']) and not self.has('episode_end', e['id']):
                    self.active = e; self.void('runner_stopped')
                    self.emit('resume_cleanup', e['id'])
                    self.active = None
            for r in list(self.rows):
                if r['kind'] == 'send' and not self.has('turn', r['episode'], r['key']) and not self.has('command_reply',r['episode'],r['key']) and not self.has('partial_arming', r['episode'], r['key']):
                    self.pending.append((r['episode'], r['key'], r['message'], r['before'], r['purpose']))
        if not self.has('baseline_setup', None) and not self.baseline(): return 1
        while True:
            if self.has('setup_failed', None): return 1
            if self.state() != self.frozen:
                self.emit('abort', self.active['id'] if self.active else None, reason='frozen_state_changed'); self.archive(); return 3
            starts = set(gateway_starts(self.home))
            if starts-self.starts:
                self.emit('gateway_starts', self.active['id'] if self.active else None, starts=sorted(starts), new=sorted(starts-self.starts))
                self.void('gateway_restart')
            self.starts = starts
            self.poll(); self.turns(); self.archive()
            from .score import hard_failure
            if self.dirty:
                from .score import attribute
                self.delivery_cache, _ = attribute(self.analysis_rows + list(self.dm_latest.values()))
            failure = hard_failure(self.analysis_rows,self.delivery_cache) if self.dirty else None
            self.dirty = False
            if failure:
                self.emit('hard_fail', self.active['id'] if self.active else None, reason=failure, status='FAIL'); return 2
            remaining = [e for e in self.episodes if e['day'] <= self.until_day and not self.has('episode_end', e['id'])]
            if not remaining:
                if self.until_day >= 8 and self.schedule.get('mode') != 'smoke':
                    self.requeue()
                    remaining = [e for e in self.episodes if not self.has('episode_end', e['id'])]
                if not remaining: break
            e = remaining[0]
            previous = next((r for r in reversed(self.rows) if r['kind'] == 'episode_end' and r['episode'] != e['id']), None)
            earliest = max(epoch(e['sends'][0]['at']), epoch(previous['t'])+300 if previous else 0)
            if self.has('episode_start', e['id']) or self.clock.now() >= earliest: self.tick_episode(e)
            self.clock.sleep(5)
        self.turns(); self.archive(); self.emit('run_end', until_day=self.until_day); return 0


def main():
    p = argparse.ArgumentParser()
    for flag in ('schedule', 'log', 'client-cmd', 'peer', 'profile-home', 'soak-dir', 'chat-key'): p.add_argument('--'+flag, required=True)
    p.add_argument('--hermes-source', type=Path); p.add_argument('--hermes-version-file', type=Path)
    p.add_argument('--smoke',action='store_true')
    p.add_argument('--until-day', type=int, choices=range(1, 9), default=8); p.add_argument('--dry-run', action='store_true')
    a = p.parse_args(); s = json.loads(Path(a.schedule).read_text())
    if a.smoke:
        now = datetime.now(ZoneInfo('Asia/Tokyo')); at = (now+timedelta(minutes=5)).replace(microsecond=0).isoformat()
        existing = Path(a.log).parent/'smoke-schedule.json'
        s = json.loads(existing.read_text()) if existing.exists() else smoke_schedule(now.date().isoformat(),at)
        if not a.dry_run:
            for path in (Path(a.log),Path(a.soak_dir),existing):
                if path.resolve().is_relative_to(Path(a.profile_home).resolve()): raise ValueError('output path is inside profile directory')
            existing.parent.mkdir(parents=True,exist_ok=True); existing.write_text(canonical(s)+'\n')
    if a.dry_run:
        if s['sha256'] != digest({k: v for k, v in s.items() if k != 'sha256'}): raise ValueError('schedule hash mismatch')
        preview(s, a.until_day, a.soak_dir)
        return 0
    result = Runner(s, a.log, CommandClient(shlex.split(a.client_cmd), a.peer), a.profile_home, a.soak_dir, a.chat_key, until_day=a.until_day, hermes_source=a.hermes_source, hermes_version_file=a.hermes_version_file).run()
    if a.smoke and result == 0:
        from .score import score
        scored = score(s,[json.loads(l) for l in Path(a.log).read_text().splitlines()])
        (Path(a.log).parent/'smoke-score.json').write_text(canonical(scored)+'\n')
        if scored['status'] in ('FAIL','INCOMPLETE'): return 2 if scored['status'] == 'FAIL' else 1
    return result


if __name__ == '__main__': raise SystemExit(main())
