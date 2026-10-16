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
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import yaml

from .upstream import QuotaError, Upstream

ROOT = Path(__file__).resolve().parents[2]
DATA = yaml.safe_load(Path(__file__).with_name('journey.yaml').read_text())
WINDOWS = {k: v * 60 for k, v in DATA['cells'].items()}
RANK = {'PASS': 0, 'NOT_EXERCISED': 1, 'NOT_RUN': 2, 'VOID': 2, 'PENDING_JUDGE': 3, 'FAIL': 4}
CLAIM_CLASS = ('isolated-rig live-model journey smoke: pinned Hermes, loopback Bot API, one model, '
               'pulse-only toolset, compression off, default plugin settings, n=3 per cell, no rate claim')
STOP_LABEL = 'stop before first tick'
FAKE_BANNER = 'FAKE UPSTREAM: plumbing check only; no claim.'


def variants(seed, rep):
    rng = random.Random(seed * 100 + rep)
    return dict(tasks=rng.sample(DATA['tasks'], 3), minutes=rng.choice(DATA['minutes']),
                job=rng.choice(DATA['jobs']), project=rng.choice(DATA['projects']))


def deadline_allows(cell, now, until):
    return until is None or now + WINDOWS[cell] + 600 <= until


def quiet_intersects(start, end, tz):
    """Closed observation interval versus the manifest's local quiet hours."""
    from heartbeat2.schedule import quiet_end
    quiet = yaml.safe_load((ROOT / 'plugin.yaml').read_text())['config_schema']['quiet_hours']['default']
    if quiet_end(start, quiet, tz) is not None:
        return True
    if not quiet:
        return False
    hour, minute = map(int, quiet.split('-')[0].split(':'))
    zone = ZoneInfo(tz)
    day = datetime.fromtimestamp(start, zone).replace(hour=hour, minute=minute, second=0, microsecond=0)
    while day.timestamp() <= end:
        if day.timestamp() >= start and quiet_end(day.timestamp(), quiet, tz) is not None:
            return True
        day += timedelta(days=1)
    return False


def upstream_name(args):
    return 'fake' if args.upstream == 'fake' else urlsplit(args.upstream_url).hostname


def declaration_for(args, selected):
    sibling = Path(str(args.pin) + '.COMMIT')
    pin_commit = sibling.read_text().strip() if sibling.is_file() else (
        subprocess.check_output(['git', '-C', str(args.pin), 'rev-parse', 'HEAD'], text=True).strip()
        if (args.pin / '.git').exists() else None)
    return dict(round=6, upstream=upstream_name(args), model=args.model, tz=args.tz, until=args.until,
                seed=args.seed, reps=args.reps, cells=selected, max_calls=args.max_calls,
                pin=str(args.pin), pin_commit=pin_commit,
                scheduled=[(s, r) for s in selected for r in range(1, args.reps + 1)],
                commit=subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip(),
                dirty=bool(subprocess.check_output(['git', '-C', str(ROOT), 'status', '--porcelain'], text=True)))


def capture_turn(rig, text, result, upstream, snapshot, bots, guard, *, role='arming', minutes=20, match=''):
    """Capture only after run-idle AND five seconds of Bot API delivery silence."""
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
        rig.gateway.wait_idle(timeout=180)
        started = changed = time.monotonic()
        seen = {m['id'] for m in bots()}
        while True:
            guard()
            current = bots()
            ids = {m['id'] for m in current}
            now = time.monotonic()
            if ids - seen:
                changed = now
            seen |= ids
            if now - started >= 60:
                raise RuntimeError('turn_not_settled')
            if now - changed >= 5:
                break
            time.sleep(.25)
        turn.update(snapshot=snapshot(), bot_ids=[m['id'] for m in current if m['id'] not in prior],
                    upstream_rows=upstream.rows[call_start:])
        turn['created'] = [w for w in turn['snapshot']['wakes'] if w['id'] not in {w['id'] for w in before['wakes']}]
        result['snapshots'].append(turn['snapshot'])
    finally:
        rig.driver.dm = original_dm
    guard()
    return turn


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
    status = result.get('status')
    if status in {'NOT_RUN', 'QUOTA', 'TERMINATED'}:
        return dict(state='NOT_RUN', reason='terminated' if status == 'TERMINATED' else result.get('reason', status), bars={})
    try:
        if (not isinstance(result.get('sent_at'), (int, float)) or not result.get('turns') or
                not isinstance(result.get('final'), dict) or not result['final'] or
                any(not isinstance(t.get('snapshot'), dict) or not t['snapshot'] for t in result['turns'])):
            raise KeyError('incomplete')
        return _score_cell(result)
    except (KeyError, IndexError, StopIteration, TypeError, ValueError, AttributeError):
        return dict(state='VOID', reason='incomplete_evidence', bars={})


def _score_cell(result):
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
                bar('B3-6', len(third_messages) == 1, 'third_reminder_delivery')
            bar('B3-3', len(wakes) == 3 and all(
                sum(w['next_due_at'] - 60 <= m['date'] <= w['next_due_at'] + 180 for m in deliveries) == 1
                for w in wakes[:2]), 'reminder_delivery')
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
            and due is not None and not any(m['date'] <= due + 300 for m in deliveries), 'not_cancelled_or_delivered')
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
    if result.get('status') != 'COMPLETE' or score_cell(result)['reason'] == 'incomplete_evidence':
        return
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


def summarize(declaration, results, fake=False):
    cells = {sid: 'PLUMBING' if fake else aggregate([r['state'] for r in results if r['cell'] == sid])
             for sid in declaration['cells']}
    reasons = {sid: FAKE_BANNER if fake else next((r['reason'] for r in results
               if r['cell'] == sid and r['state'] == state), 'missing_result') for sid, state in cells.items()}
    return dict(round=6, claim_class=CLAIM_CLASS, labels={'S-stop': STOP_LABEL}, declaration=declaration,
                fake=fake, repetitions=results, cells=cells, cell_reasons=reasons)


def write_summary(out, summary, name):
    (out / f'{name}.json').write_text(json.dumps(summary, indent=2))
    lines = ([FAKE_BANNER, ''] if summary['fake'] else []) + [f'# Round 6 {name}', '',
        'Claim class: ' + summary['claim_class'], 'S-stop: ' + STOP_LABEL, '',
        'Declaration: ' + json.dumps(summary['declaration'], sort_keys=True), '']
    lines += [f'- {r["cell"]}:{r["rep"]}: {r["state"]} ({r["reason"]})' for r in summary['repetitions']]
    lines += ['', *[f'- {sid}: {state} ({summary["cell_reasons"][sid]})' for sid, state in summary['cells'].items()], '',
        'Not proven: live gateways, other models/toolsets, customer sessions, or a rate claim; '
        'mechanical scores alone do not prove B3-7/B3-8.', '']
    (out / f'{name}.md').write_text('\n'.join(lines))


def write_score(out):
    from .adjudicate import turn_fields
    from .round3 import turn_pulse
    declaration = json.loads((out / 'declaration.json').read_text())
    results, packets = [], []
    fake = declaration.get('upstream') == 'fake'
    for sid, rep in declaration['scheduled']:
        path = out / f'{sid}-{rep}.json'
        result = json.loads(path.read_text()) if path.exists() else dict(cell=sid, rep=rep, status='NOT_RUN', reason='missing_result')
        fake |= result.get('upstream') == 'fake'
        results.append(dict(cell=sid, rep=rep, upstream=result.get('upstream', declaration.get('upstream')), **score_cell(result)))
        for turn, bar, messages, rows in judge_packets(result):
            index = next((i for i, t in enumerate(result['turns']) if t is turn), None)
            history_turns = result['turns'][:index + 1] if index is not None else result['turns']
            measured = dict(type='capture' if turn else 'wake', owner_message=turn['owner']['text'] if turn else '',
                            upstream_rows=rows, timezone=declaration.get('tz', declaration.get('timezone', 'UTC')),
                            sent_at=result['sent_at'])
            fields, _ = turn_fields(measured, round=5)
            _, pulse_results = turn_pulse(rows)
            packet_id = f'{sid}-{rep}-{bar}-' + (f'turn-{index}' if turn else str(messages[0]['id']))
            packets.append(dict(id=packet_id, cell=sid, rep=rep,
                bar=bar, history=[dict(role='user', content=t['owner']['text']) for t in history_turns],
                wake_note='; '.join(t['owner']['text'] for t in history_turns if t['role'] == 'arming'),
                owner_message=turn['owner']['text'] if turn else None,
                evidence=[json.dumps(dict(due=w.get('next_due_at'), note=w.get('note'))) for t in history_turns for w in t['created']],
                delivered_text='\n'.join(m['text'] for m in messages), pulse_results=pulse_results,
                receipts=[r['receipt'] for r in pulse_results if 'receipt' in r],
                tool_contract=fields['pulse_description'], **fields))
    summary = summarize(declaration, results, fake)
    (out / 'packets.json').write_text(json.dumps(packets, indent=2))
    write_summary(out, summary, 'summary')
    return summary


def merge_verdicts(out, verdicts):
    """Validate external judgments and combine them with immutable mechanical bars."""
    summary = json.loads((out / 'summary.json').read_text())
    packets = json.loads((out / 'packets.json').read_text())
    known = {p['id']: p for p in packets}
    if len(known) != len(packets):
        raise ValueError('duplicate packet id in packets')
    if not isinstance(verdicts, list):
        raise ValueError('verdicts must be a JSON array')
    seen = {}
    for verdict in verdicts:
        if not isinstance(verdict, dict) or verdict.get('id') not in known or verdict['id'] in seen:
            raise ValueError('unknown or duplicate packet id')
        if any(type(verdict.get(k)) is not bool for k in ('useful', 'narration', 'overpromise')):
            raise ValueError('every judgment must be boolean')
        why = verdict.get('why')
        if not isinstance(why, str) or not why.strip() or len(why.splitlines()) != 1 or any(c in why for c in ('\n', '\r')):
            raise ValueError('why must be a nonempty one-line explanation')
        seen[verdict['id']] = verdict
    results = []
    for mechanical in summary['repetitions']:
        result = dict(mechanical, bars={k: dict(v) for k, v in mechanical['bars'].items()})
        for bar in ('B3-7', 'B3-8'):
            group = [p for p in packets if p['cell'] == result['cell'] and p['rep'] == result['rep'] and p['bar'] == bar]
            if not group:
                continue
            states = []
            for packet in group:
                verdict = seen.get(packet['id'])
                failed = verdict and (verdict['overpromise'] if bar == 'B3-7' else not verdict['useful'] or verdict['narration'])
                states.append('PENDING_JUDGE' if verdict is None else 'FAIL' if failed else 'PASS')
            state = aggregate(states)
            result['bars'][bar] = dict(state=state, reason='missing_verdict' if state == 'PENDING_JUDGE' else
                                      'overpromise' if state == 'FAIL' and bar == 'B3-7' else
                                      'not_useful_or_narration' if state == 'FAIL' else 'ok')
        state = aggregate([mechanical['state'], *[b['state'] for b in result['bars'].values()]])
        result.update(state=state, reason=next((b['reason'] for b in result['bars'].values() if b['state'] == state), mechanical['reason']))
        results.append(result)
    final = summarize(summary['declaration'], results, summary['fake'])
    final['verdicts'] = verdicts
    write_summary(out, final, 'final')
    return final


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
        return 4
    from loopback import install
    install(allow_hosts=() if args.upstream == 'fake' else (urlsplit(args.upstream_url).hostname,))
    from harness import PulseRig
    from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall
    from .director import ForwardAllDirector

    variant = variants(args.seed, rep)
    result = dict(cell=sid, rep=rep, upstream=upstream_name(args), status='PENDING',
                  variant=variant, turns=[], bot_messages=[], snapshots=[])
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
    fake = upstream = rig = None
    budget = CallBudget(args.out / 'calls.json', args.max_calls)
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
    def guarded_wait(predicate, what, timeout=90):
        def checked():
            guard()
            return predicate()
        return original_wait(checked, what, timeout)
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
        return capture_turn(rig, text, result, upstream, snapshot, bots, guard,
                            role=role, minutes=minutes, match=match)
    def terminated(signum, frame):
        raise CellTerminated('terminated')
    previous_handlers = {sig: signal.signal(sig, terminated) for sig in (signal.SIGTERM, signal.SIGINT)}
    code = 0
    try:
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
        original_wait = rig.wait
        rig.wait = guarded_wait
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
        if quiet_intersects(result['sent_at'], end, args.tz):
            raise RuntimeError('quiet_hours')
        if sid == 'R-clock' and result['sent_at'] > result['target_due'] - 720:
            raise RuntimeError('send_slipped')
        if sid == 'C-reply':
            wait_to(result['sent_at'] + 300)
            send('all good, no need', 'reply')
        elif sid == 'S-stop':
            send('stop checking in on me', 'stop')
            if len(arm['created']) == 1 and arm['created'][0].get('next_due_at'):
                end = max(end, arm['created'][0]['next_due_at'] + 600)
                if quiet_intersects(result['sent_at'], end, args.tz):
                    raise RuntimeError('quiet_hours')
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
    except CellTerminated:
        budget.transaction()
        quota = budget.reason or (upstream.reason if upstream and upstream.quota else '')
        code = 2
        result.update(status='QUOTA' if quota else 'TERMINATED', reason=quota or 'terminated')
    except Exception as exc:
        code = 2 if isinstance(exc, QuotaError) or upstream and upstream.quota else 3
        result.update(status='QUOTA' if code == 2 else 'VOID', reason=(upstream.reason if upstream else '') or str(exc))
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        cleanup_cell(args.out / f'{sid}-{rep}.json', result, rig, fake, upstream, bots, snapshot)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    return code


class CellTerminated(Exception):
    pass


def cleanup_cell(path, result, rig, fake, upstream, bots, snapshot):
    # A second signal must not interrupt gateway teardown or evidence preservation.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    errors = []
    try:
        if rig:
            try:
                result.update(bot_messages=bots(), final=snapshot())
            except Exception as exc:
                errors.append(type(exc).__name__)
            try:
                rig.close()
            except Exception as exc:
                errors.append(type(exc).__name__)
    finally:
        try:
            if fake:
                fake.stop()
        except Exception as exc:
            errors.append(type(exc).__name__)
        if upstream:
            result['upstream_rows'] = upstream.rows
        if errors:
            result['cleanup_errors'] = errors
            if result['status'] == 'COMPLETE':
                result.update(status='VOID', reason='cleanup_failed')
        try:
            path.write_text(json.dumps(result, indent=2))
        except Exception as exc:
            print('result write failed: ' + type(exc).__name__, file=sys.stderr)


def orchestrate(args, argv, selected):
    jobs = [(s, r, 0) for s in selected for r in range(1, args.reps + 1)]
    scheduled = [(s, r) for s in selected for r in range(1, args.reps + 1)]
    active, stop_reason = [], ''
    def stopped(signum, frame):
        nonlocal stop_reason
        stop_reason = 'terminated'
    previous_handlers = {sig: signal.signal(sig, stopped) for sig in (signal.SIGTERM, signal.SIGINT)}
    def read(sid, rep):
        path = args.out / f'{sid}-{rep}.json'
        return json.loads(path.read_text()) if path.exists() else dict(cell=sid, rep=rep)
    def record(sid, rep, reason, rerun=False):
        result = read(sid, rep)
        if rerun and result.get('status') == 'VOID':
            result['reason'] = result.get('reason', 'void') + ';rerun_refused:' + reason
        else:
            result.update(status='NOT_RUN', reason=reason)
        result['upstream'] = upstream_name(args)
        (args.out / f'{sid}-{rep}.json').write_text(json.dumps(result))
    def shutdown(reason):
        for proc, *_ in active:
            if proc.poll() is None:
                proc.terminate()
        deadline = time.monotonic() + 60
        for proc, log, sid, rep, attempt in active:
            try:
                proc.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            finally:
                log.close()
            record(sid, rep, reason)
        active.clear()
        for sid, rep, attempt in jobs:
            record(sid, rep, reason)
        jobs.clear()
        if reason == 'refused':
            for sid, rep in scheduled:
                record(sid, rep, reason)
    try:
        while jobs or active:
            # Reap before launching: a completed quota/refusal cell vetoes the queue.
            for item in list(active):
                proc, log, sid, rep, attempt = item
                rc = proc.poll()
                if rc is None:
                    continue
                log.close()
                active.remove(item)
                result = read(sid, rep)
                if rc == 4:
                    stop_reason = 'refused'
                elif rc == 2 and stop_reason not in {'refused', 'terminated'}:
                    stop_reason = 'terminated' if result.get('status') == 'TERMINATED' else 'quota'
                elif rc not in (0, 2, 4) or result.get('status') == 'VOID':
                    if result.get('status') != 'VOID':
                        result.update(status='VOID', reason='incomplete_evidence', upstream=upstream_name(args))
                        (args.out / f'{sid}-{rep}.json').write_text(json.dumps(result))
                    if not attempt:
                        (args.out / f'{sid}-{rep}-void.json').write_text(json.dumps(result))
                        jobs.append((sid, rep, 1))
            if stop_reason:
                shutdown(stop_reason)
                return 2
            while jobs and len(active) < args.workers and not stop_reason:
                sid, rep, attempt = jobs.pop(0)
                now = time.time()
                reason = 'deadline' if not deadline_allows(sid, now, args.until) else (
                    'quiet_hours' if quiet_intersects(now, now + WINDOWS[sid] + 600, args.tz) else '')
                if reason:
                    record(sid, rep, reason, rerun=bool(attempt))
                    continue
                log = (args.out / f'{sid}-{rep}-{attempt}.log').open('w')
                try:
                    proc = subprocess.Popen([sys.executable, '-m', 'bench.live.round6', *argv, '--cell', f'{sid}:{rep}'],
                                            stdout=log, stderr=subprocess.STDOUT)
                except Exception:
                    log.close()
                    raise
                active.append((proc, log, sid, rep, attempt))
            if stop_reason:
                shutdown(stop_reason)
                return 2
            time.sleep(.1)
        states = [read(sid, rep).get('status') for sid, rep in scheduled]
        return 3 if 'VOID' in states else 0 if all(s == 'COMPLETE' for s in states) else 2
    finally:
        if active:
            shutdown(stop_reason or 'terminated')
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    command = argv[0] if argv and argv[0] in {'score', 'merge'} else None
    scoring = command is not None
    if scoring:
        argv.pop(0)
    parser.add_argument('--out', type=Path, required=True)
    if command == 'merge':
        parser.add_argument('--verdicts', type=Path, required=True)
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
        if command == 'merge':
            try:
                merge_verdicts(args.out, json.loads(args.verdicts.read_text()))
            except ValueError as exc:
                parser.error(str(exc))
        else:
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
    if (args.out / 'calls.json').exists() or any(args.out.glob('*-*.json')):
        parser.error('--out must be fresh: calls.json or result JSON already exists')
    sys.path.insert(0, str(ROOT / 'tests/e2e'))
    from loopback import install
    install(allow_hosts=() if args.upstream == 'fake' else (urlsplit(args.upstream_url).hostname,))
    (args.out / 'declaration.json').write_text(json.dumps(declaration_for(args, selected), indent=2))
    return orchestrate(args, argv, selected)


if __name__ == '__main__':
    raise SystemExit(main())
