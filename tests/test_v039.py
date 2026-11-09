"""Owner-only manual occurrences, rollback previews and consistent owner views."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from heartbeat2 import cache, commands, governor
from heartbeat2.model import FINISHED_WAKE_STATUSES, Occurrence
from heartbeat2.tool import PARAMETERS, filter_text
from test_engine import engine, arm, pending, start, session, KEY


@pytest.fixture
def command(engine, monkeypatch):
    bindings = {'HERMES_SESSION_KEY': KEY, 'HERMES_SESSION_PLATFORM': 'telegram',
                'HERMES_SESSION_CHAT_TYPE': 'dm', 'HERMES_SESSION_CHAT_ID': 'unit',
                'HERMES_SESSION_ID': 'sid'}
    monkeypatch.setattr(commands, 'session_env', lambda name: bindings.get(name, ''))
    return lambda text: commands.handle(text, SimpleNamespace(get_engine=lambda: engine))


def snapshot(e):
    return tuple(e.store.connection.iterdump())


def priced_session(e):
    session(e, last_route='openai|gpt-6.1-sol|api.openai.com',
            last_cache_touch_at=e.clock.now(), last_prompt_tokens=50000)


def test_run_queues_and_tick_uses_gate(engine, command, monkeypatch):
    e = engine
    w = arm(e, 'every', next_due_at=e.clock.now()+3600)
    signals, gates = [], []
    monkeypatch.setattr(e, 'signal', lambda: signals.append(True))
    gate = e.gate
    def record(wake, occurrence, bound):
        gates.append(occurrence.dedupe_key)
        return gate(wake, occurrence, bound)
    monkeypatch.setattr(e, 'gate', record)
    assert command('run '+w.id).startswith('Queued '+w.id+':')
    o, = e.store.list_occurrences(w.id)
    assert o.dedupe_key == f'r:{e.clock.now():.17g}' and o.state == 'pending'
    assert o.payload_json == [{'message': 'The owner asked to run this now (/pulse run).'}]
    assert signals == [True]
    e.tick()
    assert gates == [o.dedupe_key]
    text, key = e.ctx.calls[0]
    assert key == KEY and 'The owner asked to run this now (/pulse run).' in text
    assert 'late by' not in text
    assert e.store.get_occurrence(w.id, o.dedupe_key).state == 'claimed'
    start(e)
    assert e.store.get_occurrence(w.id, o.dedupe_key).state == 'started'


@pytest.mark.parametrize('state', ['pending', 'claimed', 'started'])
def test_run_does_not_duplicate_in_flight(engine, command, state):
    e = engine
    w = arm(e, 'every', next_due_at=e.clock.now()+3600)
    assert command('run '+w.id).startswith('Queued ')
    o, = e.store.list_occurrences(w.id)
    if state != 'pending':
        assert e.store.claim(w.id, o.dedupe_key, epoch=e.epoch, pid=1)
        if state == 'started': assert e.store.start(w.id, o.dedupe_key, attempt=1)
    before = snapshot(e)
    assert command('run '+w.id) == (f'Already queued: {w.id} runs on the next check.' if state == 'pending' else f'{w.id} is running now.')
    assert snapshot(e) == before


@pytest.mark.parametrize('status,kind,other,expected', [
    ('active', 'at', False, 'a one-time reminder fires once at its time; preview it with /pulse test'),
    ('proposed', 'every', False, 'approve it first with /pulse ok'),
    ('paused', 'every', False, 'resume it first with /pulse resume'),
    *[(status, 'every', False, 'it has finished') for status in sorted(FINISHED_WAKE_STATUSES)],
    ('active', 'every', True, 'Error: unknown item in this chat.'),
])
def test_run_refusals_write_nothing(engine, command, status, kind, other, expected):
    w = arm(engine, kind, status=status)
    if other: w = engine.store.create_wake(replace(w, id='w_other', session_key='other'))
    before = snapshot(engine)
    assert expected in command('run '+w.id)
    assert snapshot(engine) == before


@pytest.mark.parametrize('action', ['run', 'test'])
@pytest.mark.parametrize('rest', ['', 'all', 'one two', 'one for 3h', 'missing'])
def test_single_chat_item_required(engine, command, action, rest):
    before = snapshot(engine)
    assert command(action+' '+rest) == ('Error: unknown item in this chat.' if rest == 'missing' else f'Error: {action} expects one item id')
    assert snapshot(engine) == before


@pytest.mark.parametrize('kind', ['every', 'reaction'])
def test_run_quiet_hours_matches_scheduled_gate(engine, command, kind):
    e = engine
    e.policy = replace(e.policy, quiet_hours='00:00-23:59')
    w = arm(e, kind, creator='user', next_due_at=e.clock.now()+3600)
    bound = e.store.get_session(KEY)
    scheduled = Occurrence(w.id, f't:{e.clock.now():.17g}')
    expected = e.gate(w, scheduled, bound)
    assert expected[0] == 'quiet_hours'
    assert command('run '+w.id).startswith('Queued ')
    manual, = e.store.list_occurrences(w.id)
    assert e.gate(w, manual, bound) == expected
    e.tick()
    actual = e.store.get_occurrence(w.id, manual.dedupe_key)
    assert (actual.state, actual.error) == ('pending' if expected[1] else 'skipped', expected[0])
    if expected[1]: assert float(e.store.get_meta('not_before:'+actual.id)) == expected[2]
    assert not e.ctx.calls


def test_pending_manual_run_blocks_scheduled_duplicate(engine, command):
    e = engine
    session(e, busy_since=e.clock.now(), busy_epoch=e.epoch)
    w = arm(e, 'every', next_due_at=e.clock.now()+60)
    command('run '+w.id)
    e.clock.advance(60)
    e.tick()
    o, = e.store.list_occurrences(w.id)
    assert o.dedupe_key.startswith('r:') and o.state == 'pending' and o.error == 'busy'
    assert e.store.get_wake(w.id).next_due_at > e.clock.now()


@pytest.mark.parametrize('kind,status', [('every', 'active'), ('every', 'proposed'),
    ('every', 'paused'), ('at', 'active'), ('reaction', 'paused')])
def test_preview_is_read_only_for_nonfinished_wakes(engine, command, kind, status):
    e = engine
    w = arm(e, kind, status=status, next_due_at=e.clock.now()+3600)
    before = snapshot(e)
    out = command('test '+w.id)
    assert out.startswith(f'Test {w.id} (dry run: nothing sent, no model call)\n')
    assert ('Would run now: yes' if status == 'active' else 'Would run now: no — wake is '+('paused' if kind == 'reaction' else 'not active')) in out
    assert 'Wake text:\n' in out
    assert ('Next: '+commands._time(e,w.next_due_at) if kind != 'reaction' else 'Next: on reaction events') in out
    assert snapshot(e) == before and not e.ctx.calls


def test_preview_quiet_hours_is_held(engine, command):
    e = engine
    e.policy = replace(e.policy, quiet_hours='00:00-23:59')
    w = arm(e, 'at', creator='user')
    before = snapshot(e)
    out = command('test '+w.id)
    assert 'Would run now: no — quiet hours until ' in out and '(held; it would wait)' in out
    assert snapshot(e) == before


def test_preview_rolls_back_gate_and_refresh_metadata(engine, command):
    e = engine
    w = arm(e, 'every')
    health = {'until': e.clock.now()-1, 'trial': 'gone/r:0'}
    e.store.set_meta('provider_health', json.dumps(health))
    def refresh():
        e.store.set_meta('preview_refresh', 'temporary refresh notice')
    before = snapshot(e)
    # Keep refresh policy well formed while making its metadata write observable.
    e.refresh_policy = lambda: (refresh(), e.policy)[1]
    out = command('test '+w.id)
    assert 'Would run now: yes' in out
    assert snapshot(e) == before


@pytest.mark.parametrize('kind,trigger,next_label', [
    ('file', {'check_every': 60, 'path': 'not-read'}, 'every 60'),
    ('url', {'check_every': 90, 'url': 'https://example.invalid'}, 'every 90'),
    ('command', {'check_every': 120, 'command': ['false']}, 'every 120'),
    ('reaction', {}, 'on reaction events'),
])
def test_preview_never_runs_watchers(engine, command, monkeypatch, kind, trigger, next_label):
    e = engine
    w = arm(e, kind, trigger_json=trigger)
    monkeypatch.setattr(e, 'probe_runner', lambda _: pytest.fail('preview ran a probe'))
    before = snapshot(e)
    out = command('test '+w.id)
    assert 'Next: '+next_label in out and 'Wake text:\n' in out
    assert snapshot(e) == before and not e.ctx.calls


def test_preview_cache_and_clipped_wake_text(engine, command, monkeypatch):
    e = engine
    priced_session(e)
    w = arm(e, 'every', next_due_at=e.clock.now()+7200)
    assert e.store.cas_wake(w.id, 'active', note='Long note. '*180)
    w = e.store.get_wake(w.id)
    before = snapshot(e)
    monkeypatch.setattr(cache, 'cost', lambda route, **kw: .12 if kw.get('written') else .01)
    out = command('test '+w.id)
    assert '\n'+e.tool.cache_result(w)+'\n' in out
    text = out.split('Wake text:\n', 1)[1]
    assert len(text) <= 1500 and text.endswith('…')
    assert snapshot(e) == before


@pytest.mark.parametrize('status', sorted(FINISHED_WAKE_STATUSES))
def test_preview_refuses_finished(engine, command, status):
    w = arm(engine, status=status)
    before = snapshot(engine)
    assert command('test '+w.id) == 'Error: it has finished'
    assert snapshot(engine) == before


def test_preview_refuses_other_chat(engine, command):
    w = engine.store.create_wake(replace(arm(engine), id='w_other', session_key='other'))
    before = snapshot(engine)
    assert command('test '+w.id) == 'Error: unknown item in this chat.'
    assert snapshot(engine) == before


@pytest.mark.parametrize('action', ['run', 'test'])
def test_commands_are_never_agent_actions(engine, action):
    assert action not in PARAMETERS['properties']['action']['enum']
    assert engine.tool.handle({'action': action})['error'] == 'unknown_action'


def test_cache_cold_price_once_warm_price_and_cold(engine, monkeypatch):
    e = engine
    priced_session(e)
    e.policy = replace(e.policy, min_interval_minutes=1)
    monkeypatch.setattr(cache, 'cost', lambda route, **kw: .12 if kw.get('written') else .01)
    cold = e.tool.cache_result(arm(e, 'at', creator='user', next_due_at=e.clock.now()+7200))
    assert '120m cold (~$0.12) · next warm ≤28.5m' in cold
    assert cold.count('$0.12') == 1
    warm = e.tool.cache_result(arm(e, 'at', creator='user', next_due_at=e.clock.now()+60))
    assert 'warm (~$0.01) · cold ~$0.12' in warm


def test_owner_and_governor_use_priced_cache_outside_transaction(engine, command, monkeypatch):
    e = engine
    priced_session(e)
    calls = []
    def cost(route, **kw):
        assert not e.store.connection.in_transaction
        calls.append(kw)
        return .12 if kw.get('written') else .01
    monkeypatch.setattr(cache, 'cost', cost)
    out = command('add in 2h Check the release')
    w, = e.store.list_wakes(KEY)
    assert out.splitlines()[-2] == e.tool.cache_result(w)
    assert '$0.12' in out and calls
    calls.clear()
    with e.store.atomic():
        governor.pause(e, w, 'noop')
        assert not calls
    assert calls
    assert e.store.get_meta('card:'+w.id) == 'noop · '+e.tool.cache_result(e.store.get_wake(w.id))


def test_governor_deferred_price_is_rolled_back_or_fenced(engine, monkeypatch):
    e = engine
    priced_session(e)
    calls = []
    monkeypatch.setattr(cache, 'cost', lambda *a, **kw: calls.append(True) or .12)
    w = arm(e, 'at', next_due_at=e.clock.now()+7200)
    with e.store.atomic():
        with pytest.raises(ValueError), e.store.atomic():
            governor.pause(e, w, 'noop')
            raise ValueError('rollback')
        assert not e.store.get_meta('card:'+w.id)
    assert not calls and e.store.get_wake(w.id).status == 'active'
    with e.store.atomic():
        governor.pause(e, w, 'noop')
        paused = e.store.get_wake(w.id)
        e.store.cas_wake(w.id, 'paused', expected_version=paused.version, status='active')
        e.store.set_meta('card:'+w.id, '')
    assert e.store.get_meta('card:'+w.id) == ''


@pytest.mark.parametrize('kind,trigger,expected', [
    ('kanban_completed', {'task_id': 't1', 'board': 'release'}, ' for task t1 on board release'),
    ('kanban_blocked', {'task_id': 't1', 'board': 'release'}, ' for task t1 on board release'),
    ('kanban_worker_exited', {'task_id': 't1', 'board': 'release'}, ' for task t1 on board release'),
    ('reaction', {'message_id': '90', 'emojis': ['👍', '✅']}, ' on message 90 for 👍 ✅'),
])
def test_filters_visible_before_approval_and_in_owner_views(engine, command, kind, trigger, expected):
    e = engine
    w = arm(e, kind, trigger_json=trigger, expires_at=None)
    assert filter_text(w) == expected
    assert expected+'.' in e.tool.receipt(w, e.store.get_session(KEY))
    proposed = replace(w, status='proposed')
    assert f'It would wait for {kind} events{expected}.' in e.tool.receipt(proposed, e.store.get_session(KEY))
    assert expected in command('list')
    assert 'Filters: '+expected.strip()+'\n' in command('why '+w.id)


def test_unfiltered_owner_views_keep_existing_text(engine, command):
    e = engine
    w = arm(e, 'reaction', expires_at=None)
    assert filter_text(w) == ''
    assert e.tool.receipt(w, e.store.get_session(KEY)) == 'Waits for reaction events. Wakes are at least 10 min apart, so it can land a few minutes late.'
    assert 'Filters:' not in command('why '+w.id)
    assert 'reaction · next ' in command('list')
