"""Synthetic journey evidence only: no Hermes import, rig, DNS or HTTP traffic."""
import copy
import importlib.util
import io
import json
import socket
import signal
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from bench.live.round6 import (CallBudget, JourneyUpstream, ROOT, aggregate,
                               deadline_allows, orchestrate, score_cell, variants, write_score,
                               capture_turn, cleanup_cell, declaration_for, main, merge_verdicts, quiet_intersects)


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
    packets = json.loads((tmp_path / 'packets.json').read_text())
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
    args = SimpleNamespace(out=tmp_path, reps=1, workers=2, tz='Asia/Tokyo', upstream='fake',
                           until=101 if mode == 'deadline' else None)
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
    wake = base['turns'][0]['created'][0]
    messages, idles = [], []
    upstream = SimpleNamespace(rows=[])
    driver = SimpleNamespace(dm=lambda text: SimpleNamespace(message_id=42, raw={'message': {'date': base['sent_at']}}))
    def bots():
        if clock[0] >= 2 and not any(m['id'] == 'receipt' for m in messages):
            messages.append(dict(id='receipt', text='Reminder set.', date=base['sent_at'] + 2))
        return list(messages)
    def snapshot():
        return dict(wakes=[wake] if clock[0] >= 2 else [], session={})
    def owner(text):
        driver.dm(text)
        messages.append(dict(id='interim', text='Setting that now.', date=base['sent_at']))
    def idle(timeout):
        assert timeout == 180
        idles.append(clock[0])
    rig = SimpleNamespace(driver=driver, owner=owner, gateway=SimpleNamespace(wait_idle=idle))
    original_dm = driver.dm
    result = dict(turns=[], snapshots=[])
    turn = capture_turn(rig, 'remind me', result, upstream, snapshot, bots, lambda: None, minutes=10, match='basil')
    assert idles == [0] and clock[0] == 7
    assert driver.dm is original_dm
    assert turn['created'] == [wake]
    assert turn['bot_ids'] == ['interim', 'receipt']
    base.update(turns=[turn], bot_messages=messages + base['bot_messages'])
    assert score_cell(base)['state'] == 'PASS'  # receipt is not a wake delivery
    # A subsequent stop/reply send cannot overtake this settled arming turn.
    rig.owner = lambda text: (driver.dm(text), messages.append(dict(id='stop-ack', text='Stopped', date=base['sent_at'] + 7)))
    stop = capture_turn(rig, 'stop', result, upstream, snapshot, bots, lambda: None, role='stop')
    assert stop['bot_ids'] == ['stop-ack'] and clock[0] == 12


def test_turn_settle_cap_is_void_reason(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr('bench.live.round6.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('bench.live.round6.time.sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    driver = SimpleNamespace(dm=lambda text: SimpleNamespace(message_id=1, raw={'message': {'date': 123}}))
    original_dm = driver.dm
    rig = SimpleNamespace(driver=driver, owner=lambda text: driver.dm(text), gateway=SimpleNamespace(wait_idle=lambda timeout: None))
    with pytest.raises(RuntimeError, match='^turn_not_settled$'):
        capture_turn(rig, 'arm', dict(turns=[], snapshots=[]), SimpleNamespace(rows=[]),
                     lambda: dict(wakes=[]), lambda: [dict(id=str(clock[0]))], lambda: None)
    assert clock[0] == 60 and driver.dm is original_dm


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
    assert summary['repetitions'][0]['reason'] == 'ok'
    final = merge_verdicts(tmp_path, [])
    assert final['cells']['M'] == 'PLUMBING'


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
    arms = [p for p in packets if p['bar'] == 'B3-7' and p['rep'] == 1]
    assert len(arms) == 3
    assert [len(p['history']) for p in arms] == [1, 2, 3]
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
    assert all(p['rep'] == 1 for p in packets)
    assert len({p['id'] for p in packets}) == len(packets)
    assert len([p for p in packets if p['bar'] == 'B3-7']) == 3


def verdict(packet, **changes):
    return dict(id=packet['id'], useful=True, narration=False, overpromise=False, why='Supported by fixture.', **changes)


@pytest.mark.parametrize('bad', ['unknown', 'duplicate', 'boolean', 'empty', 'multiline', 'carriage', 'array'])
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
    else: rows = {}
    with pytest.raises(ValueError):
        merge_verdicts(tmp_path, rows)
    assert not (tmp_path / 'final.json').exists()


@pytest.mark.parametrize('failure', ['overpromise', 'useful', 'narration', 'missing', 'none'])
def test_merge_aggregation(tmp_path, failure):
    scored_fixture(tmp_path, reps=2)
    packets = json.loads((tmp_path / 'packets.json').read_text())
    rows = [verdict(p) for p in packets]
    if failure == 'overpromise':
        next(v for v in rows if 'B3-7' in v['id'])['overpromise'] = True
    elif failure in {'useful', 'narration'}:
        next(v for v in rows if 'B3-8' in v['id'])[failure] = failure == 'narration'
    elif failure == 'missing': rows.pop()
    final = merge_verdicts(tmp_path, rows)
    assert final['cells']['R-rel'] == ('PASS' if failure == 'none' else 'PENDING_JUDGE' if failure == 'missing' else 'FAIL')
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


@pytest.mark.parametrize('mode', ['quiet', 'rerun_deadline', 'rerun_quiet', 'void_twice', 'refused', 'sigterm', 'sigint'])
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
                cell=self.sid, rep=int(self.rep), status='VOID', reason='upstream_unavailable')))
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
    arm = next(p for p in packets if p['bar'] == 'B3-7')
    delivery = next(p for p in packets if p['bar'] == 'B3-8')
    assert arm['tool_contract'] == 'fixture tool contract' and arm['tool_results']
    assert arm['receipts'] == ['arming receipt']
    assert delivery['receipts'] == ['delivery receipt']
    assert 'old receipt' not in json.dumps(delivery)
