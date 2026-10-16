"""Amendment 20: synthetic three-day evidence and unchanged full-run bars."""
from datetime import datetime

import pytest

from bench.soak.score import epoch, score
from test_b3_runner import rig
from test_b3_score import corpus


def run_corpus(until_day=3, horizon_source='run_end'):
    schedule, logs, verdicts = corpus()
    episodes = [e for e in schedule['episodes'] if e['day'] <= until_day]
    ids = {e['id'] for e in episodes}
    wake_ids = {e['id']+f'-{n}' for e in episodes if e['kind'] != 'N'
                for n in range(len(e['sends']))}
    logs = [r for r in logs if (r['episode'] is None or r['episode'] in ids)
            and (r['kind'] != 'wake_turn' or r['occurrence']['wake_id'] in wake_ids)]
    for row in logs:
        for key in ('snapshot', 'before'):
            if key in row:
                row[key]['wakes'] = [w for w in row[key]['wakes'] if w['id'] in wake_ids]
                row[key]['occurrences'] = [o for o in row[key]['occurrences'] if o['wake_id'] in wake_ids]
    logs[-1]['until_day'] = until_day
    if horizon_source == 'declaration':
        logs[0]['until_day'] = until_day
        logs.pop()
    return schedule, logs, verdicts


def miss_capture(schedule, logs):
    watcher = next(e for e in schedule['episodes'] if e['kind'] == 'W')
    logs.append(dict(kind='w_baseline_missing', episode=watcher['id'], t=watcher['latest_close']))


def late_delivery(logs):
    dm = next(r for r in logs if r['kind'] == 'dm' and
              r['message']['text'] not in ('Set.', 'Okay.', 'Stopped.'))
    dm['message']['edit_date'] = datetime.fromtimestamp(epoch(dm['message']['date'])+600).astimezone().isoformat()


def unflag_matching(logs):
    turn = next(r for r in logs if r['kind'] == 'turn' and r['pulse_results'])
    old_ids = {w['id'] for w in turn['before']['wakes']}
    next(w for w in turn['snapshot']['wakes'] if w['id'] not in old_ids)['owner_requested'] = 0


@pytest.mark.parametrize('horizon_source', ['run_end', 'declaration'])
def test_three_day_pass_excludes_days_four_through_seven(horizon_source):
    schedule, logs, verdicts = run_corpus(horizon_source=horizon_source)
    scoped = [e for e in schedule['episodes'] if e['day'] <= 3]
    assert len(scoped) == 14
    assert sum(e['kind'] != 'N' for e in scoped) == 11
    assert {e['kind'] for e in scoped} == {e['kind'] for e in schedule['episodes']}
    result = score(schedule, logs, verdicts)
    assert result['status'] == 'PASS'
    assert set(result['bars'].values()) == {'PASS'}
    assert result['metrics']['wake_cost']['api_calls'] == 11


def test_declared_horizon_cannot_change_during_the_run():
    schedule, logs, verdicts = run_corpus()
    logs[0]['until_day'] = 8  # declared 8, but a later runner session ended at day 3
    with pytest.raises(ValueError, match='until_day changed'):
        score(schedule, logs, verdicts)
    logs[0]['until_day'] = 3
    assert score(schedule, logs, verdicts)['status'] == 'PASS'


def test_three_day_one_capture_miss_fails():
    schedule, logs, verdicts = run_corpus()
    miss_capture(schedule, logs)
    result = score(schedule, logs, verdicts)
    assert result['bars']['B3-1'] == 'FAIL'
    assert result['violations']['B3-1'][0]['capture_misses'] == 1


def test_three_day_one_unflagged_matching_wake_fails():
    schedule, logs, verdicts = run_corpus()
    unflag_matching(logs)
    assert score(schedule, logs, verdicts)['bars']['B3-1'] == 'FAIL'


def test_three_day_one_late_delivery_fails():
    schedule, logs, verdicts = run_corpus()
    late_delivery(logs)
    assert score(schedule, logs, verdicts)['bars']['B3-3'] == 'FAIL'


@pytest.mark.parametrize('field,value', [('useful', False), ('narration', True)])
def test_three_day_every_delivery_must_be_useful_without_narration(field, value):
    schedule, logs, verdicts = run_corpus()
    next(v for v in verdicts if v['id'].startswith('d-'))[field] = value
    assert score(schedule, logs, verdicts)['bars']['B3-8'] == 'FAIL'


def test_short_run_rejects_one_bad_delivery_even_above_ninety_five_percent():
    schedule, logs, verdicts = run_corpus(6)
    late_delivery(logs)
    next(v for v in verdicts if v['id'].startswith('d-'))['useful'] = False
    result = score(schedule, logs, verdicts)
    deliveries = [p for p in result['packets'] if p['id'].startswith('d-')]
    assert (len(deliveries)-1)/len(deliveries) >= .95
    assert result['bars']['B3-3'] == result['bars']['B3-8'] == 'FAIL'


def test_three_day_void_without_replacement_is_incomplete():
    schedule, logs, verdicts = run_corpus()
    episode = schedule['episodes'][0]
    logs.append(dict(kind='void', episode=episode['id'], t=episode['sends'][0]['at'], reason='client_error'))
    result = score(schedule, logs, verdicts)
    assert result['status'] == 'INCOMPLETE'
    assert set(result['bars'].values()) == {'INCOMPLETE'}


def test_seven_day_tolerances_and_legacy_horizon_are_unchanged():
    schedule, logs, verdicts = run_corpus(7)
    miss_capture(schedule, logs)
    late_delivery(logs)
    unflag_matching(logs)
    next(v for v in verdicts if v['id'].startswith('d-'))['useful'] = False
    result = score(schedule, logs, verdicts)
    assert result['status'] == 'PASS'
    assert set(result['bars'].values()) == {'PASS'}
    next(r for r in logs if r['kind'] == 'run_end').pop('until_day')
    assert score(schedule, logs, verdicts) == result


def test_runner_records_horizon_in_declaration(tmp_path):
    runner, _, _, _ = rig(tmp_path, 'R-clock')
    runner.until_day = 3
    assert runner.run() == 0
    declaration = next(r for r in runner.rows if r['kind'] == 'declaration')
    assert declaration['until_day'] == 3
