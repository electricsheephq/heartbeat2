"""0.3.2: /pulse status agrees with the desk about a role that has spent today's budget."""
from types import SimpleNamespace

from heartbeat2 import commands
from test_engine import KEY, arm, end, engine, pending, start


def _status(e, monkeypatch):
    monkeypatch.setattr(commands, 'session_env', lambda name: KEY if name == 'HERMES_SESSION_KEY' else '')
    return commands.handle('status', SimpleNamespace(get_engine=lambda: e))


def _row(out, wake):
    return next(line for line in out.splitlines() if line.startswith(f'{wake.id} '))


def test_status_says_a_spent_role_rests_and_leaves_it_out_of_next_due(engine, monkeypatch):
    e = engine
    now = e.clock.now()
    role = arm(e, 'every', role=True, title='Notes', budget_turns_per_day=1,
               trigger_json={'kind': 'every', 'every': '2m'}, next_due_at=now + 120)
    later = arm(e, 'at', title='Later', next_due_at=now + 3 * 3600)
    out = _status(e, monkeypatch)
    assert '· next ' in _row(out, role) and _row(out, role).endswith('today 0/1')
    assert f'next due: {commands._time(e, now + 120)}' in out

    e.dispatch(pending(e, role))
    start(e)
    end(e, 'progress', False)
    e.store.cas_wake(role.id, 'active', next_due_at=now + 120)  # the poller keeps a resting role's due time
    out = _status(e, monkeypatch)
    row = _row(out, role)
    assert '· resting until tomorrow · today 1/1' in row and '· next ' not in row
    assert f'next due: {commands._time(e, later.next_due_at)}' in out
    assert '· next ' in _row(out, later)


def test_status_header_is_none_when_the_only_role_is_resting(engine, monkeypatch):
    e = engine
    role = arm(e, 'every', role=True, title='Notes', budget_turns_per_day=1,
               trigger_json={'kind': 'every', 'every': '2m'})
    e.dispatch(pending(e, role))
    start(e)
    end(e, 'progress', False)
    e.store.cas_wake(role.id, 'active', next_due_at=e.clock.now() + 120)
    out = _status(e, monkeypatch)
    assert 'next due: none' in out and '· resting until tomorrow · today 1/1' in _row(out, role)


def test_status_resting_ignores_paused_and_non_role_wakes(engine, monkeypatch):
    e = engine
    role = arm(e, 'every', role=True, title='Spent then paused', budget_turns_per_day=1,
               trigger_json={'kind': 'every', 'every': '2m'})
    e.dispatch(pending(e, role))
    start(e)
    end(e, 'progress', False)
    assert commands._resting(e, e.store.get_wake(role.id))
    assert e.store.cas_wake(role.id, 'active', status='paused', status_reason='command')
    paused = e.store.get_wake(role.id)
    out = _status(e, monkeypatch)
    assert 'resting until tomorrow' not in _row(out, paused) and '· paused · ' in _row(out, paused)
    assert not commands._resting(e, paused)
    plain = arm(e, 'at', title='Plain')
    assert not commands._resting(e, plain)
