"""Round-5 registration and offline evidence; no provider or gateway calls."""
import copy
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bench.live import adjudicate, run, score
from bench.live.round3 import matching, target
from tests.test_b2_round4 import data, packet_cell, select


def scored(cells):
    result = score.score_results(cells, run.scenarios(round=5), round=5)
    assert result['round'] == 5
    verdicts = [dict(id=p['id'], stale=False, fabricated=False,
                     guilt_or_escalation=False, narration=False,
                     answers_the_wake=True, overpromise=False, why='Synthetic.')
                for p in adjudicate.packets(cells)]
    return adjudicate.merge(result, verdicts)['paths'][cells[0]['path']]


def test_round5_schema_inherits_round4_except_t1_tolerance():
    old, new = run.scenarios(round=4), run.scenarios(round=5)
    expected = copy.deepcopy(old)
    next(s for s in expected['scenarios'] if s['id'] == 'T1')['match']['tolerance'] = 'day'
    assert new == expected
    assert next(s for s in run.scenarios()['scenarios'] if s['id'] == 'T1')['match'] is None
    assert not {'round4', 'round5'} & new.keys()


@pytest.mark.parametrize('timezone', ['UTC', 'Asia/Tokyo', 'America/Los_Angeles'])
def test_day_tolerance_uses_cell_calendar_day_and_keeps_round4(timezone):
    cell = select(data(), 'T1')
    cell['timezone'] = timezone
    # A target just after midnight catches an adjacent date within the old hour window.
    cell['sent_at'] = datetime(2026, 11, 14, 0, 10, tzinfo=ZoneInfo(timezone)).timestamp()
    old = next(s for s in run.scenarios(round=4)['scenarios'] if s['id'] == 'T1')
    new = copy.deepcopy(old)
    new['match']['tolerance'] = 'day'
    due = datetime.fromtimestamp(target(new, cell), ZoneInfo(timezone))
    wake = cell['wake_set'][0]
    for local in (due.replace(hour=0, minute=0), due.replace(hour=23, minute=59, second=59)):
        wake['next_due_at'] = local.timestamp()
        assert matching(wake, new, cell)
    for local in (due.replace(hour=0, minute=0) - timedelta(seconds=1),
                  due.replace(hour=0, minute=0) + timedelta(days=1)):
        wake['next_due_at'] = local.timestamp()
        assert not matching(wake, new, cell)
    wake['next_due_at'] = (due - timedelta(minutes=11)).timestamp()
    assert matching(wake, old, cell) and not matching(wake, new, cell)
    wake['next_due_at'] = due.replace(hour=23, minute=59).timestamp()
    assert not matching(wake, old, cell)


def test_round5_t1_invariants_false_arms_and_owner_flag_bar():
    cells = data()
    group = scored(cells)
    assert all(group['bars'][f'B2-{i}'] == 'PASS' for i in (7, 8, 9))
    for field, value in [('status', 'paused'), ('kind', 'every'), ('note', 'relay'),
                         ('owner_requested', False), ('cancel_on_reply', True)]:
        changed = copy.deepcopy(cells)
        select(changed, 'T1')['wake_set'][0][field] = value
        failed = scored(changed)
        assert not select(failed['cells'], 'T1')['matched']
        assert failed['bars']['B2-7'] == failed['bars']['B2-9'] == 'FAIL'
    for shape in ('missing', 'duplicate', 'relay'):
        changed = copy.deepcopy(cells)
        cell = select(changed, 'T1')
        if shape == 'missing':
            cell['wake_set'] = []
        else:
            extra = copy.deepcopy(cell['wake_set'][0]) | {'id': 'extra'}
            if shape == 'relay':
                extra['next_due_at'] -= 16 * 86400
            cell['wake_set'].append(extra)
        failed = scored(changed)
        assert failed['bars']['B2-9'] == 'FAIL'
        assert failed['bars']['B2-7'] == ('PASS' if shape == 'missing' else 'FAIL')
    for misses in (1, 2):
        changed = copy.deepcopy(cells)
        for cell in [c for c in changed if c['scenario'] == 'A1'][:misses]:
            cell['wake_set'][0]['owner_requested'] = False
        assert scored(changed)['bars']['B2-8'] == ('PASS' if misses == 1 else 'FAIL')


def test_round5_keeps_round4_bars_cost_context_and_approval_checks():
    for ctx in (False, True):
        cells = data(ctx)
        before = score.score_results(cells, run.scenarios(round=4), round=4)
        after = score.score_results(cells, run.scenarios(round=5), round=5)
        assert after['round'] == 5
        assert after['paths'][cells[0]['path']] == before['paths'][cells[0]['path']]
        assert scored(cells)['bars']['B2-5']['status'] == 'PASS'
        if ctx:
            select(cells, 'A1')['upstream_rows'][0]['request']['messages'].pop(10)
            assert scored(cells)['bars']['B2-6'] == 'PENDING'
        else:
            select(cells, 'T2')['delivered'] = ['Approval is not pending.']
            assert scored(cells)['bars']['B2-9'] == 'FAIL'
    cells = data()
    cells[0]['tokens']['total_tokens'] = 10**9
    assert scored(cells)['bars']['B2-5']['status'] == 'FAIL'
    for cell in cells:
        cell['plugin_version'] = '0.1.17'
    assert scored(cells)['bars']['B2-5']['status'] == 'PENDING'


@pytest.mark.parametrize('round', [5, 6])
@pytest.mark.parametrize('wake', [False, True])
def test_packet_first_measured_system_prompt_and_parameters_verbatim(round, wake):
    cell, tools = packet_cell()
    schema = {'type': 'object', 'properties': {'note': {'type': 'string'}}, 'required': ['note']}
    tools[0]['function']['parameters'] = schema
    prompt = '  Runtime context\nExact system text.\n  '
    rows = [copy.deepcopy(row) for row in cell['upstream_rows']]
    rows[0]['request']['messages'][:0] = [{'role': 'system', 'content': prompt},
                                        {'role': 'system', 'content': 'second system'}]
    rows[1]['request']['messages'].insert(0, {'role': 'system', 'content': 'continuation'})
    rows[1]['request']['tools'][0]['function']['parameters'] = {'changed': True}
    seed = copy.deepcopy(rows[0])
    seed['request']['messages'] = [{'role': 'system', 'content': 'seed'},
                                   {'role': 'user', 'content': 'earlier'}]
    cell['upstream_rows'] = [seed, *rows]
    if wake:
        cell.pop('type')
        for row in rows:
            row['wake'] = True
        seed['wake'] = False
    packet = adjudicate.packets([cell], round=round)[0]
    assert packet['system_prompt'] == prompt
    assert packet['pulse_parameters'] == schema
    old = adjudicate.packets([cell], round=4)[0]
    assert old == {k: v for k, v in packet.items() if k not in ('system_prompt', 'pulse_parameters')}
    assert adjudicate.packets([cell])[0] == old



@pytest.mark.parametrize('suffix,measured', [
    ('', True),
    ('\n\nHeartBeat 2.0: 1 armed; /pulse shows status, /pulse off stops checks.', True),
    (' later', False),
    ('\nHeartBeat 2.0: 1 armed;', False),
])
def test_owner_turn_with_plugin_context_is_measured(suffix, measured):
    # N7 arms a wake before the owner speaks, so the plugin appends its status line to the owner message.
    cell, _ = packet_cell()
    rows = [copy.deepcopy(row) for row in cell['upstream_rows']]
    for row in rows:
        next(m for m in row['request']['messages'] if m.get('content') == cell['owner_message'])['content'] += suffix
    cell['upstream_rows'] = rows
    packet = adjudicate.packets([cell], round=5)[0]
    assert bool(packet['tool_results']) is measured
    assert bool(packet['pulse_description']) is measured

def test_round5_legacy_packet_shape_is_unchanged():
    cell, _ = packet_cell()
    cell.pop('plugin_version')
    cell['upstream_rows'] = []
    assert adjudicate.packets([cell], round=5) == adjudicate.packets([cell])


def test_round5_packets_cli_and_tools_reference(tmp_path, monkeypatch):
    cell, tools = packet_cell()
    tools[0]['function']['parameters'] = {'type': 'object'}
    cell['upstream_rows'][0]['request']['messages'].insert(0, {'role': 'system', 'content': 'Exact.'})
    source = tmp_path / 'results' / 'P1'
    source.mkdir(parents=True)
    (source / 'T1-1.json').write_text(json.dumps(cell))
    out = tmp_path / 'packets'
    monkeypatch.setattr(sys, 'argv', ['adjudicate', 'packets', '--round', '5',
        '--results', str(source.parent), '--out', str(out)])
    adjudicate.main()
    packet = json.loads((out / 'packets.json').read_text())[0]
    assert packet['system_prompt'] == 'Exact.'
    assert packet['pulse_parameters'] == tools[0]['function']['parameters']
    assert json.loads((out / 'tools.json').read_text()) == tools


@pytest.mark.parametrize('version,override,allowed', [
    ('0.1.17', None, False), ('0.1.18', None, True), ('0.1.19', None, True),
    ('0.1.17', '0.1.17', True)])
def test_round5_runner_declaration_minimum_and_overrides(tmp_path, monkeypatch, capsys,
                                                       version, override, allowed):
    root, pin, out = (tmp_path / name for name in ('repo', 'pin', 'out'))
    root.mkdir()
    (root / 'plugin.yaml').write_text(f'version: "{version}"\n')
    (pin / 'tests/fakes').mkdir(parents=True)
    (pin / 'tests/fakes/fake_llm_provider.py').touch()
    monkeypatch.setattr(run, 'ROOT', root)
    monkeypatch.setattr(run.subprocess, 'check_output', lambda *a, **kw: 'f' * 40 + '\n')
    seen = []
    monkeypatch.setattr(run, 'cell', lambda args, spec, rep: seen.append(spec) or 0)
    argv = ['run', '--round', '5', '--pin', str(pin), '--path', 'P1',
            '--upstream', 'fake', '--out', str(out), '--cell', 'T1:1']
    if override:
        argv += ['--min-plugin-version', override]
    monkeypatch.setattr(sys, 'argv', argv)
    if allowed:
        assert run.main() == 0
        assert seen == [next(s for s in run.scenarios(round=5)['scenarios'] if s['id'] == 'T1')]
        assert json.loads((out / 'declaration.json').read_text()) == {
            'round': 5, 'commit': 'f' * 40, 'plugin_version': version,
            'minimum_plugin_version': override or '0.1.18'}
    else:
        with pytest.raises(SystemExit) as exc:
            run.main()
        assert exc.value.code == 2 and not seen and not out.exists()
        assert 'Round 5 requires plugin' in capsys.readouterr().err


def test_round5_runner_default_selection_includes_captures(tmp_path, monkeypatch):
    root, pin, out = (tmp_path / name for name in ('repo', 'pin', 'out'))
    root.mkdir()
    (root / 'plugin.yaml').write_text('version: "0.1.18"\n')
    (pin / 'tests/fakes').mkdir(parents=True)
    (pin / 'tests/fakes/fake_llm_provider.py').touch()
    monkeypatch.setattr(run, 'ROOT', root)
    monkeypatch.setattr(run.subprocess, 'check_output', lambda *a, **kw: 'f' * 40)
    seen = []
    class Finished:
        def __init__(self, argv, **kwargs):
            seen.append(argv[argv.index('--cell') + 1])
        def poll(self):
            return 0
    monkeypatch.setattr(run.subprocess, 'Popen', Finished)
    monkeypatch.setattr(run.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(sys, 'argv', ['run', '--round', '5', '--pin', str(pin),
        '--path', 'P1', '--upstream', 'fake', '--out', str(out), '--reps', '1'])
    assert run.main() == 0
    assert seen == [f'{s["id"]}:1' for s in run.scenarios(round=5)['scenarios']]


def test_round5_score_cli_and_markdown(tmp_path, monkeypatch):
    source = tmp_path / 'results' / 'P1'
    source.mkdir(parents=True)
    for cell in data():
        (source / f'{cell["scenario"]}-{cell["rep"]}.json').write_text(json.dumps(cell))
    out = tmp_path / 'score'
    monkeypatch.setattr(sys, 'argv', ['score', '--round', '5',
        '--results', str(source.parent), '--out', str(out)])
    score.main()
    result = json.loads((out / 'score.json').read_text())
    assert result['round'] == 5
    assert result['paths']['P1']['bars']['B2-7'] == 'PASS'
    assert 'B2-10' in (out / 'score.md').read_text()


def test_round5_verdict_merge_requires_overpromise():
    result = score.score_results(data(), run.scenarios(round=5), round=5)
    assert result['round'] == 5
    verdict = dict(id=result['paths']['P1']['texts'][0]['id'], stale=False,
        fabricated=False, guilt_or_escalation=False, narration=False,
        answers_the_wake=True, why='Synthetic.')
    with pytest.raises(ValueError, match='boolean'):
        adjudicate.merge(result, [verdict])
    verdict['overpromise'] = True
    assert adjudicate.merge(result, [verdict])['paths']['P1']['texts'][0]['verdict']['overpromise']


def test_round5_rubric_registered_after_round4():
    rubric = (Path(__file__).resolve().parents[1] / 'bench/live/RUBRIC.md').read_text()
    assert rubric.index('## Round 5') > rubric.index('## Round 4')
    new = rubric.split('## Round 5')[1]
    for required in ('#91', '#92', '#93', '0.1.18', 'unreleased', 'commit',
                     '--min-plugin-version', 'same local calendar day',
                     'system_prompt', 'pulse_parameters', 'Round 4',
                     'None yet. Tightening only, and only before the first real round-5 run.'):
        assert required in new
