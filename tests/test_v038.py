"""Timed self-rest, kanban filters and truthful priced-spend wording."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from heartbeat2 import commands
from heartbeat2.desk import cost_line
from heartbeat2.tool import PARAMETERS
from test_engine import KEY, arm, engine, pending, start


@pytest.fixture
def command(engine, monkeypatch):
    monkeypatch.setattr(commands, 'session_env', lambda n: KEY if n == 'HERMES_SESSION_KEY' else '')
    return lambda text: commands.handle(text, SimpleNamespace(get_engine=lambda: engine))


def pause(e, wid, **args):
    return e.tool.handle(dict(action='pause', id=wid, **args), session_id='sid')


def timed(e, **changes):
    w = arm(e, 'every', trigger_json={'kind': 'every', 'every': '2h'}, **changes)
    result = pause(e, w.id, pause_for='2h')
    assert result.get('ok'), result
    assert result['wake']['status'] == 'paused' and result['wake']['status_reason'] == 'agent_timed'
    assert float(e.store.get_meta('resume_after:' + w.id)) == e.clock.now() + 7200
    return w


def test_timed_pause_resumes_only_at_its_time(engine):
    w = timed(engine)
    engine.clock.advance(7199)
    commands.resume_timed(engine)
    assert engine.store.get_wake(w.id).status == 'paused'
    engine.clock.advance(1)
    commands.resume_timed(engine)
    resumed = engine.store.get_wake(w.id)
    assert resumed.status == 'active' and resumed.status_reason == ''
    assert resumed.next_due_at > engine.clock.now()
    assert not engine.store.get_meta('resume_after:' + w.id)


def test_owner_pause_without_timer_takes_over(engine, command):
    w = timed(engine)
    assert not command('pause ' + w.id).startswith('Error:')
    assert engine.store.get_wake(w.id).status_reason == 'command'
    assert not engine.store.get_meta('resume_after:' + w.id)
    engine.clock.advance(7201)
    commands.resume_timed(engine)
    assert engine.store.get_wake(w.id).status == 'paused'
    assert 'pause needs an active item' in command('pause ' + w.id)


def test_owner_timer_replaces_agent_timer(engine, command):
    w = timed(engine)
    assert not command(f'pause {w.id} for 1h').startswith('Error:')
    assert engine.store.get_wake(w.id).status_reason == 'command'
    assert float(engine.store.get_meta('resume_after:' + w.id)) == engine.clock.now() + 3600
    engine.clock.advance(3599)
    commands.resume_timed(engine)
    assert engine.store.get_wake(w.id).status == 'paused'
    engine.clock.advance(1)
    commands.resume_timed(engine)
    assert engine.store.get_wake(w.id).status == 'active'


def test_off_keeps_agent_timer_paused(engine, command):
    w = timed(engine)
    command('off')
    assert not engine.store.get_session(KEY).enabled
    engine.clock.advance(7201)
    commands.resume_timed(engine)
    assert engine.store.get_wake(w.id).status == 'paused'


def test_governor_reason_keeps_priority(engine, command):
    w = timed(engine)
    assert engine.store.cas_wake(w.id, 'paused', status_reason='governor:noop')
    assert 'pause needs an active item' in command('pause ' + w.id)
    engine.clock.advance(7201)
    commands.resume_timed(engine)
    assert engine.store.get_wake(w.id).status_reason == 'governor:noop'


@pytest.mark.parametrize('args', [
    {'action': 'list', 'pause_for': '2h'},
    {'action': 'off', 'pause_for': '2h'},
    {'action': 'pause', 'id': 'all', 'pause_for': '2h'},
    {'action': 'pause', 'id': None, 'pause_for': '2h'},
    {'action': 'pause', 'pause_for': ''},
    {'action': 'pause', 'pause_for': '59s'},
    {'action': 'pause', 'pause_for': '15d'},
    {'action': 'pause', 'pause_for': 120},
])
def test_invalid_pause_for_is_refused_without_changes(engine, args):
    w = arm(engine)
    request = {'id': w.id, **args}
    if request['id'] is None: request.pop('id')
    result = engine.tool.handle(request, session_id='sid')
    assert result.get('error') == 'invalid_request', result
    assert engine.store.get_wake(w.id) == w
    assert engine.store.get_session(KEY).enabled


@pytest.mark.parametrize('duration', ['1m', '1d'])
def test_pause_for_uses_inclusive_configured_bounds(engine, duration):
    engine.policy = replace(engine.policy, max_lifetime_days=1)
    w = arm(engine)
    assert pause(engine, w.id, pause_for=duration).get('ok')


def test_pause_for_obeys_configured_lifetime(engine):
    engine.policy = replace(engine.policy, max_lifetime_days=1)
    w = arm(engine)
    assert pause(engine, w.id, pause_for='2d').get('error') == 'invalid_request'


def test_plain_pause_clears_earlier_timer(engine):
    w = timed(engine)
    assert engine.tool.handle(dict(action='resume', id=w.id), session_id='sid').get('ok')
    result = pause(engine, w.id)
    assert result.get('ok') and result['wake']['status_reason'] == 'agent'
    assert not engine.store.get_meta('resume_after:' + w.id)
    engine.clock.advance(7201)
    commands.resume_timed(engine)
    assert engine.store.get_wake(w.id).status == 'paused'


@pytest.mark.parametrize('changes,error', [
    ({'status': 'proposed'}, 'not_pausable'),
    ({'status': 'paused', 'status_reason': 'command'}, 'not_pausable'),
    ({'trigger_json': {'builtin': True, 'own_time': True}}, 'builtin_settings_owned'),
])
def test_timed_pause_keeps_mutation_gate(engine, changes, error):
    w = arm(engine, **changes)
    assert pause(engine, w.id, pause_for='2h').get('error') == error
    assert engine.store.get_wake(w.id) == w


@pytest.mark.parametrize('pause_for', [None, '2h'])
def test_self_pause_preserves_started_response_behavior(engine, pause_for):
    w = arm(engine, 'every')
    engine.dispatch(pending(engine, w))
    start(engine)
    assert pause(engine, w.id, **({'pause_for': pause_for} if pause_for else {})).get('ok')
    result = engine.tool.handle(dict(action='respond', outcome='progress', notify=True,
                                    summary='Resting until the task is ready.'), session_id='sid')
    assert result.get('ok'), result
    flight = engine.intake.flights['sid']
    assert not engine.store.get_occurrence(w.id, flight.dedupe_key).force_silent


def test_timed_pause_display_uses_existing_resume_wording(engine, command):
    w = timed(engine, role=True)
    until = float(engine.store.get_meta('resume_after:' + w.id))
    assert f'paused · resumes {commands._time(engine, until)} or on resume' in command('list')
    assert f'paused · resumes {commands._time(engine, until, compact=True)} or on resume' in command('desk')
    assert not command(f'pause {w.id} for 1h').startswith('Error:')
    until = float(engine.store.get_meta('resume_after:' + w.id))
    assert f'paused · resumes {commands._time(engine, until, compact=True)} or on resume' in command('desk')


@pytest.mark.parametrize('kind', ['kanban_completed', 'kanban_blocked', 'kanban_worker_exited'])
@pytest.mark.parametrize('filters', [{'task_id': 't1'}, {'board': 'release'},
                                     {'task_id': 't1', 'board': 'release'}])
def test_kanban_filters_store_match_and_appear_in_receipt(engine, kind, filters):
    result = engine.tool.handle(dict(action='create', note='Check the task.',
                                    trigger={'kind': kind, **filters}), session_id='sid')
    assert result.get('ok'), result
    w = engine.store.get_wake(result['wake']['id'])
    assert all(w.trigger_json[k] == v for k, v in filters.items())
    for field in filters:
        engine.intake.event(kind, session_id='sid', **{**filters, field: 'other'})
        engine.events()
        assert not engine.store.list_occurrences(w.id)
    engine.intake.event(kind, session_id='sid', **filters)
    engine.events()
    assert len(engine.store.list_occurrences(w.id)) == 1
    if 'task_id' in filters: assert ' for task t1' in result['receipt']
    if 'board' in filters: assert ' on board release' in result['receipt']


@pytest.mark.parametrize('field', ['task_id', 'board'])
@pytest.mark.parametrize('kind', ['file', 'reaction'])
def test_kanban_filters_refused_on_other_kinds(engine, field, kind):
    result = engine.tool.handle(dict(action='create', note='Check.',
                                    trigger={'kind': kind, 'path': 'task.txt', field: 't1'}), session_id='sid')
    assert result.get('error') == 'invalid_request', result
    assert 'task_id and board apply to kanban triggers' in result['detail']
    assert not engine.store.list_wakes(KEY)


def test_schema_and_cost_wording():
    assert PARAMETERS['properties']['pause_for']['type'] == 'string'
    fields = PARAMETERS['properties']['trigger']['properties']
    assert fields['task_id']['type'] == fields['board']['type'] == 'string'
    store = SimpleNamespace(agent_cost_today=lambda *args: {'cost_microusd': 0, 'unpriced_calls': 2})
    assert cost_line(store, KEY, 'UTC', SimpleNamespace(daily_cost_limit_usd=0)) == '2 unpriced calls not counted'
