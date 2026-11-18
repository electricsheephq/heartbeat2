"""0.3.1: the desk shows each role's rhythm and when its budget is spent; refused command watchers say what would work."""
from dataclasses import replace

from test_engine import KEY, arm, end, engine, pending, start
from test_v03_u3 import desk
from test_v0114 import create


def test_desk_shows_each_roles_rhythm(engine):
    e = engine
    every = arm(e, 'every', role=True, title='Notes', trigger_json={'kind': 'every', 'every': '2m'})
    daily = arm(e, 'every', role=True, title='Daily', trigger_json={'kind': 'every', 'every': '1d', 'at': '09:00'})
    nightly = arm(e, 'cron', role=True, title='Nightly', trigger_json={'kind': 'cron', 'cron': '0 3 * * *'})
    paced = arm(e, 'self', role=True, title='Paced', trigger_json={'kind': 'self'})
    out = desk(e)
    assert f'- role {every.id} Notes · every 2m · ' in out
    assert f'- role {daily.id} Daily · every 1d at 09:00 · ' in out
    assert f'- role {nightly.id} Nightly · cron `0 3 * * *` · ' in out
    assert f'- role {paced.id} Paced · self-paced · ' in out


def test_desk_says_a_spent_role_rests_until_tomorrow(engine):
    e = engine
    role = arm(e, 'every', role=True, title='Notes', budget_turns_per_day=1,
               trigger_json={'kind': 'every', 'every': '2m'})
    row = next(line for line in desk(e).splitlines() if line.startswith(f'- role {role.id} '))
    assert '· next ' in row and row.endswith('today 0/1')
    e.dispatch(pending(e, role))
    start(e)
    end(e, 'progress', False)
    row = next(line for line in desk(e).splitlines() if line.startswith(f'- role {role.id} '))
    assert row.startswith(f'- role {role.id} Notes · every 2m · last: ')
    assert row.endswith('· resting until tomorrow · today 1/1') and '· next ' not in row


def test_refused_command_watcher_names_cwd_and_the_allowlist(engine, tmp_path):
    e = engine
    result = create(e, {'kind': 'command', 'command': ['git', '-C', str(tmp_path), 'rev-parse', 'HEAD']})
    assert result.get('error') == 'invalid_request', result
    detail = result['detail']
    assert detail.startswith('command probe is not allowlisted; drop git -C and set the cwd field to the repository')
    assert '(allowed: gh pr, gh run, git ls-remote, git status, git log)' in detail
    plain = create(e, {'kind': 'command', 'command': ['rm', 'file']})['detail']
    assert plain.startswith('command probe is not allowlisted (allowed: ') and 'git -C' not in plain
    e.policy = replace(e.policy, probe_commands='mytool --token SECRET123 report, git log')
    shown = create(e, {'kind': 'command', 'command': ['git', '-C', str(tmp_path), 'log']})['detail']
    assert '(allowed: mytool --token, git log)' in shown and 'SECRET123' not in shown
    e.policy = replace(e.policy, source_command=False)
    off = create(e, {'kind': 'command', 'command': ['git', '-C', str(tmp_path), 'log']})
    assert off.get('error') == 'source_disabled' and 'git -C' not in str(off), off


def test_desk_rhythm_survives_odd_stored_triggers(engine):
    e = engine
    odd = arm(e, 'every', role=True, title='Odd', trigger_json=['every', '2m'])
    wide = arm(e, 'every', role=True, title='Wide', trigger_json={'kind': 'every', 'every': '2m' + ' ' * 1900})
    weekly = arm(e, 'cron', role=True, title='Weekly', trigger_json={'kind': 'cron', 'cron': '30 9 1,15 * mon-fri'})
    out = desk(e)
    assert f'- role {odd.id} Odd · every · ' in out
    assert f'- role {wide.id} Wide · every 2m · ' in out
    assert f'- role {weekly.id} Weekly · cron `30 9 1,15 * mon-fri` · ' in out
