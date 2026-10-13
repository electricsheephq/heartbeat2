"""O12: real command/tool/poller paths with synthetic sessions and probe matches."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from heartbeat2 import cache, commands, governor
from heartbeat2.policy import is_owner
from heartbeat2.schedule import time_due
from test_engine import engine, arm, session, KEY  # noqa: F401

ROUTE = 'openai|gpt-6.1-sol|api.openai.com'
COMMAND = {'kind': 'command', 'command': ['gh', 'pr', 'checks', '1']}


@pytest.fixture
def runtime(engine, monkeypatch):
    monkeypatch.setattr(commands, 'session_env', lambda n: KEY if n == 'HERMES_SESSION_KEY' else '')
    return SimpleNamespace(get_engine=lambda: engine)


def owner_add(engine, runtime, text='every 1h Owner reminder'):
    output = commands.handle('add ' + text, runtime)
    assert not output.startswith('Error:'), output
    wake, = engine.store.list_wakes(KEY)
    assert wake.creator == 'user' and not wake.agent_edited
    return wake


def update(engine, wake, **args):
    result = engine.tool.handle({'action': 'update', 'id': wake.id, **args}, session_id='sid')
    assert result.get('ok'), result
    return result, engine.store.get_wake(wake.id)


@pytest.mark.parametrize('text,new_note', [
    pytest.param('every 1h Owner reminder', {'note': 'Agent loop'}, id='T1-recurring-note'),
    pytest.param('in 2h Owner one-shot', {}, id='T2-one-shot'),
])
def test_agent_edit_gets_agent_floor_immediately(engine, runtime, text, new_note):
    wake = owner_add(engine, runtime, text)
    result, changed = update(engine, wake, trigger={'kind': 'every', 'every': '15m'}, **new_note)
    assert changed.agent_edited and not is_owner(changed)
    assert changed.base_interval_s == changed.eff_interval_s == 3300
    assert engine.policy.interval_floor(changed) == 3300
    assert 'agent_ungated_floor' in result['clamped']['every']
    assert changed.next_due_at == engine.clock.now() + 3300
    assert changed.expires_at == wake.expires_at  # Owner lifetime remains the owner's.


def test_T3_agent_edited_owner_snaps_at_update_and_each_tick(engine, runtime):
    engine.policy = replace(engine.policy, agent_ungated_floor_minutes=15)
    session(engine, last_route=ROUTE, last_cache_touch_at=engine.clock.now())
    wake = owner_add(engine, runtime)
    result, changed = update(engine, wake, trigger={'kind': 'every', 'every': '30m'})
    ttl, _ = cache.ttl_for_session(engine.store, engine.store.get_session(KEY))
    assert ttl == 1800
    edge = ttl - max(30, .05 * ttl)
    assert changed.eff_interval_s == edge
    assert 'prompt_cache' in result['clamped']['eff_interval_s']
    assert changed.next_due_at == engine.clock.now() + edge
    assert engine.timing(changed)['cache_ttl'] == ttl
    engine.clock.advance(edge)
    engine.jitter = lambda: 1
    engine.tick()
    assert len(engine.ctx.calls) == 1
    assert engine.store.get_wake(wake.id).next_due_at == engine.clock.now() + .9 * edge
    # Exercise schedule's predicate independently of Poller.timing's filter.
    due = time_due(changed, now=engine.clock.now(), catch_up_s=21600,
                   cache_ttl=ttl, minimum_s=900, jitter_unit=1)
    assert due.next_due_at == engine.clock.now() + .9 * edge


@pytest.mark.parametrize('interval', ['15m', '30m'])
def test_T4_untouched_owner_keeps_interval_and_jitter(engine, runtime, interval):
    engine.policy = replace(engine.policy, agent_ungated_floor_minutes=15)
    session(engine, last_route=ROUTE, last_cache_touch_at=engine.clock.now())
    wake = owner_add(engine, runtime, 'every ' + interval + ' Owner reminder')
    seconds = 900 if interval == '15m' else 1800
    assert wake.eff_interval_s == seconds and is_owner(wake)
    assert engine.timing(wake)['cache_ttl'] is None
    assert cache.interval(wake, seconds, 1800, 900) == seconds
    engine.clock.advance(seconds)
    engine.jitter = lambda: 1
    engine.tick()
    assert engine.store.get_wake(wake.id).next_due_at == engine.clock.now() + 1.1 * seconds
    due = time_due(wake, now=engine.clock.now(), catch_up_s=21600,
                   cache_ttl=1800, minimum_s=900, jitter_unit=1)
    assert due.next_due_at == engine.clock.now() + 1.1 * seconds


def conversion(engine, runtime, *, control='watch', extra=None, trigger=None):
    wake = owner_add(engine, runtime)
    output = commands.handle(control + ' ' + wake.id, runtime)
    assert not output.startswith('Error:'), output
    _, changed = update(engine, wake, trigger=trigger or COMMAND, **(extra or {}))
    assert changed.status == 'paused'  # Update alone never resumes the wake.
    return changed


@pytest.mark.parametrize('kind', ['command', 'file', 'subagent_done'])
def test_T5_requested_watcher_stays_owner_and_match_fires(engine, runtime, tmp_path, kind):
    path = tmp_path / 'watched.log'
    path.write_text('fixture')
    trigger = COMMAND if kind == 'command' else {'kind': kind, **({'path': str(path)} if kind == 'file' else {})}
    changed = conversion(engine, runtime, trigger=trigger)
    assert not changed.agent_edited and is_owner(changed)
    assert changed.status_reason == ''
    assert engine.tool.handle({'action': 'resume', 'id': changed.id}, session_id='sid')['ok']
    session(engine, unanswered_count=engine.policy.max_unanswered)
    engine.probe_runner = lambda _: {'fire': True, 'identity': 'match', 'message': 'Changed'}
    if kind == 'subagent_done':
        engine.store.add_event(kind, 'match', {'message': 'Changed'}, session_key=KEY)
    engine.tick()
    occurrence, = engine.store.list_occurrences(changed.id)
    assert occurrence.state == 'claimed' and occurrence.held_since is None
    assert len(engine.ctx.calls) == 1


def test_T6_note_edit_revokes_requested_conversion_and_match_is_held(engine, runtime):
    changed = conversion(engine, runtime, extra={'note': 'Agent instructions'})
    assert changed.agent_edited and not is_owner(changed)
    assert engine.tool.handle({'action': 'resume', 'id': changed.id}, session_id='sid')['ok']
    session(engine, unanswered_count=engine.policy.max_unanswered)
    engine.probe_runner = lambda _: {'fire': True, 'identity': 'match', 'message': 'Changed'}
    engine.tick()
    occurrence, = engine.store.list_occurrences(changed.id)
    assert occurrence.state == 'pending' and occurrence.held_reason == 'held_unanswered'
    assert engine.ctx.calls == []


def test_T7_token_is_single_use(engine, runtime):
    changed = conversion(engine, runtime)
    assert changed.status_reason == ''
    _, second = update(engine, changed, trigger={**COMMAND, 'check_every': '2m'})
    assert second.agent_edited and not is_owner(second)


def test_T8_plain_pause_gives_no_owner_conversion_token(engine, runtime):
    changed = conversion(engine, runtime, control='pause')
    assert changed.agent_edited and not is_owner(changed)


def test_T9_only_watch_mints_token(engine, runtime):
    wake = owner_add(engine, runtime)
    assert not commands.handle('pause ' + wake.id, runtime).startswith('Error:')
    assert engine.store.get_wake(wake.id).status_reason == 'command'
    assert not commands.handle('resume ' + wake.id, runtime).startswith('Error:')
    assert engine.store.get_wake(wake.id).status_reason == ''
    assert engine.tool.handle({'action': 'pause', 'id': wake.id}, session_id='sid')['ok']
    assert engine.store.get_wake(wake.id).status_reason == 'agent'
    assert engine.tool.handle({'action': 'resume', 'id': wake.id}, session_id='sid')['ok']
    _, changed = update(engine, wake, note='Agent note')
    assert changed.status_reason == ''
    governor.pause(engine, changed, 'noop')
    assert engine.store.get_wake(wake.id).status_reason == 'governor:noop'
    assert not commands.handle('watch ' + wake.id, runtime).startswith('Error:')
    assert engine.store.get_wake(wake.id).status_reason == 'watch_requested'
    assert not commands.handle('resume ' + wake.id, runtime).startswith('Error:')
    assert engine.store.get_wake(wake.id).status_reason == ''


@pytest.mark.parametrize('extra', [
    {'until': {'expires': 'in 1d'}}, {'until': {'max_fires': 3}},
    {'until': {'owner_replies': True}}, {'title': 'Agent title'}, {'urgent': True},
])
def test_requested_conversion_with_other_edits_revokes_owner(engine, runtime, extra):
    changed = conversion(engine, runtime, extra=extra)
    assert changed.agent_edited and changed.status_reason == ''


def test_non_watcher_update_consumes_token_and_gets_agent_floor(engine, runtime):
    changed = conversion(engine, runtime, trigger={'kind': 'every', 'every': '15m'})
    assert changed.agent_edited and changed.status_reason == ''
    assert changed.eff_interval_s == 3300


def test_noop_update_consumes_token_without_changing_owner_status(engine, runtime):
    wake = owner_add(engine, runtime)
    assert not commands.handle('watch ' + wake.id, runtime).startswith('Error:')
    _, changed = update(engine, wake)
    assert changed.status_reason == '' and not changed.agent_edited
    _, converted = update(engine, changed, trigger=COMMAND)
    assert converted.agent_edited


def test_token_does_not_restore_already_revoked_owner_status(engine, runtime):
    changed = conversion(engine, runtime, extra={'note': 'Agent instructions'})
    assert not commands.handle('watch ' + changed.id, runtime).startswith('Error:')
    _, changed = update(engine, changed, trigger={**COMMAND, 'check_every': '2m'})
    assert changed.agent_edited and changed.status_reason == ''


def test_builtin_is_agent_authored_only_after_edit(engine):
    from heartbeat2.policy import agent_authored
    wake = arm(engine, 'every', creator='user', trigger_json={'kind': 'every', 'builtin': True})
    assert not agent_authored(wake)
    assert agent_authored(replace(wake, agent_edited=True))
    assert agent_authored(replace(wake, creator='agent'))


@pytest.mark.parametrize('text,moved', [
    pytest.param('every 15m Owner check', True, id='raised-floor-moves-pending-due'),
    pytest.param('every 2h Owner check', False, id='slower-owner-cadence-keeps-due'),
    pytest.param('in 2h Owner one-shot', False, id='one-shot-keeps-due'),
])
def test_note_only_agent_edit_applies_floor_to_pending_occurrence(engine, runtime, text, moved):
    wake = owner_add(engine, runtime, text)
    engine.clock.advance(60)
    _, changed = update(engine, wake, note='Agent note')
    assert changed.agent_edited
    if not moved:
        assert changed.next_due_at == wake.next_due_at
        return
    assert changed.eff_interval_s == 3300
    assert changed.next_due_at == engine.clock.now() + 3300
    engine.clock.advance(900)  # The owner's old 15-minute occurrence no longer fires.
    engine.tick()
    assert engine.ctx.calls == []
