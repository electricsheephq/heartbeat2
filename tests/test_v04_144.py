"""Every wake-turn rule holds for the whole turn, also after the owner stops its occurrence mid-turn (#144)."""
from types import SimpleNamespace

import pytest

from heartbeat2 import commands
from test_engine import KEY, engine, pending, start


@pytest.fixture
def pulse(monkeypatch):
    monkeypatch.setattr(commands, 'session_env', lambda name: KEY if name == 'HERMES_SESSION_KEY' else '')
    return lambda e, args: commands.handle(args, SimpleNamespace(get_engine=lambda: e))


def role_turn_paused_by_owner(e, pulse):
    result = e.tool.handle(dict(action='create', note='Own the release notes.', trigger={'kind': 'self'}, role=True),
                           session_id='sid')
    assert result.get('ok'), result
    w = e.store.get_wake(result['wake']['id'])
    e.dispatch(pending(e, w))
    start(e)
    assert e.intake.flight_for('sid', KEY) is not None
    assert 'paused' in pulse(e, f'pause {w.id}')
    # The occurrence is stopped, but the turn that started as this wake is still running.
    assert e.intake.flight_for('sid', KEY) is None and e.intake.in_wake_turn('sid', KEY)
    return w


def test_a_role_made_after_the_pause_keeps_the_wake_turn_budget(engine, pulse):
    e = engine
    parent = role_turn_paused_by_owner(e, pulse)
    result = e.tool.handle(dict(action='create', note='Own the changelog.', trigger={'kind': 'self'}, role=True,
                                budget={'turns_per_day': 999}), session_id='sid')
    assert result.get('ok'), result
    cap = e.policy.role_budget(e.store.get_wake(parent.id))
    assert result['wake']['budget_turns_per_day'] == cap
    assert result['clamped']['budget.turns_per_day'] == f'999 → {cap} (wake_turn cannot raise budget)'
    assert result['wake']['created_in'] == 'wake_turn' and result['wake']['parent_wake_id'] == parent.id


def test_the_child_limit_still_counts_after_the_pause(engine, pulse):
    e = engine
    role_turn_paused_by_owner(e, pulse)
    limit = e.policy.max_children_per_wake_turn
    made = [e.tool.handle(dict(action='create', note=f'Child {n}.', trigger={'kind': 'at', 'at': 'in 2h'}),
                          session_id='sid') for n in range(limit+1)]
    assert all(r.get('ok') for r in made[:limit]), made
    assert made[limit].get('error'), made[limit]


def test_an_owner_inbound_raced_into_a_paused_wake_turn_is_not_owner_evidence(engine, pulse):
    e = engine
    role_turn_paused_by_owner(e, pulse)
    e.intake.owner_turns['sid'] = e.intake.turns.get('sid')  # The rare race: an owner inbound seen in this turn.
    result = e.tool.handle(dict(action='create', note='Call the bank.', trigger={'kind': 'at', 'at': 'in 2h'},
                                owner_asked=True), session_id='sid')
    assert result.get('ok'), result
    assert result['wake']['owner_requested'] is False and result['owner_asked_ignored'] == 'wake_turn'
