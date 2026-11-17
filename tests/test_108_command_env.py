"""#108 item 3: the owner can pass named secrets to one allowlisted watcher command."""
import pytest

from heartbeat2 import probes
from heartbeat2.model import Wake
from heartbeat2.policy import Policy

# The child prints the names (and value lengths) of the HB_T_ variables it received, never the values.
REPORT = ("import os\n"
          "print(','.join(f'{k}:{len(v)}' for k, v in sorted(os.environ.items()) if k.startswith('HB_T_')) or 'none')\n")


def wake(argv):
    return Wake('agent:telegram:dm:unit', 'Check trip', 'command', trigger_json={'command': argv, 'fire_on': 'success'},
                probe_state_json=None, next_due_at=100)


def run(tmp_path, monkeypatch, *, commands, env='', argv=None):
    script = tmp_path/'check.py'
    script.write_text(REPORT)
    monkeypatch.setenv('HB_T_KEY', 's3cr3t-value')
    monkeypatch.setenv('HB_T_OTHER', 'other-value')
    policy = Policy.from_settings({'probe_commands': commands.format(script=script), 'probe_command_env': env.format(script=script)})
    result = probes.check(wake(argv or ['{python}', str(script)]), policy=policy, home=tmp_path)
    assert result.get('error') is None, result
    assert 's3cr3t-value' not in str(result) and 'other-value' not in str(result)
    return result['message'].strip()


def test_a_rule_passes_exactly_the_named_variables(tmp_path, monkeypatch):
    assert run(tmp_path, monkeypatch, commands='{{python}} {script}', env='{{python}} {script} = HB_T_KEY') == 'HB_T_KEY:12'


def test_without_a_rule_the_command_gets_no_secrets(tmp_path, monkeypatch):
    assert run(tmp_path, monkeypatch, commands='{{python}} {script}') == 'none'


def test_a_rule_for_another_entry_passes_nothing(tmp_path, monkeypatch):
    assert run(tmp_path, monkeypatch, commands='{{python}} {script}, git status', env='git status = HB_T_KEY') == 'none'


def test_the_longest_admitting_entry_decides(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, commands='{{python}} {script}, {{python}} {script} --trip',
              env='{{python}} {script} = HB_T_OTHER; {{python}} {script} --trip = HB_T_KEY',
              argv=['{python}', str(tmp_path/'check.py'), '--trip'])
    assert out == 'HB_T_KEY:12'


def test_several_names_in_one_rule(tmp_path, monkeypatch):
    assert run(tmp_path, monkeypatch, commands='{{python}} {script}',
               env='{{python}} {script} = HB_T_KEY, HB_T_OTHER') == 'HB_T_KEY:12,HB_T_OTHER:11'


@pytest.mark.parametrize('rule', ['git status = PATH', 'git status = LD_PRELOAD', 'git status = DYLD_INSERT_LIBRARIES',
                                  'git status = PYTHONPATH', 'git status = HERMES_HOME', 'git status = lower_case',
                                  'git status = ', '= HB_T_KEY', 'git status HB_T_KEY', 'git status = A B'])
def test_invalid_or_reserved_rules_are_refused(rule):
    with pytest.raises(ValueError):
        Policy.from_settings({'probe_command_env': rule})


def test_blank_is_the_default_and_valid():
    assert Policy().probe_command_env == '' and Policy.from_settings({'probe_command_env': ' '}).probe_command_env.strip() == ''


def test_the_agent_cannot_set_it():
    from heartbeat2.tool import PARAMETERS
    assert 'probe_command_env' not in str(PARAMETERS)


def test_a_granted_name_cannot_undo_the_pager_and_no_prompt_settings(tmp_path, monkeypatch):
    script = tmp_path/'pager.py'
    script.write_text("import os\nprint(os.environ.get('PAGER'), os.environ.get('GIT_TERMINAL_PROMPT'))\n")
    monkeypatch.setenv('PAGER', 'less')
    monkeypatch.setenv('GIT_TERMINAL_PROMPT', '1')
    policy = Policy.from_settings({'probe_commands': f'{{python}} {script}',
                                   'probe_command_env': f'{{python}} {script} = PAGER, GIT_TERMINAL_PROMPT'})
    result = probes.check(wake(['{python}', str(script)]), policy=policy, home=tmp_path)
    assert result.get('error') is None, result
    assert result['message'].strip() == 'cat 0'
