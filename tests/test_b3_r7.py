"""B3-lite scorer regressions for issue 86; synthetic evidence only."""
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bench.soak.score import epoch, score
from test_b3_score import corpus


def snapshot_corpus():
    schedule, logs, verdicts = corpus()
    for start in (r for r in logs if r['kind'] == 'episode_start'):
        send = next(r for r in logs if r['kind'] == 'send' and
                    r['episode'] == start['episode'] and r['key'] == 'request:0')
        start['snapshot'] = deepcopy(send['before'])
    return schedule, logs, verdicts


def episode_rows(schedule, logs, kind):
    episode = next(e for e in schedule['episodes'] if e['kind'] == kind)
    return episode, [r for r in logs if r['episode'] == episode['id']]


def dm(logs, mid, at, text, edit_date=None):
    logs.append(dict(kind='dm', episode=None, t=at,
                     message=dict(id=mid, date=at, edit_date=edit_date,
                                  out=False, text=text)))


@pytest.mark.parametrize('end_kind', ['archive', 'episode_end'])
@pytest.mark.parametrize('trigger', [{}, '{"builtin": false}', '["builtin"]', 'invalid'])
def test_outside_owner_turn_false_arm_uses_episode_snapshots(end_kind, trigger):
    schedule, logs, verdicts = snapshot_corpus()
    episode, rows = episode_rows(schedule, logs, 'N')
    start = next(r for r in rows if r['kind'] == 'episode_start')
    end = next(r for r in rows if r['kind'] == 'episode_end')
    at = epoch(start['t'])+60
    extra = dict(id='outside', created_at=at, trigger_json=trigger, status='paused', created_in='wake_turn')
    prior = dict(id='preexisting', created_at=at, trigger_json={}, created_in='wake_turn')
    start['snapshot']['wakes'].append(prior)
    snapshot = deepcopy(end['snapshot'])
    snapshot['wakes'].extend([extra, prior,
                             dict(id='builtin-dict', trigger_json={'builtin': True}, created_in='wake_turn'),
                             dict(id='builtin-json', trigger_json='{"builtin": true}', created_in='wake_turn'),
                             dict(id='owner-turn-unlogged', created_at=at, trigger_json={}, created_in='human_turn')])
    if end_kind == 'archive':
        logs.append(dict(kind='setup', episode=episode['id'], t=end['t'],
                         key='archive', snapshot=snapshot))
        # Cleanup-only additions must not replace the earlier archive inventory.
        end['snapshot']['wakes'].append(dict(id='cleanup-only', created_at=at, trigger_json={}, created_in='wake_turn'))
    else:
        end['snapshot'] = snapshot
    result = score(schedule, logs, verdicts)
    assert result['violations']['B3-2'] == [dict(episode=episode['id'], wake='outside',
                                               reason='created_outside_owner_turn')]
    assert result['bars']['B3-2'] == result['status'] == 'FAIL'


@pytest.mark.parametrize('at_void', [False, True])
def test_outside_false_arm_uses_creation_time_before_void(at_void):
    schedule, logs, verdicts = snapshot_corpus()
    episode, rows = episode_rows(schedule, logs, 'N')
    start = epoch(next(r for r in rows if r['kind'] == 'episode_start')['t'])
    end = next(r for r in rows if r['kind'] == 'episode_end')
    end['snapshot']['wakes'].extend([
        dict(id='prevoid', created_at=start+30, trigger_json={}, created_in='wake_turn'),
        dict(id='postvoid', created_at=start+60+int(not at_void), trigger_json={}, created_in='wake_turn'),
    ])
    logs.append(dict(kind='void', episode=episode['id'], t=start+60, reason='client_error'))
    result = score(schedule, logs, verdicts)
    assert result['violations']['B3-2'] == [dict(episode=episode['id'], wake='prevoid',
                                               reason='created_outside_owner_turn')]
    assert result['bars']['B3-2'] == result['status'] == 'FAIL'


@pytest.mark.parametrize('missing', ['start', 'end', 'both'])
def test_completed_episode_without_comparison_snapshot_is_incomplete(missing):
    schedule, logs, verdicts = snapshot_corpus()
    _, rows = episode_rows(schedule, logs, 'N')
    for row in rows:
        if row['kind'] == 'episode_start' and missing in ('start', 'both'):
            row.pop('snapshot')
        if row['kind'] == 'episode_end' and missing in ('end', 'both'):
            row.pop('snapshot')
    result = score(schedule, logs, verdicts)
    assert not result['violations']['B3-2']
    assert result['bars']['B3-2'] == result['status'] == 'INCOMPLETE'


def test_control_turn_created_wakes_are_not_counted_twice():
    """Control: the baseline already excludes these wakes from snapshot scoring."""
    schedule, logs, verdicts = snapshot_corpus()
    episode, rows = episode_rows(schedule, logs, 'N')
    request = next(r for r in rows if r['kind'] == 'turn')
    end = next(r for r in rows if r['kind'] == 'episode_end')
    at = epoch(request['t'])
    request_extra = dict(id='request-extra', created_at=at, trigger_json={}, created_in='wake_turn')
    request['snapshot']['wakes'].append(request_extra)
    end['snapshot']['wakes'].append(deepcopy(request_extra))
    for key, purpose in [('reply', 'reply'), ('setup', 'setup')]:
        turn = deepcopy(request)
        turn.update(key=key, purpose=purpose, before=deepcopy(end['snapshot']),
                    snapshot=deepcopy(end['snapshot']))
        extra = dict(id=key+'-extra', created_at=at, trigger_json={}, created_in='wake_turn')
        turn['snapshot']['wakes'].append(extra)
        end['snapshot']['wakes'].append(deepcopy(extra))
        logs.append(turn)
    result = score(schedule, logs, verdicts)
    assert result['violations']['B3-2'] == [dict(episode=episode['id'], extra_wakes=1),
                                          dict(episode=episode['id'], key='reply')]


def delivery(logs, wid, mid, at, text, edit_date=None):
    started = epoch(at)
    logs.append(dict(kind='wake_turn', episode=None, t=at, key=wid+'/one',
                     occurrence=dict(wake_id=wid, dedupe_key='one', delivered=True,
                                     started_at=started-5, ended_at=started),
                     final_text=text, rows=[dict(role='assistant', content=text)]))
    dm(logs, mid, at, text, edit_date)


def test_unrequested_correlated_deliveries_use_message_dates_and_declaration():
    schedule, logs, verdicts = snapshot_corpus()
    logs[0]['t'] = '2026-10-15T00:00:00+09:00'
    baseline_bars = score(schedule, logs, verdicts)['bars']
    delivery(logs, 'old-background', 90001, '2026-10-14T14:59:59+00:00', 'old background')
    delivery(logs, 'builtin-background', 90002, '2026-10-15T15:00:00+00:00', 'builtin background',
             edit_date='2026-10-17T16:00:00+00:00')
    delivery(logs, 'foreign-background', 90003, '2026-10-15T14:59:59+00:00', 'foreign background')
    delivery(logs, 'boundary-background', 90004, logs[0]['t'], 'boundary background')
    dm(logs, 90005, '2026-10-25T00:00:00+09:00', 'ordinary unresolved background')
    result = score(schedule, logs, verdicts)
    assert result['metrics']['unrequested_messages_per_day'] == {
        '2026-10-15': 2, '2026-10-16': 1, '2026-10-25': 1}
    assert result['bars'] == baseline_bars
    assert len(result['unresolved']) == 1 and not result['unresolved'][0]['could_belong']


def test_unrequested_metric_excludes_all_request_matches_even_voided_and_discarded_w():
    schedule, logs, verdicts = snapshot_corpus()
    watcher, _ = episode_rows(schedule, logs, 'W')
    logs.append(dict(kind='w_baseline_missing', episode=watcher['id'], t=watcher['latest_close']))
    reminder, _ = episode_rows(schedule, logs, 'R-rel')
    logs.append(dict(kind='void', episode=reminder['id'], t=reminder['latest_close'], reason='client_error'))
    # Duplicate deliveries for each requested wake still belong to the request.
    for n, row in enumerate([r for r in logs if r['kind'] == 'wake_turn']):
        dm(logs, 91000+n, row['t'], row['final_text'])
    delivery(logs, 'unmatched', 92000, '2026-10-25T00:00:00+09:00', 'unmatched delivery')
    result = score(schedule, logs, verdicts)
    assert result['metrics']['unrequested_messages_per_day'] == {'2026-10-25': 1}
    watcher_wid = watcher['id']+'-0'
    assert watcher_wid not in result['metrics']['wake_cost_by_wake']
    assert reminder['id']+'-0' in result['metrics']['wake_cost_by_wake']


@pytest.mark.parametrize('kind,logged', [('M', 'close'), ('S', 'close'), ('W', 'w_window'), ('W', 'both')])
def test_unresolved_windows_use_max_logged_close_and_window(kind, logged):
    schedule, logs, verdicts = snapshot_corpus()
    episode, _ = episode_rows(schedule, logs, kind)
    at = max(epoch(e['latest_close']) for e in schedule['episodes'])+3600
    stamp = datetime.fromtimestamp(at).astimezone().isoformat()
    later = at+300
    if logged in ('close', 'both'):
        logs.append(dict(kind='close', episode=episode['id'], t=stamp, close_at=later))
        logs.append(dict(kind='close', episode=episode['id'], t=stamp, close_at=at-60))
    if logged in ('w_window', 'both'):
        logs.append(dict(kind='w_window', episode=episode['id'], t=stamp,
                         latest_close=later))
        logs.append(dict(kind='w_window', episode=episode['id'], t=stamp, latest_close=at-60))
    dm(logs, 93000, stamp, 'unattributable extended-window message')
    boundary = datetime.fromtimestamp(later).astimezone().isoformat()
    after = datetime.fromtimestamp(later+1).astimezone().isoformat()
    dm(logs, 93001, boundary, 'unattributable boundary message')
    dm(logs, 93002, after, 'outside all windows')
    result = score(schedule, logs, verdicts)
    unresolved = {u['message']['id']: u for u in result['unresolved']}
    assert unresolved[93000]['could_belong'] and unresolved[93001]['could_belong']
    assert not unresolved[93002]['could_belong']
    assert result['bars']['B3-3'] == result['bars']['B3-5'] == result['status'] == 'INCOMPLETE'
    day = datetime.fromtimestamp(later+1, ZoneInfo(schedule['tz'])).date().isoformat()
    assert result['metrics']['unrequested_messages_per_day'] == {day: 1}


def test_control_logged_closes_do_not_shorten_rebased_windows():
    """Control: rebased scheduled close remains a lower bound."""
    schedule, logs, verdicts = snapshot_corpus()
    episode = schedule['episodes'][-1]
    send = next(r for r in logs if r['kind'] == 'send' and
                r['episode'] == episode['id'] and r['key'] == 'request:0')
    send['message']['date'] = datetime.fromtimestamp(epoch(send['message']['date'])+120).astimezone().isoformat()
    at = epoch(episode['latest_close'])+60
    stamp = datetime.fromtimestamp(at).astimezone().isoformat()
    logs.extend([dict(kind='close', episode=episode['id'], t=stamp, close_at=at-60),
                 dict(kind='w_window', episode=episode['id'], t=stamp, latest_close=at-30)])
    dm(logs, 94000, stamp, 'unknown within rebased close')
    result = score(schedule, logs, verdicts)
    assert next(u for u in result['unresolved'] if u['message']['id'] == 94000)['could_belong']


def test_partial_arming_match_is_a_requested_wake():
    schedule, logs, verdicts = snapshot_corpus()
    episode, rows = episode_rows(schedule, logs, 'M')
    turn = next(r for r in rows if r['kind'] == 'turn' and r['key'] == 'request:1')
    wid = next(w['id'] for w in turn['snapshot']['wakes'] if w['id'] not in {x['id'] for x in turn['before']['wakes']})
    logs.remove(turn)
    logs.append(dict(turn, kind='partial_arming'))
    delivery(logs, wid, 95000, '2026-10-25T00:00:00+09:00', 'partial arming delivery')
    delivery(logs, 'unmatched', 95001, '2026-10-25T01:00:00+09:00', 'unmatched delivery')
    assert score(schedule, logs, verdicts)['metrics']['unrequested_messages_per_day'] == {'2026-10-25': 1}


def test_logged_extensions_stop_at_the_first_void():
    schedule, logs, verdicts = snapshot_corpus()
    episode, _ = episode_rows(schedule, logs, 'W')
    at = max(epoch(e['latest_close']) for e in schedule['episodes'])+3600
    stamp = lambda t: datetime.fromtimestamp(t).astimezone().isoformat()
    logs.append(dict(kind='void', episode=episode['id'], t=stamp(at), reason='client_error'))
    logs.append(dict(kind='w_window', episode=episode['id'], t=stamp(at+60), latest_close=at+600))
    dm(logs, 96000, stamp(at+300), 'after the void, inside the logged extension')
    result = score(schedule, logs, verdicts)
    assert not next(u for u in result['unresolved'] if u['message']['id'] == 96000)['could_belong']
