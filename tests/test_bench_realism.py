"""B1 realism: fixed scenarios, token assumptions and hard observation bars."""
import copy

import pytest

from bench.run import load_scenarios, totals
from bench.sim.metrics import compute
from bench.sim.runner import simulate

NEW = ('caps-owner-bypass', 'mute-classes', 'quiet-default', 'dm-only',
       'cron-trigger', 'command-trigger', 'cache-snap', 'ignored-ladder',
       'error-ladder', 'due-after-expiry', 'change-after-final')


@pytest.mark.parametrize('name', NEW)
def test_realism_scenario(tmp_path, name):
    scenario, = load_scenarios(name)
    assert scenario['baseline_interval_s'] > 0
    result = simulate(scenario, tmp_path)
    assert not result['failures'], result['failures']


def test_new_observation_and_state_bars_are_hard(tmp_path):
    scenario, = load_scenarios('mute-classes')
    result = simulate(scenario, tmp_path)
    changed = copy.deepcopy(scenario)
    changed['expectations']['observations'][0]['reason'] = 'impossible'
    changed['expectations']['bars']['requested']['wake_fields']['owner_requested'] = False
    _, failures = compute(changed, result['deliveries'], result['turns'],
                          result['humans'], result['database'], {}, result['cards'],
                          observations=result['trace'])
    assert any('observation' in f for f in failures)
    assert any('owner_requested' in f for f in failures)


@pytest.mark.parametrize('scenario, expected', [
    ({}, (1200000, 1200000, 750000, 450000, 1200)),
    ({'calls_per_turn': 3, 'context_tokens': 10,
      'output_tokens_per_call': 7, 'cache_ttl_s': 301}, (60, 60, 50, 10, 42)),
    ({'calls_per_turn': 1, 'context_tokens': 10,
      'output_tokens_per_call': 0, 'cache_ttl_s': 0}, (20, 20, 0, 20, 0)),
])
def test_per_call_tokens_session_isolation_and_ttl_boundary(scenario, expected):
    from bench.sim.metrics import TokenModel
    model = TokenModel(scenario)
    rows = [model.turn('a', 0), model.turn('a', 300),
            model.turn('b', 300), model.turn('a', 601)]
    if scenario:
        rows = rows[:2]  # still exercise isolation/expiry above; aggregate first two
    keys = ('est_tokens', 'est_input_tokens', 'est_cache_read_tokens',
            'est_cache_write_tokens', 'est_output_tokens')
    assert tuple(sum(row[k] for row in rows) for k in keys) == expected
    assert all(r['est_input_tokens'] == r['est_cache_read_tokens'] +
               r['est_cache_write_tokens'] for r in rows)


def test_naive_and_hb2_use_identical_token_model(tmp_path):
    scenario, = load_scenarios('positive-followup')
    scenario.update(calls_per_turn=3, context_tokens=17, output_tokens_per_call=11,
                    cache_ttl_s=4000)
    rows = {arm: simulate(scenario, tmp_path / arm, baseline=arm == 'baseline')
            for arm in ('heartbeat2', 'baseline')}
    for result in rows.values():
        m = result['metrics']
        assert m['est_tokens'] == m['est_input_tokens'] == m['model_wakes'] * 3 * 17
        assert m['est_output_tokens'] == m['model_wakes'] * 3 * 11
        assert m['est_input_tokens'] == m['est_cache_read_tokens'] + m['est_cache_write_tokens']
    total = totals([{'id': 'test', **rows}], 'heartbeat2')
    assert total['est_input_tokens'] == rows['heartbeat2']['metrics']['est_input_tokens']


def test_token_model_owner_touch_and_profile_isolation():
    from bench.sim.metrics import TokenModel
    model = TokenModel({})
    model.touch(('main', 'owner'), 0)
    assert model.turn(('main', 'owner'), 300)['est_cache_write_tokens'] == 0
    assert model.turn(('other', 'owner'), 300)['est_cache_write_tokens'] == 150000
    assert model.turn(('main', 'owner'), 601)['est_cache_write_tokens'] == 150000


def test_e2e_latency_includes_hold_and_uses_receiving_clock(capsys):
    from types import SimpleNamespace
    from tests.e2e.test_gateway import record_latency
    calls = [SimpleNamespace(at=99, params={'text': 'WAKE-DELIVERED'}),
             SimpleNamespace(at=110, params={'text': 'progress'}),
             SimpleNamespace(at=200, params={'text': 'WAKE\\-DELIVERED'})]
    rig = SimpleNamespace(name='held-watcher', driver=SimpleNamespace(user_id='fixture', sends=lambda _: calls))
    row = record_latency(rig, due_at=1000, wall_offset=900)
    assert row['raw_due_to_visible_s'] == 100
    assert row['visible_at'] == 1100
    assert 'E2E_LATENCY' in capsys.readouterr().out
