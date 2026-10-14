"""Review-round-2 regressions. Synthetic profiles and fake transports only."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from bench.soak import evidence, runner, score as scoring
from bench.soak.client import ClientError
from bench.soak.runner import Runner, epoch
from heartbeat2.model import Wake
from test_b3_r3 import shape_rig
from test_b3_r4 import declare, restart
from test_b3_score import corpus


@pytest.fixture(autouse=True)
def bounded_clock(monkeypatch):
    from test_b3_runner import FakeClock
    original = FakeClock.sleep
    counts = {}
    def sleep(clock, seconds):
        counts[id(clock)] = counts.get(id(clock), 0)+1
        assert counts[id(clock)] <= 6000, 'fake run exceeded its bounded budget'
        return original(clock, seconds)
    monkeypatch.setattr(FakeClock, 'sleep', sleep)


def started(tmp_path, kind='W', delay=0):
    r, c, store, e = shape_rig(tmp_path, kind)
    declare(r)
    r.emit('runner_start', resumed=False)
    r.clock.callback = lambda: None
    r.clock.wall = epoch(e['sends'][0]['at'])+delay
    r.tick_episode(e)
    r.clock.sleep(15); r.turns()
    return r, c, store, e


@pytest.mark.parametrize('terminal,code', [('abort', 3), ('hard_fail', 2)])
def test_terminal_restart_cleans_all_started_episodes(tmp_path, terminal, code):
    r, c, store, e = started(tmp_path)
    second = deepcopy(e); second['id'] = 'second'
    r.episodes.append(second)
    r.emit('episode_start', second['id'], schedule=second, snapshot=r.snapshot())
    descendant = store.create_wake(Wake('chat', 'synthetic descendant', 'at', status='paused'))
    r.emit(terminal, e['id'], reason='synthetic', **({'status': 'FAIL'} if code == 2 else {}))
    resumed = restart(r, c)
    resumed.episodes.append(second)
    assert resumed.run() == code
    assert all(resumed.has('episode_end', x['id']) for x in (e, second))
    assert store.get_wake(descendant.id).status == 'cancelled'
    assert len([x for x in resumed.rows if x['kind'] == 'runner_start']) == 2
    store.close()


def test_terminal_cleanup_budget_moves_on_and_records_live_wakes(tmp_path):
    r, c, store, e = started(tmp_path)
    second = deepcopy(e); second['id'] = 'second'
    r.episodes.append(second)
    r.emit('episode_start', second['id'], schedule=second, snapshot=r.snapshot())
    r.pending.clear(); r.cleanup = lambda _: False
    at = r.clock.now()
    r.terminal_cleanup('synthetic', budget=10)
    rows = [x for x in r.rows if x['kind'] == 'terminal_cleanup_incomplete']
    assert [x['episode'] for x in rows] == [e['id'], second['id']]
    assert all(x['cause'] == 'synthetic' for x in rows)
    assert rows[0]['wakes'][0]['status'] == 'active'
    assert r.clock.now()-at == 20
    store.close()


def test_hard_failure_cleans_started_episode_without_active_pointer(tmp_path, monkeypatch):
    r, c, store, e = started(tmp_path)
    r.emit('baseline_setup', session=r.pulse.session('chat'))
    r.active = None
    monkeypatch.setattr(scoring, 'hard_failure', lambda *a: 'B3-6')
    assert r.run() == 2 and r.has('episode_end', e['id'])
    assert all(w['status'] == 'cancelled' for w in r.pulse.wakes('chat'))
    store.close()


@pytest.mark.parametrize('key,text', [('setup_cancel:missing', '/pulse cancel missing'), ('setup_on', '/pulse on')])
def test_cleanup_client_error_never_voids_even_before_close(tmp_path, key, text):
    r, c, store, e = started(tmp_path)
    c.send = lambda _: (_ for _ in ()).throw(ClientError('synthetic'))
    r.send(e, key, text, 'setup')
    assert r.has('client_error', e['id'], key)
    assert not r.has('void', e['id']) and not r.has('setup_failed', None)
    store.close()


@pytest.mark.parametrize('target', ['cancel', 'on'])
def test_cleanup_unconfirmed_stays_pending_once_without_global_failure(tmp_path, target):
    r, c, store, e = started(tmp_path)
    w = store.list_wakes('chat')[0]
    key, text = ('setup_cancel:'+w.id, '/pulse cancel '+w.id) if target == 'cancel' else ('setup_on', '/pulse on')
    c.send = lambda text: c.message(text, True)
    r.send(e, key, text, 'setup')
    r.clock.sleep(120); r.turns(); r.turns()
    rows = [x for x in r.rows if x['kind'] == 'cleanup_unconfirmed']
    assert len(rows) == 1 and rows[0]['episode'] == e['id'] and rows[0]['key'] == key
    assert any(x[:2] == (e['id'], key) for x in r.pending)
    assert not r.has('setup_failed', None)
    store.close()


@pytest.mark.parametrize('target,status', [('cancel','active'), ('cancel','paused'), ('cancel','proposed'), ('on',None)])
def test_cleanup_retries_six_times_at_two_minute_intervals(tmp_path, target, status):
    r, c, store, e = started(tmp_path)
    w = store.list_wakes('chat')[0]
    if target == 'cancel': store.cas_wake(w.id, w.status, status=status)
    else:
        store.cas_wake(w.id, w.status, status='cancelled')
        store.cas_session('chat', {'enabled':True}, enabled=False)
    sent = []
    def ignored(text):
        sent.append((r.clock.now(), text)); return c.message(text, True)
    c.send = ignored
    for attempt in range(6):
        assert r.cleanup(e) is False
        assert len(sent) == attempt+1
        r.clock.sleep(119); r.turns(); assert r.cleanup(e) is False
        assert len(sent) == attempt+1
        r.clock.sleep(1); r.turns()
    assert r.cleanup(e) is False; assert r.cleanup(e) is False
    failed = [x for x in r.rows if x['kind'] == 'cleanup_failed']
    assert len(failed) == 1 and failed[0]['episode'] == e['id']
    assert failed[0]['key'] == ('setup_on' if target == 'on' else 'setup_cancel:'+w.id)
    assert failed[0]['reason'] == ('chat_disabled' if target == 'on' else 'wake_still_active')
    assert len(sent) == 6 and all(b[0]-a[0] == 120 for a,b in zip(sent,sent[1:]))
    assert not r.has('setup_failed', None)
    r.terminal_cleanup('cleanup_failed')
    assert r.has('terminal_cleanup_incomplete', e['id'])
    store.close()


def test_cleanup_restart_gets_fresh_attempts_and_unique_keys(tmp_path):
    r, c, store, e = started(tmp_path)
    w = store.list_wakes('chat')[0]; base = 'setup_cancel:'+w.id
    for n in range(6):
        r.emit('send_intent', e['id'], key=base+(f':retry:{n}' if n else ''), purpose='setup')
    r.clock.sleep(120)
    r.emit('cleanup_failed', e['id'], key=base, reason='wake_still_active')
    r.emit('runner_start', resumed=True)
    c.send = lambda text: c.message(text, True)
    assert r.cleanup(e) is False
    assert r.has('send', e['id'], base+':retry:6')
    assert not any(x['kind'] == 'cleanup_failed' for x in r.session_rows())
    store.close()


def test_cleanup_failed_aborts_run_with_bounded_terminal_cleanup(tmp_path):
    r, c, store, e = shape_rig(tmp_path)
    original = c.send
    c.send = lambda text: c.message(text, True) if text.startswith('/pulse cancel ') else original(text)
    assert r.run() == 3
    assert any(x['kind'] == 'abort' and x['reason'] == 'cleanup_failed' for x in r.rows)
    assert r.has('terminal_cleanup_incomplete', e['id']) and not r.has('setup_failed', None)
    assert not r.has('void', e['id'])
    store.close()


def test_live_state_overrides_unconfirmed_cancel_without_resend(tmp_path):
    r, c, store, e = started(tmp_path)
    w = store.list_wakes('chat')[0]
    c.send = lambda text: c.message(text, True)
    assert not r.cleanup(e)
    r.clock.sleep(120); r.turns()
    store.cas_wake(w.id, w.status, status='cancelled')
    assert r.cleanup(e) and not r.has('setup_failed', None)
    assert len([x for x in r.rows if x['kind'] == 'send_intent' and x['purpose'] == 'setup']) == 1
    store.close()


def test_cleanup_rereads_live_targets_while_attempt_is_pending(tmp_path):
    r, c, store, e = started(tmp_path)
    c.send = lambda text: c.message(text, True)
    assert not r.cleanup(e)
    reads = []; snapshot = r.snapshot; session = r.pulse.session
    r.snapshot = lambda: reads.append('wakes') or snapshot()
    r.pulse.session = lambda key: reads.append('session') or session(key)
    assert not r.cleanup(e)
    assert 'wakes' in reads and 'session' in reads
    store.close()


@pytest.mark.parametrize('reason', ['client_error', 'client_outage_5m', 'gateway_restart', 'turn_unresolved', 'runner_stopped'])
def test_close_locks_voids_for_every_caller(tmp_path, reason):
    r, c, store, e = started(tmp_path)
    r.emit('close', e['id'], close_at=r.clock.now())
    r.void(reason)
    assert not r.has('void', e['id'])
    store.close()


@pytest.mark.parametrize('fault', [OSError('synthetic'), subprocess.TimeoutExpired('git', 5)])
def test_hermes_git_fault_preserves_valid_stamp(tmp_path, monkeypatch, fault):
    stamp = tmp_path/'stamp.json'; stamp.write_text(json.dumps({'baseVersion':'1.0', 'commit':'b'*40}))
    monkeypatch.setattr(evidence.subprocess, 'run', lambda *a, **k: (_ for _ in ()).throw(fault))
    assert evidence.hermes_revision(tmp_path, stamp) == dict(hermes_version='1.0', hermes_commit='b'*40, hermes_source_commit=None)
    stamp.write_text('1.0\n')
    assert evidence.hermes_revision(tmp_path, stamp)['hermes_commit'] is None


def test_install_stamp_and_source_commits_are_distinct(tmp_path, monkeypatch):
    stamp = tmp_path/'stamp.json'; stamp.write_text(json.dumps({'baseVersion':'1.0', 'commit':'b'*64}))
    monkeypatch.setattr(evidence.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, 'a'*40))
    assert evidence.hermes_revision(tmp_path, stamp) == dict(hermes_version='1.0', hermes_commit='b'*64, hermes_source_commit='a'*40)
    stamp.write_text(json.dumps({'baseVersion':'1.0', 'commit':'invalid'}))
    revision = evidence.hermes_revision(tmp_path, stamp)
    assert revision['hermes_commit'] == revision['hermes_source_commit'] == 'a'*40


@pytest.mark.parametrize('fault', [OSError(), ValueError(), KeyError(), TypeError(), yaml.YAMLError(), subprocess.SubprocessError()])
def test_state_faults_are_unreadable(tmp_path, monkeypatch, fault):
    r, c, store, e = shape_rig(tmp_path, 'N')
    monkeypatch.setattr(runner, 'frozen_state', lambda *a: (_ for _ in ()).throw(fault))
    assert Runner.state(r) is None
    store.close()


@pytest.mark.parametrize('recovers', [True, False])
def test_declaration_read_retries_without_traceback(tmp_path, recovers):
    r, c, store, e = shape_rig(tmp_path, 'N'); frozen = r.state(); reads = []
    def state():
        reads.append(r.clock.now()); return frozen if recovers and len(reads) > 1 else None
    r.state = state
    assert r.run() == (0 if recovers else 1)
    assert reads[1]-reads[0] == 5
    if not recovers:
        assert len(reads) == 4 and not c.sent
        assert r.rows[-1]['reason'] == 'frozen_state_unreadable'
    store.close()


def test_transient_mismatch_streaks_reset_and_do_not_abort(tmp_path):
    r, c, store, e = shape_rig(tmp_path, 'N'); frozen = r.state()
    values = iter([None, frozen, frozen | {'hermes_version':'changed'}, frozen])
    r.state = lambda: next(values, frozen) if r.has('baseline_setup', None) else frozen
    assert r.run() == 0
    mismatches = [x for x in r.rows if x['kind'] == 'state_mismatch']
    assert len(mismatches) == 2 and mismatches[0]['unreadable'] and not mismatches[1]['unreadable']
    assert mismatches[1]['differing_keys'] == ['hermes_version']
    assert not r.has('abort', e['id'])
    store.close()


@pytest.mark.parametrize('unreadable', [False, True])
def test_sustained_state_fault_waits_sixty_seconds_then_cleans(tmp_path, unreadable):
    r, c, store, e = shape_rig(tmp_path); frozen = r.state(); reads = []
    def state():
        if r.has('turn', e['id'], 'request:0'):
            reads.append(r.clock.now())
            return None if unreadable else frozen | {'hermes_version':'changed'}
        return frozen
    r.state = state
    assert r.run() == 3
    abort = next(x for x in r.rows if x['kind'] == 'abort')
    assert abort['reason'] == ('frozen_state_unreadable' if unreadable else 'frozen_state_changed')
    assert epoch(abort['t'])-reads[0] == 60 and len(reads) >= 3
    assert len([x for x in r.rows if x['kind'] == 'state_mismatch']) == 1
    assert r.has('episode_end', e['id'])
    assert all(w['status'] == 'cancelled' for w in r.pulse.wakes('chat'))
    store.close()


@pytest.mark.parametrize('mode', ['transient', 'changed', 'commit'])
def test_preflight_rereads_state_and_checks_install_commit(tmp_path, mode):
    r, c, store, e = shape_rig(tmp_path, 'N'); declare(r); at = r.clock.now()
    if mode == 'commit':
        r.frozen['hermes_source_commit'] = 'b'*40; r.state = lambda: r.frozen
    else:
        bad = r.frozen | {'hermes_version':'changed'}
        values = iter([None, bad, r.frozen] if mode == 'transient' else [bad]*3)
        r.state = lambda: next(values)
    assert r.preflight() == (mode == 'transient')
    if mode == 'transient': assert r.clock.now()-at == 10
    else: assert r.rows[-1]['reason'] == ('hermes_commit_mismatch' if mode == 'commit' else 'frozen_state_changed')
    assert not c.sent
    store.close()


def test_second_preflight_after_eight_oclock_wait_blocks_changed_state(tmp_path):
    r, c, store, e = shape_rig(tmp_path, 'N'); declare(r)
    r.clock.wall = datetime.fromisoformat(e['sends'][0]['at']).replace(hour=7, minute=59, second=55).timestamp()
    frozen = r.frozen
    r.clock.callback = lambda: setattr(r, 'state', lambda: frozen | {'hermes_version':'changed'})
    assert not r.baseline() and not c.sent
    assert r.rows[-1]['reason'] == 'frozen_state_changed'
    store.close()


@pytest.mark.parametrize('kind', ['R-rel', 'R-clock', 'M', 'W', 'C-reply', 'S'])
def test_offset_uses_transport_time_and_survives_restart(tmp_path, kind):
    r, c, store, e = shape_rig(tmp_path, kind)
    assert r.offset(e) == 0
    declare(r); r.clock.wall = epoch(e['sends'][0]['at'])
    message = c.message(e['sends'][0]['text'], True, r.clock.now()+90)
    r.emit('send', e['id'], key='request:0', message=message, before=r.snapshot(), purpose='request')
    expected = 0 if kind in ('M', 'R-clock') else 90
    assert r.offset(e) == restart(r,c).offset(e) == expected
    store.close()


def test_w_flip_gate_and_baseline_deadline_follow_actual_send(tmp_path):
    r, c, store, e = started(tmp_path, 'W', delay=90)
    original = deepcopy(e); w = store.list_wakes('chat')[0]
    r.clock.wall = epoch(e['flip'])-45; r.evidence(e)
    assert not r.has('gate', e['id'], w.id)
    r.clock.wall += 90; r.evidence(e)
    assert next(x for x in r.rows if x['kind'] == 'gate')['due'] == epoch(e['flip'])+90
    store.cas_wake(w.id, w.status, probe_state_json={'size':8})
    r.clock.wall = epoch(e['flip'])+1800+89; r.tick_episode(e)
    baseline = next(x for x in r.rows if x['kind'] == 'baseline')
    assert not r.has('w_baseline_missing', e['id']) and not r.has('close', e['id'])
    assert r.close_at(e) == epoch(baseline['t'])+1860
    r.clock.sleep(60); r.tick_episode(e)
    assert r.has('file_write', e['id'], 'flip') and not r.has('close', e['id'])
    assert r.close_at(e) == r.clock.now()+1800 and e == original
    store.close()


def test_w_action_waits_for_rebased_flip(tmp_path):
    r, c, store, e = started(tmp_path, 'W', delay=90)
    w = store.list_wakes('chat')[0]; store.cas_wake(w.id, w.status, probe_state_json={'size':8})
    r.evidence(e)
    r.clock.wall = epoch(e['flip']); r.tick_episode(e)
    assert not r.has('file_write', e['id'], 'flip')
    r.clock.wall += 90; r.tick_episode(e)
    assert r.has('file_write', e['id'], 'flip')
    store.close()


def test_c_reply_and_latest_close_follow_actual_send(tmp_path):
    r, c, store, e = started(tmp_path, 'C-reply', delay=90)
    original = deepcopy(e)
    r.clock.wall = epoch(e['reply_at'])+89; r.tick_episode(e)
    assert not r.has('send', e['id'], 'cancel_reply')
    r.clock.sleep(1); r.tick_episode(e)
    assert r.has('send', e['id'], 'cancel_reply')
    r.clock.sleep(15); r.turns()
    r.clock.wall = epoch(e['latest_close'])+89; r.tick_episode(e)
    assert not r.has('close', e['id']) and r.close_at(e) == epoch(e['latest_close'])+90
    r.clock.sleep(1); r.tick_episode(e)
    assert r.has('close', e['id']) and e == original
    store.close()


def test_s_first_delivery_timeout_follows_actual_send(tmp_path):
    r, c, store, e = started(tmp_path, 'S', delay=90)
    r.deliveries = lambda _: []
    r.clock.wall = epoch(e['sends'][0]['at'])+7200; r.tick_episode(e)
    assert not r.has('s_no_first_delivery', e['id']) and not r.has('close', e['id'])
    r.clock.sleep(90); r.tick_episode(e)
    assert r.has('s_no_first_delivery', e['id']) and r.close_at(e) == r.clock.now()
    store.close()


@pytest.mark.parametrize('kind,index', [('R-rel',0), ('C-silent',0), ('M',1), ('S',0)])
def test_hard_bar_ignores_nonmatching_extra_wakes(kind, index):
    s, logs, verdicts = corpus(); e = next(e for e in s['episodes'] if e['kind'] == kind)
    turn = next(x for x in logs if x['kind'] == 'turn' and x['episode'] == e['id'] and x['key'] == f'request:{index}')
    extra = deepcopy(next(w for w in turn['snapshot']['wakes'] if w['id'] == e['id']+f'-{index}'))
    extra.update(id='extra', note='wrong subject')
    turn['snapshot']['wakes'].append(extra)
    logs[-2]['snapshot']['occurrences'].append(dict(wake_id='extra', held_reason='unanswered'))
    # S already had matching-only protection; exercise the new shared rebase API too.
    if kind == 'S': assert scoring.rebased(e, [x for x in logs if x['episode'] == e['id']])['sends'] == e['sends']
    assert scoring.hard_failure(logs) is None
    result = scoring.score(s, logs, verdicts)
    assert result['bars']['B3-2'] == 'FAIL' and result['bars']['B3-6'] == 'PASS'


@pytest.mark.parametrize('kind,index', [('R-rel',0), ('M',1), ('S',0)])
def test_hard_bar_matches_rebased_request_index(kind, index):
    s, logs, verdicts = corpus(); e = next(e for e in s['episodes'] if e['kind'] == kind)
    send = next(x for x in logs if x['kind'] == 'send' and x['episode'] == e['id'] and x['key'] == f'request:{index}')
    send['message']['date'] = datetime.fromtimestamp(epoch(send['message']['date'])+600).astimezone().isoformat()
    turn = next(x for x in logs if x['kind'] == 'turn' and x['episode'] == e['id'] and x['key'] == f'request:{index}')
    w = next(w for w in turn['snapshot']['wakes'] if w['id'] == e['id']+f'-{index}')
    if kind != 'S': w['next_due_at'] += 600
    else: w['next_due_at'] = epoch(send['message']['date'])+6900
    rebased = scoring.rebased(e, [x for x in logs if x['episode'] == e['id']])
    assert scoring.matching(w, rebased, index)
    if kind != 'S':
        logs[-2]['snapshot']['occurrences'].append(dict(wake_id=w['id'], held_reason='unanswered'))
        assert scoring.hard_failure(logs) == 'B3-6'


@pytest.mark.parametrize('missing', ['wake', 'session'])
def test_stop_snapshot_missing_wake_or_session_is_incomplete(missing):
    s, logs, verdicts = corpus(); turn = next(x for x in logs if x['kind'] == 'turn' and x['key'] == 'stop')
    if missing == 'wake': turn['snapshot']['wakes'] = [w for w in turn['snapshot']['wakes'] if w['id'] != turn['episode']+'-0']
    else: turn['snapshot']['sessions'] = []
    assert scoring.score(s, logs, verdicts)['bars']['B3-5'] == 'INCOMPLETE'


@pytest.mark.parametrize('bar,prefix', [(7,'a-'), (8,'d-')])
def test_retained_prevoid_packets_need_verdicts(bar, prefix):
    s, logs, verdicts = corpus(); e = next(e for e in s['episodes'] if e['kind'] == 'R-rel')
    packets = scoring.score(s, logs, verdicts)['packets']
    turn = next(x for x in logs if x['kind'] == 'turn' and x['episode'] == e['id'] and x['key'] == 'request:0')
    arming = 'a-'+str(turn['message_id'])
    delivery = next(p['id'] for p in packets if p['id'].startswith('d-') and p['request'] == turn['owner']['content'])
    missing = arming if prefix == 'a-' else delivery
    logs.append(dict(kind='void', episode=e['id'], t=e['latest_close'], reason='client_error'))
    delta = 7*86400
    def moved(value, key=None):
        if isinstance(value, dict): return {k:moved(v,k) for k,v in value.items()}
        if isinstance(value, list): return [moved(v,key) for v in value]
        if isinstance(value, str):
            if value.startswith('2026-'): return (datetime.fromisoformat(value)+timedelta(seconds=delta)).isoformat()
            if value.startswith(e['id']): return value.replace(e['id'], e['id']+'-r', 1)
        if isinstance(value, (int,float)) and key in ('timestamp','next_due_at','started_at','ended_at','due','sent_at'): return value+delta
        if isinstance(value, int) and key in ('id','message_id'): return value+10000
        return value
    replacement = moved(e); replacement.update(replaces=e['id'], day=8)
    replacement_logs = [moved(x) for x in logs if x['episode'] == e['id'] and x['kind'] != 'void' or
                        x['kind'] == 'wake_turn' and x['occurrence']['wake_id'].startswith(e['id']+'-')]
    replacement_verdicts = [v | {'id':v['id'].split('-')[0]+'-'+str(int(v['id'].split('-')[1])+10000)}
                            for v in verdicts if v['id'] in (arming,delivery)]
    logs.extend(replacement_logs)
    logs.append(dict(kind='requeue', episode=None, t=e['latest_close'], episodes=[replacement]))
    result = scoring.score(s, logs, [v for v in verdicts if v['id'] != missing]+replacement_verdicts)
    assert any(p['id'] == missing for p in result['packets'])
    assert result['bars'][f'B3-{bar}'] == 'PENDING'


def test_delivery_packet_uses_correlated_owner_text():
    s, logs, verdicts = corpus(); e = next(e for e in s['episodes'] if e['kind'] == 'W')
    turn = next(x for x in logs if x['kind'] == 'turn' and x['episode'] == e['id'] and x['key'] == 'request:0')
    turn['owner']['content'] = turn['owner']['content'].replace('{soak_dir}', 'fixtures')
    assert '{soak_dir}' not in turn['owner']['content']
    packets = scoring.score(s, logs, verdicts)['packets']
    delivery = next(p for p in packets if p['id'].startswith('d-') and p.get('flip') == e['flip'])
    assert delivery['request'] == turn['owner']['content']


def test_unresolved_message_uses_rebased_latest_close():
    s, logs, verdicts = corpus(); e = s['episodes'][-1]
    send = next(x for x in logs if x['kind'] == 'send' and x['episode'] == e['id'] and x['key'] == 'request:0')
    send['message']['date'] = datetime.fromtimestamp(epoch(send['message']['date'])+120).astimezone().isoformat()
    at = datetime.fromtimestamp(epoch(e['latest_close'])+60).astimezone().isoformat()
    logs.append(dict(kind='dm', episode=None, t=at, message=dict(id=99999, date=at, out=False, text='unknown')))
    unresolved = scoring.score(s, logs, verdicts)['unresolved']
    assert next(u for u in unresolved if u['message']['id'] == 99999)['could_belong']


@pytest.mark.parametrize('reason', ['frozen_state_unreadable', 'cleanup_failed'])
def test_new_abort_reasons_report_aborted(reason):
    s, logs, verdicts = corpus()
    logs.append(dict(kind='abort', episode=None, t=logs[-1]['t'], reason=reason))
    assert scoring.score(s, logs, verdicts)['status'] == 'ABORTED'


def test_soak_path_expanded_and_resolved_in_runner_and_preview(tmp_path, monkeypatch, capsys):
    r, c, store, e = shape_rig(tmp_path, 'W')
    monkeypatch.setenv('HOME', str(tmp_path))
    other = Runner(r.schedule, r.path, c, r.home, '~/jobs/../resolved', 'chat', pulse=r.pulse, transcript=r.transcript)
    assert other.soak_dir == tmp_path/'resolved'
    runner.preview(r.schedule, 8, '~/jobs/../resolved')
    text = capsys.readouterr().out
    assert str(tmp_path/'resolved') in text and '~/jobs' not in text
    store.close()


def test_hashseed_subprocess_has_explicit_timeout(monkeypatch):
    import test_b3_r2
    original = test_b3_r2.subprocess.run
    def bounded(*args, **kwargs):
        assert kwargs.get('timeout') == 120
        return original(*args, **kwargs)
    monkeypatch.setattr(test_b3_r2.subprocess, 'run', bounded)
    test_b3_r2.test_hashseed_process_determinism()


def test_early_smoke_cli_uses_eight_oh_five(tmp_path, monkeypatch):
    r, c, store, e = shape_rig(tmp_path, 'N')
    now = datetime.fromisoformat(e['sends'][0]['at']).replace(hour=7, minute=0)
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None): return now
    schedule = tmp_path/'schedule.json'; schedule.write_text(json.dumps(r.schedule))
    monkeypatch.setattr(runner, 'datetime', FixedDatetime)
    monkeypatch.setattr('sys.argv', ['runner', '--smoke', '--dry-run', '--schedule', str(schedule),
        '--log', str(tmp_path/'run.jsonl'), '--client-cmd', 'fake-only', '--peer', 'synthetic',
        '--profile-home', str(r.home), '--soak-dir', str(r.soak_dir), '--chat-key', 'chat'])
    planned = []; original = runner.preview
    monkeypatch.setattr(runner, 'preview', lambda s, *args: planned.append(s) or original(s, *args))
    assert runner.main() == 0
    assert datetime.fromisoformat(planned[0]['episodes'][0]['sends'][0]['at']).strftime('%H:%M') == '08:05'
    store.close()


def test_amendment_seventeen_and_abort_reasons():
    rubric = Path(runner.__file__).with_name('RUBRIC.md').read_text()
    assert '17. **2026-10-14 (runner PR, review round 2)' in rubric
    abort = rubric.split('- **Abort:**')[1].split('\n')[0]
    assert all(reason in abort for reason in ('frozen_state_changed', 'frozen_state_unreadable', 'cleanup_failed'))


def tokyo(e, hour, minute=0, days=0):
    from zoneinfo import ZoneInfo
    day = datetime.fromisoformat(e['sends'][0]['at']).astimezone(ZoneInfo('Asia/Tokyo')).date() + timedelta(days=days)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo('Asia/Tokyo')).timestamp()


def test_cleanup_waits_for_active_hours_without_writing_rows(tmp_path):
    r, c, store, e = started(tmp_path)
    w = store.list_wakes('chat')[0]
    r.clock.wall = tokyo(e, 22, 30)
    before = len(r.rows)
    for _ in range(10):
        assert r.cleanup(e) is False; r.clock.sleep(300)
    assert [x['kind'] for x in r.rows[before:]] == ['setup']  # The archive row only.
    assert not any(x['kind'] in ('send_intent', 'hours_guard') for x in r.rows[before:])
    r.clock.wall = tokyo(e, 8, 0, days=1)
    assert r.cleanup(e) is False
    assert r.has('send_intent', e['id'], 'setup_cancel:'+w.id)
    store.close()


def test_missing_chat_session_counts_as_disabled(tmp_path):
    r, c, store, e = started(tmp_path)
    w = store.list_wakes('chat')[0]
    store.cas_wake(w.id, w.status, status='cancelled')
    r.pulse.session = lambda key: None
    assert r.cleanup(e) is False
    assert r.has('cleanup_verified', e['id']) and r.has('send_intent', e['id'], 'setup_on')
    store.close()


def test_arming_packets_carry_the_owner_reminder_contract():
    s, logs, verdicts = corpus()
    packets = [p for p in scoring.score(s, logs, verdicts)['packets'] if p['id'].startswith('a-')]
    assert packets and all(p['tool_contract'] == scoring.TOOL_CONTRACT for p in packets)
    assert 'owner_requested: true' in scoring.TOOL_CONTRACT and 'not muted' in scoring.TOOL_CONTRACT
    rubric = Path(runner.__file__).with_name('RUBRIC.md').read_text()
    judge = Path(runner.__file__).with_name('JUDGE.md').read_text()
    assert '18. **2026-10-14 (runner PR, review round 2): Judge context.**' in rubric and 'tool_contract' in judge
