"""Synthetic journey evidence only: no Hermes import, rig, DNS or HTTP traffic."""
import copy
import importlib.util
import io
import json
import re
import socket
import signal
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from bench.live.round6 import (CallBudget, JourneyUpstream, ROOT, aggregate, decoded,
                               deadline_allows, orchestrate, score_cell, variants, write_score,
                               capture_turn, cleanup_cell, declaration_for, main, merge_verdicts, quiet_intersects)
from bench.live.score import VERDICT_KEYS


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
    assert summary['cells']['R-rel'] == 'PENDING_JUDGE'
    packets = json.loads((tmp_path / 'packets.json').read_text())
    packet_map = json.loads((tmp_path / 'packets-map.json').read_text())
    assert {p['bar'] for p in packet_map.values()} == {'B3-7', 'B3-8'}
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
            (tmp_path / f'{sid}-{rep}.json').write_text(json.dumps(dict(cell=sid, rep=int(rep), status='VOID' if self.rc else 'COMPLETE',
                                                                        reason='upstream_unavailable' if self.rc else 'ok')))
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
    args = SimpleNamespace(out=tmp_path, reps=1, workers=2, tz='Asia/Tokyo', upstream='fake',
                           until=101 if mode == 'deadline' else None)
    rc = orchestrate(args, ['--out', str(tmp_path)], ['R-rel', 'W', 'M'])
    if mode == 'deadline':
        assert not launched
        assert json.loads((tmp_path / 'W-1.json').read_text())['reason'] == 'deadline'
    elif mode == 'mixed_quota':
        assert rc == 2
        assert launched == [('R-rel', '1'), ('W', '1'), ('M', '1'), ('W', '1')]
        result = json.loads((tmp_path / 'M-1.json').read_text())
        assert result['status'] == 'VOID' and result['reason'] == 'upstream_unavailable;stopped:quota'
    elif mode == 'quota':
        assert rc == 2 and launched == [('R-rel', '1'), ('W', '1')]
        assert processes[1].terminated
        assert json.loads((tmp_path / 'M-1.json').read_text())['reason'] == 'quota'
    else:
        assert rc == 0 and len(launched) == 6
        assert launched[-1] == ('M', '1')
        assert len(list(tmp_path.glob('*-void.json'))) == 3


@pytest.mark.parametrize('start,end,expected', [
    ('2026-10-16T10:00+09:00', '2026-10-16T11:00+09:00', False),
    ('2026-10-16T21:30+09:00', '2026-10-16T22:10+09:00', True),
    ('2026-10-16T07:50+09:00', '2026-10-16T08:30+09:00', True),
    ('2026-10-16T21:30+09:00', '2026-10-17T08:30+09:00', True),
    ('2026-10-16T08:00+09:00', '2026-10-16T22:00+09:00', True),
])
def test_quiet_hours(start, end, expected):
    assert quiet_intersects(datetime.fromisoformat(start).timestamp(), datetime.fromisoformat(end).timestamp(),
                            'Asia/Tokyo') is expected


def test_quiet_hours_read_manifest_and_timezone(monkeypatch, tmp_path):
    (tmp_path / 'plugin.yaml').write_text('config_schema:\n  quiet_hours:\n    default: "12:00-13:00"\n')
    monkeypatch.setattr('bench.live.round6.ROOT', tmp_path)
    start = datetime.fromisoformat('2026-10-16T03:15+00:00').timestamp()
    assert quiet_intersects(start, start + 60, 'Asia/Tokyo')
    assert not quiet_intersects(start, start + 60, 'UTC')


def test_m_separate_delivery_bars():
    result = evidence('M')
    result['bot_messages'][0]['date'] += 200
    bars = score_cell(result)['bars']
    assert bars['B3-3']['state'] == 'FAIL'
    assert bars['B3-6']['state'] == 'PASS'
    result = evidence('M')
    result['bot_messages'][2]['date'] += 181
    bars = score_cell(result)['bars']
    assert bars['B3-3']['state'] == 'PASS'
    assert bars['B3-6']['state'] == 'FAIL'


@pytest.mark.parametrize('index', [0, 1])
@pytest.mark.parametrize('missing', ['due', 'delivery'])
def test_m_missing_due_or_delivery_fails_not_void(index, missing):
    result = evidence('M')
    if missing == 'due':
        result['turns'][index]['created'][0]['next_due_at'] = None
    else:
        result['bot_messages'].pop(index)
    scored = score_cell(result)
    assert scored['state'] == 'FAIL'
    assert scored['bars']['B3-3']['state'] == 'FAIL'
    assert scored['bars']['B3-6']['state'] == 'PASS'


@pytest.mark.parametrize('offset,passes', [(-91, False), (-90, True), (90, True), (91, False), (None, False)])
def test_reply_requires_twenty_minute_due(offset, passes):
    result = evidence('C-reply')
    result['turns'][0]['created'][0]['next_due_at'] = None if offset is None else result['sent_at'] + 1200 + offset
    assert score_cell(result)['bars']['B3-1']['state'] == ('PASS' if passes else 'FAIL')


@pytest.mark.parametrize('status,reason,state', [
    ('VOID', 'rig_config', 'VOID'), ('VOID', 'rig_config;rerun_refused:quiet_hours', 'VOID'),
    ('QUOTA', 'call_cap', 'NOT_RUN'), ('TERMINATED', 'terminated', 'NOT_RUN')])
def test_pre_send_status_reason_is_preserved(status, reason, state):
    assert score_cell(dict(status=status, reason=reason)) == dict(state=state, reason=reason, bars={})


def test_reply_delivery_cutoff():
    result = evidence('C-reply')
    due = result['turns'][0]['created'][0]['next_due_at']
    result['bot_messages'] = [dict(id='late', text='basil', date=due + 301)]
    assert score_cell(result)['bars']['B3-4']['state'] == 'PASS'
    result['bot_messages'][0]['date'] = due + 300
    assert score_cell(result)['bars']['B3-4']['state'] == 'FAIL'


def test_turn_waits_for_idle_and_receipt(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr('bench.live.round6.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('bench.live.round6.time.sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    base = evidence('R-rel')
    monkeypatch.setattr('bench.live.round6.time.time', lambda: base['sent_at'] + clock[0])
    wake = base['turns'][0]['created'][0]
    messages, guards = [], []
    upstream = SimpleNamespace(rows=[])
    def bots():
        if clock[0] >= 2 and not any(m['id'] == 'receipt' for m in messages):
            messages.append(dict(id='receipt', text='Reminder set.', date=base['sent_at'] + 2))
        return list(messages)
    def snapshot():
        return dict(wakes=[wake] if clock[0] >= 2 else [], session={})
    def dm(text):
        messages.append(dict(id='interim', text='Setting that now.', date=base['sent_at']))
        return SimpleNamespace(message_id=42, raw={'message': {'date': base['sent_at']}})
    driver = SimpleNamespace(dm=dm)
    rig = SimpleNamespace(driver=driver, owner=lambda _: pytest.fail('must bypass rig.owner'),
                          gateway=SimpleNamespace(active_agents=lambda: int(clock[0] < 1), alive=lambda: True))
    original_dm = driver.dm
    result = dict(turns=[], snapshots=[])
    turn = capture_turn(rig, 'remind me', result, upstream, snapshot, bots, lambda: guards.append(clock[0]), minutes=10, match='basil')
    assert clock[0] == 7 and .5 in guards  # guards run while the agent is active
    assert driver.dm is original_dm
    assert turn['created'] == [wake]
    assert turn['bot_ids'] == ['interim', 'receipt']
    base.update(turns=[turn], bot_messages=messages + base['bot_messages'])
    assert score_cell(base)['state'] == 'PASS'  # receipt is not a wake delivery
    # A subsequent stop/reply send cannot overtake this settled arming turn.
    def stop_dm(text):
        messages.append(dict(id='stop-ack', text='Stopped', date=base['sent_at'] + 7))
        return SimpleNamespace(message_id=43, raw={'message': {'date': base['sent_at'] + 7}})
    driver.dm = stop_dm
    stop = capture_turn(rig, 'stop', result, upstream, snapshot, bots, lambda: None, role='stop')
    assert stop['bot_ids'] == ['stop-ack'] and clock[0] == 12


def test_turn_settle_cap_is_void_reason(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr('bench.live.round6.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('bench.live.round6.time.sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    sent = []
    def dm(text):
        sent.append(True)
        return SimpleNamespace(message_id=1, raw={'message': {'date': 123}})
    driver = SimpleNamespace(dm=dm)
    original_dm = driver.dm
    rig = SimpleNamespace(driver=driver, gateway=SimpleNamespace(active_agents=lambda: 0, alive=lambda: True))
    with pytest.raises(RuntimeError, match='^turn_not_settled$'):
        capture_turn(rig, 'arm', dict(turns=[], snapshots=[]), SimpleNamespace(rows=[]),
                     lambda: dict(wakes=[]), lambda: [dict(id=str(clock[0]))] if sent else [], lambda: None)
    assert clock[0] == 60 and driver.dm is original_dm


@pytest.mark.parametrize('reply_at,idle_at,reason,elapsed', [
    (45, 50, None, 55), (0, 45, None, 50), (None, 0, 'no_reply', 180),
    (0, 181, 'gateway_not_idle', 180)])
def test_turn_uses_guarded_180_second_budgets(monkeypatch, reply_at, idle_at, reason, elapsed):
    clock, polls = [0.0], []
    monkeypatch.setattr('bench.live.round6.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('bench.live.round6.time.time', lambda: 1000 + clock[0])
    monkeypatch.setattr('bench.live.round6.time.sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    sends = []
    def dm(text):
        sends.append(text)
        return SimpleNamespace(message_id=42, raw={'message': {'date': 1000}})
    def bots():
        return [dict(id='reply', text='Set', date=1000 + reply_at)] if sends and reply_at is not None and clock[0] >= reply_at else []
    rig = SimpleNamespace(driver=SimpleNamespace(dm=dm), owner=lambda _: pytest.fail('30 s harness called'),
                          gateway=SimpleNamespace(active_agents=lambda: int(clock[0] < idle_at), alive=lambda: True))
    result = dict(turns=[], snapshots=[])
    def capture():
        return capture_turn(rig, 'arm', result, SimpleNamespace(rows=[]), lambda: dict(wakes=[]), bots,
                            lambda: polls.append(clock[0]))
    if reason:
        with pytest.raises(RuntimeError, match='^' + reason + '$'):
            capture()
        assert score_cell(dict(status='VOID', reason=reason))['reason'] == reason
    else:
        assert capture()['bot_ids'] == ['reply']
    assert clock[0] == elapsed and len(polls) >= elapsed * 4
    assert sends == ['arm'] and rig.driver.dm is dm
    assert result['turns'][0]['owner']['id'] == 42 and result['sent_at'] == 1000


@pytest.mark.parametrize('sid', ['R-rel', 'R-clock', 'C-reply', 'W', 'M', 'S-stop'])
def test_claimed_delivery_during_turn_is_not_a_reply(monkeypatch, sid, tmp_path):
    result = evidence(sid)
    base = result['sent_at']
    # In M this is the second arming turn, with reminder 1 claimed mid-turn.
    index = 1 if sid == 'M' else 0
    turn = result['turns'][index]
    created = turn['created']
    wake_id = result['turns'][0]['created'][0]['id']
    due = result['turns'][0]['created'][0]['next_due_at']
    clock, sent = [0.0], []
    monkeypatch.setattr('bench.live.round6.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('bench.live.round6.time.time', lambda: due - 2 + clock[0])
    monkeypatch.setattr('bench.live.round6.time.sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    delivery = dict(id='during', text='basil DONE', date=due)
    def bots():
        return [delivery] if sent and clock[0] >= 2 else []
    def snapshot():
        return dict(at=due - 2 + clock[0], wakes=created if clock[0] >= 2 else [], session={},
                    occurrences=[dict(wake_id=wake_id, claimed_at=due)] if clock[0] >= 2 else [])
    def dm(text):
        sent.append(text)
        return SimpleNamespace(message_id=42, raw={'message': {'date': due - 2}})
    captured = dict(turns=result['turns'][:index], snapshots=[])
    rig = SimpleNamespace(driver=SimpleNamespace(dm=dm), gateway=SimpleNamespace(active_agents=lambda: 0, alive=lambda: True))
    measured = capture_turn(rig, 'arm', captured, SimpleNamespace(rows=[]), snapshot, bots, lambda: None)
    assert measured['bot_ids'] == []
    assert measured['deliveries_during_turn'] == [delivery]
    # Keep original timing/capture evidence; use measured attribution to test scoring and packets.
    turn.update(bot_ids=measured['bot_ids'], deliveries_during_turn=measured['deliveries_during_turn'])
    result['bot_messages'] = [delivery, *result['bot_messages'][1:]] if sid not in {'C-reply', 'S-stop'} else [delivery]
    if sid == 'M':
        assert score_cell(result)['bars']['B3-3']['state'] == 'PASS'
    elif sid == 'C-reply':
        assert score_cell(result)['bars']['B3-4']['state'] == 'FAIL'
    elif sid == 'S-stop':
        assert score_cell(result)['bars']['B3-5']['state'] == 'FAIL'
    (tmp_path / 'declaration.json').write_text(json.dumps(dict(cells=[sid], scheduled=[[sid, 1]])))
    (tmp_path / f'{sid}-1.json').write_text(json.dumps(result))
    write_score(tmp_path)
    packets = json.loads((tmp_path / 'packets.json').read_text())
    packet_map = json.loads((tmp_path / 'packets-map.json').read_text())
    assert any(packet_map[p['id']]['bar'] == 'B3-8' and p['delivered_text'] == delivery['text'] for p in packets)
    assert all(p['delivered_text'] != delivery['text'] for p in packets if packet_map[p['id']]['bar'] == 'B3-7')


def scored_fixture(tmp_path, sid='R-rel', reps=1, upstream='live.invalid'):
    declaration = dict(cells=[sid], tz='Asia/Tokyo', scheduled=[[sid, r] for r in range(1, reps + 1)],
                       upstream=upstream, model='synthetic', until=123, seed=1, reps=reps, max_calls=4,
                       pin='fixture-pin', pin_commit='a' * 40, commit='b' * 40, dirty=True)
    (tmp_path / 'declaration.json').write_text(json.dumps(declaration))
    for rep in range(1, reps + 1):
        result = evidence(sid)
        result.update(rep=rep, upstream=upstream)
        for index, turn in enumerate(result['turns']):
            turn['owner']['text'] = f'turn {index}'
            turn['bot_ids'] = [f'ack-{index}', f'receipt-{index}']
            result['bot_messages'].extend([dict(id=turn['bot_ids'][0], text='Interim', date=result['sent_at']),
                                           dict(id=turn['bot_ids'][1], text='Receipt', date=result['sent_at'] + 1)])
        (tmp_path / f'{sid}-{rep}.json').write_text(json.dumps(result))
    return write_score(tmp_path)


@pytest.mark.parametrize('fake_source', ['declaration', 'result'])
def test_fake_summary_is_plumbing(tmp_path, fake_source):
    scored_fixture(tmp_path, 'M')
    path = tmp_path / ('declaration.json' if fake_source == 'declaration' else 'M-1.json')
    data = json.loads(path.read_text())
    data['upstream'] = 'fake'
    path.write_text(json.dumps(data))
    summary = write_score(tmp_path)
    assert set(summary['cells'].values()) == {'PLUMBING'}
    assert (tmp_path / 'summary.md').read_text().splitlines()[0] == 'FAKE UPSTREAM: plumbing check only; no claim.'
    assert summary['claim_class'] and summary['labels']['S-stop'] == 'stop before first tick'
    assert summary['declaration']['dirty']
    assert summary['repetitions'][0]['reason'] == 'missing_verdict'
    final = merge_verdicts(tmp_path, [])
    assert final['cells']['M'] == 'PLUMBING'
    for name in ('summary', 'final'):
        written = json.loads((tmp_path / f'{name}.json').read_text())
        rep = written['repetitions'][0]
        assert rep['state'] == 'PLUMBING' and rep['mechanical_state'] == 'PASS'
        assert all(b['state'] == 'PLUMBING' for b in rep['bars'].values())
        assert not re.search(r'"state"\s*:\s*"PASS"', (tmp_path / f'{name}.json').read_text())
        assert ': PASS (' not in (tmp_path / f'{name}.md').read_text()
    packets = json.loads((tmp_path / 'packets.json').read_text())
    final = merge_verdicts(tmp_path, [verdict(p) for p in packets])
    assert final['repetitions'][0]['state'] == 'PLUMBING'


@pytest.mark.parametrize('missing', ['sent_at', 'turns', 'final', 'snapshot'])
def test_partial_results_never_crash_scoring(tmp_path, missing):
    scored_fixture(tmp_path)
    path = tmp_path / 'R-rel-1.json'
    result = json.loads(path.read_text())
    if missing == 'snapshot':
        del result['turns'][0]['snapshot']
    else:
        del result[missing]
    path.write_text(json.dumps(result))
    summary = write_score(tmp_path)
    assert summary['repetitions'][0]['state'] == 'VOID'
    assert summary['repetitions'][0]['reason'] == 'incomplete_evidence'
    assert json.loads((tmp_path / 'packets.json').read_text()) == []


def test_packets_complete_only_unique_history_and_text(tmp_path):
    scored_fixture(tmp_path, 'M', reps=2)
    packets = json.loads((tmp_path / 'packets.json').read_text())
    packet_map = json.loads((tmp_path / 'packets-map.json').read_text())
    arms = [p for p in packets if packet_map[p['id']]['bar'] == 'B3-7' and packet_map[p['id']]['rep'] == 1]
    assert len(arms) == 3
    assert [len(p['history']) for p in arms] == [2, 4, 6]
    assert arms[-1]['history'] == [row for i in range(3) for row in (
        dict(role='user', content=f'turn {i}'), dict(role='assistant', content='Interim\nReceipt'))]
    assert all(not {'cell', 'rep', 'bar'} & p.keys() for p in packets)
    assert all(re.fullmatch('[0-9a-f]{64}', p['id']) for p in packets)
    assert set(packet_map) == {p['id'] for p in packets}
    assert all(set(p) == {'cell', 'rep', 'bar', 'turn'} for p in packet_map.values())
    write_score(tmp_path)
    assert packets == json.loads((tmp_path / 'packets.json').read_text())  # stable ids
    assert all(p['delivered_text'] == 'Interim\nReceipt' for p in arms)
    assert all(isinstance(v, str) for p in packets for v in p['evidence'])
    result_path = tmp_path / 'M-2.json'
    result = json.loads(result_path.read_text())
    result['status'] = 'VOID'
    result_path.write_text(json.dumps(result))
    # Missing replies still create distinct arming packet ids.
    result_path = tmp_path / 'M-1.json'
    result = json.loads(result_path.read_text())
    for turn in result['turns']:
        turn['bot_ids'] = []
    result_path.write_text(json.dumps(result))
    write_score(tmp_path)
    packets = json.loads((tmp_path / 'packets.json').read_text())
    packet_map = json.loads((tmp_path / 'packets-map.json').read_text())
    assert all(p['rep'] == 1 for p in packet_map.values())
    assert len({p['id'] for p in packets}) == len(packets)
    assert len([p for p in packet_map.values() if p['bar'] == 'B3-7']) == 3


def verdict(packet, **changes):
    row = dict(id=packet['id'], **{k: k == 'answers_the_wake' for k in VERDICT_KEYS},
               overpromise=False, why='Supported by fixture.')
    return row | changes


@pytest.mark.parametrize('bad', ['unknown', 'duplicate', 'boolean', 'empty', 'multiline', 'carriage', 'array',
                                'unhashable_list', 'unhashable_dict', 'extra', 'missing_key', 'old_keys'])
def test_merge_rejects_invalid_verdicts(tmp_path, bad):
    scored_fixture(tmp_path)
    packet = json.loads((tmp_path / 'packets.json').read_text())[0]
    rows = [verdict(packet)]
    if bad == 'unknown': rows[0]['id'] = 'unknown'
    elif bad == 'duplicate': rows *= 2
    elif bad == 'boolean': rows[0]['overpromise'] = 0
    elif bad == 'empty': rows[0]['why'] = ' '
    elif bad == 'multiline': rows[0]['why'] = 'line\nline'
    elif bad == 'carriage': rows[0]['why'] = 'line\rline'
    elif bad == 'unhashable_list': rows[0]['id'] = ['x']
    elif bad == 'unhashable_dict': rows[0]['id'] = {'x': 1}
    elif bad == 'extra': rows[0]['useful'] = True
    elif bad == 'missing_key': del rows[0]['stale']
    elif bad == 'old_keys': rows = [dict(id=packet['id'], useful=True, narration=False, overpromise=False, why='old')]
    else: rows = {}
    with pytest.raises(ValueError):
        merge_verdicts(tmp_path, rows)
    assert not (tmp_path / 'final.json').exists()
    path = tmp_path / 'verdicts.json'
    path.write_text(json.dumps(rows))
    with pytest.raises(SystemExit) as exc:
        main(['merge', '--out', str(tmp_path), '--verdicts', str(path)])
    assert exc.value.code == 2


@pytest.mark.parametrize('bar,key', [('B3-7', 'overpromise'), ('B3-7', 'fabricated'),
    *[('B3-8', k) for k in VERDICT_KEYS], ('B3-8', 'missing'), ('B3-8', 'none')])
def test_merge_aggregation(tmp_path, bar, key):
    scored_fixture(tmp_path, reps=2)
    packets = json.loads((tmp_path / 'packets.json').read_text())
    packet_map = json.loads((tmp_path / 'packets-map.json').read_text())
    rows = [verdict(p) for p in packets]
    if key == 'missing': rows.pop()
    elif key != 'none':
        next(v for v in rows if packet_map[v['id']]['bar'] == bar)[key] = key != 'answers_the_wake'
    final = merge_verdicts(tmp_path, rows)
    assert final['cells']['R-rel'] == ('PASS' if key == 'none' else 'PENDING_JUDGE' if key == 'missing' else 'FAIL')
    assert (tmp_path / 'final.md').is_file()
    assert aggregate(['VOID', 'PENDING_JUDGE', 'FAIL']) == 'FAIL'
    assert aggregate(['VOID', 'PENDING_JUDGE']) == 'PENDING_JUDGE'


def test_merge_preserves_mechanical_failure_and_cli(tmp_path):
    scored_fixture(tmp_path)
    result = json.loads((tmp_path / 'R-rel-1.json').read_text())
    result['bot_messages'][0]['date'] += 181
    (tmp_path / 'R-rel-1.json').write_text(json.dumps(result))
    write_score(tmp_path)
    packets = json.loads((tmp_path / 'packets.json').read_text())
    verdicts = tmp_path / 'verdicts.json'
    verdicts.write_text(json.dumps([verdict(p) for p in packets]))
    assert main(['merge', '--out', str(tmp_path), '--verdicts', str(verdicts)]) == 0
    assert json.loads((tmp_path / 'final.json').read_text())['cells']['R-rel'] == 'FAIL'


def test_declaration_provenance(monkeypatch, tmp_path):
    args = SimpleNamespace(pin=tmp_path / 'pin', upstream=None, upstream_url='https://user:secret@example.invalid/v1?key=secret',
                           model='unit', tz='UTC', until=42, seed=7, reps=2, max_calls=10)
    args.pin.mkdir()
    calls = []
    def git(command, **kwargs):
        calls.append(command)
        return ' M bench/live/round6.py\n' if command[-1] == '--porcelain' else 'c' * 40 + '\n'
    monkeypatch.setattr('bench.live.round6.subprocess.check_output', git)
    declaration = declaration_for(args, ['M'])
    assert declaration['upstream'] == 'example.invalid' and 'secret' not in json.dumps(declaration)
    assert declaration['dirty'] is True and declaration['pin_commit'] is None
    Path(str(args.pin) + '.COMMIT').write_text('d' * 40 + '\n')
    assert declaration_for(args, ['M'])['pin_commit'] == 'd' * 40
    Path(str(args.pin) + '.COMMIT').unlink()
    (args.pin / '.git').write_text('gitdir: fixture')
    assert declaration_for(args, ['M'])['pin_commit'] == 'c' * 40
    assert any(str(args.pin) in c for c in calls)


@pytest.mark.parametrize('old', ['calls.json', 'R-rel-1.json'])
def test_run_requires_fresh_out(tmp_path, old, capsys):
    pin = tmp_path / 'pin'
    helper = pin / 'tests/fakes/fake_llm_provider.py'
    helper.parent.mkdir(parents=True)
    helper.write_text('# fixture only\n')
    out = tmp_path / 'out'
    out.mkdir()
    (out / old).write_text('{}')
    with pytest.raises(SystemExit) as exc:
        main(['--pin', str(pin), '--out', str(out), '--upstream', 'fake'])
    assert exc.value.code == 2 and 'fresh' in capsys.readouterr().err
    assert not (out / 'declaration.json').exists()


def test_cleanup_always_closes_and_writes_after_signal_ignore(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr('bench.live.round6.signal.signal', lambda sig, action: events.append((sig, action)))
    result = dict(status='TERMINATED', reason='terminated')
    rig = SimpleNamespace(close=lambda: events.append('closed'))
    fake = SimpleNamespace(stop=lambda: events.append('fake_stopped'))
    def broken():
        raise RuntimeError('synthetic capture failure')
    path = tmp_path / 'result.json'
    cleanup_cell(path, result, rig, fake, SimpleNamespace(rows=[]), broken, broken)
    assert events[:2] == [(signal.SIGTERM, signal.SIG_IGN), (signal.SIGINT, signal.SIG_IGN)]
    assert events[2:] == ['closed', 'fake_stopped']
    assert score_cell(json.loads(path.read_text())) == dict(state='NOT_RUN', reason='terminated', bars={})


@pytest.mark.parametrize('mode', ['quiet', 'rerun_deadline', 'rerun_quiet', 'void_twice', 'void_model', 'refused', 'sigterm', 'sigint'])
def test_orchestrator_final_states(monkeypatch, tmp_path, mode):
    launched, handlers, waits = [], {}, []
    now = [datetime.fromisoformat('2026-10-16T10:00+09:00').timestamp()]
    args = SimpleNamespace(out=tmp_path, reps=1, workers=2, until=None, tz='Asia/Tokyo', upstream='fake')
    monkeypatch.setattr('bench.live.round6.time.time', lambda: now[0])
    monkeypatch.setattr('bench.live.round6.time.sleep', lambda _: None)
    def handler(sig, action):
        previous = handlers.get(sig)
        handlers[sig] = action
        return previous
    monkeypatch.setattr('bench.live.round6.signal.signal', handler)
    if mode == 'quiet':
        now[0] = datetime.fromisoformat('2026-10-16T21:40+09:00').timestamp()
    class Process:
        def __init__(self, command, **kwargs):
            self.sid, self.rep = command[-1].split(':')
            launched.append(self)
            self.rc = None
            self.terminated = False
            self.emitted = False
            (tmp_path / f'{self.sid}-{self.rep}.json').write_text(json.dumps(dict(
                cell=self.sid, rep=int(self.rep), status='VOID', reason='no_reply' if mode == 'void_model' else 'upstream_unavailable')))
        def poll(self):
            if not self.emitted and not self.terminated:
                self.emitted = True
                if mode.startswith('sig'):
                    handlers[signal.SIGTERM if mode == 'sigterm' else signal.SIGINT](0, None)
                elif mode == 'refused':
                    self.rc = 4
                else:
                    self.rc = 3
                    if mode == 'rerun_deadline': args.until = now[0]
                    if mode == 'rerun_quiet': now[0] = datetime.fromisoformat('2026-10-16T21:40+09:00').timestamp()
            return self.rc
        def terminate(self):
            self.terminated, self.rc = True, -15
        def wait(self, timeout):
            waits.append(timeout)
            return self.rc
    monkeypatch.setattr('bench.live.round6.subprocess.Popen', Process)
    rc = orchestrate(args, [], ['R-rel', 'W', 'M'])
    results = [json.loads((tmp_path / f'{sid}-1.json').read_text()) for sid in ('R-rel', 'W', 'M')]
    if mode == 'quiet':
        assert rc == 2 and not launched and all(r['reason'] == 'quiet_hours' for r in results)
    elif mode.startswith('rerun_'):
        assert rc == 3
        assert results[0]['status'] == 'VOID'
        assert results[0]['reason'] == 'upstream_unavailable;rerun_refused:' + ('deadline' if mode.endswith('deadline') else 'quiet_hours')
        assert (tmp_path / 'R-rel-1-void.json').exists()
    elif mode == 'void_twice':
        assert rc == 3 and len(launched) == 6 and all(r['status'] == 'VOID' for r in results)
    elif mode == 'void_model':
        # A model-behaviour VOID is final: rerunning it would resample the model.
        assert rc == 3 and len(launched) == 3 and not list(tmp_path.glob('*-void.json'))
        assert all(r['status'] == 'VOID' and r['reason'] == 'no_reply' for r in results)
    elif mode == 'refused':
        assert rc == 2 and len(launched) == 2
        assert all(r['status'] == 'NOT_RUN' and r['reason'] == 'refused' for r in results)
    else:
        assert rc == 2 and len(launched) == 2 and all(p.terminated for p in launched)
        assert all(0 <= t <= 60 for t in waits)
        assert all(r['status'] == 'NOT_RUN' and r['reason'] == 'terminated' for r in results)


def test_cell_pin_refusal_exits_four_without_rig(monkeypatch, tmp_path):
    import sys
    import tempfile
    from bench.live.round6 import cell
    monkeypatch.setitem(sys.modules, 'hermes_cli', SimpleNamespace(__file__=str(tmp_path / 'outside/__init__.py')))
    monkeypatch.setattr(sys, 'path', list(sys.path))
    monkeypatch.setattr(sys, 'dont_write_bytecode', True)
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    for key in ('HOME', 'HERMES_HOME', 'HERMES_TIMEZONE', 'HERMES_DISABLE_LAZY_INSTALLS', 'PYTHONDONTWRITEBYTECODE'):
        monkeypatch.setenv(key, 'fixture')
    assert cell(SimpleNamespace(out=tmp_path, pin=tmp_path / 'pin', tz='UTC'), 'R-rel', 1) == 4


@pytest.mark.parametrize('reason', ['quota', 'terminated'])
def test_shutdown_preserves_queued_void_rerun(monkeypatch, tmp_path, reason):
    calls, handlers = [], {}
    def signal_handler(sig, action):
        previous = handlers.get(sig)
        handlers[sig] = action
        return previous
    monkeypatch.setattr('bench.live.round6.signal.signal', signal_handler)
    monkeypatch.setattr('bench.live.round6.time.sleep', lambda _: None)
    monkeypatch.setattr('bench.live.round6.time.time', lambda: datetime.fromisoformat('2026-10-16T10:00+09:00').timestamp())
    class Process:
        def __init__(self, command, **kwargs):
            self.sid, rep = command[-1].split(':')
            calls.append(self.sid)
            self.rc, self.polls = (3 if self.sid == 'W' else None), 0
            self.path = tmp_path / f'{self.sid}-{rep}.json'
            self.path.write_text(json.dumps(dict(cell=self.sid, rep=int(rep),
                status='VOID' if self.sid == 'W' else 'PENDING', reason='upstream_unavailable')))
        def poll(self):
            self.polls += 1
            if self.sid == 'R-rel' and self.polls == 2:
                if reason == 'quota':
                    self.rc = 2
                    self.path.write_text(json.dumps(dict(cell=self.sid, rep=1, status='QUOTA', reason='429')))
                else:
                    handlers[signal.SIGTERM](0, None)
            return self.rc
        def terminate(self): self.rc = -15
        def wait(self, timeout=None): return self.rc
    monkeypatch.setattr('bench.live.round6.subprocess.Popen', Process)
    args = SimpleNamespace(out=tmp_path, reps=1, workers=2, tz='Asia/Tokyo', upstream='fake', until=None)
    assert orchestrate(args, [], ['R-rel', 'W', 'M']) == 2
    result = json.loads((tmp_path / 'W-1.json').read_text())
    assert calls.count('W') == 1
    assert result['status'] == 'VOID' and result['reason'] == 'upstream_unavailable;stopped:' + reason
    assert score_cell(result)['reason'] == result['reason']
    assert json.loads((tmp_path / 'W-1-void.json').read_text())['reason'] == 'upstream_unavailable'


def test_packets_retain_contract_receipts_and_occurrence_scope(tmp_path):
    scored_fixture(tmp_path)
    path = tmp_path / 'R-rel-1.json'
    result = json.loads(path.read_text())
    sent = result['sent_at']
    def row(wake, occurrence, at, owner, receipt):
        return dict(wake=wake, occurrence=occurrence, started_at=at, request=dict(
            tools=[dict(type='function', function=dict(name='pulse', description='fixture tool contract', parameters={}))],
            messages=[dict(role='user', content=owner),
                      dict(role='assistant', tool_calls=[dict(id='call', function=dict(name='pulse', arguments='{}'))]),
                      dict(role='tool', tool_call_id='call', content=json.dumps(dict(receipt=receipt)))]))
    result['turns'][0]['upstream_rows'] = [row(False, None, sent, 'turn 0', 'arming receipt')]
    result['upstream_rows'] = [row(True, 'old:1', sent + 300, 'wake', 'old receipt'),
                               row(True, 'new:1', sent + 598, 'wake', 'delivery receipt'),
                               row(True, 'new:1', sent + 599, 'wake', 'delivery receipt')]
    path.write_text(json.dumps(result))
    write_score(tmp_path)
    packets = json.loads((tmp_path / 'packets.json').read_text())
    packet_map = json.loads((tmp_path / 'packets-map.json').read_text())
    arm = next(p for p in packets if packet_map[p['id']]['bar'] == 'B3-7')
    delivery = next(p for p in packets if packet_map[p['id']]['bar'] == 'B3-8')
    assert arm['tool_contract'] == 'fixture tool contract' and arm['tool_results']
    assert arm['receipts'] == ['arming receipt']
    assert delivery['receipts'] == ['delivery receipt']
    assert 'old receipt' not in json.dumps(delivery)


def test_score_marks_unjudged_bars_pending_until_merge(tmp_path):
    summary = scored_fixture(tmp_path)
    rep = summary['repetitions'][0]
    assert summary['cells']['R-rel'] == 'PENDING_JUDGE' and rep['state'] == 'PENDING_JUDGE'
    assert rep['mechanical_state'] == 'PASS' and rep['reason'] == 'missing_verdict'
    assert {rep['bars'][b]['state'] for b in ('B3-7', 'B3-8')} == {'PENDING_JUDGE'}
    assert not re.search(r': PASS \(', (tmp_path / 'summary.md').read_text())
    packets = json.loads((tmp_path / 'packets.json').read_text())
    assert merge_verdicts(tmp_path, [verdict(p) for p in packets])['cells']['R-rel'] == 'PASS'


def test_void_reps_produce_no_packets(tmp_path):
    scored_fixture(tmp_path, 'R-clock')
    path = tmp_path / 'R-clock-1.json'
    result = json.loads(path.read_text())
    result['sent_at'] = result['target_due'] - 700
    path.write_text(json.dumps(result))
    summary = write_score(tmp_path)
    assert summary['cells']['R-clock'] == 'VOID' and summary['cell_reasons']['R-clock'] == 'send_slipped'
    assert json.loads((tmp_path / 'packets.json').read_text()) == []


def test_m_delivery_packets_name_only_the_firing_wake(tmp_path):
    scored_fixture(tmp_path, 'M')
    path = tmp_path / 'M-1.json'
    result = json.loads(path.read_text())
    wakes = [t['created'][0] for t in result['turns']]
    result['upstream_rows'] = [dict(wake=True, occurrence=f'{w["id"]}/due:1', started_at=w['next_due_at'] - 5,
                                    request=dict(tools=[], messages=[dict(role='user', content='wake')]))
                               for w in wakes]
    path.write_text(json.dumps(result))
    write_score(tmp_path)
    packets = json.loads((tmp_path / 'packets.json').read_text())
    packet_map = json.loads((tmp_path / 'packets-map.json').read_text())
    deliveries = {packet_map[p['id']]['turn']: p for p in packets if packet_map[p['id']]['bar'] == 'B3-8'}
    assert sorted(deliveries) == ['0', '1', '2']
    for index, wake in enumerate(wakes):
        packet = deliveries[str(index)]
        assert packet['wake_note'] == f'turn {index}'
        assert [json.loads(e)['due'] for e in packet['evidence']] == [wake['next_due_at']]


@pytest.mark.parametrize('ended', ['wake', 'session'])
def test_stop_requires_a_live_wake_before_the_stop_turn(ended):
    result = evidence('S-stop')
    if ended == 'wake':
        result['turns'][0]['created'][0].update(status='cancelled', status_reason='agent')
    else:
        result['turns'][0]['snapshot']['session']['enabled'] = 0
    assert score_cell(result)['bars']['B3-5'] == dict(state='FAIL', reason='not_live_before_stop')


def test_duplicate_cells_are_refused(tmp_path, capsys):
    pin = tmp_path / 'pin'
    helper = pin / 'tests/fakes/fake_llm_provider.py'
    helper.parent.mkdir(parents=True)
    helper.write_text('# fixture only\n')
    with pytest.raises(SystemExit) as exc:
        main(['--pin', str(pin), '--out', str(tmp_path / 'out'), '--upstream', 'fake', '--cells', 'R-rel,R-rel'])
    assert exc.value.code == 2 and 'duplicate' in capsys.readouterr().err


def test_stopped_active_rerun_keeps_first_void(monkeypatch, tmp_path):
    handlers, launched = {}, []
    def signal_handler(sig, action):
        previous = handlers.get(sig)
        handlers[sig] = action
        return previous
    monkeypatch.setattr('bench.live.round6.signal.signal', signal_handler)
    monkeypatch.setattr('bench.live.round6.time.sleep', lambda _: None)
    monkeypatch.setattr('bench.live.round6.time.time', lambda: datetime.fromisoformat('2026-10-16T10:00+09:00').timestamp())
    class Process:
        def __init__(self, command, **kwargs):
            launched.append(self)
            self.path = tmp_path / 'W-1.json'
            self.rerun = len(launched) == 2
            self.rc = None if self.rerun else 3
            if not self.rerun:
                self.path.write_text(json.dumps(dict(cell='W', rep=1, status='VOID', reason='upstream_unavailable')))
        def poll(self):
            if self.rerun and self.rc is None:
                handlers[signal.SIGTERM](0, None)
            return self.rc
        def terminate(self):
            # The terminated cell writes its own result, which must not replace the first attempt.
            self.path.write_text(json.dumps(dict(cell='W', rep=1, status='TERMINATED', reason='terminated')))
            self.rc = -15
        def wait(self, timeout=None): return self.rc
    monkeypatch.setattr('bench.live.round6.subprocess.Popen', Process)
    args = SimpleNamespace(out=tmp_path, reps=1, workers=1, tz='Asia/Tokyo', upstream='fake', until=None)
    assert orchestrate(args, [], ['W']) == 2
    assert len(launched) == 2
    result = json.loads((tmp_path / 'W-1.json').read_text())
    assert result['status'] == 'VOID' and result['reason'] == 'upstream_unavailable;stopped:terminated'


@pytest.mark.parametrize('stored,expected', [('null', {}), (None, {}), ('[]', {}), ('{"offset": 7}', {'offset': 7}),
                                             ({'offset': 7}, {'offset': 7})])
def test_decoded_tolerates_stored_null(stored, expected):
    # A new file watcher's probe state is stored as 'null' until its first probe.
    assert decoded(stored) == expected
    assert 'offset' in decoded(stored) or not expected


def test_watcher_with_null_trigger_scores_not_crashes():
    result = evidence('W')
    result['turns'][0]['created'][0]['trigger_json'] = 'null'
    assert score_cell(result)['bars']['B3-1']['state'] == 'FAIL'
