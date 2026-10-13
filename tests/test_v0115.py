"""Unsupported event filters and truthful first-fire holds (SPEC-F4)."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from heartbeat2.model import Session
from heartbeat2.poller import Poller
from heartbeat2.tool import PARAMETERS
from test_engine import engine, session, KEY, arm
from test_v0114 import command, create, local, night, receipt


DETAIL = ('trigger.event is not supported; choose the event with trigger.kind '
          '(for example kanban_completed)')


@pytest.mark.parametrize('event', ['completed', '', None])
def test_create_rejects_event_without_storing(engine, event):
    result = create(engine, {'kind':'kanban_completed','event':event})
    assert result.get('error') == 'invalid_request', result
    assert result['detail'] == DETAIL
    assert not engine.store.list_wakes(KEY)


def test_update_rejects_event_without_changing_wake(engine):
    w = arm(engine, 'kanban_completed', next_due_at=None)
    result = engine.tool.handle({'action':'update','id':w.id,
        'trigger':{'kind':'kanban_completed','event':'completed'}}, session_id='sid')
    assert result.get('error') == 'invalid_request', result
    assert result['detail'] == DETAIL
    assert engine.store.get_wake(w.id) == w


def test_trigger_schema_drops_event_without_adding_filters():
    fields = PARAMETERS['properties']['trigger']['properties']
    assert 'event' not in fields
    assert 'task_id' not in fields and 'board' not in fields


@pytest.mark.parametrize('status', ['active','paused','proposed'])
def test_start_fails_legacy_watcher_once(engine, command, status):
    w = arm(engine, 'kanban_completed', status=status, next_due_at=None,
            trigger_json={'event':'completed','task_id':'task','board':'board'})
    engine.store.add_occurrence(w.id, 'legacy-pending')
    assert engine.acquire()
    changed = engine.store.get_wake(w.id)
    assert changed.status == 'failed' and changed.status_reason == 'invalid_trigger_event'
    assert changed.trigger_json == w.trigger_json  # Never broaden the filter.
    occurrence = engine.store.get_occurrence(w.id, 'legacy-pending')
    assert occurrence.state == 'superseded' and occurrence.error == 'invalid_trigger_event'
    expected = [f'A watcher ({w.id}) used an unsupported event filter and could never fire, so it was stopped. Ask me to set it up again.']
    assert json.loads(engine.store.get_meta('notice:'+KEY)) == expected
    assert 'Status reason: invalid_trigger_event' in command('why '+w.id)
    engine.release()
    restarted = Poller(engine.ctx, engine.store, engine.policy, engine.clock)
    try:
        assert restarted.acquire()
        assert engine.store.get_wake(w.id) == changed
        assert engine.store.get_occurrence(w.id, 'legacy-pending') == occurrence
        assert json.loads(engine.store.get_meta('notice:'+KEY)) == expected
    finally:
        restarted.close()


def test_sweep_queues_one_notice_per_session_and_keeps_healthy_rows(engine):
    other = 'agent:telegram:dm:other'
    engine.store.upsert_session(Session(other, 'telegram', 'dm', 'other'))
    invalid = [arm(engine, 'reaction', trigger_json={'event':'reaction'}) for _ in range(2)]
    other_wake = engine.store.create_wake(replace(invalid[0], id='w_other', session_key=other))
    healthy = arm(engine, 'kanban_completed', trigger_json={'task_id':'task'})
    terminal = arm(engine, 'reaction', status='cancelled', trigger_json={'event':'reaction'})
    assert engine.acquire()
    assert all(engine.store.get_wake(w.id).status_reason == 'invalid_trigger_event'
               for w in [*invalid, other_wake])
    for key in (KEY, other):
        assert len(json.loads(engine.store.get_meta('notice:'+key))) == 1
    assert engine.store.get_wake(healthy.id) == healthy
    assert engine.store.get_wake(terminal.id) == terminal


@pytest.mark.parametrize('filters', [{'task_id':'task'}, {'board':'board'},
                                     {'task_id':'task','board':'board'}])
def test_stored_task_and_board_filters_still_fire(engine, filters):
    w = arm(engine, 'kanban_completed', next_due_at=None, trigger_json=filters)
    mismatches = [{**{'task_id':'task','board':'board'}, field:'other'} for field in filters]
    for payload in mismatches:
        engine.intake.event('kanban_completed', session_id='sid', **payload)
        engine.tick()
        assert not engine.ctx.calls and not engine.store.list_occurrences(w.id)
    engine.intake.event('kanban_completed', session_id='sid', task_id='task', board='board')
    engine.tick()
    assert len(engine.ctx.calls) == 1
    occurrence, = engine.store.list_occurrences(w.id)
    assert occurrence.state == 'claimed'
    assert occurrence.payload_json == [{'task_id':'task','board':'board'}]


def test_owner_silence_quiet_hours_can_make_expiry_impossible(engine):
    now = night(engine, 21)
    session(engine, last_human_at=now)
    result = create(engine, {'kind':'owner_silent','after':'2h'}, until={'expires':'in 3h'})
    assert result.get('error') == 'invalid_request', result
    assert 'due after expiry' in result['detail']
    assert not engine.store.list_wakes(KEY)


def test_owner_silence_receipt_reports_quiet_release(engine):
    now = night(engine, 21)
    session(engine, last_human_at=now)
    result = create(engine, {'kind':'owner_silent','after':'2h'})
    assert result.get('ok'), result
    assert f'Quiet hours hold it until {local(engine,now+11*3600)}.' in receipt(result)


@pytest.mark.parametrize('kind', ['at','every','owner_silent','kanban_completed'])
def test_active_spacing_can_make_expiry_impossible(engine, kind):
    engine.policy = replace(engine.policy, wake_spacing_minutes=120)
    session(engine, last_wake_turn_at=engine.clock.now())
    trigger = {'kind':kind, **({'at':'in 1h'} if kind=='at' else
               {'every':'1h'} if kind=='every' else {'after':'1h'} if kind=='owner_silent'
               else {'deadline':'in 1h'})}
    result = create(engine, trigger, until={'expires':'in 90m'})
    assert result.get('error') == 'invalid_request', result
    assert 'after expiry' in result['detail']
    assert not engine.store.list_wakes(KEY)


@pytest.mark.parametrize('kind', ['at','owner_silent','kanban_completed'])
def test_spacing_precedes_quiet_adjustment_and_receipt(engine, kind):
    now = night(engine, 21)
    engine.policy = replace(engine.policy, wake_spacing_minutes=120)
    session(engine, last_wake_turn_at=now)
    trigger = {'kind':kind, **({'at':'in 30m'} if kind=='at' else
               {'after':'30m'} if kind=='owner_silent' else {'deadline':'in 30m'})}
    result = create(engine, trigger, until={'expires':'in 4h'})
    assert result.get('error') == 'invalid_request', result
    result = create(engine, trigger)
    assert result.get('ok'), result
    assert f'Quiet hours hold it until {local(engine,now+11*3600)}.' in receipt(result)


@pytest.mark.parametrize('kind', ['at','owner_silent','kanban_completed'])
def test_point_outside_both_windows_is_unchanged(engine, kind):
    now = night(engine, 8)
    engine.policy = replace(engine.policy, wake_spacing_minutes=10)
    session(engine, last_wake_turn_at=now-3600)
    trigger = {'kind':kind, **({'at':'in 1h'} if kind=='at' else
               {'after':'1h'} if kind=='owner_silent' else {'deadline':'in 1h'})}
    result = create(engine, trigger, until={'expires':'in 2h'})
    assert result.get('ok'), result
    w = engine.store.get_wake(result['wake']['id'])
    assert w.expires_at == now+7200 and engine.tool.feasible(w) == w
    if kind != 'owner_silent':
        assert (w.next_due_at if kind=='at' else w.deadline_at) == now+3600
    assert 'Quiet hours hold' not in receipt(result)


def test_builtin_silence_still_bypasses_feasibility(engine):
    now = night(engine, 21)
    engine.policy = replace(engine.policy, wake_spacing_minutes=120)
    session(engine, last_wake_turn_at=now)
    w = arm(engine, 'owner_silent', trigger_json={'builtin':True,'after':'2h'}, expires_at=now+60)
    assert engine.tool.feasible(w) == w


def test_version_changelog_and_skill():
    root = Path(__file__).resolve().parents[1]
    assert 'version: "0.1.15"' in (root/'plugin.yaml').read_text()
    changelog = (root/'CHANGELOG.md').read_text()
    assert changelog.count('## 0.1.15 — 2026-10-14') == 1
    section = changelog.split('## 0.1.15 — 2026-10-14')[1].split('## 0.1.14')[0]
    assert '#57' in section and '#68' in section and 'owner_asked' in section
    skill = (root/'skills/heartbeat2-usage/SKILL.md').read_text()
    assert ('When the owner asks for a one-time reminder in their current message, '
            'set `owner_asked: true` on the `at` create; never set it for your own follow-ups.') in skill
    assert 'The receipt is what to repeat.' in skill


@pytest.mark.parametrize('kind,trigger', [
    ('at', {'at':'in 1h','event':'stray'}),
    ('every', {'every':'1h','event':'stray'}),
    ('owner_silent', {'after':'1h','event':'stray'}),
])
def test_sweep_ignores_stray_event_key_on_non_event_kinds(engine, kind, trigger):
    w = arm(engine, kind, trigger_json=trigger)
    assert engine.acquire()
    assert engine.store.get_wake(w.id) == w
    assert engine.store.get_meta('notice:'+KEY) in (None, '', '[]')
    result = engine.tool.handle({'action':'update','id':w.id,'title':'renamed'}, session_id='sid')
    assert result.get('ok'), result


@pytest.mark.parametrize('bypass', ['spacing','quiet'])
def test_deadline_free_event_watcher_counts_holds(engine, bypass):
    if bypass == 'spacing':
        engine.policy = replace(engine.policy, wake_spacing_minutes=10)
        session(engine, last_wake_turn_at=engine.clock.now()-5*60)
    else:
        night(engine, 23)
    result = create(engine, {'kind':'kanban_completed'}, until={'expires':'in 2m'})
    assert result.get('error') == 'invalid_request', result
    assert 'after expiry' in result['detail']
    assert not engine.store.list_wakes(KEY)
    assert create(engine, {'kind':'kanban_completed'}, until={'expires':'in 30m' if bypass=='spacing' else 'in 10h'}).get('ok')


def test_sweep_leaves_started_turn_for_restart_settlement(engine):
    w = arm(engine, 'kanban_completed', next_due_at=None, trigger_json={'event':'completed'})
    engine.store.add_occurrence(w.id, 'legacy-started')
    engine.store.connection.execute("UPDATE occurrences SET state='started' WHERE wake_id=? AND dedupe_key=?", (w.id, 'legacy-started'))
    assert engine.acquire()
    assert engine.store.get_wake(w.id).status_reason == 'invalid_trigger_event'
    assert engine.store.get_occurrence(w.id, 'legacy-started').error != 'invalid_trigger_event'


@pytest.mark.parametrize('hold', ['spacing','quiet'])
def test_deadline_free_event_watcher_receipt_reports_holds(engine, hold):
    if hold == 'spacing':
        engine.policy = replace(engine.policy, wake_spacing_minutes=10)
        session(engine, last_wake_turn_at=engine.clock.now()-5*60)
        expected = f'Wake spacing holds it until {local(engine, engine.clock.now()+5*60)}.'
    else:
        now = night(engine, 23)
        expected = f'Quiet hours hold it until {local(engine, now+9*3600)}.'
    result = create(engine, {'kind':'kanban_completed'})
    assert result.get('ok'), result
    assert expected in receipt(result)
