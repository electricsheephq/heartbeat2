"""Loopback-only bench tests; no Hermes imports on the unit path."""
import importlib.util
import json
import re
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

from bench.live.adjudicate import merge, packets
from bench.live.score import NARRATION, score_results
from bench.live.upstream import Upstream

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    spec = importlib.util.spec_from_file_location('b2_loopback', ROOT / 'tests/e2e/loopback.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.install(lambda obj, name, value: monkeypatch.setattr(obj, name, value))


@pytest.fixture
def server():
    seen, statuses = [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            seen.append((self.path, self.headers.get('Authorization'),
                         json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            status = statuses.pop(0) if statuses else 200
            body = {'choices': [{'message': {'content': 'done'}}],
                    'usage': {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10}}
            self.send_response(status)
            self.send_header('Retry-After', '0')
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{http.server_port}/v1', seen, statuses
    http.shutdown()
    http.server_close()
    thread.join()


def test_forwarder_and_fault_fallback(server, monkeypatch, tmp_path):
    url, seen, statuses = server
    monkeypatch.setenv('B2_TEST_KEY', 'synthetic-key')
    up = Upstream(url, {'primary': 'up-primary', 'fallback': 'up-secondary'},
                  'B2_TEST_KEY', tmp_path / 'record.jsonl', {'primary': 'primary'})
    up.scenario, up.rep, up.path, up.wake = 'L1', 1, 'P3', True
    body = {'model': 'primary', 'stream': True, 'messages': [{'role': 'user', 'content': 'wake'}]}
    assert up.forward(body)[1] == 503 and not seen
    response, status = up.forward(body | {'model': 'fallback'})
    assert status == 200 and response['usage']['total_tokens'] == 10
    assert seen[0][:2] == ('/v1/chat/completions', 'Bearer synthetic-key')
    assert seen[0][2]['model'] == 'up-secondary' and not seen[0][2]['stream']
    assert body['model'] == 'primary' and body['stream']
    rows = [json.loads(row) for row in up.record_path.read_text().splitlines()]
    assert rows[0]['injected'] and rows[1]['usage']['prompt_tokens'] == 7
    assert all('headers' not in r and 'Authorization' not in json.dumps(r) for r in rows)
    assert rows[1]['scenario'] == 'L1' and rows[1]['path'] == 'P3'
    up.fail_plan = None
    statuses[:] = [503, 200]
    assert up.forward(body)[1] == 200 and len(seen) == 3
    assert seen[-1][2]['model'] == 'up-primary'
    statuses[:] = [429, 429]
    assert up.forward(body)[1] == 429 and up.quota
    assert up.rows[-1]['run_status'] == 'QUOTA'
    assert up.forward(body)[1] == 429 and len(seen) == 5


def test_director_routes_without_importing_hermes(monkeypatch):
    harness, fake = ModuleType('harness'), ModuleType('tests.fakes.fake_llm_provider')
    class Base:
        def __init__(self):
            self._active, self.wake_requests, self.wake_starts = None, [], []
        @staticmethod
        def _text(row):
            return row.get('content', '')
        def __call__(self, record):
            return 'SCRIPTED'
        def auxiliary(self, record):
            return 'AUX'
    @dataclass
    class Raw:
        body: str
        status: int
        content_type: str
    harness.WakeDirector, harness.MARKER = Base, re.compile(r'⟦pulse o:(\S+) a:(\d+)⟧')
    fake.Raw = Raw
    monkeypatch.setitem(sys.modules, 'harness', harness)
    monkeypatch.setitem(sys.modules, 'tests.fakes.fake_llm_provider', fake)
    spec = importlib.util.spec_from_file_location('b2_director', ROOT / 'bench/live/director.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls, response = [], {'choices': [{'message': {'tool_calls': [{'id': 'one'}]}}]}
    def forward(body):
        calls.append(body)
        return response, 200
    director = module.LiveDirector(SimpleNamespace(forward=forward), {'stop': True, 'seed': False})
    def request(user, role='user', old=False):
        messages = ([{'role': 'user', 'content': '⟦pulse o:old a:1⟧'}] if old else [])
        messages.append({'role': 'user', 'content': user})
        if role == 'tool':
            messages.append({'role': 'tool', 'content': 'result'})
        return {'body': {'messages': messages}}
    assert director(request('seed', old=True)) == 'SCRIPTED'
    raw = director(request('⟦pulse o:new a:1⟧'))
    assert json.loads(raw.body) == response and director._active == ('new', 1)
    response['choices'][0]['message'] = {'content': 'final'}
    director(request('steered owner', 'tool', old=True))
    assert director._active is None
    assert director(request('arm', old=True)) == 'SCRIPTED'
    director(request('stop checking in [in:stop]'))
    assert len(calls) == 3 and director.auxiliary({}) == 'AUX'


def definitions():
    return yaml.safe_load((ROOT / 'bench/live/scenarios.yaml').read_text())


def test_scenario_schema_and_bars():
    data = definitions()
    assert set(data['bars']) == {f'B2-{i}' for i in range(1, 6)}
    assert [s['id'] for s in data['scenarios']] == [f'L{i}' for i in range(1, 10)]
    for scenario in data['scenarios']:
        assert scenario['label'] in ('MUST_NOTIFY', 'MUST_SILENT', 'SILENT_OR_NEUTRAL',
                                      'NOTIFY_OR_SILENT_NO_FABRICATION')
        assert scenario['arm'] and scenario['traps'] and 'facts' in scenario
        assert all(set(pair) == {'owner', 'assistant'} for pair in scenario['history'])
    assert data['scenarios'][7]['forward'] == {'stop': True}


def test_read_only_facts(monkeypatch, tmp_path, fake_ctx):
    from bench.live.facts_plugin import register
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    register(fake_ctx)
    lookup = fake_ctx.tools['lookup']['handler']
    assert json.loads(lookup({'topic': 'shipment'})) == {'result': 'no record'}
    (tmp_path / 'facts.json').write_text('{"report": "42 items"}')
    assert json.loads(lookup({'topic': 'report'})) == {'result': '42 items'}
    assert json.loads(lookup({'topic': 'shipment'})) == {'result': 'no record'}


@pytest.mark.parametrize('text,hit', [('I checked and nothing changed', True),
    ('Let me check', True), ('Let me know if you want the full log.', False), ('I will now call lookup', True), ('no changes detected', True),
    ('Tool call: lookup', True), ('Model fallback: primary unavailable', True),
    ('Call the bank at 17:00.', False),
    ('Report completed: 12 checks passed.', False), ('No shipment information yet.', False)])
def test_narration(text, hit):
    assert bool(NARRATION.search(text)) == hit


def test_scoring_files_and_blind_merge(tmp_path):
    results = []
    for scenario in definitions()['scenarios']:
        for rep in range(1, 4):
            delivered = [' '.join(scenario['expect'])] if scenario['label'] == 'MUST_NOTIFY' else []
            result = {'path': 'P1', 'scenario': scenario['id'], 'rep': rep, 'status': 'COMPLETE',
                'delivered': delivered, 'wake_set': [{'creator': 'agent', 'status': 'cancelled'}],
                'history': scenario['history'], 'wake_note': scenario['arm']['note'],
                'tokens': {'total_tokens': 10}, 'upstream_rows': [{'wake': True}]}
            path = tmp_path / f'{scenario["id"]}-{rep}.json'
            path.write_text(json.dumps(result))
            results.append(json.loads(path.read_text()))
    score = score_results(results, definitions())
    assert all(score['paths']['P1']['bars'][f'B2-{i}'] == 'PASS' for i in range(1, 5))
    blind = packets(results)
    assert all(set(p) == {'id', 'history', 'wake_note', 'delivered_text'} for p in blind)
    assert all(len(p['history']) <= 6 for p in blind)
    verdict = {'id': blind[0]['id'], 'stale': False, 'fabricated': False,
        'guilt_or_escalation': False, 'narration': True, 'answers_the_wake': True, 'why': 'Process chatter.'}
    assert merge(score, [verdict])['paths']['P1']['bars']['B2-2'] == 'FAIL'
    with pytest.raises(ValueError):
        merge(score, [verdict | {'stale': 'false'}])
    assert score_results(results[:1], definitions())['paths']['P1']['bars']['B2-1'] == 'PENDING'
    results[0]['delivered'].append('Model fallback: primary unavailable')
    rule_score = score_results(results, definitions())['paths']['P1']['bars']
    assert rule_score['B2-1'] == 'PASS' and rule_score['B2-2'] == 'FAIL'
    results[0]['delivered'] = ['A reminder without its subject']
    assert score_results(results, definitions())['paths']['P1']['bars']['B2-1'] == 'FAIL'
    results[0]['delivered'] = []
    assert score_results(results, definitions())['paths']['P1']['bars']['B2-1'] == 'FAIL'
    for result in results:
        if result['scenario'] == 'L8':
            result['wake_set'][0]['status'] = 'done'
    assert score_results(results, definitions())['paths']['P1']['bars']['B2-4'] == 'FAIL'
    l6 = next(r for r in results if r['scenario'] == 'L6')
    l6['delivered'] = ['It arrives tomorrow.']
    score = score_results(results, definitions())
    assert score['paths']['P1']['bars']['B2-3'] == 'PENDING'
    packet = packets([l6])[0]
    verdict = verdict | {'id': packet['id'], 'narration': False, 'fabricated': True}
    assert merge(score, [verdict])['paths']['P1']['bars']['B2-3'] == 'FAIL'
