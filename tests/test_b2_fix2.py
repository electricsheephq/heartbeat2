"""Review regressions: isolated setup, CLI selection, and round-2 output."""
import copy
import json
import re
import sqlite3
import sys
import tempfile
from types import ModuleType, SimpleNamespace as NS

import pytest
import yaml

from bench.live import run, score
from heartbeat2.clock import SystemClock
from heartbeat2.model import Session, Wake
from heartbeat2.store import Store
from tests.test_b2_round3 import results


def test_n7_prearm_without_chat_turn(monkeypatch, tmp_path):
    spec = next(s for s in run.scenarios()['scenarios'] if s['id'] == 'N7')
    messages, requests, pre_owner = [], [], []
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    monkeypatch.setattr(sys, 'path', sys.path.copy())
    monkeypatch.setattr(sys, 'dont_write_bytecode', True)
    for name in ('HOME', 'HERMES_HOME', 'HERMES_DISABLE_LAZY_INSTALLS', 'PYTHONDONTWRITEBYTECODE'):
        monkeypatch.setenv(name, 'isolated')
    monkeypatch.setenv('B2_SYNTHETIC_KEY', 'loopback-only')
    monkeypatch.setattr(run.signal, 'signal', lambda *a: None)

    class Director:
        def __init__(self, upstream, forward):
            self.upstream, self.scripts = upstream, {}
        def script(self, token, *actions):
            self.scripts[token] = actions
        @staticmethod
        def _text(message):
            return message.get('content', '')

    class Rig:
        def __init__(self, name, pin, *, director, config):
            self.root = tmp_path / 'rig'
            self.root.mkdir()
            self.gateway = NS(home=self.root, hermes_home=self.root)
            self.pulse_db = self.root / 'plugin-data/heartbeat2/pulse.db'
            config['plugins']['entries']['heartbeat2']['allow_gateway_injection'] = True
            (self.root / 'config.yaml').write_text(yaml.safe_dump(config))
            self.director = director
            self.llm = NS(main_requests=lambda: [])
        def start(self, **kwargs):
            with Store(self.pulse_db, SystemClock()) as store:
                store.upsert_session(Session('test-session', 'telegram', 'dm', 'test-chat', 'sid'))
        def wait(self, predicate, what, timeout=90):
            assert predicate(), what
        def rows(self, table):
            with sqlite3.connect(self.pulse_db) as con:
                con.row_factory = sqlite3.Row
                return [dict(r) for r in con.execute(f'SELECT * FROM {table}')]
        def session(self):
            return self.rows('sessions')[0]
        def agent_arm(self, token, trigger, *, note):
            # Model the old helper's extra owner/tool/assistant history.
            messages.extend([{'role': 'user', 'content': f'arm the requested item [in:{token}]'},
                             {'role': 'assistant', 'content': '', 'tool_calls': []},
                             {'role': 'tool', 'content': '{}'},
                             {'role': 'assistant', 'content': f'ARMED-{token}'}])
            with Store(self.pulse_db, SystemClock()) as store:
                return store.create_wake(Wake('test-session', note, trigger['kind'],
                                             trigger_json=trigger)).id
        def owner(self, text, reply=None):
            if '[in:capture]' in text:
                pre_owner.extend(self.rows('wakes'))
            messages.append({'role': 'user', 'content': re.sub(r' \[in:[^]]+\]', '', text)})
            if '[in:capture]' in text:
                requests.append(copy.deepcopy(messages))
                self.director.upstream.rows.append({'request': {'messages': requests[-1]}, 'usage': {}})
            else:
                token = re.search(r'\[in:([^]]+)\]', text)[1]
                messages.append({'role': 'assistant', 'content': self.director.scripts[token][0].text})
        def visible_ids(self):
            return set()
        def new_visible(self, before):
            return ['Understood.']
        def close(self):
            pass

    def module(name, **attrs):
        mod = ModuleType(name)
        mod.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)
    module('harness', PulseRig=Rig)
    module('tests.fakes.fake_llm_provider', FakeLLMServer=object,
           Text=lambda text: NS(text=text), ToolCall=object)
    module('bench.live.director', LiveDirector=Director)
    from bench.live import facts_plugin
    monkeypatch.setattr(facts_plugin, 'install', lambda *a: None)
    monkeypatch.setattr(run, 'Upstream', lambda *a: NS(rows=[], quota=False))
    args = NS(out=tmp_path / 'cells', pin=tmp_path, path='P1', upstream='live',
              upstream_url='unused', key_env='B2_SYNTHETIC_KEY', model='synthetic',
              fallback_model='synthetic', context_tokens=0)
    assert run.cell(args, copy.deepcopy(spec), 1) == 0
    expected = [{'role': role, 'content': pair[key]} for pair in spec['history']
                for role, key in [('user', 'owner'), ('assistant', 'assistant')]]
    assert requests == [expected + [{'role': 'user', 'content': spec['owner']}]]
    assert len(pre_owner) == 1
    wake = pre_owner[0]
    assert wake['creator'] == 'agent' and wake['status'] == 'active'
    assert wake['kind'] == 'every' and json.loads(wake['trigger_json'])['every'] == '1h'
    assert wake['note'] == 'check in on the owner'
    result = json.loads((args.out / 'P1/N7-1.json').read_text())
    assert result['prearm_ids'] == [wake['id']]
    assert wake['created_at'] <= result['sent_at'] < wake['next_due_at']


def invoke_runner(monkeypatch, tmp_path, *options):
    pin = tmp_path / 'pin/tests/fakes'
    pin.mkdir(parents=True)
    (pin / 'fake_llm_provider.py').touch()
    selected = []
    def popen(command, **kwargs):
        selected.append(command[-1].split(':')[0])
        return NS(poll=lambda: 0)
    monkeypatch.setattr(run.subprocess, 'Popen', popen)
    monkeypatch.setattr(run.time, 'sleep', lambda *a: None)
    monkeypatch.setattr(sys, 'argv', ['bench.live.run', '--pin', str(tmp_path / 'pin'),
        '--path', 'P1', '--upstream', 'fake', '--reps', '1', '--out', str(tmp_path / 'out'), *options])
    assert run.main() == 0
    return selected


@pytest.mark.parametrize('selection', ['unknown', 'L1,unknown'])
def test_unknown_scenario_rejected_before_context_filter(monkeypatch, tmp_path, capsys, selection):
    with pytest.raises(SystemExit) as exc:
        invoke_runner(monkeypatch, tmp_path, '--scenarios', selection, '--context-tokens', '100')
    assert exc.value.code == 2
    assert 'unknown scenario' in capsys.readouterr().err
    assert not (tmp_path / 'out').exists()


def test_empty_context_selection_rejected(monkeypatch, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        invoke_runner(monkeypatch, tmp_path, '--scenarios', 'N7,T2', '--context-tokens', '100')
    assert exc.value.code == 2
    assert 'context-eligible scenario' in capsys.readouterr().err
    assert not (tmp_path / 'out').exists()


def test_default_round2_selects_nine_wake_scenarios(monkeypatch, tmp_path):
    assert invoke_runner(monkeypatch, tmp_path) == [f'L{i}' for i in range(1, 10)]


def test_round3_selects_all_25_scenarios(monkeypatch, tmp_path):
    assert invoke_runner(monkeypatch, tmp_path, '--round', '3') == [s['id'] for s in run.scenarios()['scenarios']]


def test_round2_score_bytes_ignore_capture_files(monkeypatch, tmp_path):
    data = results()
    for name, cells in [('wake-only', [r for r in data if r.get('type') != 'capture']),
                        ('mixed', data)]:
        directory = tmp_path / name / 'P1'
        directory.mkdir(parents=True)
        for cell in cells:
            (directory / f'{cell["scenario"]}-{cell["rep"]}.json').write_text(json.dumps(cell))
        monkeypatch.setattr(sys, 'argv', ['bench.live.score', '--results', str(directory.parent),
                                        '--out', str(tmp_path / f'{name}-score')])
        score.main()
    for filename in ('score.json', 'score.md'):
        assert (tmp_path / 'wake-only-score' / filename).read_bytes() == (tmp_path / 'mixed-score' / filename).read_bytes()
    scored = json.loads((tmp_path / 'mixed-score/score.json').read_text())['paths']['P1']
    assert len(scored['cells']) == 27 and scored['bars']['B2-2'] == 'PASS'
    assert scored['bars']['B2-5']['tokens'] == 270
