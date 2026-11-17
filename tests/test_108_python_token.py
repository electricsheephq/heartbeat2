"""#108: `{python}` in a command watcher stands for the Python running Hermes, resolved at each check."""
from dataclasses import replace
import subprocess
import sys

import pytest

from heartbeat2 import probes
from heartbeat2.model import Wake
from heartbeat2.policy import Policy
from test_engine import engine


def wake(argv, **trigger):
    return Wake('agent:telegram:dm:unit', 'Check trip', 'command', trigger_json={'command': argv, **trigger},
                probe_state_json=None, next_due_at=100)


def test_entry_matches_the_token_and_its_script_only(tmp_path):
    script = str(tmp_path/'check.py')
    policy = Policy(probe_commands=f'{{python}} {script}, git status')
    assert policy.command_allowed(['{python}', script, '--trip', 'A1'])
    assert not policy.command_allowed(['{python}', str(tmp_path/'other.py')])
    assert not policy.command_allowed([sys.executable, script])
    assert not policy.command_allowed(['{python}', '-c', 'print(1)'])
    assert policy.command_allowed(['git', 'status'])


@pytest.mark.parametrize('entry', ['{python} -I', '{python} -X dev', '{python} -m mod', '{python} -u'])
def test_a_token_entry_must_name_its_script_next(entry, tmp_path):
    script = str(tmp_path/'any.py')
    policy = Policy(probe_commands=f'{entry}, git status')
    argv = ['{python}', *entry.split()[1:], script]
    assert not policy.command_allowed(argv)
    assert not policy.command_allowed(['{python}', *entry.split()[1:]])
    assert policy.command_allowed(['git', 'status'])


def test_a_bare_token_entry_allows_nothing(tmp_path):
    policy = Policy(probe_commands='{python}, git status')
    assert not policy.command_allowed(['{python}', str(tmp_path/'check.py')])
    assert not policy.command_allowed(['{python}'])
    assert policy.command_allowed(['git', 'status'])


def test_check_runs_the_script_with_the_python_running_hermes(tmp_path):
    script = tmp_path/'check.py'
    script.write_text('import sys\nprint(sys.executable)\n')
    w = wake(['{python}', str(script)], fire_on='success')
    result = probes.check(w, policy=Policy(probe_commands=f'{{python}} {script}'), home=tmp_path)
    assert result.get('error') is None and result['fire'] is True
    assert result['message'].strip() == sys.executable


def test_unknown_interpreter_is_an_error_without_running_anything(tmp_path, monkeypatch):
    script = tmp_path/'check.py'
    monkeypatch.setattr(probes.sys, 'executable', '')
    spawned = []
    def popen(*args, **kwargs):
        spawned.append(args)
        raise OSError('blocked in test')
    monkeypatch.setattr(subprocess, 'Popen', popen)
    result = probes.check(wake(['{python}', str(script)]), policy=Policy(probe_commands=f'{{python}} {script}'), home=tmp_path)
    assert result['fire'] is False and result.get('error')
    assert spawned == []


def test_agent_can_arm_a_token_watcher(engine, tmp_path):
    script = str(tmp_path/'check.py')
    engine.policy = replace(engine.policy, probe_commands=f'{{python}} {script}')
    result = engine.tool.handle({'action': 'create', 'note': 'Watch the trip',
                                 'trigger': {'kind': 'command', 'command': ['{python}', script], 'cwd': str(tmp_path)}})
    assert result['ok']
