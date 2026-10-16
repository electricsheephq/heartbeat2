"""Attempt-2 transport and frozen-state amendments, using synthetic fixtures only."""
import json
import os
import subprocess

import pytest

from bench.soak.client import ClientError, CommandClient
from bench.soak.evidence import hermes_revision
from bench.soak.runner import Runner, epoch
from test_b3_runner import rig


def fail_client(*args, **kwargs):
    raise ClientError('synthetic failure', exit_code=1, detail='lock busy')


def test_finished_turn_survives_poll_past_correlation_deadline(tmp_path, monkeypatch):
    r, c, store, e = rig(tmp_path, 'N')
    r.active = e; start = epoch(e['sends'][0]['at']); r.clock.wall = start
    original_row, original_read = c.row, c.read
    def row(role, text, at, *args, **kwargs):
        original_row(role, text, start + 30 if role == 'assistant' else at, *args, **kwargs)
    def slow_read(after_id, limit):
        r.clock.wall = start + 400
        return original_read(after_id, limit)
    monkeypatch.setattr(c, 'row', row)
    monkeypatch.setattr(c, 'read', slow_read)
    r.send(e, 'request:0', e['sends'][0]['text'], 'request')
    r.poll(); r.turns()
    turns = [row for row in r.rows if row['kind'] == 'turn']
    assert len(turns) == 1
    assert epoch(turns[0]['t']) == start + 400
    assert turns[0]['rows'][-1]['timestamp'] == start + 30
    assert turns[0]['final_text'] == 'Okay.'
    assert not r.has('turn_unresolved', e['id']) and not r.has('void', e['id'])
    assert not r.pending
    store.close()


def test_missing_turn_unresolved_at_five_minutes(tmp_path, monkeypatch):
    r, c, store, e = rig(tmp_path, 'N')
    r.active = e; start = epoch(e['sends'][0]['at']); r.clock.wall = start
    monkeypatch.setattr(c, 'row', lambda *args, **kwargs: None)
    r.send(e, 'request:0', e['sends'][0]['text'], 'request')
    r.clock.wall = start + 299; r.turns()
    assert r.pending and not r.has('turn_unresolved', e['id'])
    assert not r.has('void', e['id'])
    r.clock.wall = start + 300; r.turns()
    unresolved = [row for row in r.rows if row['kind'] == 'turn_unresolved']
    assert len(unresolved) == 1 and epoch(unresolved[0]['t']) == start + 300
    assert [row['reason'] for row in r.rows if row['kind'] == 'void'] == ['turn_unresolved']
    assert not r.pending and not r.has('turn', e['id'])
    store.close()


@pytest.mark.parametrize('kind', ['N', 'M'])
def test_turn_finished_after_deadline_is_not_rescued(tmp_path, monkeypatch, kind):
    r, c, store, e = rig(tmp_path, kind)
    start = epoch(e['sends'][0]['at']); r.clock.wall = start
    original_row = c.row
    def row(role, text, at, *args, **kwargs):
        finished_at = start + 360 if role == 'assistant' and text else at
        original_row(role, text, finished_at, *args, **kwargs)
    monkeypatch.setattr(c, 'row', row)
    r.tick_episode(e)
    r.clock.wall = start + 400; r.turns()
    assert not r.has('turn', e['id'])
    if kind == 'M':
        assert r.pending and not r.has('turn_unresolved', e['id'])
        assert not r.has('void', e['id'])
        r.tick_episode(e)
        assert [row['reason'] for row in r.rows if row['kind'] == 'void'] == ['m_arming_timeout']
    else:
        assert r.has('turn_unresolved', e['id'])
        assert [row['reason'] for row in r.rows if row['kind'] == 'void'] == ['turn_unresolved']
    assert not r.pending
    store.close()


def test_m_request_excluded_from_turn_unresolved_deadline(tmp_path, monkeypatch):
    r, c, store, e = rig(tmp_path, 'M')
    r.active = e; start = epoch(e['sends'][0]['at']); r.clock.wall = start
    monkeypatch.setattr(c, 'row', lambda *args, **kwargs: None)
    r.send(e, 'request:0', e['sends'][0]['text'], 'request')
    for elapsed in (300, 400):
        r.clock.wall = start + elapsed; r.turns()
        assert r.pending and not r.has('turn_unresolved', e['id'])
        assert not r.has('void', e['id'])
    store.close()


def test_single_read_error_logs_diagnostics_without_void(tmp_path, monkeypatch):
    r, c, store, e = rig(tmp_path, 'R-rel')
    r.active = e
    monkeypatch.setattr(c, 'read', fail_client)
    r.poll()
    error = next(row for row in r.rows if row['kind'] == 'client_error')
    assert error['operation'] == 'read'
    assert (error['exit_code'], error['detail']) == (1, 'lock busy')
    assert not r.has('void', e['id'])
    store.close()


def test_read_outage_voids_once_at_five_minutes(tmp_path):
    r, c, store, e = rig(tmp_path, 'R-rel')
    r.active = e; c.failed = 'read'
    start = r.clock.now()
    for elapsed in range(0, 300, 30):
        r.clock.wall = start + elapsed; r.poll()
        assert not r.has('void', e['id'])
    for elapsed in (300, 330, 600):
        r.clock.wall = start + elapsed; r.poll()
    voids = [row for row in r.rows if row['kind'] == 'void']
    assert len(voids) == 1 and voids[0]['reason'] == 'client_outage_5m'
    assert epoch(voids[0]['t']) == start + 300
    store.close()


@pytest.mark.parametrize('blocked_seconds', [100, 300])
def test_blocking_failed_read_counts_toward_outage(tmp_path, monkeypatch, blocked_seconds):
    r, c, store, e = rig(tmp_path, 'R-rel')
    r.active = e; start = r.clock.now()
    def slow_failed_read(*args, **kwargs):
        r.clock.wall += blocked_seconds
        fail_client()
    monkeypatch.setattr(c, 'read', slow_failed_read)
    r.poll()
    voids = [row['reason'] for row in r.rows if row['kind'] == 'void']
    assert voids == (['client_outage_5m'] if blocked_seconds == 300 else [])
    assert r.outage == start
    store.close()


def test_successful_read_resets_outage_clock(tmp_path):
    r, c, store, e = rig(tmp_path, 'R-rel')
    r.active = e; c.failed = 'read'
    start = r.clock.now()
    r.poll()
    r.clock.wall = start + 270; c.failed = False; r.poll()
    assert r.outage is None
    r.clock.wall = start + 300; c.failed = 'read'; r.poll()
    r.clock.wall = start + 570; r.poll()
    assert r.outage == start + 300
    r.clock.wall = start + 600; c.failed = False; r.poll()
    assert r.outage is None and not r.has('void', e['id'])
    store.close()


def test_send_error_still_voids_and_logs_diagnostics(tmp_path, monkeypatch):
    r, c, store, e = rig(tmp_path, 'R-rel')
    r.active = e; r.clock.wall = epoch(e['sends'][0]['at'])
    monkeypatch.setattr(c, 'send', fail_client)
    r.send(e, 'request:0', e['sends'][0]['text'], 'request')
    error = next(row for row in r.rows if row['kind'] == 'client_error')
    assert error['operation'] == 'send'
    assert (error['exit_code'], error['detail']) == (1, 'lock busy')
    assert [row['reason'] for row in r.rows if row['kind'] == 'void'] == ['client_error']
    store.close()


def test_client_default_waits_and_environment_overrides(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, '')
    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setenv('TGCTL_LOCK_TIMEOUT', '10')
    monkeypatch.setenv('SOAK_FIXTURE_ENV', 'inherited')
    assert CommandClient(['fake'], 'peer').read(None, 100) == []
    assert calls[-1][1]['timeout'] == 300
    assert calls[-1][1]['env'] == dict(os.environ, TGCTL_LOCK_TIMEOUT='240')
    client = CommandClient(['fake'], 'peer', timeout=90, env={'TGCTL_LOCK_TIMEOUT': '80'})
    client.read(None, 100)
    assert calls[-1][1]['timeout'] == 90
    assert calls[-1][1]['env'] == dict(os.environ, TGCTL_LOCK_TIMEOUT='80')


@pytest.mark.parametrize('stderr,detail', [
    ('lock busy\nmore', 'lock busy'),
    ('\n  \n  lock busy  \nmore', 'lock busy'),
    ('x' * 500, 'x' * 200),
    ('\n  \n', ''),
])
def test_client_exit_diagnostics_exclude_stdout(monkeypatch, stderr, detail):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw:
                        subprocess.CompletedProcess(a, 1, 'private message fixture', stderr))
    with pytest.raises(ClientError) as caught:
        CommandClient(['fake'], 'peer').read(None, 100)
    assert caught.value.exit_code == 1 and caught.value.detail == detail
    assert 'private message fixture' not in str(caught.value)


@pytest.mark.parametrize('fault,detail', [
    (subprocess.TimeoutExpired('fake', 300, output='private message fixture', stderr='ignored'), 'TimeoutExpired'),
    (OSError('private message fixture'), 'OSError'),
    ('invalid JSON fixture', 'JSONDecodeError'),
    (json.dumps({'text': 'private message fixture'}), 'ValueError'),
])
def test_client_exception_diagnostics(monkeypatch, fault, detail):
    def run(*args, **kwargs):
        if isinstance(fault, Exception): raise fault
        return subprocess.CompletedProcess(args, 0, fault, '')
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(ClientError) as caught:
        CommandClient(['fake'], 'peer').read(None, 100)
    assert caught.value.exit_code is None and caught.value.detail == detail
    assert 'private message fixture' not in str(caught.value)


@pytest.mark.parametrize('fault', [subprocess.TimeoutExpired('git', 30), OSError('synthetic')])
def test_hermes_git_fault_is_unreadable_not_changed(tmp_path, monkeypatch, fault):
    r, c, store, e = rig(tmp_path, 'N')
    r.hermes_source = tmp_path / 'source'
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if r.has('baseline_setup', None): raise fault
        return subprocess.CompletedProcess(argv, 0, 'a' * 40)
    monkeypatch.setattr(subprocess, 'run', run)
    del r.state  # Exercise Runner.state(), rather than the rig's frozen-state fake.
    r.frozen = r.state()
    r.emit('declaration', schedule_sha256=r.schedule['sha256'], frozen=r.frozen)
    assert r.baseline()
    assert r.state() is None
    with pytest.raises(subprocess.SubprocessError):
        hermes_revision(r.hermes_source, r.hermes_version_file)
    start = r.clock.now()
    assert r.run() == 3
    mismatches = [row for row in r.rows if row['kind'] == 'state_mismatch']
    assert len(mismatches) == 1 and mismatches[0]['unreadable'] is True
    aborts = [row for row in r.rows if row['kind'] == 'abort']
    assert [row['reason'] for row in aborts] == ['frozen_state_unreadable']
    assert epoch(aborts[0]['t']) - start >= 60
    assert len(calls) >= 3 and all(kw['timeout'] == 30 for _, kw in calls)
    store.close()


def test_non_git_source_is_stable_and_generator_waits_thirty_seconds(tmp_path, monkeypatch):
    r, c, store, e = rig(tmp_path, 'N')
    r.hermes_source = tmp_path / 'source'
    r.hermes_version_file.write_text(json.dumps({'baseVersion': '1.0', 'commit': 'b' * 40}))
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 1, '', 'not a checkout')
    monkeypatch.setattr(subprocess, 'run', run)
    del r.state
    first = r.state()
    assert first is not None and first['hermes_source_commit'] is None
    assert first == r.state()
    assert r.run() == 0
    assert not r.has('state_mismatch', None)
    assert all(kw['timeout'] == 30 for _, kw in calls)
    store.close()
