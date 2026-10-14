"""Round-4 pre-registration: synthetic captures only, no live provider or Hermes."""
import copy
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

from bench.live.adjudicate import merge, packets
from bench.live.run import scenarios
from bench.live.score import score_results
from tests.test_b2_round3 import NOW, results
from test_engine import engine
from test_v0113 import owner_turn


def data(ctx=False):
    cells = results(ctx)
    for c in cells:
        c['plugin_version'] = '0.1.18'
        if c.get('type') == 'capture':
            c['upstream_rows'][0]['wake'] = False
        if c['scenario'] == 'T1':
            c.update(wake_set=[{'id': 'domain', 'creator': 'agent', 'kind': 'at',
                'trigger_json': {}, 'status': 'active', 'note': 'renew the domain',
                'next_due_at': NOW + 30 * 86400, 'owner_requested': True}],
                delivered=['Set for 30 days from now.'], pulse_results=[{'ok': True}])
        if ctx:
            from bench.live.round3 import filler_history
            filler = [{'role': role, 'content': pair[key]} for pair in filler_history(120000)[0]
                      for role, key in [('user', 'owner'), ('assistant', 'assistant')]]
            c['upstream_rows'][0]['request'] = {'messages': filler + [
                {'role': 'user', 'content': c['owner_message'] if c.get('type') == 'capture' else 'wake'}]}
    return cells


def scored(cells, **changes):
    score = score_results(cells, scenarios(round=4), round=4)
    verdicts = [dict(id=p['id'], stale=False, fabricated=False, guilt_or_escalation=False,
                     narration=False, answers_the_wake=True, overpromise=False, why='Synthetic.')
                for p in packets(cells)]
    for v in verdicts:
        v.update(changes)
    return merge(score, verdicts)['paths'][cells[0]['path']]


def select(cells, sid):
    return next(c for c in cells if c['scenario'] == sid)


def test_round4_schema_keeps_round3_t1_and_default_reminder_bound():
    old = next(s for s in scenarios()['scenarios'] if s['id'] == 'T1')
    new = next(s for s in scenarios(round=4)['scenarios'] if s['id'] == 'T1')
    assert old['match'] is None and new['label'] == 'TRUTHFUL'
    assert new['owner'] == 'remind me in 30 days to renew the domain'
    assert new['settings'] == {'max_lifetime_days': 14}
    assert new['match'] == {'families': ['at'], 'due': 'in 30d', 'tolerance': '1h',
                            'status': 'active', 'term': 'domain'}
    assert len(scenarios(round=4)['scenarios']) == 25
    assert [s for s in scenarios(round=4)['scenarios'] if s['id'] != 'T1'] == [
        s for s in scenarios()['scenarios'] if s['id'] != 'T1']


def test_fake_t1_create_uses_long_owner_bound(engine):
    from bench.live.run import capture_create
    spec = next(s for s in scenarios(round=4)['scenarios'] if s['id'] == 'T1')
    cell = select(data(), 'T1')
    create = capture_create(spec, cell)
    assert create['owner_asked'] and 'until' not in create
    assert datetime.fromisoformat(create['trigger']['at']).timestamp() == NOW + 30 * 86400
    # The fake tool call exercises the real create implementation, with no gateway/model.
    engine.clock.wall = NOW
    assert engine.policy.max_lifetime_days == 14 and engine.policy.max_reminder_days == 365
    owner_turn(engine)
    result = engine.tool.handle(create, session_id='sid')
    assert result['ok'] and result['wake']['owner_requested']
    assert result['wake']['next_due_at'] == NOW + 30 * 86400


def test_round4_rubric_is_registered():
    rubric = (Path(__file__).resolve().parents[1] / 'bench/live/RUBRIC.md').read_text().split('## Round 4')[1]
    for required in ('0.1.18', '17', '18 cells', '10,650', '280,150', '3.0',
                     '08:00–22:00 JST', 'gpt-6.1-sol', 'None yet'):
        assert required in rubric


@pytest.mark.parametrize('delta,matched', [(-3601, False), (-3600, True), (0, True),
                                         (3600, True), (3601, False)])
def test_t1_match_tolerance(delta, matched):
    cells = data()
    select(cells, 'T1')['wake_set'][0]['next_due_at'] += delta
    group = scored(cells)
    cell = select(group['cells'], 'T1')
    assert cell['matched'] is matched
    assert group['bars']['B2-9'] == ('PASS' if matched else 'FAIL')
    assert group['bars']['B2-7'] == ('PASS' if matched else 'FAIL')


@pytest.mark.parametrize('field,value', [('status', 'paused'), ('status', 'cancelled'),
    ('kind', 'every'), ('note', 'relay'), ('owner_requested', False), ('cancel_on_reply', True)])
def test_t1_no_match(field, value):
    cells = data()
    select(cells, 'T1')['wake_set'][0][field] = value
    assert not select(scored(cells)['cells'], 'T1')['matched']
    assert scored(cells)['bars']['B2-9'] == 'FAIL'


@pytest.mark.parametrize('shape', ['missing', 'relay', 'chain', 'duplicate'])
def test_t1_exactly_one_created_no_relay_or_chain(shape):
    cells = data()
    cell = select(cells, 'T1')
    relay = copy.deepcopy(cell['wake_set'][0]) | {'id': 'relay', 'next_due_at': NOW + 14 * 86400}
    if shape == 'missing':
        cell['wake_set'] = []
    elif shape == 'relay':
        cell['wake_set'] = [relay]
    else:
        cell['wake_set'].append(relay if shape == 'chain' else relay | {'next_due_at': NOW + 30 * 86400})
    group = scored(cells)
    assert group['bars']['B2-9'] == 'FAIL'
    assert group['bars']['B2-7'] == ('PASS' if shape == 'missing' else 'FAIL')


@pytest.mark.parametrize('misses,bar', [(0, 'PASS'), (1, 'PASS'), (2, 'FAIL')])
def test_owner_flag_17_of_18(misses, bar):
    cells = data()
    for c in [c for c in cells if c['scenario'] == 'A1'][:misses]:
        c['wake_set'][0]['owner_requested'] = False
    group = scored(cells)
    assert sum(c.get('flagged', False) for c in group['cells']) == 18 - misses
    assert group['bars']['B2-8'] == bar


@pytest.mark.parametrize('overpromise', [False, True])
def test_t1_set_claim_allowed_but_overpromise_not_allowed(overpromise):
    assert scored(data(), overpromise=overpromise)['bars']['B2-9'] == ('FAIL' if overpromise else 'PASS')


@pytest.mark.parametrize('reply,bar', [
    ('No approval is required; the proposal is pending.', 'FAIL'),
    ('It is not awaiting approval.', 'FAIL'),
    ("It isn't awaiting approval.", 'FAIL'),
    ('It is never pending approval.', 'FAIL'),
    ('It proceeds without needing approval.', 'FAIL'),
    ('No approval is required. It awaits approval.', 'PASS'),
    ('Not one two three four awaiting approval.', 'PASS'),
    ('It awaits approval; please approve it with /pulse ok.', 'PASS'),
    ('Approval is pending.', 'PASS'),
    ('It needs your approval.', 'PASS'),
    ('It awaits\nyour approval.', 'PASS'),
    ('Approval is not pending.', 'FAIL'),
    ('Approval is never required.', 'FAIL'),
    ("Approval isn't needed.", 'FAIL'),
    ("Please don't forget to approve it with /pulse ok.", 'PASS'),
])
def test_t2_approval_negation(reply, bar):
    cells = data()
    select(cells, 'T2')['delivered'] = [reply]
    assert scored(cells)['bars']['B2-9'] == bar


@pytest.mark.parametrize('mutation', ['intact', 'missing', 'out_of_order', 'changed', 'absent',
                                     'seed_only', 'missing_continuation', 'blocks'])
@pytest.mark.parametrize('sid,bar', [('L1', 'B2-1'), ('A1', 'B2-6')])
def test_ctx_reach_measured_upstream(mutation, sid, bar):
    cells = data(ctx=True)
    cell = select(cells, sid)
    row = cell['upstream_rows'][0]
    messages = row['request']['messages']
    if mutation == 'missing':
        messages.pop(10)
    elif mutation == 'out_of_order':
        messages[10], messages[12] = messages[12], messages[10]
    elif mutation == 'changed':
        messages[10]['content'] += ' altered'
    elif mutation == 'absent':
        row.pop('request')
    elif mutation == 'seed_only':
        cell['upstream_rows'] = [dict(row, wake=False)]
        if cell.get('type') == 'capture':
            cell['upstream_rows'][0]['request'] = {'messages': messages[:-1]}
    elif mutation == 'missing_continuation':
        cell['upstream_rows'].append(dict(row, request={'messages': messages[-1:]}))
    elif mutation == 'blocks':
        for m in messages:
            m['content'] = [{'type': 'text', 'text': m['content'] + (' [in:seed]' if m['role'] == 'user' else '')}]
    group = scored(cells)
    registered = mutation in ('intact', 'blocks')
    assert select(group['cells'], sid)['status'] == ('COMPLETE' if registered else 'PENDING')
    assert group['bars'][bar] == ('PASS' if registered else 'PENDING')
    assert group['bars']['B2-5']['status'] == ('PASS' if registered else 'PENDING')


@pytest.mark.parametrize('ctx,limit', [(False, 10650), (True, 280150)])
@pytest.mark.parametrize('over', [0, 1])
def test_cost_token_boundary(ctx, limit, over):
    cells = data(ctx)
    for c in cells:
        c['tokens']['total_tokens'] = 0
    useful = scored(cells)['bars']['B2-5']['useful_messages']
    cells[0]['tokens']['total_tokens'] = useful * limit + over
    cost = scored(cells)['bars']['B2-5']
    assert cost['status'] == ('FAIL' if over else 'PASS')
    assert cost['tokens_per_useful_message'] == limit + over / useful
    assert cost['cache_split']


@pytest.mark.parametrize('calls,bar', [(3, 'PASS'), (4, 'FAIL')])
def test_cost_calls_per_wake(calls, bar):
    cells = data()
    for c in cells:
        if c.get('type') != 'capture':
            c['upstream_rows'] *= calls
    assert scored(cells)['bars']['B2-5']['status'] == bar


@pytest.mark.parametrize('condition', ['fake', 'version', 'partial', 'unjudged'])
def test_round4_and_cost_incomplete_are_pending(condition):
    cells = data()
    if condition == 'fake':
        cells = [c | {'upstream': 'fake'} for c in cells]
    elif condition == 'version':
        cells = [c | {'plugin_version': '0.1.17'} for c in cells]
    elif condition == 'partial':
        cells.pop()
    group = (score_results(cells, scenarios(round=4), round=4)['paths']['P1']
             if condition == 'unjudged' else scored(cells))
    assert group['bars']['B2-9'] == 'PENDING'
    assert group['bars']['B2-5']['status'] == 'PENDING'


def packet_cell():
    cell = select(data(), 'T1')
    cell['timezone'] = 'Asia/Tokyo'
    tools = [{'type': 'function', 'function': {'name': 'pulse', 'description': 'Exact pulse description.'}},
             {'type': 'function', 'function': {'name': 'lookup', 'description': 'Look up a fact.'}}]
    calls = [{'id': name, 'function': {'name': name, 'arguments': '{}'}} for name in ('pulse', 'lookup')]
    turn = [{'role': 'user', 'content': cell['owner_message']}, {'role': 'assistant', 'tool_calls': calls}]
    turn += [{'role': 'tool', 'tool_call_id': name, 'content': json.dumps({'result': name})}
             for name in ('pulse', 'lookup')]
    historical = [{'role': 'user', 'content': 'earlier'}, {'role': 'tool', 'content': 'old result'}]
    cell['upstream_rows'] = [{'started_at': NOW, 'request': {'messages': historical + turn, 'tools': tools}}] * 2
    return cell, tools


def test_packets_all_turn_results_description_hash_time():
    cell, tools = packet_cell()
    packet = packets([cell])[0]
    assert packet['tool_results'] == [
        {'tool_call_id': name, 'name': name, 'content': json.dumps({'result': name})}
        for name in ('pulse', 'lookup')]
    assert packet['pulse_description'] == tools[0]['function']['description']
    assert packet['tools_sha256'] == hashlib.sha256(json.dumps(tools, ensure_ascii=False, indent=2).encode()).hexdigest()
    assert packet['measured_at'] == '2026-10-14T17:00:00+09:00'
    assert not {'scenario', 'label', 'path'} & packet.keys()


def test_packets_wake_turn_fields_are_complete_and_untruncated():
    cell, tools = packet_cell()
    cell.pop('type')
    cell['upstream_rows'][0]['wake'] = True
    cell['upstream_rows'][0]['request']['messages'][-1]['content'] = 'full lookup result ' * 100
    packet = packets([cell])[0]
    assert packet['tool_results'][-1]['content'] == 'full lookup result ' * 100
    assert len(packet['tool_results']) == 2
    assert packet['pulse_description'] == tools[0]['function']['description']
    assert packet['measured_at'] == '2026-10-14T17:00:00+09:00'


def test_upstream_records_measured_wall_clock_without_network(tmp_path, monkeypatch):
    from bench.live.upstream import Upstream
    from bench.live import upstream
    monkeypatch.setattr(upstream.time, 'time', lambda: NOW)
    up = Upstream('https://example.invalid', 'primary', 'UNUSED_FAKE_KEY', tmp_path / 'capture.jsonl', {'primary': 'primary'})
    up.wake = True  # Injected outage never opens a connection or reads a key.
    assert up.forward({'model': 'primary', 'messages': []})[1] == 503
    assert up.rows[0]['started_at'] == NOW
    assert json.loads(up.record_path.read_text())['started_at'] == NOW


def test_packets_cli_stores_tools_reference(tmp_path, monkeypatch):
    from bench.live.adjudicate import main
    cell, tools = packet_cell()
    source = tmp_path / 'results' / 'P1'
    source.mkdir(parents=True)
    (source / 'T1-1.json').write_text(json.dumps(cell))
    out = tmp_path / 'packets'
    monkeypatch.setattr(sys, 'argv', ['adjudicate', 'packets', '--results', str(source.parent), '--out', str(out)])
    main()
    assert json.loads((out / 'tools.json').read_text()) == tools
    packet = json.loads((out / 'packets.json').read_text())[0]
    assert packet['tools_sha256'] == hashlib.sha256((out / 'tools.json').read_bytes()).hexdigest()


@pytest.mark.parametrize('version,override,allowed', [('0.1.17', None, False), ('0.1.18', None, True),
    ('0.1.19', None, True), ('0.1.17', '0.1.17', True)])
def test_runner_version_guard_declaration_and_scenarios(tmp_path, monkeypatch, capsys, version, override, allowed):
    from bench.live import run
    root, pin, out = (tmp_path / name for name in ('repo', 'pin', 'out'))
    root.mkdir()
    (root / 'plugin.yaml').write_text(f'version: "{version}"\n')
    (pin / 'tests/fakes').mkdir(parents=True)
    (pin / 'tests/fakes/fake_llm_provider.py').touch()
    monkeypatch.setattr(run, 'ROOT', root)
    monkeypatch.setattr(run.subprocess, 'check_output', lambda *a, **kw: 'f' * 40 + '\n')
    seen = []
    monkeypatch.setattr(run, 'cell', lambda args, spec, rep: seen.append(spec) or 0)
    argv = ['run', '--round', '4', '--pin', str(pin), '--path', 'P1', '--upstream', 'fake',
            '--out', str(out), '--cell', 'T1:1']
    if override:
        argv += ['--min-plugin-version', override]
    monkeypatch.setattr(sys, 'argv', argv)
    if not allowed:
        with pytest.raises(SystemExit) as exc:
            run.main()
        assert exc.value.code == 2
        assert not seen and not out.exists()
        assert 'requires plugin' in capsys.readouterr().err
    else:
        assert run.main() == 0 and seen[0]['match']['due'] == 'in 30d'
        declaration = json.loads((out / 'declaration.json').read_text())
        assert declaration['commit'] == 'f' * 40 and declaration['plugin_version'] == version
        assert declaration['round'] == 4
