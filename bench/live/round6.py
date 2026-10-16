"""Isolated promise-to-delivery journeys. Pure scoring imports no Hermes."""
import argparse
import fcntl
import json
import math
import os
import random
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import yaml

from .upstream import QuotaError, Upstream

ROOT = Path(__file__).resolve().parents[2]
DATA = yaml.safe_load(Path(__file__).with_name('journey.yaml').read_text())
WINDOWS = {k: v * 60 for k, v in DATA['cells'].items()}
RANK = {'PASS': 0, 'NOT_EXERCISED': 1, 'NOT_RUN': 2, 'VOID': 2, 'FAIL': 3}


def variants(seed, rep):
    rng = random.Random(seed * 100 + rep)
    return dict(tasks=rng.sample(DATA['tasks'], 3), minutes=rng.choice(DATA['minutes']),
                job=rng.choice(DATA['jobs']), project=rng.choice(DATA['projects']))


def deadline_allows(cell, now, until):
    return until is None or now + WINDOWS[cell] + 600 <= until


class CallBudget:
    """One locked counter and stop latch for every physical upstream attempt."""
    def __init__(self, path, maximum):
        self.path, self.maximum, self.reason = Path(path), maximum, ''

    def transaction(self, take=False, stop=''):
        with self.path.open('a+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.seek(0)
            state = json.loads(stream.read() or '{"count": 0, "reason": ""}')
            if stop:
                state['reason'] = state['reason'] or stop
            allowed = not state['reason'] and state['count'] < self.maximum
            if take:
                if allowed:
                    state['count'] += 1
                elif not state['reason']:
                    state['reason'] = 'call_cap'
            self.reason = state['reason']
            stream.seek(0)
            stream.truncate()
            stream.write(json.dumps(state))
            stream.flush()
            return allowed

    def take(self):
        return self.transaction(take=True)

    def stop(self, reason):
        self.transaction(stop=reason)


class JourneyUpstream(Upstream):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, stop_on_429=True, **kwargs)
        self.occurrence = None

    def forward(self, body):
        if self.unavailable:
            return {'error': {'message': 'upstream unavailable; cell void'}}, 503
        current = next((m.get('content', '') for m in reversed(body.get('messages', [])) if m.get('role') == 'user'), '')
        match = re.search(r'⟦pulse o:(\S+) a:(\d+)⟧', str(current))
        if match:
            self.occurrence = match[1] + ':' + match[2]
        response, status = super().forward(body)
        if status < 400 and not any(c.get('message', {}).get('tool_calls') for c in response.get('choices', [])):
            self.occurrence = None
        return response, status


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value or {}


def aggregate(states):
    return max(states, key=lambda state: RANK[state]) if states else 'NOT_RUN'


def score_cell(result):
    bars = {}
    def bar(name, passed, reason, state='FAIL'):
        bars[name] = {'state': 'PASS' if passed else state, 'reason': 'ok' if passed else reason}
    if result['status'] != 'COMPLETE':
        state = 'NOT_RUN' if result['status'] in {'NOT_RUN', 'QUOTA'} else 'VOID'
        return {'state': state, 'reason': result.get('reason', result['status']), 'bars': {}}
    sid, turns = result['cell'], result['turns']
    arms = [t for t in turns if t['role'] == 'arming']
    wakes = [w for t in arms for w in t['created']]
    deliveries = [m for m in result['bot_messages'] if m['id'] not in
                  {i for t in turns for i in t['bot_ids']}]
    final = result['final']
    wake = wakes[0] if len(wakes) == 1 else {}
    due = wake.get('next_due_at')
    if sid == 'R-clock' and result['sent_at'] > result['target_due'] - 720:
        return {'state': 'VOID', 'reason': 'send_slipped', 'bars': {}}
    for turn in turns:
        if turn['role'] in {'reply', 'stop'}:
            bar('B3-2', not turn['created'], 'new_wake_on_' + turn['role'])
    if sid in {'R-rel', 'R-clock', 'M'}:
        expected = 3 if sid == 'M' else 1
        capture = len(wakes) == expected and all(len(t['created']) == 1 for t in arms)
        reason = 'capture'
        for turn in arms:
            for w in turn['created']:
                if not w.get('owner_requested'):
                    reason = 'missing_owner_requested'
                target = result['target_due'] if sid == 'R-clock' else turn['owner']['date'] + turn['minutes'] * 60
                capture &= (w['kind'] == 'at' and w.get('owner_requested') == 1 and
                            w.get('next_due_at') is not None and abs(w['next_due_at'] - target) <= 90)
        bar('B3-1', capture, reason)
        if sid == 'M':
            third = wakes[2] if len(wakes) == 3 else {}
            count = result.get('third_gate', {}).get('session', {}).get('unanswered_count', -1)
            third_due = third.get('next_due_at')
            third_messages = [m for m in deliveries if third_due is not None and third_due - 60 <= m['date'] <= third_due + 180]
            if not third.get('owner_requested'):
                bar('B3-6', False, 'capture_miss', 'NOT_EXERCISED')
            elif count < 2:
                bar('B3-6', False, 'unanswered_below_2', 'NOT_EXERCISED')
            else:
                bar('B3-6', len(third_messages) == 1 and len(deliveries) == 3 and all(
                    sum(w['next_due_at'] - 60 <= m['date'] <= w['next_due_at'] + 180 for m in deliveries) == 1
                    for w in wakes), 'reminder_delivery')
        else:
            bar('B3-3', len(deliveries) == 1 and due is not None and due - 60 <= deliveries[0]['date'] <= due + 180
                and arms[0]['match'].casefold() in deliveries[0]['text'].casefold(), 'delivery_window_or_text')
    elif sid == 'C-reply':
        alternative = wake.get('kind') == 'owner_silent'
        bar('B3-1', len(wakes) == 1 and wake.get('kind') == 'at' and bool(wake.get('cancel_on_reply')),
            'owner_silent_alternative' if alternative else 'capture')
        reply = next(t for t in turns if t['role'] == 'reply')
        after = next((w for w in reply['snapshot']['wakes'] if w['id'] == wake.get('id')), {})
        bar('B3-4', after.get('status') in {'cancelled', 'done'} and after.get('status_reason') == 'owner_replied'
            and not deliveries, 'not_cancelled_or_delivered')
    elif sid == 'W':
        trigger = decoded(wake.get('trigger_json'))
        interval = trigger.get('check_every', 0)
        from heartbeat2.schedule import parse_duration
        try:
            interval = parse_duration(interval)
            pattern = bool(trigger.get('pattern')) and bool(re.search(trigger['pattern'], 'DONE'))
        except (ValueError, TypeError, re.error):
            interval, pattern = 0, False
        bar('B3-1', len(wakes) == 1 and wake.get('kind') == 'file' and trigger.get('path') == result['watch_path']
            and pattern and 0 < interval <= 1500, 'check_every_too_long' if interval > 1500 else 'capture')
        flip = result.get('flipped_at')
        bar('B3-3', flip is not None and result.get('baseline') is not None and len(deliveries) == 1 and
            flip <= deliveries[0]['date'] <= flip + interval + 180 and
            any(term.casefold() in deliveries[0]['text'].casefold() for term in ('DONE', result['variant']['job'])),
            'watch_delivery')
    elif sid == 'S-stop':
        declined = not wakes
        bar('B3-1', len(wakes) == 1 and wake.get('kind') in {'every', 'cron'} and due is not None and
            due >= result['sent_at'] + 3300 and not wake.get('cancel_on_reply'),
            'declined' if declined else 'cancel_on_reply_on_S' if wake.get('cancel_on_reply') else 'capture',
            'NOT_EXERCISED' if declined else 'FAIL')
        stop = next(t for t in turns if t['role'] == 'stop')
        after = next((w for w in stop['snapshot']['wakes'] if w['id'] == wake.get('id')), {})
        engine_cancelled = after.get('status_reason') == 'owner_replied'
        stopped = (after.get('status') in {'cancelled', 'paused'} or stop['snapshot']['session'].get('enabled') == 0)
        claims = [o for o in final['occurrences'] if o.get('claimed_at') is not None and o['claimed_at'] >= stop['owner']['date']]
        unexercised = declined or engine_cancelled and not deliveries and not claims
        incomplete = not unexercised and stopped and not claims and not deliveries and (
            due is None or final.get('at', 0) < due + 600)
        bar('B3-5', not unexercised and not incomplete and stopped and not claims and not deliveries,
            'declined' if declined else 'owner_replied' if unexercised else 'observation_incomplete' if incomplete else 'stop_failed',
            'NOT_EXERCISED' if unexercised else 'VOID' if incomplete else 'FAIL')
    state = aggregate([b['state'] for b in bars.values()])
    return {'state': state, 'reason': next((b['reason'] for b in bars.values() if b['state'] == state), 'ok'), 'bars': bars}


def judge_packets(result):
    replies = {i for t in result.get('turns', []) for i in t['bot_ids']}
    for turn in result.get('turns', []):
        if turn['role'] == 'arming':
            yield turn, 'B3-7', [m for m in result['bot_messages'] if m['id'] in turn['bot_ids']], turn['upstream_rows']
    for message in result.get('bot_messages', []):
        if message['id'] not in replies:
            rows = [r for r in result.get('upstream_rows', []) if r.get('wake') and r['started_at'] <= message['date'] + 1]
            # Scope the packet to the latest wake occurrence, including tool continuations.
            latest = next((r.get('occurrence') for r in reversed(rows)), None)
            rows = [r for r in rows if r.get('occurrence') == latest]
            yield None, 'B3-8', [message], rows


def write_score(out):
    from .adjudicate import turn_fields
    from .round3 import turn_pulse
    declaration = json.loads((out / 'declaration.json').read_text())
    results, packets = [], []
    for sid, rep in declaration['scheduled']:
        path = out / f'{sid}-{rep}.json'
        result = json.loads(path.read_text()) if path.exists() else dict(cell=sid, rep=rep, status='NOT_RUN', reason='missing_result')
        results.append(dict(cell=sid, rep=rep, **score_cell(result)))
        for turn, bar, messages, rows in judge_packets(result):
            measured = dict(type='capture' if turn else 'wake', owner_message=turn['owner']['text'] if turn else '',
                            upstream_rows=rows, timezone=declaration['timezone'], sent_at=result['sent_at'])
            fields, _ = turn_fields(measured, round=5)
            _, pulse_results = turn_pulse(rows)
            packets.append(dict(id=f'{sid}-{rep}-{bar}-{messages[0]["id"] if messages else "missing"}',
                bar=bar, history=[dict(role='user', content=t['owner']['text']) for t in result['turns']],
                wake_note='; '.join(t['owner']['text'] for t in result['turns'] if t['role'] == 'arming'),
                owner_message=turn['owner']['text'] if turn else None,
                evidence=[dict(due=w.get('next_due_at'), note=w.get('note')) for t in result['turns'] for w in t['created']],
                delivered_text='\n'.join(m['text'] for m in messages), pulse_results=pulse_results,
                receipts=[r['receipt'] for r in pulse_results if 'receipt' in r],
                tool_contract=fields['pulse_description'], **fields))
    summary = dict(round=6, repetitions=results, cells={sid: aggregate([r['state'] for r in results if r['cell'] == sid])
                   for sid in declaration['cells']})
    (out / 'packets.jsonl').write_text(''.join(json.dumps(p) + '\n' for p in packets))
    (out / 'summary.json').write_text(json.dumps(summary, indent=2))
    (out / 'summary.md').write_text('# Round 6 mechanical bars\n\n' + '\n'.join(
        f'- {r["cell"]}:{r["rep"]}: {r["state"]} ({r["reason"]})' for r in results) +
        '\n\nOverall: ' + ', '.join(f'{s} {v}' for s, v in summary['cells'].items()) +
        '\n\nNot proven: live gateways, other models/toolsets, customer sessions, or a rate claim; B3-7/B3-8 need judging.\n')
    return summary


def cell(args, sid, rep):
    # No Hermes imports above this boundary, and never use a shared home.
    tempfile.tempdir = str(args.out)
    parent = Path(tempfile.mkdtemp(prefix='r6-parent-'))
    os.environ.update(HOME=str(parent), HERMES_HOME=str(parent / '.hermes'), HERMES_TIMEZONE=args.tz,
                      HERMES_DISABLE_LAZY_INSTALLS='1', PYTHONDONTWRITEBYTECODE='1')
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(args.pin), str(ROOT / 'tests/e2e')]
    try:
        import hermes_cli
        valid_pin = Path(hermes_cli.__file__).resolve().is_relative_to(args.pin)
    except (ImportError, TypeError):
        valid_pin = False
    if not valid_pin:
        print('hermes_cli must resolve inside --pin', file=sys.stderr)
        return 2
    from loopback import install
    install(allow_hosts=() if args.upstream == 'fake' else (urlsplit(args.upstream_url).hostname,))
    from harness import PulseRig
    from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall
    from .director import ForwardAllDirector

    variant = variants(args.seed, rep)
    result = dict(cell=sid, rep=rep, status='PENDING', variant=variant, turns=[], bot_messages=[], snapshots=[])
    def fake_reply(record):
        messages = record['body']['messages']
        if messages[-1]['role'] == 'tool':
            return Text(' '.join(t['task'] for t in variant['tasks']) + ' DONE')
        current = next(m['content'] for m in reversed(messages) if m['role'] == 'user')
        if '⟦pulse o:' in str(current):
            return ToolCall('pulse', dict(action='respond', notify=True, outcome='progress', summary=str(current)[-100:]))
        if 'all good, no need' in str(current):
            return Text('Understood.')
        if 'stop checking in on me' in str(current):
            return ToolCall('pulse', dict(action='cancel', id='all'))
        turn = result['turns'][-1]
        trigger = dict(kind='file', path=result['watch_path'], pattern='DONE', check_every='20m') if sid == 'W' else (
            dict(kind='every', every='1h') if sid == 'S-stop' else dict(kind='at', at=datetime.fromtimestamp(
                result['target_due'] if sid == 'R-clock' else turn['owner']['sent_at'] + turn['minutes'] * 60,
                ZoneInfo(args.tz)).isoformat()))
        return ToolCall('pulse', dict(action='create', trigger=trigger, note=turn['owner']['text'],
            owner_asked=sid in {'R-rel', 'R-clock', 'M'}, until={'owner_replies': sid == 'C-reply'}))
    fake = FakeLLMServer(fake_reply) if args.upstream == 'fake' else None
    if fake:
        fake.start()
        args.key_env = 'R6_FAKE_KEY'
        os.environ[args.key_env] = 'fake-loopback-only'
    budget = CallBudget(args.out / 'calls.json', args.max_calls)
    upstream = JourneyUpstream(fake.base_url if fake else args.upstream_url, args.model, args.key_env,
                               args.out / f'{sid}-{rep}.jsonl', call_budget=budget)
    upstream.scenario, upstream.rep = sid, rep
    director = ForwardAllDirector(upstream)
    rig = PulseRig(f'r6-{sid}-{rep}', args.pin, director=director, replace_settings={'timezone': args.tz},
        config={'timezone': args.tz, 'model': {'streaming': False}, 'logging': {'level': 'INFO'}})
    def snapshot():
        sessions = rig.rows('sessions')
        return dict(at=time.time(), wakes=rig.rows('wakes'), occurrences=rig.rows('occurrences'),
                    session=sessions[0] if len(sessions) == 1 else {}, ledger=rig.rows('ledger'))
    def guard():
        if not budget.transaction() and not budget.reason:
            budget.stop('call_cap')
        if upstream.quota or budget.reason:
            raise QuotaError(upstream.reason or budget.reason)
        if upstream.unavailable:
            raise RuntimeError('upstream_unavailable')
        if args.until is not None and time.time() >= args.until:
            raise RuntimeError('deadline')
    original_wait = rig.wait
    def guarded_wait(predicate, what, timeout=90):
        def checked():
            guard()
            return predicate()
        return original_wait(checked, what, timeout)
    rig.wait = guarded_wait
    def wait_to(target):
        while time.time() < target:
            guard()
            if rig.gateway.proc.poll() is not None:
                raise RuntimeError('gateway_exited')
            time.sleep(max(0, min(.5, target - time.time())))
        guard()
    def bots():
        return [dict(id=str(c.response['message_id']), text=c.response['text'], date=c.response['date'])
                for c in rig.driver.sends(rig.driver.user_id)]
    def send(text, role='arming', minutes=20, match=''):
        before, call_start = snapshot(), len(upstream.rows)
        prior = {m['id'] for m in bots()}
        turn = dict(role=role, minutes=minutes, match=match, owner=dict(text=text, sent_at=time.time()),
                    bot_ids=[], created=[], upstream_rows=[], snapshot=before)
        result['turns'].append(turn)
        original_dm = rig.driver.dm
        def recorded_dm(text):
            inbound = original_dm(text)
            turn['owner'].update(id=inbound.message_id, date=inbound.raw['message']['date'])
            result.setdefault('sent_at', turn['owner']['date'])
            return inbound
        rig.driver.dm = recorded_dm
        try:
            rig.owner(text)
        finally:
            rig.driver.dm = original_dm
            turn.update(snapshot=snapshot(), bot_ids=[m['id'] for m in bots() if m['id'] not in prior],
                        upstream_rows=upstream.rows[call_start:])
            turn['created'] = [w for w in turn['snapshot']['wakes'] if w['id'] not in {w['id'] for w in before['wakes']}]
            result['snapshots'].append(turn['snapshot'])
        guard()
        return turn
    def terminated(signum, frame):
        raise QuotaError('quota')
    signal.signal(signal.SIGTERM, terminated)
    code = 0
    try:
        key = os.environ.pop(args.key_env)
        try:
            rig.start(bootstrap=False)
        finally:
            os.environ[args.key_env] = key
        cfg = yaml.safe_load((rig.gateway.hermes_home / 'config.yaml').read_text())
        if (cfg['plugins']['entries']['heartbeat2']['settings'] != {'timezone': args.tz} or
            cfg.get('platform_toolsets', {}).get('telegram') != ['heartbeat2'] or
            'api_max_retries' not in cfg.get('agent', {})):
            raise RuntimeError('rig_config')
        task = variant['tasks'][0]
        minutes = variant['minutes']
        if sid == 'W':
            path = rig.root / 'jobs' / (variant['job'] + '.log')
            path.parent.mkdir()
            path.write_text('started\n')
            result['watch_path'] = str(path.resolve())
            text = f'tell me when {path.resolve()} says DONE'
        elif sid == 'S-stop':
            text = f'check on me every hour until the {variant["project"]} deploy is finished'
            result['label'] = 'stop before first tick'
        elif sid == 'R-clock':
            result['target_due'] = math.ceil((time.time() + 900) / 300) * 300
            clock = datetime.fromtimestamp(result['target_due'], ZoneInfo(args.tz)).strftime('%H:%M')
            text = f'remind me at {clock} to {task["task"]}'
        elif sid == 'C-reply':
            text = f'check in with me in 20 minutes about {task["task"]} unless I reply before then'
        else:
            minutes = 20 if sid == 'M' else minutes
            text = f'remind me in {minutes} minutes to {task["task"]}'
        arm = send(text, minutes=minutes, match=task['match'])
        end = result['sent_at'] + WINDOWS[sid]
        if sid == 'R-clock' and result['sent_at'] > result['target_due'] - 720:
            raise RuntimeError('send_slipped')
        if sid == 'C-reply':
            wait_to(result['sent_at'] + 300)
            send('all good, no need', 'reply')
        elif sid == 'S-stop':
            send('stop checking in on me', 'stop')
            if len(arm['created']) == 1 and arm['created'][0].get('next_due_at'):
                end = max(end, arm['created'][0]['next_due_at'] + 600)
                if args.until is not None and end > args.until:
                    raise RuntimeError('observation_incomplete')
        elif sid == 'M':
            for minutes, task in zip((40, 60), variant['tasks'][1:]):
                send(f'remind me in {minutes} minutes to {task["task"]}', minutes=minutes, match=task['match'])
            third = result['turns'][-1]['created']
            if len(third) == 1 and third[0].get('next_due_at'):
                wait_to(min(end, third[0]['next_due_at'] - 1))
                result['third_gate'] = snapshot()
        elif sid == 'W':
            baseline = None
            while time.time() < end:
                current = rig.rows('wakes')
                if len(arm['created']) == 1:
                    row = next((w for w in current if w['id'] == arm['created'][0]['id']), {})
                    state = decoded(row.get('probe_state_json'))
                    if 'offset' in state:
                        baseline = dict(at=time.time(), wake=row)
                        break
                wait_to(min(end, time.time() + .5))
            result['baseline'] = baseline
            if baseline:
                wait_to(max(result['sent_at'] + 600, time.time()))
                with path.open('a') as stream:
                    stream.write('DONE\n')
                result['flipped_at'] = int(time.time())
        wait_to(end)
        result.update(status='COMPLETE', final=snapshot())
    except Exception as exc:
        code = 2 if isinstance(exc, QuotaError) or upstream.quota else 3
        result.update(status='QUOTA' if code == 2 else 'VOID', reason=upstream.reason or str(exc))
    finally:
        # Preserve partial owner sends too, even if their model turn failed.
        result.update(bot_messages=bots(), upstream_rows=upstream.rows, final=snapshot())
        try:
            rig.close()
        finally:
            if fake:
                fake.stop()
            (args.out / f'{sid}-{rep}.json').write_text(json.dumps(result, indent=2))
    return code


def orchestrate(args, argv, selected):
    jobs = [(s, r, 0) for s in selected for r in range(1, args.reps + 1)]
    active, code = [], 0
    def not_run(sid, rep, reason):
        path = args.out / f'{sid}-{rep}.json'
        result = json.loads(path.read_text()) if path.exists() else dict(cell=sid, rep=rep)
        result.update(status='NOT_RUN', reason=reason)
        path.write_text(json.dumps(result))
    try:
        while jobs or active:
            # Reap before launching: a completed quota cell must veto the queue.
            for proc, log, sid, rep, attempt in list(active):
                rc = proc.poll()
                if rc is None:
                    continue
                log.close()
                active.remove((proc, log, sid, rep, attempt))
                if rc == 2:
                    code = 2
                elif rc == 3 and not attempt:
                    (args.out / f'{sid}-{rep}.json').replace(args.out / f'{sid}-{rep}-void.json')
                    jobs.append((sid, rep, 1))
                elif rc and code != 2:
                    code = max(code, 3)
            if code == 2:
                for proc, log, sid, rep, attempt in active:
                    proc.terminate()
                for proc, log, sid, rep, attempt in active:
                    proc.wait(timeout=30)
                    log.close()
                    not_run(sid, rep, 'quota')
                for sid, rep, attempt in jobs:
                    not_run(sid, rep, 'quota')
                return 2
            while jobs and len(active) < args.workers:
                sid, rep, attempt = jobs.pop(0)
                if not deadline_allows(sid, time.time(), args.until):
                    not_run(sid, rep, 'deadline')
                    continue
                log = (args.out / f'{sid}-{rep}-{attempt}.log').open('w')
                proc = subprocess.Popen([sys.executable, '-m', 'bench.live.round6', *argv, '--cell', f'{sid}:{rep}'],
                                        stdout=log, stderr=subprocess.STDOUT)
                active.append((proc, log, sid, rep, attempt))
            time.sleep(.1)
    finally:
        for proc, log, *_ in active:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=30)
            log.close()
    return code


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    scoring = bool(argv and argv[0] == 'score')
    if scoring:
        argv.pop(0)
    parser.add_argument('--out', type=Path, required=True)
    if not scoring:
        parser.add_argument('--pin', type=Path, required=True)
        parser.add_argument('--upstream', choices=['fake'])
        parser.add_argument('--upstream-url')
        parser.add_argument('--model')
        parser.add_argument('--key-env')
        parser.add_argument('--reps', type=int, default=3)
        parser.add_argument('--workers', type=int, choices=[1, 2], default=2)
        parser.add_argument('--cells', default=','.join(WINDOWS))
        parser.add_argument('--tz', default='Asia/Tokyo')
        parser.add_argument('--max-calls', type=int, default=400)
        parser.add_argument('--until')
        parser.add_argument('--seed', type=int, default=20261017)
        parser.add_argument('--cell', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.out = args.out.resolve()
    if args.out == ROOT or ROOT in args.out.parents:
        parser.error('results must stay outside the repository')
    if scoring:
        write_score(args.out)
        return 0
    args.pin = args.pin.resolve()
    if '.hermes' in args.pin.parts or not (args.pin / 'tests/fakes/fake_llm_provider.py').is_file():
        parser.error('select an isolated Hermes pin containing E2E helpers')
    if Path(sys.prefix).resolve().is_relative_to(Path.home() / '.hermes'):
        parser.error('sys.prefix must be outside ~/.hermes')
    if args.upstream != 'fake' and not (args.upstream_url and args.model and args.key_env and args.until):
        parser.error('real upstream requires --upstream-url, --model, --key-env and --until')
    try:
        ZoneInfo(args.tz)
        deadline = datetime.fromisoformat(args.until) if args.until else None
        if deadline is not None and deadline.tzinfo is None:
            parser.error('--until must include a timezone')
        args.until = deadline.timestamp() if deadline else None
    except (ValueError, KeyError):
        parser.error('invalid --until or --tz')
    if args.until is not None and args.until <= time.time():
        parser.error('--until must be in the future')
    if args.upstream != 'fake' and (urlsplit(args.upstream_url).scheme not in {'http', 'https'} or not urlsplit(args.upstream_url).hostname):
        parser.error('upstream URL must name one http(s) host')
    selected = args.cells.split(',')
    if set(selected) - WINDOWS.keys() or args.reps < 1 or args.max_calls < 1:
        parser.error('unknown cells or nonpositive --reps/--max-calls')
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    sys.dont_write_bytecode = True
    args.model = args.model or 'fake-primary'
    args.out.mkdir(parents=True, exist_ok=True)
    if args.cell:
        sid, rep = args.cell.split(':')
        return cell(args, sid, int(rep))
    sys.path.insert(0, str(ROOT / 'tests/e2e'))
    from loopback import install
    install(allow_hosts=() if args.upstream == 'fake' else (urlsplit(args.upstream_url).hostname,))
    (args.out / 'declaration.json').write_text(json.dumps(dict(round=6, timezone=args.tz, seed=args.seed,
        cells=selected, scheduled=[(s, r) for s in selected for r in range(1, args.reps + 1)],
        commit=subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip())))
    return orchestrate(args, argv, selected)


if __name__ == '__main__':
    raise SystemExit(main())
