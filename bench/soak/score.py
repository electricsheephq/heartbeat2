"""Mechanical frozen-rubric scoring and blind adjudication packets."""
import argparse
from datetime import date, datetime
import json
from pathlib import Path
import re
from bench.live.round3 import set_claim
from heartbeat2.schedule import parse_duration
from .evidence import decoded
from .schedule import build, canonical, digest


def epoch(t):
    return datetime.fromisoformat(t).timestamp() if isinstance(t, str) else t


def norm(text):
    return ' '.join((text or '').split())


def text_matches(text, output):
    a, b = norm(text), norm(output)
    return bool(a and b and (a == b or b.startswith(a) or b.endswith(a)))


def created(turn):
    old = {w['id'] for w in turn['before']['wakes']}
    return [w for w in turn['snapshot']['wakes'] if w['id'] not in old]


def matching(wake, e, n=0, path=None):
    trigger = decoded(wake.get('trigger_json')) or {}
    k, sent = e['kind'], epoch(e['sends'][n]['at'])
    term = e['tasks'][n]['term'] if e['tasks'] else 'DONE' if k == 'W' else 'check'
    if wake.get('status') != 'active' or term.lower() not in wake.get('note', '').lower(): return False
    if bool(wake.get('cancel_on_reply')) != k.startswith('C'): return False
    try:
        if k.startswith('R') or k in ('M', 'C-reply', 'C-silent'):
            return wake['kind'] == 'at' and abs(wake['next_due_at']-epoch(e['due'][n])) <= 120
        if k == 'W':
            return (wake['kind'] == 'file' and trigger.get('path') == path and wake.get('deadline_at') is None and
                    'deadline' not in trigger and 0 < parse_duration(trigger['check_every']) <= 1500 and bool(re.search(trigger['pattern'], 'DONE')) and not re.search(trigger['pattern'], 'RUNNING'))
        if k == 'S':
            from croniter import croniter
            hourly = wake['kind'] == 'cron' and croniter.expand(trigger['cron'])[0] == [[0], ['*'], ['*'], ['*'], ['*']]
            interval = 3600 if hourly else wake.get('eff_interval_s')
            due = wake['next_due_at']
            return ((hourly or wake['kind'] == 'every' and interval is not None and 3300 <= interval <= 3600) and
                    sent+3300 <= due <= sent+6900 and (wake.get('max_fires') is None or wake['max_fires'] >= 3) and
                    (wake.get('expires_at') is None or wake['expires_at'] >= due+2*interval))
    except (KeyError, TypeError, ValueError, re.error): return False
    return False


def arming_message(turn, message, turns):
    if message['out'] or not turn.get('owner') or not turn.get('rows'): return False
    start = turn['owner']['timestamp']
    following = [t['owner']['timestamp'] for t in turns if t.get('owner') and t['owner']['timestamp'] > start]
    end = min([turn['rows'][-1]['timestamp']+60] + following)
    identity = any(str(r.get('platform_message_id')) == str(message['id']) for r in turn['rows'])
    return start <= epoch(message['date']) < end and (identity or text_matches(message['text'], turn['final_text']))


def attribute(rows):
    messages = {r['message']['id']: r['message'] for r in rows if r['kind'] == 'dm'}
    turns = list({r.get('key', str((r['occurrence']['wake_id'], r['occurrence'].get('dedupe_key')))): r for r in rows if r['kind'] == 'wake_turn'}.values())
    turns = [r for r in turns if r['occurrence'].get('delivered')]
    owner_turns = [r for r in rows if r['kind'] == 'turn']
    deliveries, unresolved = [], []
    for m in messages.values():
        if m['out']: continue
        at = epoch(m['date'])
        if any(arming_message(t, m, owner_turns) for t in owner_turns): continue
        candidates = [t for t in turns if t['occurrence']['started_at']-60 <= at <= t['occurrence']['ended_at']+60 and
                      (text_matches(m['text'], t['final_text']) or not m['text'] and not m.get('media'))]
        if len(candidates) != 1:
            unresolved.append(dict(message=m, candidates=[t.get('key') for t in candidates])); continue
        t = candidates[0]
        deliveries.append(dict(message=m, wake_id=t['occurrence']['wake_id'], occurrence=t['occurrence'],
                               final_text=t['final_text'], rows=t.get('rows'), visible_at=epoch(m.get('edit_date') or m['date']),
                               text_unreadable=not bool(m['text'])))
    return deliveries, unresolved


def hard_failure(rows):
    deliveries, _ = attribute(rows)
    for start in (r for r in rows if r['kind'] == 'episode_start'):
        e, eid = start['schedule'], start['episode']
        turns = [r for r in rows if r['kind'] in ('turn', 'partial_arming') and r['episode'] == eid and r['key'].startswith('request:')]
        ids = {w['id'] for t in turns for w in created(t)}
        if e['kind'] == 'S':
            stops = [r for r in rows if r['kind'] == 'send' and r['episode'] == eid and r['key'] == 'stop']
            if stops and any(d['wake_id'] in ids and d['visible_at'] > epoch(stops[0]['message']['date']) for d in deliveries): return 'B3-5'
            for r in rows:
                if r['kind'] == 'before_stop' and r['episode'] == eid and any(w and (w['status'] != 'active' or w['next_due_at'] is None or not epoch(r['t']) <= w['next_due_at'] <= epoch(r['t'])+7200) for w in r['wakes']): return 'B3-5'
                if r['kind'] == 'turn' and r['episode'] == eid and r['key'] == 'stop':
                    session = next((s for s in r['snapshot']['sessions'] if s['session_key'] == start['session']['session_key']), None)
                    if session and session['enabled'] and any(w['id'] in ids and w['status'] not in ('cancelled', 'paused') for w in r['snapshot']['wakes']): return 'B3-5'
        if e['kind'].startswith('R') or e['kind'] in ('M', 'C-silent'):
            if any(o['wake_id'] in ids and (o.get('held_reason') in ('unanswered', 'held_unanswered') or o.get('error') in ('unanswered', 'held_unanswered')) for r in rows if r.get('snapshot') for o in r['snapshot']['occurrences']): return 'B3-6'
            if e['kind'] == 'M':
                gates = [r for r in rows if r['kind'] == 'gate' and r['episode'] == eid and r.get('request_index') == 2]
                if gates and any(d['wake_id'] == gates[0]['key'] for d in deliveries) and (gates[0].get('session', {}).get('unanswered_count', 0) < 2 or any(r['kind'] == 'm3_owner_check' and r['episode'] == eid and r['owner_after'] for r in rows)): return 'B3-6'
    return None


def release_at(receipt, due, tz):
    if isinstance(receipt, dict): return epoch(receipt.get('release_at', due))
    from zoneinfo import ZoneInfo
    local = datetime.fromtimestamp(due, ZoneInfo(tz)); latest = due
    for stamp in re.findall(r'hold(?:s)? it until (\w{3} \d{2} \w{3} \d{2}:\d{2})', receipt or ''):
        parsed = datetime.strptime(stamp, '%a %d %b %H:%M').replace(year=local.year, tzinfo=local.tzinfo)
        latest = max(latest, parsed.timestamp())
    return latest


def tool_results(rows):
    names = {call['id']: call['function']['name'] for r in rows for call in decoded(r.get('tool_calls')) or []}
    return [dict(name=r.get('tool_name') or names.get(r.get('tool_call_id')), result=decoded(r.get('content')))
            for r in rows if r['role'] == 'tool' and (r.get('tool_name') or names.get(r.get('tool_call_id'))) != 'pulse']


def score(schedule, rows, verdicts=None):
    declaration = rows[0] if rows else {}
    regenerated = build(date.fromisoformat(declaration['start']), declaration['tz'])
    if regenerated != schedule or declaration.get('schedule_sha256') != regenerated['sha256']:
        raise ValueError('regenerated schedule hash mismatch')
    verdicts = {v['id']: v for v in (verdicts or [])}
    deliveries, unresolved = attribute(rows)
    dm_messages = list({r['message']['id']: r['message'] for r in rows if r['kind'] == 'dm'}.values())
    packets, evidence, violations = [], {i: True for i in range(1, 9)}, {i: [] for i in range(1, 9)}
    pending = {7: False, 8: False}; capture_misses, flagged, delivery_checks, latencies = 0, 0, [], []
    voids = [r for r in rows if r['kind'] == 'void']
    void_ids = {r['episode'] for r in voids}
    episodes = list(schedule['episodes'])
    for r in rows:
        if r['kind'] == 'requeue': episodes.extend(r['episodes'])
    selected = {e.get('replaces', e['id']): e for e in episodes if e['id'] not in void_ids}
    last_snapshot = next((r['snapshot'] for r in reversed(rows) if r.get('snapshot')), None)
    final_wakes = {w['id']: w for w in (last_snapshot or {}).get('wakes', [])}
    baseline = next((r for r in rows if r['kind'] == 'baseline_setup'), {})
    if not (baseline.get('starting_transcript') is not None and baseline.get('transcript_sha256') == digest(baseline['starting_transcript']) and
            baseline.get('effective_quiet') == '22:00-08:00' and baseline.get('session', {}).get('enabled') and
            baseline.get('session', {}).get('unanswered_count') == 0 and declaration.get('frozen', {}).get('allow_gateway_injection') and
            declaration.get('frozen', {}).get('hermes_version') and declaration.get('frozen', {}).get('hermes_commit')):
        for i in evidence: evidence[i] = False
    measured_ids, watcher_intervals = set(), []
    for e in episodes:
        eid, k = e['id'], e['kind']; included = selected.get(e.get('replaces', eid)) == e
        logs = [r for r in rows if r['episode'] == eid]
        turns = [r for r in logs if r['kind'] == 'turn' and r.get('purpose') != 'setup']
        malformed = [r for r in turns if not {'before', 'snapshot', 'pulse_results', 'final_text', 'owner', 'rows', 'message_id'} <= r.keys()]
        if included and malformed:
            for i in evidence: evidence[i] = False
        turns = [r for r in turns if r not in malformed]
        request_turns = {int(r['key'].split(':')[1]): r for r in turns if r['key'].startswith('request:')}
        ended = any(r['kind'] == 'episode_end' for r in logs)
        started = any(r['kind'] == 'episode_start' for r in logs)
        if included and (not ended or len(request_turns) != len(e['sends'])):
            for i in evidence: evidence[i] = False
        if included and (not started or last_snapshot is None):
            for i in evidence: evidence[i] = False
        if included:
            for start_row in (r for r in logs if r['kind'] == 'episode_start'):
                if not {'frozen', 'gateway_starts'} <= start_row.keys():
                    for i in evidence: evidence[i] = False
        matched = []
        failed_capture = any(r['kind'] == 'w_baseline_missing' for r in logs)
        e = json.loads(canonical(e))
        for n, send in enumerate(e['sends']):
            actual = next((r['message']['date'] for r in logs if r['kind'] == 'send' and r['key'] == f'request:{n}'), None)
            if actual:
                delta = epoch(actual)-epoch(send['at']); send['at'] = actual
                if e['due'] and k != 'R-clock': e['due'][n] = datetime.fromtimestamp(epoch(e['due'][n])+delta).astimezone().isoformat()
        for n in range(len(e['sends'])):
            t = request_turns.get(n)
            if not t: continue
            path = next((r['path'] for r in logs if r['kind'] == 'file_write' and r['mode'] == 'wb'), None)
            ws = created(t); matches = [w for w in ws if matching(w, e, n, path)] if k != 'N' else []
            ok = len(ws) == len(matches) == 1
            if k != 'N' and not ok: failed_capture = True
            false = len(ws)-int(bool(matches)) if k != 'N' else len(ws)
            if false: violations[2].append(dict(episode=eid, extra_wakes=false))
            if ok:
                w = matches[0]; matched.append((n, w, t)); measured_ids.add(w['id'])
                if k.startswith('R') or k.startswith('C') or k == 'M': flagged += included and bool(w.get('owner_requested'))
        if failed_capture: capture_misses += 1
        for t in turns:
            if not t['key'].startswith('request:') and created(t): violations[2].append(dict(episode=eid, key=t['key']))
            if t.get('purpose') not in ('request', 'stop'): continue
            n = int(t['key'].split(':')[1]) if t['key'].startswith('request:') else 0
            successful = any(index == n and any(p.get('args', {}).get('action') == 'create' and isinstance(p.get('result'), dict) and not p['result'].get('error') and p['result'].get('ok') and p['result'].get('wake', {}).get('id') == w['id'] for p in t['pulse_results']) for index, w, _ in matched) if t['key'] != 'stop' else False
            visible_replies = [m for m in dm_messages if arming_message(t, m, [r for r in rows if r['kind'] == 'turn'])]
            visible_text = visible_replies[-1]['text'] if len(visible_replies) == 1 else None
            if included and visible_text is None: evidence[7] = False
            if not successful and visible_text is not None and set_claim(visible_text): violations[7].append(dict(episode=eid, key=t['key'], reason='set_without_matching_create'))
            packet = dict(id=f'a-{t["message_id"]}', request=t['owner']['content'], visible_text=visible_text, pulse_results=t['pulse_results'])
            packets.append(packet); verdict = verdicts.get(packet['id'])
            if verdict and verdict.get('overpromise') is True: violations[7].append(dict(packet=packet['id'], reason='overpromise'))
            elif included and (not verdict or not isinstance(verdict.get('overpromise'), bool)): pending[7] = True
            if included and any(p.get('result') is None for p in t['pulse_results']): evidence[7] = False
        if included and any(not any(t['message_id'] == r['message']['id'] for t in turns) for r in logs if r['kind'] == 'send' and r.get('purpose') != 'setup'):
            evidence[2] = evidence[7] = False
        for n, w, t in matched:
            ds = [d for d in deliveries if d['wake_id'] == w['id']]
            recorded = [o for o in (last_snapshot or {}).get('occurrences', []) if o['wake_id'] == w['id'] and o.get('delivered')]
            unlinked = [o for o in recorded if not any(d['occurrence'].get('dedupe_key') == o.get('dedupe_key') for d in ds)]
            if included and unlinked:
                evidence[3] = evidence[8] = False
                if k == 'S': evidence[5] = False
                if k == 'C-reply': evidence[4] = False
            gates = [r for r in logs if r['kind'] == 'gate' and r['key'] == w['id']]
            if included and (not gates or epoch(gates[0]['t']) >= gates[0]['due']): evidence[3] = evidence[6] = False
            if k == 'C-reply':
                reply_turn = next((r for r in turns if r['key'] == 'cancel_reply'), None)
                cancelled = next((x for x in reply_turn['snapshot']['wakes'] if x['id'] == w['id']), None) if reply_turn else None
                if ds or cancelled and cancelled['status'] != 'cancelled': violations[4].append(dict(episode=eid, wake=w['id']))
                if included and (not ended or not cancelled or not any(r['kind'] == 'send' and r['key'] == 'cancel_reply' for r in logs)): evidence[4] = False
                continue
            if k == 'S':
                stops = [r for r in logs if r['kind'] == 'send' and r['key'] == 'stop']
                pre = [r for r in logs if r['kind'] == 'before_stop']
                stop_turn = next((r for r in turns if r['key'] == 'stop'), None)
                if included and (not pre or not stops or not stop_turn): evidence[5] = False
                if stops and any(d['visible_at'] > epoch(stops[0]['message']['date']) for d in ds): violations[5].append(dict(episode=eid, reason='delivery_after_stop'))
                if pre:
                    row = next((x for x in pre[0]['wakes'] if x and x['id'] == w['id']), None)
                    if row and (row['status'] != 'active' or row['next_due_at'] is None or not epoch(pre[0]['t']) <= row['next_due_at'] <= epoch(pre[0]['t'])+7200): violations[5].append(dict(episode=eid, reason='not_recurring_before_stop'))
                if stop_turn:
                    ended_wake = next((x for x in stop_turn['snapshot']['wakes'] if x['id'] == w['id']), None)
                    enabled = any(x['enabled'] for x in stop_turn['snapshot']['sessions'] if x['session_key'] == w['session_key'])
                    if ended_wake and ended_wake['status'] not in ('cancelled', 'paused') and enabled: violations[5].append(dict(episode=eid, reason='stop_did_not_end_wake'))
            if len(ds) != 1:
                recorded = [o for o in (last_snapshot or {}).get('occurrences', []) if o['wake_id'] == w['id'] and o.get('delivered')]
                if len(ds) > 1 or ended and not recorded: violations[3].append(dict(episode=eid, wake=w['id'], deliveries=len(ds)))
                elif included: evidence[3] = False
            if k == 'W':
                try: every = parse_duration(decoded(w['trigger_json'])['check_every'])
                except (KeyError, TypeError, ValueError):
                    if included: evidence[3] = False
                    continue
                watcher_intervals.append(dict(episode=eid, check_every=every))
                flips = [r for r in logs if r['kind'] == 'file_write' and r.get('key') == 'flip']
                if included and (not flips or not any(r['kind'] == 'baseline' and flips and epoch(r['t'])+60 <= epoch(flips[0]['t']) for r in logs)): evidence[3] = evidence[8] = False
                due = epoch(flips[0]['t']) if flips else epoch(e['flip']); latest = due+every+180
            else:
                due = w['next_due_at']; receipts = [p['result'].get('receipt', '') for p in t['pulse_results'] if isinstance(p.get('result'), dict) and p['result'].get('wake', {}).get('id') == w['id']]
                if included and not receipts: evidence[3] = False
                latest = max([due] + [release_at(r, due, schedule['tz']) for r in receipts]) + 180
            for d in ds:
                early = d['visible_at'] < due - (0 if k == 'W' else 60)
                if early: violations[3].append(dict(episode=eid, reason='early_delivery'))
                if included: delivery_checks.append(not early and d['visible_at'] <= latest); latencies.append(d['visible_at']-due)
                visible = d['final_text'] if d['text_unreadable'] else d['message']['text']
                term = e['tasks'][n]['term'] if e['tasks'] else 'DONE' if k == 'W' else None
                if term and term.lower() not in visible.lower() and not (k == 'W' and e['job'].lower() in visible.lower()): violations[8].append(dict(episode=eid, reason='wrong_subject'))
                packet = dict(id=f'd-{d["message"]["id"]}', request=e['sends'][n]['text'], stored_due_time=due, visible_text=visible,
                              event_payloads=decoded(d['occurrence'].get('payload_json')), tool_results=tool_results(d['rows'] or []))
                if included and (not d['rows'] or packet['event_payloads'] is None): evidence[8] = False
                if d['text_unreadable']: packet['text_source'] = 'recorded_turn_output'
                if k == 'W':
                    packet.update(file_event=decoded(d['occurrence'].get('payload_json')), flip=flips[0]['t'] if flips else None)
                    if included and packet['file_event'] is None: evidence[8] = False
                packets.append(packet); verdict = verdicts.get(packet['id'])
                if verdict and verdict.get('fabrication') is True: violations[8].append(dict(packet=packet['id'], reason='fabrication'))
                if included and (not verdict or not all(isinstance(verdict.get(f), bool) for f in ('useful', 'fabrication', 'narration'))): pending[8] = True
            if k == 'M' and n == 2:
                check = next((r for r in logs if r['kind'] == 'm3_owner_check'), None)
                if included and (not gates or epoch(gates[0]['t']) > due-30 or not check): evidence[6] = False
                if gates and check and (gates[0]['session']['unanswered_count'] < 2 or check['owner_after']): violations[6].append(dict(episode=eid, reason='M3_not_at_unanswered_2'))
                if included and not ds: evidence[6] = False
    covered = {e.get('replaces', e['id']) for e in episodes if e['id'] not in void_ids and any(r['kind'] == 'episode_end' and r['episode'] == e['id'] for r in rows)}
    if len(covered) < len(schedule['episodes']):
        for i in evidence: evidence[i] = False
    if capture_misses > 1 or evidence[1] and flagged < 20: violations[1].append(dict(capture_misses=capture_misses, owner_requested=flagged))
    if delivery_checks and sum(delivery_checks)/len(delivery_checks) < .95: violations[3].append(dict(reason='under_95_percent_on_time'))
    for u in unresolved:
        at = epoch(u['message']['date'])
        occurrence_span = any(o['wake_id'] in measured_ids and o.get('started_at') is not None and o.get('ended_at') is not None and o['started_at']-60 <= at <= o['ended_at']+60 for o in (last_snapshot or {}).get('occurrences', []))
        u['could_belong'] = occurrence_span or bool(u['candidates']) or any(epoch(e['sends'][0]['at']) <= at <= epoch(e['latest_close']) for e in episodes) or any(text_matches(u['message']['text'], r['final_text']) for r in rows if r['kind'] == 'wake_turn')
    if any(u['could_belong'] for u in unresolved): evidence[3] = evidence[5] = False
    hard = next((r['reason'] for r in rows if r['kind'] == 'hard_fail'), None) or hard_failure(rows)
    if hard: violations[int(hard[-1])].append(dict(reason='observed_hard_bar'))
    judged_delivery = [verdicts[p['id']] for p in packets if p['id'].startswith('d-') and p['id'] in verdicts]
    if judged_delivery and sum(v.get('useful') is True and not v.get('narration') for v in judged_delivery)/len(judged_delivery) < .95: violations[8].append(dict(reason='under_95_percent_useful'))
    bars = {f'B3-{i}': 'FAIL' if violations[i] else 'INCOMPLETE' if not evidence[i] else 'PENDING' if pending.get(i) else 'PASS' for i in evidence}
    costs = [o for o in (last_snapshot or {}).get('occurrences', []) if o['wake_id'] in measured_ids]
    percentile = lambda q: sorted(latencies)[min(len(latencies)-1, int((len(latencies)-1)*q))] if latencies else None
    daily = {}
    for u in unresolved:
        if not u['could_belong']:
            from zoneinfo import ZoneInfo
            day = datetime.fromtimestamp(epoch(u['message']['date']), ZoneInfo(schedule['tz'])).date().isoformat(); daily[day] = daily.get(day, 0)+1
    metrics = dict(daily_cap_exemption='not tested', unrequested_messages_per_day=daily,
                   latency_seconds=dict(p50=percentile(.5), p95=percentile(.95)), watcher_check_every=watcher_intervals,
                   wake_cost={key: sum(o.get(key, 0) for o in costs) for key in ('cost_microusd', 'tokens_in', 'cache_read_tokens', 'cache_write_tokens', 'tokens_out', 'api_calls')}, arming_turn_cost='not measured')
    metrics['wake_cost_by_wake'] = {wid: {key: sum(o.get(key, 0) for o in costs if o['wake_id'] == wid) for key in metrics['wake_cost']} for wid in measured_ids}
    useful = sum(v.get('useful') is True for v in judged_delivery)
    metrics['wake_cost_per_useful_delivery_microusd'] = metrics['wake_cost']['cost_microusd']/useful if useful and not pending[8] else None
    status = 'FAIL' if bars['B3-5'] == 'FAIL' or bars['B3-6'] == 'FAIL' else 'ABORTED' if any(r['kind'] == 'abort' and r.get('reason') == 'frozen_state_changed' for r in rows) else 'FAIL' if 'FAIL' in bars.values() else 'INCOMPLETE' if 'INCOMPLETE' in bars.values() else 'PENDING' if 'PENDING' in bars.values() else 'PASS'
    return dict(status=status, bars=bars, violations={f'B3-{i}': v for i, v in violations.items()}, voids=voids, unresolved=unresolved, metrics=metrics, packets=packets)


def main():
    p = argparse.ArgumentParser()
    for flag in ('schedule', 'log', 'out'): p.add_argument('--'+flag, type=Path, required=True)
    p.add_argument('--verdicts', type=Path); a = p.parse_args()
    result = score(json.loads(a.schedule.read_text()), [json.loads(l) for l in a.log.read_text().splitlines()], json.loads(a.verdicts.read_text()) if a.verdicts else None)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out/'packets.jsonl').write_text(''.join(canonical(p)+'\n' for p in result.pop('packets')))
    (a.out/'score.json').write_text(canonical(result)+'\n')
    lines = ['# B3-lite score', '', 'Status: '+result['status'], '', '| Bar | Result |', '|---|---|']
    lines += [f'| {k} | {v} |' for k, v in result['bars'].items()]
    for key in ('voids', 'unresolved', 'metrics'): lines += ['', '## '+key, '', '```json', json.dumps(result[key], indent=2), '```']
    (a.out/'score.md').write_text('\n'.join(lines)+'\n')


if __name__ == '__main__': main()
