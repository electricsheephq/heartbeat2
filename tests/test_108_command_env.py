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
                                  'git status = ENV', 'git status = BASH_ENV', 'git status = IFS', 'git status = LC_ALL',
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


@pytest.mark.parametrize('rules', ['', 'git status = HB_T_KEY'])
def test_the_setting_does_not_change_the_limits_fingerprint(tmp_path, clock, rules):
    from test_engine import GatewayCtx, KEY, admit
    from heartbeat2.model import Session
    from heartbeat2.poller import Poller
    from heartbeat2.store import Store
    # Default Policy limits at main 645877f (the baseline tests/test_v05_voice_bridge.py pins).
    baseline = 'c8ea8abc6d7f5f05b08f0edeb020602464461c74b7dd60138848b5c0ad34c1fb'
    store = Store(tmp_path/'pulse.db', clock)
    store.upsert_session(Session(KEY, 'telegram', 'dm', 'unit', 'sid', clock.now(),
                                 clock.now(), 'episode', limits_fingerprint=baseline))
    e = Poller(GatewayCtx(tmp_path), store, Policy(clock=clock, probe_command_env=rules), clock,
               session_key_fn=lambda: KEY, paused=lambda: False, compression_tip=lambda sid: sid)
    try:
        admit(e)
        result = e.intake.pre_llm_call(session_id='sid', user_message='hello')
        assert 'limits changed' not in (result or {}).get('context', '')
        assert store.get_session(KEY).limits_fingerprint == baseline
    finally:
        e.close()
        store.close()


def test_a_blank_setting_leaves_a_malformed_later_allowlist_entry_harmless(tmp_path, monkeypatch):
    assert run(tmp_path, monkeypatch, commands='{{python}} {script}, git "status') == 'none'


def test_a_grant_skips_a_malformed_allowlist_entry(tmp_path, monkeypatch):
    assert run(tmp_path, monkeypatch, commands='{{python}} {script}, git "status',
               env='{{python}} {script} = HB_T_KEY') == 'HB_T_KEY:12'


@pytest.mark.parametrize('rule', ['{python} check.py = HB_T_KEY', '{python} ./bin/check.py = HB_T_KEY',
                                  './check = HB_T_KEY', 'bin/check --x = HB_T_KEY', '~/bin/check = HB_T_KEY'])
def test_a_grant_must_name_its_program_and_script_by_absolute_path(rule):
    # A relative path resolves in the watcher's cwd, which the agent chooses, so another file could get the secret.
    with pytest.raises(ValueError):
        Policy.from_settings({'probe_command_env': rule})


@pytest.mark.parametrize('rule', ['gh pr view = HB_T_KEY', '/usr/bin/git status = HB_T_KEY',
                                  '{python} /srv/trip/check.py --trip = HB_T_KEY'])
def test_bare_program_names_and_absolute_paths_are_accepted(rule):
    assert Policy.from_settings({'probe_command_env': rule}).probe_command_env == rule
