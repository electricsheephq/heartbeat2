"""0.3.6: the agent reads its desk from the tool; an owner-set role budget is a ceiling; cron ignores next_check out loud."""
from types import SimpleNamespace

import pytest

from heartbeat2 import commands
from heartbeat2.desk import render_desk
from heartbeat2.tool import DESCRIPTION, PARAMETERS
from test_engine import KEY, arm, engine, pending, start
from test_v0113 import owner_turn


@pytest.fixture
def command(engine, monkeypatch):
    monkeypatch.setattr(commands, 'session_env', lambda n: KEY if n == 'HERMES_SESSION_KEY' else '')
    return lambda text: commands.handle(text, SimpleNamespace(get_engine=lambda: engine))


def _role(e, kind='every', **trigger):
    return arm(e, kind, role=True, title='Release notes', trigger_json={'kind': kind, **(trigger or {'every': '2h'})})


def _budget(e, wid, n, sid='sid', **args):
    return e.tool.handle(dict(action='update', id=wid, budget={'turns_per_day': n}, **args), session_id=sid)


def test_desk_action_reads_the_desk_without_changing_anything(engine):
    e = engine
    role = _role(e)
    assert 'desk' in PARAMETERS['properties']['action']['enum'] and 'desk to read your desk' in DESCRIPTION
    before = [(w.id, w.version) for w in e.store.list_wakes(KEY)]
    result = e.tool.handle({'action': 'desk'}, session_id='sid')
    assert result.get('ok'), result
    assert result['desk'] == render_desk(e, e.store.get_session(KEY), clock=e.clock,
                                         timezone=e.policy.resolved_timezone(), policy=e.policy)
    assert result['desk'].startswith('Your desk:') and f'role {role.id} Release notes' in result['desk']
    assert 'turns today 0/' in result['desk']
    assert [(w.id, w.version) for w in e.store.list_wakes(KEY)] == before
    assert not e.store.list_occurrences(session_key=KEY)


def test_desk_action_inside_a_wake_turn_counts_its_own_slot(engine):
    from dataclasses import replace
    e = engine
    e.policy = replace(e.policy, max_messages_per_day=1)  # The turn holds the day's only slot: it must not read as held.
    role = _role(e)
    e.dispatch(pending(e, role))
    start(e)
    desk = e.tool.handle({'action': 'desk'}, session_id='sid')['desk']
    # The turn's own message slot is shown as in progress, and it is not told messages are held.
    assert 'messages today 0/1 (+1 in progress)' in desk and 'Messages held now' not in desk


def test_owner_set_budget_is_a_ceiling_the_agent_cannot_raise_unasked(engine, command):
    e = engine
    role = _role(e)
    assert 'today 0/3' in command(f'budget {role.id} 3/day')
    assert e.store.owner_budget(role.id) == 3
    # Outside a wake turn and without the owner's ask, a raise stops at the owner's number.
    result = _budget(e, role.id, 9)
    assert result.get('ok') and result['wake']['budget_turns_per_day'] == 3, result
    assert f'they send /pulse budget {role.id} 9/day' in result['clamped']['budget.turns_per_day']
    # Even in the owner's own turn, owner_asked does not raise it: only their /pulse budget command does.
    owner_turn(e)
    assert _budget(e, role.id, 9, owner_asked=True)['wake']['budget_turns_per_day'] == 3
    assert _budget(e, role.id, 9, sid='other', owner_asked=True)['wake']['budget_turns_per_day'] == 3
    # Going under the owner's number, and back up to it, is the agent's own call.
    assert _budget(e, role.id, 2)['wake']['budget_turns_per_day'] == 2
    result = _budget(e, role.id, 3)
    assert result['wake']['budget_turns_per_day'] == 3 and 'budget.turns_per_day' not in result['clamped']
    assert e.store.owner_budget(role.id) == 3


def test_only_the_owner_command_raises_the_ceiling(engine, command):
    from dataclasses import replace
    e = engine
    role = _role(e)
    command(f'budget {role.id} 3/day')
    assert 'today 0/9' in command(f'budget {role.id} 9/day') and e.store.owner_budget(role.id) == 9
    assert _budget(e, role.id, 12, sid='other')['wake']['budget_turns_per_day'] == 9
    # The owner's own number is kept even while the profile maximum is lower.
    e.policy = replace(e.policy, role_turns_per_day_max=5)
    command(f'budget {role.id} 8/day')
    assert e.store.owner_budget(role.id) == 8 and e.store.get_wake(role.id).budget_turns_per_day == 5
    assert e.store.owner_budget('missing') is None


def test_a_lowering_the_owner_asked_for_becomes_the_ceiling(engine):
    e = engine
    role = _role(e)
    owner_turn(e)
    assert _budget(e, role.id, 2, owner_asked=True)['wake']['budget_turns_per_day'] == 2
    assert e.store.owner_budget(role.id) == 2
    # Later, unasked, the agent cannot restore the old number.
    assert _budget(e, role.id, 6, sid='other')['wake']['budget_turns_per_day'] == 2
    # A lowering without owner-turn proof is the agent's own: no ceiling.
    other = _role(e)
    assert _budget(e, other.id, 2, sid='other', owner_asked=True)['wake']['budget_turns_per_day'] == 2
    assert e.store.owner_budget(other.id) is None


def test_a_role_without_an_owner_budget_keeps_todays_rules(engine):
    e = engine
    role = _role(e)
    assert e.store.owner_budget(role.id) is None
    assert _budget(e, role.id, 999)['wake']['budget_turns_per_day'] == e.policy.role_turns_per_day_max


def test_cron_next_check_is_reported_ignored(engine):
    e = engine
    role = _role(e, 'cron', cron='0 9 * * *')
    e.dispatch(pending(e, role))
    start(e)
    result = e.tool.handle(dict(action='respond', outcome='progress', notify=False, summary='Drafted notes.',
                                next_check='6h'), session_id='sid')
    assert result.get('ok'), result
    assert 'cron wake follows its schedule' in result['clamped']['next_check']
    f = e.intake.flights['sid']
    assert e.store.get_occurrence(f.wake_id, f.dedupe_key).next_check_s is None


def test_every_role_still_takes_next_check(engine):
    e = engine
    role = _role(e)
    e.dispatch(pending(e, role))
    start(e)
    result = e.tool.handle(dict(action='respond', outcome='progress', notify=False, summary='Drafted notes.',
                                next_check='6h'), session_id='sid')
    f = e.intake.flights['sid']
    assert result.get('ok') and e.store.get_occurrence(f.wake_id, f.dedupe_key).next_check_s is not None


def test_idle_hint_offers_only_what_works(engine):
    from dataclasses import replace
    from heartbeat2 import governor
    e = engine
    n = e.policy.suggest_gate_after
    role = replace(_role(e), noop_streak=n)
    text = governor.hint(e, role)
    assert 'watcher via pulse update' not in text and 'separate watcher' in text and 'push next_check out' in text
    cron_role = replace(_role(e, 'cron', cron='0 9 * * *'), noop_streak=n)
    assert 'change its cron schedule' in governor.hint(e, cron_role) and 'next_check' not in governor.hint(e, cron_role)
    plain = replace(arm(e, 'every', trigger_json={'kind': 'every', 'every': '2h'}), noop_streak=n)
    assert governor.hint(e, plain) == 'Consider a file|process|url|command watcher via pulse update, push next_check out, or cancel.'
    assert governor.hint(e, replace(plain, noop_streak=n - 1)) == ''
    probe = replace(plain, kind='file', trigger_json={'kind': 'file', 'path': '/x'})
    assert governor.hint(e, probe) == 'Consider a file|process|url|command watcher via pulse update, or cancel.'
    checkin = replace(plain, kind='owner_silent', trigger_json={'kind': 'owner_silent', 'builtin': True})
    assert governor.hint(e, checkin) == ''
    own = replace(plain, kind='self', trigger_json={'kind': 'self', 'own_time': True, 'builtin': True})
    assert 'outcome=done' not in governor.hint(e, own) and 'owner sets how often' in governor.hint(e, own)


def test_readme_lists_every_setting_and_the_role_commands():
    from pathlib import Path
    import yaml
    root = Path(__file__).resolve().parents[1]
    readme = (root / 'README.md').read_text()
    schema = yaml.safe_load((root / 'plugin.yaml').read_text())['config_schema']
    assert [k for k in schema if f'`{k}`' not in readme] == []
    for row in ('`/pulse role self <charter>`', '`/pulse desk`', '`/pulse budget <id> 3/day`',
                '`create · list · desk · update'):
        assert row in readme
    assert 'turn it into a watcher, or finish it' not in (root / 'skills/heartbeat2-usage/SKILL.md').read_text()
