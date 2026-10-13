"""Prepare blind packets or merge externally supplied verdicts; never run a model."""
import argparse
import json
import re
from pathlib import Path

from .score import VERDICT_KEYS, aggregate, packet_id, write


PAYLOAD = re.compile(r'<wake-payload\b[^>]*>\n?(.*?)\n?</wake-payload>', re.S)


def evidence(result, limit=6, width=600):
    """What the wake turn saw: event payloads and non-pulse tool results, never labels or paths."""
    found = []
    for row in result.get('upstream_rows', []):
        if not row.get('wake'):
            continue
        messages = (row.get('request') or {}).get('messages') or []
        start = max((i for i, m in enumerate(messages) if m.get('role') == 'user'), default=-1)
        if start < 0:
            continue
        names = {}
        for message in messages[start:]:
            for call in message.get('tool_calls') or []:
                names[call.get('id')] = (call.get('function') or {}).get('name')
        items = PAYLOAD.findall(str(messages[start].get('content') or ''))
        items += [str(m.get('content') or '') for m in messages[start:]
                  if m.get('role') == 'tool' and names.get(m.get('tool_call_id')) != 'pulse']
        found += [item.strip()[:width] for item in items if item.strip() and item.strip()[:width] not in found]
    return found[:limit]


def packets(results):
    output = []
    for result in results:
        history = [row for pair in result['history'] for row in (
            {'role': 'user', 'content': pair['owner']},
            {'role': 'assistant', 'content': pair['assistant']})][-6:]
        if not result.get('context_tokens') and result.get('type') != 'capture':
            history = result.get('history_rows', history)[-6:]
        for i, text in enumerate(result['delivered']):
            output.append({'id': packet_id(result, i), 'history': history,
                           'wake_note': result['wake_note'], 'evidence': evidence(result),
                           'delivered_text': text})
            if 'pulse_results' in result:
                output[-1].update(pulse_results=result['pulse_results'], receipts=[r['receipt'] for r in result['pulse_results'] if 'receipt' in r])
            if result.get('type') == 'capture':
                output[-1]['owner_message'] = result['owner_message']
    return output


def merge(score, verdicts):
    keys = (*VERDICT_KEYS, 'overpromise') if score.get('round') == 3 else VERDICT_KEYS
    texts = {t['id']: t for group in score['paths'].values() for t in group['texts']}
    seen = set()
    for verdict in verdicts:
        if verdict['id'] not in texts or verdict['id'] in seen:
            raise ValueError('unknown or duplicate packet id')
        seen.add(verdict['id'])
        if any(type(verdict.get(k)) is not bool for k in keys):
            raise ValueError('every judgment must be boolean')
        why = verdict.get('why')
        if not isinstance(why, str) or not why.strip() or '\n' in why:
            raise ValueError('why must be a nonempty one-line explanation')
    for verdict in verdicts:
        texts[verdict['id']]['verdict'] = {k: verdict[k] for k in (*keys, 'why')}
    return aggregate(score)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    packet = sub.add_parser('packets')
    packet.add_argument('--results', type=Path, required=True)
    packet.add_argument('--out', type=Path, required=True)
    merger = sub.add_parser('merge')
    merger.add_argument('--score', type=Path, required=True)
    merger.add_argument('--verdicts', type=Path, required=True)
    merger.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    from .run import ROOT
    if args.out.resolve() == ROOT or ROOT in args.out.resolve().parents:
        parser.error('results must stay outside the repository')
    if args.command == 'packets':
        result = packets([json.loads(p.read_text()) for p in args.results.glob('P*/*.json')])
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / 'packets.json').write_text(json.dumps(result, indent=2))
    else:
        write(merge(json.loads(args.score.read_text()), json.loads(args.verdicts.read_text())), args.out)


if __name__ == '__main__':
    main()
