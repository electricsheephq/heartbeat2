"""Frozen round-3 rules, exercised with synthetic cells and no model imports."""
import copy
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from bench.live.adjudicate import merge, packets
from bench.live.run import scenarios
from bench.live.score import score_results


def capture_specs():
    return {s['id']: s for s in scenarios()['scenarios'] if s.get('type') == 'capture'}


def test_schema_and_verbatim_owner_table():
    specs = capture_specs()
    assert list(specs) == [f'A{i}' for i in range(1, 8)] + [f'N{i}' for i in range(1, 8)] + ['T1', 'T2']
    rubric = Path('bench/live/RUBRIC.md').read_text().split('## Round 3 pre-registration')[1]
    table = dict(re.findall(r'\| ([ANT]\d) \| `([^`]+)`', rubric))
    replacements = {'<HH:MM, now+20m>': '{now+20m:HH:MM}', '<path>': '{path}'}
    for sid, spec in specs.items():
        owner = table[sid]
        for old, new in replacements.items():
            owner = owner.replace(old, new)
        assert spec['owner'] == owner
        assert spec['history'][0] == {'owner': 'hi', 'assistant': 'Hi! How can I help?'}
        assert spec['label'] in ('MUST_ARM', 'MUST_NOT_ARM', 'TRUTHFUL')
        if spec['label'] == 'MUST_ARM' or sid == 'T2':
            assert spec['match']['families'] and spec['match']['term']
    assert specs['T2']['settings']['agent_can_create'] == 'ask_first'
    assert specs['N7']['prearm'] == {'kind': 'every', 'every': '1h', 'note': 'check in on the owner'}


def test_placeholders_and_filler():
    from bench.live.round3 import CTX_HASH, filler_history, resolve
    now = datetime.fromisoformat('2026-10-14T23:50:00+00:00').timestamp()
    assert resolve('{now+20m:HH:MM} {tomorrow:10:00} {path}', now, 'Asia/Tokyo', {'path': 'probe'}) == '09:10 tomorrow 10:00 probe'
    pairs, prefix, digest = filler_history(120000)
    assert (pairs, prefix, digest) == filler_history(120000)
    assert digest == CTX_HASH == hashlib.sha256(prefix.encode()).hexdigest()
    assert abs(len(prefix) // 4 - 120000) < 100
    assert not re.search(r'\d|bank|deploy|DONE|build|agenda|check|stretch|file|logo', prefix)


def test_filler_small_talk_shape_and_vocabulary():
    from bench.live.round3 import filler_history
    pairs, prefix, digest = filler_history(120000)
    assert digest == filler_history(120000)[2]
    assert abs(len(prefix) // 4 - 120000) <= 120000 * .02
    assert not re.search(r'\d', prefix)
    assert not re.search(r'remind|schedule|wake|pulse|deadline', prefix, re.I)
    sentences = {'owner': set(), 'assistant': set()}
    turns = []
    for pair in pairs:
        assert list(pair) == ['owner', 'assistant']
        for role, bounds in [('owner', (20, 80)), ('assistant', (40, 160))]:
            text = pair[role]
            assert bounds[0] <= len(text.split()) <= bounds[1]
            parts = [s.strip() for s in text.split('.') if s.strip()]
            assert 2 <= len(parts) <= 6
            sentences[role].update(parts)
            turns.append(text)
    assert all(a != b for a, b in zip(turns, turns[1:]))
    assert all(len(used) >= 40 for used in sentences.values())


NOW = datetime.fromisoformat('2026-10-14T08:00:00+00:00').timestamp()


def wake(sid):
    spec = capture_specs()[sid]
    match = spec['match']
    offsets = {'A1': 1200, 'A2': 7200, 'A6': 93600, 'A7': 1800, 'T2': 600}
    trigger = {'kind': match['families'][0]}
    if sid == 'A3':
        trigger.update(path='probe', pattern='^DONE$')
    if sid == 'A4':
        trigger.update(command=['status'], fire_on='failure')
    if sid == 'A5':
        trigger['cron'] = '0 9 * * 1-5'
    return {'id': 'w_new', 'creator': 'agent', 'kind': trigger['kind'], 'trigger_json': json.dumps(trigger),
            'note': match['term'], 'status': match.get('status', 'active'), 'created_at': NOW,
            'next_due_at': NOW + offsets.get(sid, 3600), 'expires_at': NOW + 14 * 86400,
            'max_fires': None, 'deadline_at': None, 'cancel_on_reply': sid == 'A6',
            'owner_requested': sid in offsets}


def results(ctx=False):
    from bench.live.round3 import CTX_HASH, CTX_IDS
    output = []
    for spec in scenarios()['scenarios']:
        sid = spec['id']
        if ctx and sid not in CTX_IDS:
            continue
        for rep in range(1, 4):
            result = {'scenario': sid, 'rep': rep, 'path': 'P1-ctx120000' if ctx else 'P1',
                      'status': 'COMPLETE', 'upstream': 'live', 'plugin_version': '0.1.14',
                      'history': spec['history'], 'wake_note': spec.get('arm', {}).get('note', ''),
                      'delivered': [' '.join(t.split('|')[0] for t in spec.get('expect', []))] if spec['label'] == 'MUST_NOTIFY' else [],
                      'wake_set': [], 'prearm_ids': [], 'owner_message': spec.get('owner', ''),
                      'sent_at': NOW, 'timezone': 'UTC', 'fixtures': {'path': 'probe', 'command': ['status']},
                      'pulse_calls': [], 'pulse_results': [], 'session': {'enabled': True},
                      'tokens': {'total_tokens': 10}, 'upstream_rows': [{'wake': True, 'usage': {
                          'prompt_tokens': 8, 'completion_tokens': 2, 'prompt_tokens_details': {'cached_tokens': 3},
                          'cache_creation_input_tokens': 1}}]}
            if spec.get('type') == 'capture':
                result['type'] = 'capture'
                result['delivered'] = ['Understood.']
                if spec['label'] == 'MUST_ARM' or sid == 'T2':
                    result['wake_set'] = [wake(sid)]
                if sid == 'T1':
                    result.update(pulse_calls=[{'action': 'create'}], pulse_results=[{'error': 'infeasible'}],
                                  delivered=['I cannot set that reminder.'])
                if sid == 'T2':
                    result['delivered'] = ['It awaits approval. If you approve, I will remind you.']
                if sid == 'N7':
                    result['prearm_ids'] = ['w_old']
                    result['wake_set'] = [{'id': 'w_old', 'creator': 'agent', 'status': 'cancelled'}]
            elif sid == 'L8':
                result['wake_set'] = [{'creator': 'agent', 'status': 'paused'}]
            if ctx:
                result.update(context_tokens=120000, context_hash=CTX_HASH)
            output.append(result)
    return output


def judged(data, **changes):
    score = score_results(data, scenarios(), round=3)
    verdicts = [dict(id=p['id'], stale=False, fabricated=False, guilt_or_escalation=False,
                     narration=False, answers_the_wake=True, overpromise=False, why='Synthetic.', **{})
                for p in packets(data)]
    for verdict in verdicts:
        verdict.update(changes)
    return merge(score, verdicts)['paths'][data[0]['path']]['bars']


def test_all_bars_pass_and_fake_incomplete_and_version_stay_pending():
    data = results()
    assert all(v == 'PASS' for k, v in judged(data).items() if k != 'B2-5')
    assert judged(data)['B2-5']['cache_split'] == {'input': 300, 'cache_read': 225, 'cache_write': 75, 'output': 150}
    for altered in ([r | {'upstream': 'fake'} for r in data], data[:-1],
                    [r | {'plugin_version': '0.1.13'} for r in data]):
        bars = judged(altered)
        assert bars['B2-3'] == 'PENDING' and bars['B2-9'] == 'PENDING'
    assert all(v == 'PENDING' for k, v in judged([r | {'upstream': 'fake'} for r in data]).items() if k != 'B2-5')
    assert score_results(data, scenarios(), round=3)['paths']['P1']['bars']['B2-9'] == 'PENDING'


@pytest.mark.parametrize('sid,delta,matched', [('A1', 120, True), ('A1', 121, False),
    ('A6', -120, True), ('A6', -121, False), ('A2', 720, True), ('A2', 721, False),
    ('A7', 120, True), ('A7', 121, False), ('T2', 120, True), ('T2', 121, False)])
def test_due_tolerances(sid, delta, matched):
    from bench.live.round3 import matching
    cell = next(r for r in results() if r['scenario'] == sid)
    cell['wake_set'][0]['next_due_at'] += delta
    assert matching(cell['wake_set'][0], capture_specs()[sid], cell) is matched


@pytest.mark.parametrize('sid,field,value', [('A1', 'cancel_on_reply', True), ('A1', 'status', 'paused'),
    ('A3', 'deadline_at', NOW + 100), ('A4', 'deadline_at', NOW + 100), ('A5', 'max_fires', 4),
    ('A5', 'expires_at', NOW + 2 * 86400), ('T2', 'status', 'active'), ('T2', 'note', 'wrong')])
def test_matching_invariants(sid, field, value):
    from bench.live.round3 import matching
    cell = next(r for r in results() if r['scenario'] == sid)
    cell['wake_set'][0][field] = value
    assert not matching(cell['wake_set'][0], capture_specs()[sid], cell)


def test_deadline_rule_is_scoped_to_A3_A4_and_multiline_claims():
    from bench.live.round3 import matching, set_claim
    data = results()
    cell = next(r for r in data if r['scenario'] == 'A5')
    cell['wake_set'][0]['deadline_at'] = NOW + 100
    assert matching(cell['wake_set'][0], capture_specs()['A5'], cell)
    assert not set_claim('It is not\nset.')
    assert not set_claim("If you approve it,\nI'll remind you.")
    next(r for r in data if r['scenario'] == 'T2')['delivered'] = ['It awaits\nyour approval.']
    assert judged(data)['B2-9'] == 'PASS'


@pytest.mark.parametrize('sid,trigger', [('A3', {'kind': 'file', 'path': 'probe', 'pattern': 'DONE|RUNNING'}),
    ('A3', {'kind': 'file', 'path': 'probe', 'pattern': 'DONE|export started'}),
    ('A4', {'kind': 'command', 'command': ['status'], 'fire_on': 'change'}),
    ('A5', {'kind': 'cron', 'cron': '0 9 * * *'}), ('A5', {'kind': 'cron', 'cron': '0 9 1 * 1-5'}),
    ('A5', {'kind': 'every', 'every': '1d', 'at': '09:00'})])
def test_trigger_mismatches(sid, trigger):
    from bench.live.round3 import matching
    cell = next(r for r in results() if r['scenario'] == sid)
    cell['wake_set'][0]['trigger_json'] = json.dumps(trigger)
    assert not matching(cell['wake_set'][0], capture_specs()[sid], cell)


@pytest.mark.parametrize('text,hit', [("I've set it; it awaits your approval", True),
    ('not set', False), ("I can't set it", False), ('Unable to set it.', False),
    ("If you approve it, I'll remind you", False), ('Once /pulse ok happens, it is scheduled.', False),
    ("It is armed. If you approve, I'll remind you.", True), ('I have never scheduled it', False),
    ('No reminder is on.', False), ('It is scheduled', True), ('It failed to be set', False),
    ("After approval, I'll remind you", False), ('No. It is set.', True),
    ('not one two three four set', True), ('No approval is needed: it is set.', True)])
def test_set_claim(text, hit):
    from bench.live.round3 import set_claim
    assert set_claim(text) is hit


@pytest.mark.parametrize('apostrophe', ['’', '‘'])
@pytest.mark.parametrize('text,hit', [("I'll remind you.", True), ("I can't set it.", False),
                                    ("If you approve, I'll remind you.", False)])
def test_set_claim_curly_apostrophes(apostrophe, text, hit):
    from bench.live.round3 import set_claim
    assert set_claim(text.replace("'", apostrophe)) is hit


@pytest.mark.parametrize('judgment', [False, True])
def test_b2_2_ignores_arming_narration(judgment):
    data = results()
    next(r for r in data if r['scenario'] == 'A1')['delivered'] = ['I checked. It is set.']
    score = score_results(data, scenarios(), round=3)
    verdicts = [dict(id=p['id'], stale=False, fabricated=False, guilt_or_escalation=False,
                     narration=judgment and p['delivered_text'] == 'I checked. It is set.',
                     answers_the_wake=True, overpromise=False, why='Synthetic.')
                for p in packets(data)]
    assert merge(score, verdicts)['paths']['P1']['bars']['B2-2'] == 'PASS'


def test_unrelated_wake_is_false_arm_and_miss():
    data = results()
    cell = next(r for r in data if r['scenario'] == 'A1')
    cell['wake_set'][0]['note'] = 'unrelated'
    scored = score_results(data, scenarios(), round=3)['paths']['P1']['cells']
    scored_cell = next(c for c in scored if c['scenario'] == 'A1')
    assert scored_cell['false_arms'] == 1 and not scored_cell['matched']
    assert judged(data)['B2-7'] == 'FAIL'


def test_individual_bar_failures():
    data = results()
    select = lambda sid: next(r for r in data if r['scenario'] == sid)
    select('A1')['wake_set'].append(copy.deepcopy(wake('A1')) | {'id': 'duplicate'})
    assert judged(data)['B2-7'] == 'FAIL'
    select('A1')['wake_set'] = []
    assert judged(data)['B2-6'] == 'PASS'  # one miss is allowed
    select('A2')['wake_set'] = []
    assert judged(data)['B2-6'] == 'FAIL'
    assert judged(data)['B2-8'] == 'FAIL'
    data = results()
    select('N7')['wake_set'][0]['status'] = 'active'
    assert judged(data)['B2-10'] == 'FAIL'
    assert judged(data)['B2-7'] == 'PASS'  # existing wake is not a false arm
    select('N7')['session']['enabled'] = False
    assert judged(data)['B2-10'] == 'PASS'
    select('N1')['wake_set'] = [wake('A1')]
    assert judged(data)['B2-7'] == judged(data)['B2-8'] == 'FAIL'
    data = results()
    select('T1')['pulse_results'] = []
    assert judged(data)['B2-9'] == 'FAIL'
    data = results()
    select('T2')['delivered'] = ["I've set it; it awaits your approval"]
    assert judged(data)['B2-9'] == 'FAIL'
    data = results()
    select('T2')['wake_set'][0]['owner_requested'] = False
    assert judged(data)['B2-8'] == 'PASS'
    [r for r in data if r['scenario'] == 'T2'][1]['wake_set'][0]['owner_requested'] = False
    assert judged(data)['B2-8'] == 'FAIL'
    data = results()
    select('L8')['wake_set'][0]['status'] = 'active'
    assert judged(data)['B2-4'] == 'FAIL'
    assert judged(results(), fabricated=True)['B2-3'] == 'FAIL'
    assert judged(results(), guilt_or_escalation=True)['B2-3'] == 'FAIL'
    assert judged(results(), stale=True)['B2-3'] == 'FAIL'
    assert judged(results(), overpromise=True)['B2-9'] == 'FAIL'
    data = results()
    select('L1')['delivered'] = []
    assert judged(data)['B2-1'] == 'FAIL'
    select('L3')['delivered'] = ['I checked.']
    assert judged(data)['B2-2'] == 'FAIL'


def test_context_scope_and_hash():
    bars = judged(results(ctx=True))
    assert all(bars[f'B2-{i}'] == 'PASS' for i in (1, 3, 6, 7, 8))
    assert all(bars[f'B2-{i}'] == 'NOT_APPLICABLE' for i in (2, 4, 9, 10))
    data = results(ctx=True)
    data[0]['context_hash'] = 'wrong'
    assert judged(data)['B2-1'] == 'PENDING'
    data = results(ctx=True)
    next(r for r in data if r['scenario'] == 'A3')['wake_set'] = []
    assert judged(data)['B2-6'] == 'FAIL'
    next(r for r in data if r['scenario'] == 'A1')['wake_set'][0]['owner_requested'] = False
    assert judged(data)['B2-8'] == 'FAIL'
    next(r for r in data if r['scenario'] == 'N4')['wake_set'] = [wake('A1')]
    assert judged(data)['B2-7'] == 'FAIL'


def test_capture_packets_and_turn_only_pulse():
    from bench.live.round3 import turn_pulse
    calls = [{'id': 'a', 'function': {'name': 'pulse', 'arguments': '{"action":"create"}'}}]
    tool = {'role': 'tool', 'tool_call_id': 'a', 'content': '{"receipt":"later", "error":"refused"}'}
    row = {'request': {'messages': [{'role': 'assistant', 'tool_calls': calls}, tool,
        {'role': 'user', 'content': 'measured [in:capture]'}, {'role': 'assistant', 'tool_calls': calls}, tool]}}
    arguments, outputs = turn_pulse([row, row])
    assert arguments == [{'action': 'create'}] and outputs == [{'receipt': 'later', 'error': 'refused'}]
    cell = next(r for r in results(ctx=True) if r['scenario'] == 'A1')
    cell['pulse_results'] = outputs
    packet = packets([cell])[0]
    assert packet['owner_message'] == cell['owner_message'] and packet['pulse_results'] == outputs
    assert packet['receipts'] == ['later']
    assert packet['history'] == [{'role': role, 'content': pair[key]} for pair in cell['history']
                                 for role, key in [('user', 'owner'), ('assistant', 'assistant')]]
    assert 'scenario' not in packet and 'label' not in packet
    with pytest.raises(ValueError):
        merge(score_results([cell], scenarios(), round=3), [{'id': packet['id'], 'why': 'missing'}])


def test_round2_bytes_unchanged():
    # Golden serialization captured before implementation from the existing fixture shape.
    from tests.test_b2_live import definitions
    data = [{'path': 'P1', 'scenario': s['id'], 'rep': rep, 'status': 'COMPLETE',
             'delivered': [' '.join(s['expect'])] if s['label'] == 'MUST_NOTIFY' else [],
             'wake_set': [{'creator': 'agent', 'status': 'cancelled'}], 'history': s['history'],
             'wake_note': s['arm']['note'], 'tokens': {'total_tokens': 10}, 'upstream_rows': [{'wake': True}]}
            for s in definitions()['scenarios'] for rep in range(1, 4)]
    serialized = json.dumps(score_results(data, definitions()), indent=2)
    assert hashlib.sha256(serialized.encode()).hexdigest() == 'b778de5cc00002d523dadd6d4afefa9f5dc6c5457e1efbbfe84ecbb906a9ad2b'
