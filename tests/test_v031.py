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
    assert f'- role {nightly.id} Nightly · cron 0 3 * * * · ' in out
    assert f'- role {paced.id} Paced · self-paced · ' in out


def test_desk_says_a_spent_role_rests_until_tomorrow(engine):
    e = engine
    role = arm(e, 'every', role=True, title='Notes', budget_turns_per_day=1,
               trigger_json={'kind': 'every', 'every': '2m'})
    assert 'next ' in desk(e) and 'today 0/1' in desk(e)
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
    assert '(allowed: gh pr view, gh pr checks' in detail and 'git log)' in detail
    plain = create(e, {'kind': 'command', 'command': ['rm', 'file']})['detail']
    assert plain.startswith('command probe is not allowlisted (allowed: ') and 'git -C' not in plain
    e.policy = replace(e.policy, source_command=False)
    off = create(e, {'kind': 'command', 'command': ['git', 'status']})
    assert off.get('error'), off
    assert 'allowed:' not in str(off)
