"""Trigger edits recheck queued watcher data without changing in-flight turns."""
from dataclasses import replace

import pytest

from test_engine import arm, engine, pending
from test_v038 import react


def update(e, w, **changes):
    result = e.tool.handle(dict(action='update', id=w.id, **changes), session_id='sid')
    assert result.get('ok'), result
    return e.store.get_wake(w.id)


def reaction(e, **filters):
    return arm(e, 'reaction', trigger_json={'kind': 'reaction', **filters})


def test_message_filter_change_supersedes_old_and_delivers_new(engine):
    e = engine
    w = reaction(e, message_id='A')
    react(e, 'A')
    old = e.store.list_occurrences(w.id)[0]
    update(e, w, trigger={'kind': 'reaction', 'message_id': 'B'})
    result = e.store.get_occurrence(w.id, old.dedupe_key)
    assert (result.state, result.error, result.force_silent) == ('superseded', 'filter_changed', True)
    e.tick()
    assert not e.ctx.calls
    react(e, 'B')
    queued = e.store.list_occurrences(w.id, state='pending')
    assert len(queued) == 1 and queued[0].payload_json[0]['message_id'] == 'B'
    e.tick()
    assert len(e.ctx.calls) == 1
    assert e.store.get_occurrence(w.id, old.dedupe_key) == result
    assert e.store.get_occurrence(w.id, queued[0].dedupe_key).state == 'claimed'


def test_narrowed_emojis_keep_only_matching_payloads(engine):
    e = engine
    w = reaction(e, emojis=['yes', 'no'])
    react(e, 'A', 'yes')
    react(e, 'B', 'no')
    old = e.store.list_occurrences(w.id)[0]
    assert len(old.payload_json) == 2
    update(e, w, trigger={'kind': 'reaction', 'emojis': ['yes']})
    result = e.store.get_occurrence(w.id, old.dedupe_key)
    assert result.state == 'pending' and not result.force_silent
    assert result.payload_json == [old.payload_json[0]]


@pytest.mark.parametrize('field', ['task_id', 'board'])
def test_kanban_filter_change_supersedes_old_match(engine, field):
    e = engine
    trigger = {'kind': 'kanban_completed', 'task_id': 'task-A', 'board': 'release'}
    w = arm(e, 'kanban_completed', trigger_json=trigger)
    e.intake.event(w.kind, session_id='sid', task_id='task-A', board='release')
    e.events()
    old = e.store.list_occurrences(w.id)[0]
    update(e, w, trigger={**trigger, field: 'other'})
    result = e.store.get_occurrence(w.id, old.dedupe_key)
    assert (result.state, result.error, result.force_silent) == ('superseded', 'filter_changed', True)


def test_cosmetic_update_leaves_pending_occurrence_unchanged(engine):
    e = engine
    w = reaction(e, emojis=['yes'])
    react(e, 'A')
    old = e.store.list_occurrences(w.id)[0]
    update(e, w, note='Check the updated draft.', title='Draft reactions')
    assert e.store.get_occurrence(w.id, old.dedupe_key) == old


@pytest.mark.parametrize('kept', [True, False])
def test_held_last_payload_rechecked_and_match_count_reset(engine, kept):
    e = engine
    e.policy = replace(e.policy, max_model_wakes_per_day=0)
    w = reaction(e, emojis=['yes', 'no'])
    react(e, 'A', 'yes')
    e.dispatch(e.store.list_occurrences(w.id)[0])
    react(e, 'B', 'yes' if kept else 'no')
    old = e.store.list_occurrences(w.id)[0]
    assert old.held_since is not None and old.held_matches == 2
    assert len(old.payload_json) == 1 and old.held_reason == 'held_daily_cap'
    update(e, w, trigger={'kind': 'reaction', 'emojis': ['yes']})
    result = e.store.get_occurrence(w.id, old.dedupe_key)
    if kept:
        assert result.state == 'pending' and result.held_matches == 1
        assert result.payload_json == old.payload_json
        assert (result.held_since, result.held_reason) == (old.held_since, old.held_reason)
    else:
        assert (result.state, result.error, result.force_silent) == ('superseded', 'filter_changed', True)
    assert not e.ctx.calls


@pytest.mark.parametrize('trigger', [{'kind': 'kanban_completed'}, {'kind': 'at', 'at': 'in 1h'}])
def test_watcher_kind_change_supersedes_every_pending_occurrence(engine, trigger):
    e = engine
    w = reaction(e)
    react(e, 'A')
    pending(e, w, 't:manual')
    update(e, w, trigger=trigger)
    rows = e.store.list_occurrences(w.id)
    assert len(rows) == 2
    assert all((o.state, o.error, o.force_silent) == ('superseded', 'trigger_changed', True) for o in rows)


@pytest.mark.parametrize('target_changed', [True, False])
def test_probe_target_change_supersedes_but_cadence_change_keeps_observation(engine, target_changed):
    e = engine
    trigger = {'kind': 'file', 'path': 'old.txt', 'check_every': 60}
    w = arm(e, 'file', trigger_json=trigger, probe_state_json={'old': 'observation'})
    old = pending(e, w, 'p:observation')
    changed = {'path': 'new.txt'} if target_changed else {'check_every': 120}
    update(e, w, trigger={**trigger, **changed})
    result = e.store.get_occurrence(w.id, old.dedupe_key)
    if target_changed:
        assert (result.state, result.error, result.force_silent) == ('superseded', 'trigger_changed', True)
        assert e.store.get_wake(w.id).probe_state_json is None
    else:
        assert result == old
        assert e.store.get_wake(w.id).probe_state_json == w.probe_state_json


@pytest.mark.parametrize('state', ['claimed', 'started'])
@pytest.mark.parametrize('trigger', [{'kind': 'reaction', 'message_id': 'B'}, {'kind': 'kanban_completed'}])
def test_in_flight_occurrences_are_untouched(engine, state, trigger):
    e = engine
    w = reaction(e, message_id='A')
    react(e, 'A')
    e.dispatch(e.store.list_occurrences(w.id)[0])
    if state == 'started':
        claimed = e.store.list_occurrences(w.id)[0]
        assert e.store.start(w.id, claimed.dedupe_key, attempt=claimed.attempt)
    old = e.store.list_occurrences(w.id)[0]
    assert old.state == state
    update(e, w, trigger=trigger)
    assert e.store.get_occurrence(w.id, old.dedupe_key) == old


def test_non_dict_payloads_are_preserved_and_non_event_keys_are_untouched(engine):
    e = engine
    w = reaction(e, emojis=['yes', 'no'])
    payloads = [None, 'legacy', 7, {'emojis': ['no']}]
    assert e.store.add_occurrence(w.id, 'e:legacy', payload=payloads)
    other = pending(e, w, 't:manual')
    update(e, w, trigger={'kind': 'reaction', 'emojis': ['yes']})
    result = e.store.get_occurrence(w.id, 'e:legacy')
    assert result.state == 'pending' and result.payload_json == payloads[:-1]
    assert e.store.get_occurrence(w.id, other.dedupe_key) == other


def test_lost_wake_cas_does_not_recheck_pending(engine, monkeypatch):
    e = engine
    w = reaction(e, message_id='A')
    react(e, 'A')
    old = e.store.list_occurrences(w.id)[0]
    monkeypatch.setattr(e.store, 'cas_wake', lambda *args, **kwargs: False)
    result = e.tool.handle(dict(action='update', id=w.id,
                               trigger={'kind': 'reaction', 'message_id': 'B'}), session_id='sid')
    assert result.get('error') == 'concurrent_change'
    assert e.store.get_wake(w.id) == w
    assert e.store.get_occurrence(w.id, old.dedupe_key) == old


def test_lost_occurrence_cas_preserves_newly_claimed_occurrence(engine, monkeypatch):
    e = engine
    w = reaction(e, message_id='A')
    react(e, 'A')
    old = e.store.list_occurrences(w.id)[0]
    cas = e.store.cas_occurrence
    claimed = []

    def claim_before_cas(wid, key, expected, new_state, **changes):
        assert e.store.claim(wid, key, epoch=e.epoch, pid=1)
        claimed.append(e.store.get_occurrence(wid, key))
        return cas(wid, key, expected, new_state, **changes)

    monkeypatch.setattr(e.store, 'cas_occurrence', claim_before_cas)
    update(e, w, trigger={'kind': 'reaction', 'message_id': 'B'})
    assert claimed
    assert e.store.get_occurrence(w.id, old.dedupe_key) == claimed[0]


def test_held_count_survives_a_trigger_edit_that_keeps_the_filters(engine):
    e = engine
    e.policy = replace(e.policy, max_model_wakes_per_day=0)
    w = reaction(e, emojis=['yes'])
    react(e, 'A', 'yes')
    e.dispatch(e.store.list_occurrences(w.id)[0])
    react(e, 'B', 'yes')
    old = e.store.list_occurrences(w.id)[0]
    assert old.held_since is not None and old.held_matches == 2
    update(e, w, trigger={'kind': 'reaction', 'emojis': ['yes'], 'deadline': 'in 2h'})
    assert e.store.get_wake(w.id).trigger_json != w.trigger_json
    result = e.store.get_occurrence(w.id, old.dedupe_key)
    assert (result.state, result.held_matches, result.payload_json) == ('pending', 2, old.payload_json)


def test_dispatch_uses_the_stored_occurrence_not_the_ticks_copy(engine):
    e = engine
    e.policy = replace(e.policy, max_model_wakes_per_day=0)
    w = reaction(e, emojis=['yes', 'no'])
    react(e, 'A', 'yes')
    react(e, 'B', 'no')
    stale = e.store.list_occurrences(w.id, state='pending')[0]
    assert [p['message_id'] for p in stale.payload_json] == ['A', 'B']
    update(e, w, trigger={'kind': 'reaction', 'emojis': ['yes']})
    e.dispatch(stale)
    result = e.store.get_occurrence(w.id, stale.dedupe_key)
    assert result.state == 'pending' and result.held_reason == 'held_daily_cap'
    assert [p['message_id'] for p in result.payload_json] == ['A'] and result.held_matches == 1
    superseded = e.store.get_occurrence(w.id, stale.dedupe_key)
    e.store.cas_occurrence(w.id, superseded.dedupe_key, 'pending', 'superseded', error='test')
    e.dispatch(stale)
    assert e.store.get_occurrence(w.id, stale.dedupe_key).state == 'superseded' and not e.ctx.calls


def test_time_wake_turned_watcher_drops_its_queued_time(engine):
    e = engine
    w = arm(e, 'every', trigger_json={'kind': 'every', 'every': '1h'}, next_due_at=e.clock.now() + 3000)
    o = pending(e, w, 't:manual')
    update(e, w, trigger={'kind': 'reaction', 'emojis': ['yes']})
    result = e.store.get_occurrence(w.id, o.dedupe_key)
    assert (result.state, result.error, result.force_silent) == ('superseded', 'trigger_changed', True)
    e.tick()
    assert not e.ctx.calls


@pytest.mark.parametrize('emojis', [['yes', 'no'], ['no', 'yes'], []])
def test_widened_filter_keeps_the_held_count(engine, emojis):
    e = engine
    e.policy = replace(e.policy, max_model_wakes_per_day=0)
    w = reaction(e, emojis=['yes'])
    react(e, 'A', 'yes')
    e.dispatch(e.store.list_occurrences(w.id)[0])
    react(e, 'B', 'yes')
    react(e, 'C', 'yes')
    old = e.store.list_occurrences(w.id)[0]
    assert old.held_matches == 3
    update(e, w, trigger={'kind': 'reaction', 'emojis': emojis})
    result = e.store.get_occurrence(w.id, old.dedupe_key)
    assert (result.state, result.held_matches) == ('pending', 3)
