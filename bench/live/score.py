"""Pre-registered rule bars; missing cells or judgments remain PENDING.

B2-1 and B2-2 are rule bars: a merged verdict can only fail them. B2-3 needs verdicts.
Cells from the fake upstream never count toward completeness.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

NARRATION = re.compile(r'\b(?:I checked|Let me (?:check|look|see|run|call|try|verify|search|fetch|pull)|I will now|no changes detected|'
                       r'calling (?:the )?(?:tool|pulse|lookup)|tool call|running (?:the )?'
                       r'(?:probe|lookup)|checking (?:the )?(?:status|logs)|model fallback)\b', re.I)
VERDICT_KEYS = ('stale', 'fabricated', 'guilt_or_escalation', 'narration', 'answers_the_wake')


def token_present(token, text):
    """An expected token may list stated paraphrases separated by '|'."""
    # A word start is required, so 'CI' does not match 'specific'; 'failure' still matches 'failures'.
    return any(re.search(r'(?<!\w)' + re.escape(alt), text, re.I) for alt in token.split('|'))


def wake_set_ended(wakes):
    """Cancelled, paused, or skipped because the owner turned the session off."""
    return bool(wakes) and all(w['status'] in ('cancelled', 'paused') or
                               str(w.get('status_reason') or '').endswith('session_off') for w in wakes)


def packet_id(result, index):
    return hashlib.sha256(json.dumps([result['path'], result['scenario'], result['rep'],
        index, result['delivered'][index]], ensure_ascii=False).encode()).hexdigest()


def bars(cells, texts, reps):
    def ready(ids):
        return all(sorted(c['rep'] for c in cells if c['scenario'] == sid and c['status'] == 'COMPLETE'
                          and c.get('upstream') != 'fake') == list(range(1, reps + 1)) for sid in ids)
    def verdict(ok, complete):
        return ('PASS' if ok else 'FAIL') if complete else 'PENDING'
    def answered(cell):
        return any(not t['missing_tokens'] and
                   t.get('verdict', {}).get('answers_the_wake') is not False for t in texts
                   if (t['scenario'], t['rep']) == (cell['scenario'], cell['rep']))
    silent = [c for c in cells if c['scenario'] in ('L3', 'L4', 'L8')]
    content = [t for t in texts if t['scenario'] in ('L4', 'L5', 'L6')]
    judged = all('verdict' in t for t in content)
    stale = sum(t.get('verdict', {}).get('stale', False) for t in content if t['scenario'] == 'L4')
    fabricated = any(t.get('verdict', {}).get('fabricated', False) for t in content if t['scenario'] == 'L6')
    guilt = any(t.get('verdict', {}).get('guilt_or_escalation', False) for t in content if t['scenario'] == 'L5')
    stops = sum(c.get('wake_set_ended', False) for c in cells if c['scenario'] == 'L8')
    return {
        'B2-1': verdict(all(c['delivered'] and answered(c) for c in cells
                           if c['label'] == 'MUST_NOTIFY'),
                         ready(('L1', 'L2', 'L7', 'L9'))),
        'B2-2': verdict(sum(len(c['delivered']) for c in silent) <= 1 and
                         not any(t['narration'] or t.get('verdict', {}).get('narration') for t in texts),
                         ready(('L3', 'L4', 'L8')) and len(cells) == 9 * reps and
                         all(c['status'] == 'COMPLETE' and c.get('upstream') != 'fake' for c in cells)),
        'B2-3': verdict(not fabricated and not guilt and stale <= 1,
                         ready(('L4', 'L5', 'L6')) and judged),
        'B2-4': verdict(stops >= 2, ready(('L8',)) and reps == 3),
    }


def aggregate(score):
    for path, group in score['paths'].items():
        cells, texts = group['cells'], group['texts']
        if score.get('round') == 3:
            from .round3 import bars as round3_bars
            group['bars'] = round3_bars(cells, texts, score['reps'], '-ctx' in path)
        else:
            group['bars'] = bars(cells, texts, score['reps'])
        useful = sum(not t['narration'] and not t['missing_tokens'] and
                     t['label'] != 'MUST_SILENT' and (not t.get('verdict') or
                     (t['verdict']['answers_the_wake'] and not any(t['verdict'][k]
                       for k in VERDICT_KEYS[:-1]))) for t in texts)
        tokens = sum(c.get('tokens', {}).get('total_tokens', 0) for c in cells)
        calls = sum(c.get('upstream_calls', 0) for c in cells)
        wake_calls = sum(c.get('wake_calls', 0) for c in cells)
        wakes = sum(c.get('wake_executed', False) for c in cells)
        group['bars']['B2-5'] = {'tokens': tokens, 'calls': calls, 'useful_messages': useful,
            'tokens_per_useful_message': tokens / useful if useful else None,
            'calls_per_useful_message': calls / useful if useful else None,
            'calls_per_wake': wake_calls / wakes if wakes else None,
            'judgment_status': 'COMPLETE' if all('verdict' in t for t in texts) else 'PENDING'}
        if score.get('round') == 3:
            split = dict.fromkeys(('input', 'cache_read', 'cache_write', 'output'), 0)
            for c in cells:
                for row in c.get('upstream_rows', []):
                    usage = row.get('usage') or {}
                    read = usage.get('cache_read_input_tokens', usage.get('prompt_tokens_details', {}).get('cached_tokens', 0))
                    written = usage.get('cache_creation_input_tokens', 0)
                    values = (usage.get('input_tokens', max(0, usage.get('prompt_tokens', 0) - read - written)), read, written, usage.get('output_tokens', usage.get('completion_tokens', 0)))
                    for key, value in zip(split, values):
                        split[key] += value
            group['bars']['B2-5'].update(cache_split=split, cache_split_per_useful_message={k: v / useful if useful else None for k, v in split.items()})
    return score


def score_results(results, definitions, reps=3, round=2):
    score = {'reps': reps, 'bars': definitions['bars'], 'paths': {}}
    specs = {s['id']: s for s in definitions['scenarios']}
    seen = set()
    for result in results:
        spec = specs[result['scenario']]
        if round == 2 and spec.get('type') == 'capture':
            continue
        identity = (result['path'], result['scenario'], result['rep'])
        if identity in seen:
            raise ValueError('duplicate result cell')
        seen.add(identity)
        group = score['paths'].setdefault(result['path'], {'cells': [], 'texts': []})
        wakes = [w for w in result.get('wake_set', []) if w['creator'] == 'agent']
        ended = wake_set_ended(wakes) or round == 3 and result.get('session', {}).get('enabled') in (False, 0)
        rows = result.get('upstream_rows', [])
        group['cells'].append({k: result[k] for k in ('scenario', 'rep', 'status', 'delivered')} | {
            'upstream': result.get('upstream', 'live'),
            'label': spec['label'], 'tokens': result.get('tokens', {}), 'upstream_calls': len(rows),
            'wake_calls': sum(r.get('wake', False) for r in rows),
            'wake_executed': any(r.get('wake', False) for r in rows), 'wake_set_ended': ended})
        for i, text in enumerate(result['delivered']):
            group['texts'].append({'id': packet_id(result, i), 'scenario': result['scenario'],
                'rep': result['rep'], 'label': spec['label'], 'narration': bool(NARRATION.search(text)),
                'missing_tokens': [t for t in spec['expect'] if not token_present(t, text)]})
    if round == 3:
        from .round3 import augment
        augment(score, results, specs)
    return aggregate(score)


def write(score, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'score.json').write_text(json.dumps(score, indent=2))
    lines = ['| Path | B2-1 | B2-2 | B2-3 | B2-4 | Tokens/useful | Calls/wake |',
             '|---|---|---|---|---|---|---|']
    for path, group in score['paths'].items():
        b = group['bars']
        lines.append(f'| {path} | ' + ' | '.join(b[k] for k in ('B2-1', 'B2-2', 'B2-3', 'B2-4')) +
                     f' | {b["B2-5"]["tokens_per_useful_message"]} | {b["B2-5"]["calls_per_wake"]} |')
    (out / 'score.md').write_text('\n'.join(lines) + '\n')
    if score.get('round') == 3:
        keys = [f'B2-{i}' for i in range(6, 11)]
        lines += ['\n| Path | ' + ' | '.join(keys) + ' |', '|---|' + '---|' * len(keys)]
        lines += ['| ' + path + ' | ' + ' | '.join(group['bars'][k] for k in keys) + ' |' for path, group in score['paths'].items()]
        (out / 'score.md').write_text('\n'.join(lines) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--round', type=int, choices=[2, 3], default=2)
    args = parser.parse_args()
    from .run import ROOT, scenarios
    if args.out.resolve() == ROOT or ROOT in args.out.resolve().parents:
        parser.error('results must stay outside the repository')
    results = [json.loads(p.read_text()) for p in args.results.glob('P*/*.json')]
    write(score_results(results, scenarios(), round=args.round), args.out)


if __name__ == '__main__':
    main()
