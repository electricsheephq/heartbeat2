"""Prepare blind packets or merge externally supplied verdicts; never run a model."""
import argparse
import json
from pathlib import Path

from .score import VERDICT_KEYS, aggregate, packet_id, write


def packets(results):
    output = []
    for result in results:
        history = [row for pair in result['history'] for row in (
            {'role': 'user', 'content': pair['owner']},
            {'role': 'assistant', 'content': pair['assistant']})][-6:]
        history = result.get('history_rows', history)[-6:]
        for i, text in enumerate(result['delivered']):
            output.append({'id': packet_id(result, i), 'history': history,
                           'wake_note': result['wake_note'], 'delivered_text': text})
    return output


def merge(score, verdicts):
    texts = {t['id']: t for group in score['paths'].values() for t in group['texts']}
    seen = set()
    for verdict in verdicts:
        if verdict['id'] not in texts or verdict['id'] in seen:
            raise ValueError('unknown or duplicate packet id')
        seen.add(verdict['id'])
        if any(type(verdict.get(k)) is not bool for k in VERDICT_KEYS):
            raise ValueError('every judgment must be boolean')
        why = verdict.get('why')
        if not isinstance(why, str) or not why.strip() or '\n' in why:
            raise ValueError('why must be a nonempty one-line explanation')
    for verdict in verdicts:
        texts[verdict['id']]['verdict'] = {k: verdict[k] for k in (*VERDICT_KEYS, 'why')}
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
