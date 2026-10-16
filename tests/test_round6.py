"""Synthetic journey evidence only: no Hermes import, rig, DNS or HTTP traffic."""
import copy
import importlib.util
import io
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from bench.live.round6 import (CallBudget, JourneyUpstream, ROOT, aggregate,
                               deadline_allows, orchestrate, score_cell, variants, write_score)


def local_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def evidence(sid):
    sent = 1_800_000_000
    def wake(n, minutes=10):
        return dict(id=f'w_{n}', kind='at', owner_requested=1, next_due_at=sent + minutes * 60,
                    cancel_on_reply=0, status='active', status_reason='', note='basil')
    def turn(w, role='arming', minutes=10):
        return dict(role=role, owner=dict(text='remind me', date=sent, sent_at=sent), minutes=minutes,
                    match='basil', created=[w] if w else [], bot_ids=[], upstream_rows=[],
                    snapshot=dict(wakes=[], session={'enabled': 1}))
    w = wake(1)
    result = dict(cell=sid, rep=1, status='COMPLETE', sent_at=sent, target_due=sent + 900,
                  variant=dict(job='atlas'), turns=[turn(w)],
                  bot_messages=[dict(id='1', text='water the basil', date=w['next_due_at'])],
                  final=dict(at=sent + 80 * 60, occurrences=[]))
    if sid == 'R-clock':
        w['next_due_at'] = sent + 900
        result['bot_messages'][0]['date'] = sent + 900
    elif sid == 'C-reply':
        w.update(cancel_on_reply=1, next_due_at=sent + 1200)
        reply = turn(None, 'reply')
        reply['owner']['date'] += 300
        reply['snapshot']['wakes'] = [w | dict(status='cancelled', status_reason='owner_replied')]
        result.update(turns=[turn(w), reply], bot_messages=[])
    elif sid == 'W':
        w.update(kind='file', trigger_json=json.dumps(dict(path='/fixture/atlas.log', pattern='^DONE$', check_every=1200)))
        result.update(watch_path='/fixture/atlas.log', baseline=dict(at=sent + 1), flipped_at=sent + 600)
        result['bot_messages'][0].update(text='atlas DONE', date=sent + 1800)
    elif sid == 'M':
        wakes = [wake(i, minutes) for i, minutes in enumerate((20, 40, 60))]
        result.update(turns=[turn(w, minutes=m) for w, m in zip(wakes, (20, 40, 60))],
                      third_gate={'session': {'unanswered_count': 2}}, bot_messages=[
                          dict(id=str(i), text='basil', date=w['next_due_at']) for i, w in enumerate(wakes)])
    elif sid == 'S-stop':
        w.update(kind='every', next_due_at=sent + 3600)
        stop = turn(None, 'stop')
        stop['owner']['date'] += 5
        stop['snapshot']['wakes'] = [w | dict(status='cancelled', status_reason='agent')]
        result.update(turns=[turn(w), stop], bot_messages=[])
    return result


@pytest.mark.parametrize('sid', ['R-rel', 'R-clock', 'C-reply', 'W', 'M', 'S-stop'])
def test_passing_cells(sid):
    assert score_cell(evidence(sid))['state'] == 'PASS'


def test_relative_failures():
    result = evidence('R-rel')
    result['bot_messages'][0]['date'] += 181
    assert score_cell(result)['bars']['B3-3']['state'] == 'FAIL'
    result = evidence('R-rel')
    result['turns'][0]['created'][0]['owner_requested'] = 0
    assert score_cell(result)['bars']['B3-1']['reason'] == 'missing_owner_requested'
    result = evidence('R-rel')
    result['bot_messages'].append(dict(id='2', text='basil', date=result['sent_at'] + 1200))
    assert score_cell(result)['state'] == 'FAIL'


def test_clock_slipped():
    result = evidence('R-clock')
    result['sent_at'] = result['target_due'] - 719
    assert score_cell(result) == dict(state='VOID', reason='send_slipped', bars={})


def test_reply_failures():
    result = evidence('C-reply')
    result['bot_messages'] = [dict(id='9', text='basil', date=result['sent_at'] + 1200)]
    assert score_cell(result)['bars']['B3-4']['state'] == 'FAIL'
    result = evidence('C-reply')
    result['turns'][0]['created'][0]['kind'] = 'owner_silent'
    assert score_cell(result)['bars']['B3-1']['reason'] == 'owner_silent_alternative'


def test_watcher_too_slow():
    result = evidence('W')
    result['turns'][0]['created'][0]['trigger_json'] = dict(path=result['watch_path'], pattern='DONE', check_every=1501)
    assert score_cell(result)['bars']['B3-1']['reason'] == 'check_every_too_long'


def test_mute_gate_not_exercised_and_capture_miss():
    result = evidence('M')
    result['third_gate']['session']['unanswered_count'] = 1
    assert score_cell(result)['bars']['B3-6'] == dict(state='NOT_EXERCISED', reason='unanswered_below_2')
    result = evidence('M')
    result['turns'][2]['created'][0]['owner_requested'] = 0
    result['bot_messages'].pop()
    scored = score_cell(result)
    assert scored['bars']['B3-1']['state'] == 'FAIL'
    assert scored['bars']['B3-6']['state'] == 'NOT_EXERCISED'


def test_stop_failures():
    result = evidence('S-stop')
    result['turns'][0]['created'] = []
    assert score_cell(result)['state'] == 'NOT_EXERCISED'
    result = evidence('S-stop')
    result['turns'][1]['snapshot']['wakes'][0]['status_reason'] = 'owner_replied'
    assert score_cell(result)['bars']['B3-5'] == dict(state='NOT_EXERCISED', reason='owner_replied')
    result['bot_messages'] = [dict(id='2', text='checking in', date=result['sent_at'] + 3600)]
    assert score_cell(result)['bars']['B3-5']['state'] == 'FAIL'
    result = evidence('S-stop')
    result['turns'][0]['created'][0]['cancel_on_reply'] = 1
    assert score_cell(result)['bars']['B3-1']['reason'] == 'cancel_on_reply_on_S'
    result = evidence('S-stop')
    result['final']['occurrences'] = [dict(claimed_at=result['sent_at'] + 3600)]
    assert score_cell(result)['state'] == 'FAIL'


def test_stop_requires_due_plus_ten_minutes_of_observation():
    result = evidence('S-stop')
    result['turns'][0]['created'][0]['next_due_at'] = result['sent_at'] + 75 * 60
    assert score_cell(result)['bars']['B3-5'] == dict(state='VOID', reason='observation_incomplete')
    result['final']['at'] = result['sent_at'] + 85 * 60
    assert score_cell(result)['state'] == 'PASS'


@pytest.mark.parametrize('sid', ['C-reply', 'S-stop'])
def test_false_arm_on_reply_or_stop(sid):
    result = evidence(sid)
    result['turns'][1]['created'] = [dict(id='new')]
    assert score_cell(result)['bars']['B3-2']['state'] == 'FAIL'


def test_aggregation_and_deadline():
    assert aggregate(['PASS', 'PASS', 'VOID']) == 'VOID'
    assert not deadline_allows('W', 100, 100 + 45 * 60 + 599)
    assert deadline_allows('W', 100, 100 + 45 * 60 + 600)
    assert deadline_allows('W', 100, None)


def test_locked_call_cap(tmp_path):
    path = tmp_path / 'calls.json'
    def take(_):
        return CallBudget(path, 37).take()
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(take, range(100))) == 37
    assert json.loads(path.read_text()) == dict(count=37, reason='call_cap')


def test_429_no_retry(monkeypatch, tmp_path):
    monkeypatch.setenv('UNIT_FAKE_KEY', 'synthetic')
    upstream = JourneyUpstream('https://example.invalid/v1', 'unit-model', 'UNIT_FAKE_KEY', tmp_path / 'calls.jsonl')
    calls = []
    def open_request(*args, **kwargs):
        calls.append(1)
        raise HTTPError('synthetic', 429, 'quota', {}, io.BytesIO(b'{"error":"quota"}'))
    monkeypatch.setattr(upstream.opener, 'open', open_request)
    monkeypatch.setattr('bench.live.upstream.time.sleep', lambda _: pytest.fail('429 must never sleep'))
    assert upstream.forward(dict(messages=[], model='unit-model'))[1] == 429
    assert upstream.quota and upstream.rows[0]['run_status'] == 'QUOTA' and len(calls) == 1
    upstream.forward(dict(messages=[], model='unit-model'))
    assert len(calls) == 1


def test_seeded_variants():
    assert variants(20261017, 1) == variants(20261017, 1)
    assert variants(20261017, 1) != variants(20261017, 2)


@pytest.mark.parametrize('allow', [False, True])
def test_loopback_allowlist(monkeypatch, allow):
    loopback = local_module('r6_loopback', 'tests/e2e/loopback.py')
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('203.0.113.7', 443))])
    monkeypatch.setattr(socket.socket, 'connect', lambda *a: 'connected')
    loopback.install(monkeypatch.setattr, allow_hosts=('example.invalid',) if allow else ())
    with socket.socket() as sock:
        if allow:
            # The IP is refused before allowlisted DNS has resolved it.
            with pytest.raises(PermissionError):
                sock.connect(('203.0.113.7', 443))
            assert socket.getaddrinfo('example.invalid', 443)[0][4][0] == '203.0.113.7'
            assert sock.connect(('203.0.113.7', 443)) == 'connected'
        else:
            with pytest.raises(PermissionError):
                socket.getaddrinfo('example.invalid', 443)
        with pytest.raises(PermissionError):
            socket.getaddrinfo('other.invalid', 443)
        with pytest.raises(PermissionError):
            sock.connect(('203.0.113.8', 443))


def test_config_replacement_without_hermes():
    # PulseRig imports the pin; exercise its config-only helper in plain units.
    helper = local_module('r6_settings', 'tests/e2e/rig_settings.py')
    original = dict(plugins=dict(entries=dict(heartbeat2=dict(settings={'fast': True}))))
    assert helper.replace_plugin_settings(copy.deepcopy(original), None) == original
    replacement = {'timezone': 'Asia/Tokyo'}
    config = helper.replace_plugin_settings(copy.deepcopy(original), replacement)
    assert config['plugins']['entries']['heartbeat2']['settings'] == replacement
    assert config['plugins']['entries']['heartbeat2']['settings'] is not replacement


def test_score_outputs_include_missing_reps_and_packets(tmp_path):
    declaration = dict(cells=['R-rel'], timezone='Asia/Tokyo', scheduled=[['R-rel', 1], ['R-rel', 2], ['R-rel', 3]])
    (tmp_path / 'declaration.json').write_text(json.dumps(declaration))
    for rep in (1, 2):
        result = evidence('R-rel')
        result['turns'][0]['bot_ids'] = ['ack']
        result['bot_messages'].append(dict(id='ack', text='set', date=result['sent_at']))
        (tmp_path / f'R-rel-{rep}.json').write_text(json.dumps(result))
    summary = write_score(tmp_path)
    assert summary['cells']['R-rel'] == 'NOT_RUN'
    packets = [json.loads(line) for line in (tmp_path / 'packets.jsonl').read_text().splitlines()]
    assert {p['bar'] for p in packets} == {'B3-7', 'B3-8'}
    assert all('tool_contract' in p and 'tool_results' in p for p in packets)
    assert (tmp_path / 'summary.md').is_file()


@pytest.mark.parametrize('mode', ['deadline', 'quota', 'void', 'mixed_quota'])
def test_orchestrator_without_subprocesses(monkeypatch, tmp_path, mode):
    launched, processes = [], []
    class Process:
        def __init__(self, command, **kwargs):
            sid, rep = command[-1].split(':')
            launched.append((sid, rep))
            self.terminated = False
            if mode == 'mixed_quota':
                self.rc = None if sid == 'R-rel' else 3
                # First W is requeued; on its rerun both quota and VOID finish in one reap pass.
                if sid == 'W' and launched.count((sid, rep)) == 2:
                    processes[0].rc = 2
            else:
                self.rc = (3 if launched.count((sid, rep)) == 1 else 0) if mode == 'void' else (2 if sid == 'R-rel' else None)
            (tmp_path / f'{sid}-{rep}.json').write_text(json.dumps(dict(cell=sid, rep=int(rep), status='VOID' if self.rc else 'COMPLETE')))
            processes.append(self)
        def poll(self):
            return self.rc
        def terminate(self):
            self.terminated, self.rc = True, -15
        def wait(self, **kwargs):
            return self.rc
    monkeypatch.setattr('bench.live.round6.subprocess.Popen', Process)
    monkeypatch.setattr('bench.live.round6.time.sleep', lambda _: None)
    monkeypatch.setattr('bench.live.round6.time.time', lambda: 100)
    args = SimpleNamespace(out=tmp_path, reps=1, workers=2, until=101 if mode == 'deadline' else None)
    rc = orchestrate(args, ['--out', str(tmp_path)], ['R-rel', 'W', 'M'])
    if mode == 'deadline':
        assert not launched
        assert json.loads((tmp_path / 'W-1.json').read_text())['reason'] == 'deadline'
    elif mode == 'mixed_quota':
        assert rc == 2
        assert launched == [('R-rel', '1'), ('W', '1'), ('M', '1'), ('W', '1')]
        assert json.loads((tmp_path / 'M-1.json').read_text())['reason'] == 'quota'
    elif mode == 'quota':
        assert rc == 2 and launched == [('R-rel', '1'), ('W', '1')]
        assert processes[1].terminated
        assert json.loads((tmp_path / 'M-1.json').read_text())['reason'] == 'quota'
    else:
        assert rc == 0 and len(launched) == 6
        assert launched[-1] == ('M', '1')
        assert len(list(tmp_path.glob('*-void.json'))) == 3
