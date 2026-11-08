"""U2: standing roles, their budgets, and reversible message holds."""
from dataclasses import asdict, replace
from datetime import datetime
import sqlite3
from types import SimpleNamespace

import pytest
import yaml

from heartbeat2 import commands
from heartbeat2.model import Wake
from heartbeat2.policy import Policy
from heartbeat2.store import Store, _SCHEMA
from heartbeat2.wake_text import render_wake
from test_engine import KEY, arm, end, engine, pending, session, start


def create(e, **args):
    return e.tool.handle(dict(action='create', note='Own the release notes.',
                              trigger={'kind': 'self'}, role=True, **args), session_id='sid')


def test_create_role_and_recurring_only(engine):
    e = engine
    result = create(e)
    assert result.get('ok'), result
    w = e.store.get_wake(result['wake']['id'])
    assert w.role is True and w.expires_at is None and w.budget_turns_per_day is None
    assert e.store.connection.execute('SELECT role FROM wakes WHERE id=?', (w.id,)).fetchone()[0] == 1
    assert 'role: runs until you or the agent end it' in result['receipt']
    assert f'/pulse cancel {w.id}' in result['receipt']
    for kind in ('at', 'file', 'owner_silent'):
        bad = e.tool.handle(dict(action='create', note='n', role=True, trigger={'kind': kind}))
        assert bad == {'error': 'role_needs_recurring'}
    for role in (False, True):
        assert e.tool.handle(dict(action='update', id=w.id, role=role)) == {'error': 'role_is_fixed'}
    assert e.tool.handle(dict(action='update', id=w.id, trigger={'kind': 'at', 'at': '+1h'}))['error'] == 'role_needs_recurring'


@pytest.mark.parametrize('mode', ['ask_first', 'no'])
def test_role_create_consent(engine, mode):
    engine.policy = replace(engine.policy, agent_can_create=mode)
    result = create(engine)
    if mode == 'no':
        assert result['error'] == 'agent_create_disabled' and not engine.store.list_wakes()
    else:
        assert result.get('ok') and not result['armed']
        assert result['wake']['role'] and result['wake']['status'] == 'proposed'


@pytest.mark.parametrize('creator', ['agent', 'user'])
def test_budget_clamps_and_wake_turn_cannot_raise(engine, creator):
    e = engine
    result = create(e, budget={'turns_per_day': 999})
    assert result.get('ok'), result
    wid = result['wake']['id']
    assert result['wake']['budget_turns_per_day'] == 12 and 'budget.turns_per_day' in result['clamped']
    e.store.cas_wake(wid, 'active', creator=creator, budget_turns_per_day=3)
    w = e.store.get_wake(wid)
    e.dispatch(pending(e, w))
    start(e)
    args = dict(action='update', id=wid, budget={'turns_per_day': 9})
    result = e.tool.handle(args, session_id='sid')
    assert result.get('ok'), result
    assert result['wake']['budget_turns_per_day'] == 3
    assert 'budget.turns_per_day' in result['clamped']
    result = e.tool.handle({**args, 'budget': {'turns_per_day': 2}, 'note': 'Keep notes current.',
                            'trigger': {'kind': 'every', 'every': '2h'}}, session_id='sid')
    assert result.get('ok') and result['wake']['budget_turns_per_day'] == 2
    assert result['wake']['kind'] == 'every' and result['wake']['note'] == 'Keep notes current.'
    end(e, 'progress', False)
    result = e.tool.handle({**args, 'budget': {'turns_per_day': 999}}, session_id='sid')
    assert result['wake']['budget_turns_per_day'] == 12
    assert e.tool.handle({**args, 'budget': {'turns_per_day': 0}})['wake']['budget_turns_per_day'] == 1


def test_implicit_budget_cannot_be_raised_in_wake_turn(engine):
    e = engine
    result = create(e)
    assert result.get('ok'), result
    w = e.store.get_wake(result['wake']['id'])
    e.dispatch(pending(e, w))
    start(e)
    result = e.tool.handle(dict(action='update', id=w.id, budget={'turns_per_day': 12}), session_id='sid')
    assert result['wake']['budget_turns_per_day'] == 6 and result['clamped']['budget.turns_per_day']


def test_role_settings_and_manifest(engine):
    p = Policy.from_settings({'role_turns_per_day_default': 99, 'role_turns_per_day_max': 99,
                              'max_model_wakes_per_day': 8, 'max_children_per_wake_turn': 0})
    assert p.role_turns_per_day_max == 8 and p.role_turns_per_day_default == 8
    assert p.max_children_per_wake_turn == 1
    p = Policy.from_settings({'role_turns_per_day_default': 0, 'role_turns_per_day_max': 0})
    assert p.role_turns_per_day_default == p.role_turns_per_day_max == 1
    from pathlib import Path
    schema = yaml.safe_load((Path(__file__).parents[1] / 'plugin.yaml').read_text())['config_schema']
    for name, default in [('role_turns_per_day_default', 6), ('role_turns_per_day_max', 12), ('max_children_per_wake_turn', 3)]:
        assert schema[name]['type'] == 'int' and schema[name]['default'] == default


def test_role_lifetime_and_explicit_expiry(engine):
    e = engine
    result = create(e)
    assert result.get('ok'), result
    role = e.store.get_wake(result['wake']['id'])
    ordinary = arm(e, 'every')
    e.clock.advance((e.policy.max_lifetime_days + 1) * 86400)
    e.tick()
    assert e.store.get_wake(role.id).status == 'active'
    assert e.store.get_wake(ordinary.id).status == 'expired'
    assert len(e.ctx.calls) == 1
    start(e)
    end(e, 'progress', False)
    # A self role's default rhythm spreads its 6 turns over the day (4h), so the expiry must outlast one.
    result = create(e, until={'expires': '+5h'})
    assert result.get('ok'), result
    limited = e.store.get_wake(result['wake']['id'])
    assert limited.expires_at == e.clock.now() + 5 * 3600
    e.clock.advance(5 * 3600 + 1)
    e.times()
    assert e.store.get_wake(limited.id).status == 'expired'


@pytest.mark.parametrize('switch', ['reset', 'session'])
def test_role_survives_session_change(engine, switch):
    e = engine
    role = arm(e, 'self', role=True, expires_at=None)
    other = arm(e, 'every')
    if switch == 'reset':
        e.intake.on_session_reset(old_session_id='sid', new_session_id='new')
    else:
        for w in (role, other):
            e.intake._session_switch(w, 'new')
        session(e, current_session_id='new')
    assert e.store.get_wake(role.id).armed_session_id == 'new'
    assert e.store.get_wake(role.id).status == 'active'
    assert e.store.get_wake(other.id).status == 'orphaned'
    e.tick()
    start(e, sid='new')
    assert e.intake.flights['new'].wake_id == role.id
    assert e.ctx.calls[-1][1] == KEY


def test_role_children_reset_depth_and_bound_each_flight(engine):
    e = engine
    role = arm(e, 'self', role=True, depth=9)
    e.tick()
    start(e)
    e.policy = replace(e.policy, max_rearm_chain=0)
    args = dict(action='create', note='follow up', trigger={'kind': 'self'})
    children = []
    for _ in range(3):
        result = e.tool.handle(args, session_id='sid')
        assert result.get('ok'), result
        child = result['wake']
        assert child['depth'] == 1 and child['parent_wake_id'] == role.id
        children.append(child)
    assert e.tool.handle(args, session_id='sid')['error'] == 'rearm_child'
    e.policy = replace(e.policy, max_rearm_chain=2)
    end(e, 'progress', False)
    e.clock.advance(3600)
    child = e.store.get_wake(children[0]['id'])
    e.dispatch(pending(e, child))
    start(e)
    result = e.tool.handle(args, session_id='sid')
    assert result.get('ok') and result['wake']['depth'] == 2
    assert e.tool.handle(args, session_id='sid')['error'] == 'rearm_child'
    end(e, 'progress', False)
    e.clock.advance(3600)
    grandchild = e.store.get_wake(result['wake']['id'])
    e.dispatch(pending(e, grandchild))
    start(e)
    assert e.tool.handle(args, session_id='sid')['error'] == 'rearm_depth'


def test_role_children_still_refuse_untrusted_rearm(engine):
    e = engine
    role = arm(e, 'self', role=True)
    e.dispatch(pending(e, role))
    start(e)
    e.intake.flights['sid'].untrusted = True
    assert e.tool.handle(dict(action='create', note='n', trigger={'kind': 'self'}), session_id='sid')['error'] == 'untrusted_rearm'


@pytest.mark.parametrize('budget', [None, 2])
def test_role_daily_dispatch_budget_and_next_local_day(engine, budget):
    e = engine
    e.policy = replace(e.policy, role_turns_per_day_default=2, timezone='Asia/Tokyo')
    e.clock.wall = datetime.fromisoformat('2025-10-12T14:00:00+00:00').timestamp()
    w = arm(e, 'self', role=True, budget_turns_per_day=budget)
    for n in range(2):
        e.dispatch(pending(e, w, f'fire:{n}'))
        start(e)
        end(e, 'progress', False)
        e.clock.advance(601)
    session(e, unanswered_count=e.policy.max_unanswered)
    o = pending(e, w, 'over-budget')
    e.dispatch(o)
    skipped = e.store.get_occurrence(w.id, o.dedupe_key)
    assert skipped.state == 'skipped' and skipped.error == 'role_budget'
    assert not skipped.force_silent and len(e.ctx.calls) == 2
    e.clock.wall = datetime.fromisoformat('2025-10-12T15:01:00+00:00').timestamp()
    e.dispatch(pending(e, w, 'tomorrow'))
    assert len(e.ctx.calls) == 3


@pytest.mark.parametrize('hold', ['unanswered', 'quiet_hours', 'daily_message_cap'])
def test_owner_role_holds_messages_but_runs(engine, hold):
    e = engine
    w = arm(e, 'self', role=True, creator='user')
    if hold == 'unanswered':
        session(e, unanswered_count=e.policy.max_unanswered)
    elif hold == 'quiet_hours':
        e.policy = replace(e.policy, quiet_hours='00:00-23:59')
    else:
        e.ledger(w, delivered=e.policy.max_messages_per_day)
    before = e.daily(KEY)['delivered']
    e.tick()
    o, = e.store.list_occurrences(w.id)
    assert o.state == 'claimed' and o.force_silent and o.held_reason == 'message_held:' + hold
    start(e)
    assert end(e, 'progress', True) == '[SILENT]'
    assert not e.store.get_occurrence(w.id, o.dedupe_key).delivered
    assert e.daily(KEY)['delivered'] == before
    e.ledger(w, model_turns=e.policy.max_model_wakes_per_day)
    e.clock.advance(3600)
    e.dispatch(pending(e, e.store.get_wake(w.id), 'cap'))
    assert e.store.get_occurrence(w.id, 'cap').error == 'daily_model_cap'


def test_role_prompt_keeps_explicit_ending(engine):
    e = engine
    w = arm(e, 'self', role=True)
    o = pending(e, w)
    text = render_wake(w, o, e.store.get_session(KEY), clock=e.clock)
    assert f'· role {w.id} ·' in text and "- If the chat shows the note's subject was settled" not in text
    ordinary = render_wake(replace(w, role=False), o, e.store.get_session(KEY), clock=e.clock)
    assert f'· wake {w.id} ·' in ordinary and "- If the chat shows the note's subject was settled" in ordinary
    e.dispatch(o)
    start(e)
    end(e, 'done', False)
    assert e.store.get_wake(w.id).status == 'done'


def test_role_commands_and_status(engine, monkeypatch):
    e = engine
    monkeypatch.setattr(commands, 'session_env', lambda name: KEY if name == 'HERMES_SESSION_KEY' else '')
    runtime = SimpleNamespace(get_engine=lambda: e)
    for text, kind in [('self Keep release notes current', 'self'), ('every 1d at 09:00 Track the release', 'every')]:
        out = commands.handle('role ' + text, runtime)
        assert not out.startswith('Error:'), out
        w = e.store.get_wake(out.split()[0])
        assert w.role and w.creator == 'user' and w.created_in == 'command' and w.kind == kind
        assert 'role: runs until' in out
        assert 'today 0/6' in out
    commands.handle(f'budget {w.id} 3/day', runtime)
    assert e.store.get_wake(w.id).budget_turns_per_day == 3
    for action in ('list', 'status'):
        out = commands.handle(action, runtime)
        assert '· role · every ·' in out and 'today 0/3' in out
    commands.handle(f'budget {w.id} 99/day', runtime)
    assert e.store.get_wake(w.id).budget_turns_per_day == 12
    e.dispatch(pending(e, w))
    assert 'today 1/12' in commands.handle('list', runtime)
    plain = arm(e)
    assert commands.handle(f'budget {plain.id} 3/day', runtime) == 'Error: budget applies to roles'
    assert 'role' in commands.HELP and 'budget <id>' in commands.HELP


def test_old_db_upgrade(tmp_path, clock):
    path = tmp_path / 'v024.db'
    with sqlite3.connect(path) as db:
        db.executescript(_SCHEMA)
        old = asdict(Wake(KEY, 'old wake', 'self'))
        columns = {row[1] for row in db.execute('PRAGMA table_info(wakes)')}
        assert 'role' not in columns and 'budget_turns_per_day' not in columns
        values = {k: ('{}' if k == 'trigger_json' else v) for k, v in old.items() if k in columns}
        db.execute('INSERT INTO wakes (' + ','.join(values) + ') VALUES (' + ','.join('?' for _ in values) + ')', list(values.values()))
    with Store(path, clock) as store:
        columns = {row[1] for row in store.connection.execute('PRAGMA table_info(wakes)')}
        assert {'role', 'budget_turns_per_day'} <= columns
        w = store.get_wake(old['id'])
        assert w.role is False and w.budget_turns_per_day is None
        assert store.get_meta('schema_version') == '1'


@pytest.mark.parametrize('failure', ['handoff', 'claim'])
def test_refunded_message_hold_lifts_and_delivers(engine, monkeypatch, failure):
    e = engine
    w = arm(e, 'every')
    o = pending(e, w)
    session(e, unanswered_count=e.policy.max_unanswered)
    with monkeypatch.context() as patch:
        if failure == 'handoff':
            served = iter([True, False])
            patch.setattr(e, 'eligible', lambda: next(served))
        else:
            patch.setattr(e.store, 'claim', lambda *a, **kw: False)
        e.dispatch(o)
    o = e.store.get_occurrence(w.id, o.dedupe_key)
    assert o.state == 'pending' and o.force_silent and o.held_reason == 'message_held:unanswered'
    session(e, unanswered_count=0)
    e.dispatch(o)
    start(e)
    assert end(e, 'progress', True) is None
    o = e.store.get_occurrence(w.id, o.dedupe_key)
    assert o.delivered and not o.force_silent and o.held_reason == ''


def test_replay_silence_is_not_cleared(engine):
    e = engine
    w = arm(e, 'self', role=True)
    o = pending(e, w)
    e.store.update_occurrence(w.id, o.dedupe_key, 'pending', force_silent=True, held_reason='restart')
    # A temporary message hold must not take ownership of replay silence.
    session(e, unanswered_count=e.policy.max_unanswered)
    served = iter([True, False])
    original = e.eligible
    e.eligible = lambda: next(served)
    e.dispatch(e.store.get_occurrence(w.id, o.dedupe_key))
    e.eligible = original
    session(e, unanswered_count=0)
    e.dispatch(e.store.get_occurrence(w.id, o.dedupe_key))
    start(e)
    assert end(e, 'progress', True) == '[SILENT]'
    o = e.store.get_occurrence(w.id, o.dedupe_key)
    assert o.force_silent and not o.delivered and o.held_reason == 'restart'
