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
from .evidence import Transcript, Pulse, decoded, frozen_state, gateway_starts
from .schedule import canonical, digest, layout, shift, MIN_GAP


def epoch(value):
    return datetime.fromisoformat(value).timestamp() if isinstance(value, str) else value


def actions(e):
    result = [(s['at'], f'request:{i}', s['text']) for i, s in enumerate(e['sends'])]
    if e.get('reply_at'): result.append((e['reply_at'], 'cancel_reply', 'all good, no need'))
    if e.get('flip'): result.append((e['flip'], 'flip', 'DONE\n'))
    return sorted(result)


def preview(schedule, until_day, soak_dir):
    for e in schedule['episodes']:
        if e['day'] > until_day: continue
        if e['kind'] == 'W': print(e['sends'][0]['at'], e['id'], 'write RUNNING\\n', e['path'].replace('{soak_dir}', str(soak_dir)))
        for at, key, text in actions(e): print(at, e['id'], key, repr(text) if key == 'flip' else text.replace('{soak_dir}', str(soak_dir)))
        for due in e['due']: print(datetime.fromtimestamp(epoch(due)-30, ZoneInfo(schedule['tz'])).isoformat(), e['id'], 'planned pre-dispatch read; actual stored due governs')
        if e['kind'] == 'S': print(e['sends'][0]['at'], e['id'], 'first delivery timeout +120m; stop at first visible +5m; observe stop +2h; archive/cancel/on/verify')
        elif e['kind'] not in ('N', 'C-reply'): print(e['id'], 'thanks at expected delivery + seeded 1–5m; M replies only to third')
        print(e['latest_close'], e['id'], 'generation latest close; logged watcher overruns shift following episodes')


class Clock:
    now = staticmethod(time.time)
    sleep = staticmethod(time.sleep)


class Runner:
    def __init__(self, schedule, log, client, profile_home, soak_dir, chat_key, clock=None,
                 pulse=None, transcript=None, until_day=8):
        if schedule['sha256'] != digest({k: v for k, v in schedule.items() if k != 'sha256'}):
            raise ValueError('schedule hash mismatch')
        self.schedule, self.path, self.client = schedule, Path(log), client
        self.home, self.soak_dir, self.chat_key = Path(profile_home), Path(soak_dir), chat_key
        self.clock, self.until_day = clock or Clock(), until_day
        self.zone = ZoneInfo(schedule['tz'])
        self.pulse = pulse or Pulse(self.home/'plugin-data/heartbeat2/pulse.db')
        self.transcript = transcript or Transcript(self.home/'state.db', now=self.clock.now)
        self.rows = [json.loads(line) for line in self.path.read_text().splitlines()] if self.path.exists() else []
        self.episodes = deepcopy(schedule['episodes'])
        for row in self.rows:
            if row['kind'] in ('shift', 'requeue'):
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
        now = datetime.fromtimestamp(self.clock.now(), self.zone)
        if not wall_time(8, 30) <= now.time() <= wall_time(21, 45):
            self.emit('hours_guard', eid, key=key); return
        self.emit('send_intent', eid, key=key, text=text, purpose=purpose)
        before = self.pulse.snapshot()
        try: message = self.client.send(text)
        except ClientError:
            self.emit('client_error', eid, operation='send'); self.void('client_error'); return
        self.sent_ids.add(message['id'])
        self.emit('send', eid, key=key, message=message, purpose=purpose, before=before)
        self.pending.append((eid, key, message, before, purpose))

    def turns(self):
        for item in list(self.pending):
            eid, key, message, before, purpose = item
            session = self.pulse.session(self.chat_key)
            if not session: continue
            sid = session['current_session_id']; owner = self.transcript.owner(sid, message['id'])
            if not owner or not self.transcript.turn_done(sid, owner['timestamp']): continue
            rows = self.transcript.turn(sid, owner['timestamp'])
            self.emit('turn', eid, key=key, message_id=message['id'], session_id=sid, owner=owner,
                      rows=rows, pulse_results=self.transcript.pulse_results(sid, owner['timestamp']),
                      final_text=rows[-1]['content'], snapshot=self.pulse.snapshot(), before=before, purpose=purpose)
            self.pending.remove(item)

    def poll(self):
        if self.clock.now() - self.last_poll < 30: return
        self.last_poll = self.clock.now()
        try: messages = self.client.read(self.last_seen-50 if self.last_seen else None, 100)
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
        snapshot = self.pulse.snapshot()
        if snapshot != self.last_snapshot:
            self.emit('snapshot', self.active['id'] if self.active else None, snapshot=snapshot); self.last_snapshot = snapshot
        session = self.pulse.session(self.chat_key)
        if not session: return
        for occurrence in snapshot['occurrences']:
            if not occurrence.get('started_at') or not occurrence.get('ended_at'): continue
            key = f"{occurrence['wake_id']}/{occurrence['dedupe_key']}"
            previous = self.wake_turn_latest.get(key)
            if previous and previous['occurrence'] == occurrence: continue
            rows = self.transcript.occurrence_turn(session['current_session_id'], occurrence)
            final = [r for r in rows if r['role'] == 'assistant' and not decoded(r.get('tool_calls'))]
            if not final: continue
            self.emit('wake_turn', key=key, occurrence=occurrence, rows=rows, final_text=final[-1]['content'])

    def wakes(self, e):
        result = []
        for r in self.by_episode.get(e['id'], []):
            if r['kind'] == 'turn' and r['episode'] == e['id'] and r['key'].startswith('request:'):
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
            index = next(int(t['key'].split(':')[1]) for t in self.by_episode.get(eid, []) if t['kind'] == 'turn' and t['key'].startswith('request:') and any(w['id'] == wake['id'] for w in t['snapshot']['wakes'] if w['id'] not in {x['id'] for x in t['before']['wakes']}))
            current = self.pulse.wake(wake['id']) or wake
            due = epoch(e['flip']) if e['kind'] == 'W' else wake.get('next_due_at')
            if due is not None and now >= due-30 and not self.has('gate', eid, wake['id']):
                self.emit('gate', eid, key=wake['id'], wake=current, session=self.pulse.session(self.chat_key), due=due, request_index=index)
            if e['kind'] == 'W' and now < epoch(e['flip']) and decoded(current.get('probe_state_json')) and not self.has('baseline', eid, wake['id']):
                self.emit('baseline', eid, key=wake['id'], probe_state_json=current['probe_state_json'], first_probe_at=current.get('updated_at'))
            if e['kind'] == 'W' and wake['kind'] == 'file' and not self.has('watcher_window', eid, wake['id']):
                trigger = decoded(wake['trigger_json'])
                from heartbeat2.schedule import parse_duration
                try: every = parse_duration(trigger['check_every'])
                except (KeyError, TypeError, ValueError): continue
                close = epoch(e['flip']) + max(1800, every + 300)
                self.emit('watcher_window', eid, key=wake['id'], check_every=every, latest_close=close)
                overrun = max(0, close-epoch(e['latest_close']))
                if overrun:
                    e['latest_close'] = datetime.fromtimestamp(close, self.zone).isoformat()
                    shifted = [shift(old, overrun) for old in self.episodes if old['day'] == e['day'] and epoch(old['sends'][0]['at']) > epoch(e['sends'][0]['at'])]
                    self.emit('shift', eid, seconds=overrun, episodes=[e] + shifted)
                    by_id = {x['id']: x for x in shifted}
                    self.episodes = [by_id.get(old['id'], old) for old in self.episodes]
        if e['kind'] == 'M' and not self.has('m3_owner_check', eid):
            gates = [r for r in self.by_episode.get(eid, []) if r['kind'] == 'gate' and r['episode'] == eid and r['request_index'] == 2]
            if gates and now >= epoch(e['due'][2]) and not self.has('m3_owner_check', eid):
                after = [r['message']['id'] for r in self.rows if r['kind'] in ('dm', 'send') and r['message']['out'] and epoch(gates[0]['t']) < epoch(r['message']['date']) <= epoch(e['due'][2])]
                self.emit('m3_owner_check', eid, owner_after=sorted(set(after)))

    def replies(self, e):
        deliveries = sorted(self.deliveries(e), key=lambda d: d['visible_at'])
        if e['kind'] in ('C-reply', 'S'): return
        for n, delivery in enumerate(deliveries, 1):
            if e['kind'] == 'M' and n < 3: continue
            key = f'thanks:{delivery["message"]["id"]}'
            delay = random.Random(f'20261014:reply:{e["id"]}:{n}').randint(1, 5)*60
            if self.clock.now() >= delivery['visible_at'] + delay:
                self.send(e, key, 'thanks', purpose='reply')

    def cleanup(self, e):
        if e['kind'] != 'S': return True
        if any(item[0] == e['id'] for item in self.pending): return False
        self.emit('setup', e['id'], key='archive', action='archive', snapshot=self.pulse.snapshot()) if not self.has('setup', e['id'], 'archive') else None
        for w in self.wakes(e):
            w = self.pulse.wake(w['id'])
            if w and w['status'] == 'active':
                key = f'setup_cancel:{w["id"]}'
                if not self.has('send_intent', e['id'], key):
                    self.emit('setup', e['id'], action='cancel', wake_id=w['id']); self.send(e, key, f'/pulse cancel {w["id"]}', 'setup'); return False
        session = self.pulse.session(self.chat_key)
        if session and not session['enabled'] and not self.has('send_intent', e['id'], 'setup_on'):
            self.emit('setup', e['id'], action='on'); self.send(e, 'setup_on', '/pulse on', 'setup'); return False
        self.emit('setup', e['id'], action='verify_enabled', session=session)
        return bool(session and session['enabled'])

    def tick_episode(self, e):
        self.active = e; eid, now = e['id'], self.clock.now()
        if self.has('resume_cleanup', eid):
            self.archive()
            if self.cleanup(e):
                self.emit('episode_end', eid, snapshot=self.pulse.snapshot()); self.active = None
            return
        if not self.has('episode_start', eid):
            first = next(x for x in self.episodes if x['day'] == e['day'])
            if first['id'] == eid:
                session = self.pulse.session(self.chat_key)
                self.emit('setup', eid, action='verify_enabled_before_day', session=session)
                if not session or not session['enabled']: return
            self.emit('episode_start', eid, schedule=e, frozen=frozen_state(self.home), gateway_starts=gateway_starts(self.home), session=self.pulse.session(self.chat_key))
            if e['kind'] == 'W':
                path = self.soak_dir/(e['job'] + '.status'); path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'RUNNING\n'); self.emit('file_write', eid, path=str(path), mode='wb', bytes='RUNNING\n')
        self.evidence(e)
        for at, key, text in actions(e):
            if now < epoch(at): continue
            if key == 'flip':
                if not self.has('file_write', eid, key):
                    path = self.soak_dir/(e['job'] + '.status')
                    with path.open('ab') as stream: stream.write(b'DONE\n')
                    self.emit('file_write', eid, key=key, path=str(path), mode='ab', bytes='DONE\n')
            else: self.send(e, key, text.replace('{soak_dir}', str(self.soak_dir)), 'request' if key.startswith('request:') else 'reply')
        self.replies(e)
        close = epoch(e['latest_close'])
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
        if now >= close and self.cleanup(e):
            self.archive(); self.emit('episode_end', eid, snapshot=self.pulse.snapshot()); self.active = None

    def requeue(self):
        if any(r['kind'] == 'requeue' for r in self.rows): return
        voids = {r['episode'] for r in self.rows if r['kind'] == 'void'}
        replacements = []
        for e in self.schedule['episodes']:
            if e['id'] in voids:
                replacement = deepcopy(e); replacement.update(id=e['id']+'-r', day=8, replaces=e['id'])
                if e['kind'] == 'W':
                    replacement['job'] += '-r'; replacement['path'] = '{soak_dir}/'+replacement['job']+'.status'
                    replacement['sends'][0]['text'] = f'the {replacement["job"]} job writes its status to {replacement["path"]}; tell me when it says DONE'
                replacements.append(replacement)
        if not replacements: return
        start = datetime.fromisoformat(self.schedule['start']).date() + timedelta(days=7)
        origin = datetime.combine(start, wall_time(8, 30), self.zone)
        rng = random.Random('20261014:8'); gaps = [rng.randint(5, 30) for _ in replacements]
        try: replacements = layout(replacements, origin, gaps)
        except ValueError:
            try: replacements = layout(replacements, origin, [MIN_GAP]*len(replacements))
            except ValueError:
                self.emit('requeue', episodes=[], reason='day8_does_not_fit'); return
        self.emit('requeue', episodes=replacements); self.episodes.extend(replacements)

    def run(self, dry_run=False):
        if dry_run:
            preview(self.schedule, self.until_day, self.soak_dir); return 0
        resumed = bool(self.rows)
        if not self.frozen:
            self.frozen = frozen_state(self.home)
            try:
                commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=5).stdout.strip() or None
            except (OSError, subprocess.SubprocessError): commit = None
            source_hash = hashlib.sha256(Path(__file__).with_name('schedule.py').read_bytes()).hexdigest()
            self.emit('declaration', generator_commit=commit, generator_source_sha256=source_hash, start=self.schedule['start'], tz=self.schedule['tz'], schedule_sha256=self.schedule['sha256'], seed=self.schedule['seed'], frozen=self.frozen)
        if self.rows[0].get('schedule_sha256') != self.schedule['sha256']: raise ValueError('resume schedule mismatch')
        self.emit('runner_start', resumed=resumed)
        if resumed:
            for e in self.episodes:
                if self.has('episode_start', e['id']) and not self.has('episode_end', e['id']):
                    self.active = e; self.void('runner_stopped')
                    if e['kind'] == 'S': self.emit('resume_cleanup', e['id'])
                    else: self.emit('episode_end', e['id'], snapshot=self.pulse.snapshot())
                    self.active = None
            for r in list(self.rows):
                if r['kind'] == 'send' and not self.has('turn', r['episode'], r['key']):
                    self.pending.append((r['episode'], r['key'], r['message'], r['before'], r['purpose']))
        while True:
            if frozen_state(self.home) != self.frozen:
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
            failure = hard_failure(self.analysis_rows + list(self.dm_latest.values())) if self.dirty else None
            self.dirty = False
            if failure:
                self.emit('abort', self.active['id'] if self.active else None, reason=failure, status='FAIL'); return 2
            remaining = [e for e in self.episodes if e['day'] <= self.until_day and not self.has('episode_end', e['id'])]
            if not remaining:
                if self.until_day >= 8:
                    self.requeue()
                    remaining = [e for e in self.episodes if not self.has('episode_end', e['id'])]
                if not remaining: break
            e = remaining[0]
            if self.clock.now() >= epoch(e['sends'][0]['at']): self.tick_episode(e)
            self.clock.sleep(5)
        self.turns(); self.archive(); self.emit('run_end', until_day=self.until_day); return 0


def main():
    p = argparse.ArgumentParser()
    for flag in ('schedule', 'log', 'client-cmd', 'peer', 'profile-home', 'soak-dir', 'chat-key'): p.add_argument('--'+flag, required=True)
    p.add_argument('--until-day', type=int, choices=range(1, 9), default=8); p.add_argument('--dry-run', action='store_true')
    a = p.parse_args(); s = json.loads(Path(a.schedule).read_text())
    if a.dry_run:
        if s['sha256'] != digest({k: v for k, v in s.items() if k != 'sha256'}): raise ValueError('schedule hash mismatch')
        preview(s, a.until_day, a.soak_dir)
        return 0
    return Runner(s, a.log, CommandClient(shlex.split(a.client_cmd), a.peer), a.profile_home, a.soak_dir, a.chat_key, until_day=a.until_day).run()


if __name__ == '__main__': raise SystemExit(main())
