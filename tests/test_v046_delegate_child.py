"""Delegate children retain their originating wake turn's attribution and limits."""
from types import SimpleNamespace

import pytest

from heartbeat2 import commands
from heartbeat2.registration import EVENTS, HOOKS, Runtime
from test_engine import KEY, arm, engine, pending, start


@pytest.fixture
def command(engine, monkeypatch):
    monkeypatch.setattr(commands, 'session_env', lambda n: KEY if n == 'HERMES_SESSION_KEY' else '')
    return lambda text: commands.handle(text, SimpleNamespace(get_engine=lambda: engine))


def running_role(e):
    w = arm(e, 'self', role=True)
    e.dispatch(pending(e, w))
    start(e)
    return w, e.intake.flights['sid']


def assert_wake_limits(e, w, sid='child', budget_target=None):
    result = e.tool.handle(dict(action='update', id=(budget_target or w).id,
                               budget={'turns_per_day': 12}), session_id=sid)
    assert result.get('ok'), result
    assert 'wake_turn cannot raise budget' in str(result.get('clamped', result)), result
    created = e.tool.handle(dict(action='create', note='Check the delegated task.',
                                 trigger={'kind': 'at', 'at': 'in 1m'}, owner_asked=True), session_id=sid)
    assert created.get('ok'), created
    assert created['wake']['created_in'] == 'wake_turn'
    assert created['wake']['parent_wake_id'] == w.id
    assert created['wake']['owner_requested'] is False
    assert created['owner_asked_ignored'] == 'wake_turn'


@pytest.mark.parametrize('switch', ['reset', 'session'])
def test_fenced_parent_keeps_child_wake_limits(engine, switch):
    w, flight = running_role(engine)
    engine.intake.subagent_start(parent_session_id='sid', child_session_id='child')
    if switch == 'reset':
        engine.intake.on_session_reset(old_session_id='sid', new_session_id='new')
    else:
        engine.intake._session_switch(engine.store.get_wake(w.id), 'new')
    assert flight.invalid
    assert engine.store.get_occurrence(w.id, flight.dedupe_key).state == 'superseded'
    assert engine.intake.flight_for('child', KEY) is flight
    assert_wake_limits(engine, w)


@pytest.mark.parametrize('action', ['pause', 'cancel'])
def test_owner_stopped_parent_keeps_child_wake_limits(engine, command, action):
    w, flight = running_role(engine)
    engine.intake.subagent_start(parent_session_id='sid', child_session_id='child')
    assert not command(action + ' ' + w.id).startswith('Error:')
    assert engine.store.get_occurrence(w.id, flight.dedupe_key).state == 'superseded'
    assert engine.intake.flight_for('child', KEY) is flight
    budget_target = None
    if action == 'cancel':
        # Terminal wakes refuse updates before considering any budget change.
        result = engine.tool.handle(dict(action='update', id=w.id,
                                         budget={'turns_per_day': 12}), session_id='child')
        assert result.get('error') == 'terminal_wake', result
        budget_target = arm(engine, 'self', role=True, title='Other role')
    assert_wake_limits(engine, w, budget_target=budget_target)


def test_nested_child_keeps_the_same_fenced_flight(engine):
    w, flight = running_role(engine)
    engine.intake.subagent_start(parent_session_id='sid', child_session_id='child')
    engine.intake.on_session_reset(old_session_id='sid', new_session_id='new')
    engine.intake.subagent_start(parent_session_id='child', child_session_id='grandchild')
    assert engine.intake.children['grandchild'] is flight
    assert engine.intake.flight_for('grandchild', KEY) is flight
    assert_wake_limits(engine, w, 'grandchild')


def test_child_outside_a_wake_stays_a_human_turn(engine):
    assert engine.intake.subagent_start(parent_session_id='sid', child_session_id='child') is None
    assert not engine.intake.children
    created = engine.tool.handle(dict(action='create', note='Check the task.',
                                     trigger={'kind': 'at', 'at': 'in 1m'}), session_id='child')
    assert created.get('ok'), created
    assert created['wake']['created_in'] == 'human_turn'
    assert created['wake']['parent_wake_id'] is None


@pytest.mark.parametrize('ids', [{}, {'parent_session_id': 'sid'}, {'child_session_id': 'child'}])
def test_subagent_start_missing_ids_is_a_noop(engine, ids):
    running_role(engine)
    assert engine.intake.subagent_start(**ids) is None
    assert not engine.intake.children


@pytest.mark.parametrize('ending', ['session_end', 'human_pre_llm_call'])
def test_parent_turn_end_drops_child_and_nested_mappings(engine, command, ending):
    w, flight = running_role(engine)
    engine.intake.subagent_start(parent_session_id='sid', child_session_id='child')
    engine.intake.subagent_start(parent_session_id='child', child_session_id='grandchild')
    assert not command('pause ' + w.id).startswith('Error:')
    if ending == 'session_end':
        engine.intake.on_session_end(session_id='sid', turn_id='turn', completed=True)
    else:
        engine.intake.pre_llm_call(session_id='sid', turn_id='turn', user_message='Hello.', platform='telegram')
    assert not engine.intake.flights
    assert not engine.intake.children
    for sid in ('child', 'grandchild'):
        assert engine.intake.flight_for(sid, KEY) is None
        result = engine.tool.handle(dict(action='update', id=w.id, budget={'turns_per_day': 12}), session_id=sid)
        assert result.get('ok'), result
        assert 'wake_turn cannot raise budget' not in str(result.get('clamped', result)), result
        created = engine.tool.handle(dict(action='create', note='Check after the turn.',
                                         trigger={'kind': 'at', 'at': 'in 1m'}), session_id=sid)
        assert created.get('ok'), created
        assert created['wake']['created_in'] == 'human_turn'


def test_compression_moves_parent_but_preserves_child_flight(engine, command):
    w, flight = running_role(engine)
    engine.intake.subagent_start(parent_session_id='sid', child_session_id='child')
    engine.compression_tip = lambda sid: 'compressed' if sid == 'sid' else None
    assert engine.intake.flight_for('compressed', KEY) is flight
    assert 'sid' not in engine.intake.flights
    assert engine.intake.flights['compressed'] is flight
    assert flight.session_id == 'compressed'
    assert not command('pause ' + w.id).startswith('Error:')
    assert engine.intake.flight_for('child', KEY) is flight
    assert_wake_limits(engine, w)
    engine.intake.on_session_end(session_id='compressed', turn_id='turn', completed=True)
    assert not engine.intake.children


def test_subagent_start_registration_and_runtime_dispatch(engine):
    _, flight = running_role(engine)
    assert 'subagent_start' in HOOKS
    assert 'subagent_start' not in EVENTS
    runtime = Runtime(engine.ctx, clock=engine.clock)
    runtime._engine = engine
    assert runtime.hook('subagent_start', parent_session_id='sid', parent_turn_id='turn',
                        parent_subagent_id='', child_session_id='child', child_subagent_id='delegate',
                        child_role='researcher', child_goal='Check the task.') is None
    assert engine.intake.children['child'] is flight


def test_live_mapped_and_unmapped_children_resolve_the_same_flight(engine):
    _, flight = running_role(engine)
    assert engine.intake.flight_for('unmapped', KEY) is flight
    engine.intake.subagent_start(parent_session_id='sid', child_session_id='child')
    assert engine.intake.flight_for('child', KEY) is flight
    assert engine.intake.flight_for('unmapped', KEY) is flight


def test_subagent_start_finds_a_parent_through_its_binding(engine, command):
    w, flight = running_role(engine)
    engine.intake.bindings['unmapped'] = KEY
    engine.intake.subagent_start(parent_session_id='unmapped', child_session_id='child')
    assert engine.intake.children['child'] is flight
    assert not command('pause ' + w.id).startswith('Error:')
    assert engine.intake.flight_for('child', KEY) is flight


@pytest.mark.parametrize('stale', ['different_key', 'removed_flight'])
def test_child_lookup_discards_stale_mapping(engine, command, stale):
    w, flight = running_role(engine)
    engine.intake.subagent_start(parent_session_id='sid', child_session_id='child')
    assert not command('pause ' + w.id).startswith('Error:')
    if stale == 'removed_flight':
        engine.intake.flights.pop('sid')
    key = 'other-session-key' if stale == 'different_key' else KEY
    assert engine.intake.flight_for('child', key) is None
    assert 'child' not in engine.intake.children


def test_compressed_child_keeps_wake_limits_after_pause(engine, command):
    w, flight = running_role(engine)
    engine.intake.subagent_start(parent_session_id='sid', child_session_id='child')
    assert not command('pause ' + w.id).startswith('Error:')
    engine.compression_tip = lambda sid: 'child2' if sid == 'child' else None
    assert engine.intake.flight_for('child2', KEY) is flight
    assert 'child' not in engine.intake.children and engine.intake.children['child2'] is flight
    assert_wake_limits(engine, w, sid='child2')
    engine.intake.subagent_start(parent_session_id='child2', child_session_id='grandchild')
    assert engine.intake.children['grandchild'] is flight
    engine.intake.on_session_end(session_id='sid', turn_id='turn', completed=True)
    assert not engine.intake.children


def test_compressed_child_start_follows_before_mapping_a_grandchild(engine, command):
    w, flight = running_role(engine)
    engine.intake.subagent_start(parent_session_id='sid', child_session_id='child')
    assert not command('pause ' + w.id).startswith('Error:')
    engine.compression_tip = lambda sid: 'child2' if sid == 'child' else None
    engine.intake.subagent_start(parent_session_id='child2', child_session_id='grandchild')
    assert engine.intake.children['grandchild'] is flight
    assert_wake_limits(engine, w, sid='grandchild')


def test_unrelated_session_is_not_taken_by_a_child_mapping(engine, command):
    w, flight = running_role(engine)
    engine.intake.subagent_start(parent_session_id='sid', child_session_id='child')
    assert not command('pause ' + w.id).startswith('Error:')
    engine.compression_tip = lambda sid: None
    assert engine.intake.flight_for('stranger', KEY) is None
    assert set(engine.intake.children) == {'child'}
