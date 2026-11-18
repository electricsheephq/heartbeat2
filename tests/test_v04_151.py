"""/pulse test preview matches what dispatch would do in the rare cases (#151 item 2)."""
from test_engine import engine, arm
from heartbeat2.model import Occurrence
from test_v039 import command, snapshot


def queued(e, command, **changes):
    w = arm(e, 'every', next_due_at=e.clock.now()+3600, **changes)
    assert command('run '+w.id).startswith('Queued ')
    return w, e.store.list_occurrences(w.id)[0]


def test_a_hold_carried_from_quiet_hours_is_not_called_a_retry(engine, command):
    e = engine
    w, o = queued(e, command)
    e.store.set_meta('not_before:'+o.id, str(e.clock.now()+600))  # Left by an earlier quiet-hours deferral.
    before = snapshot(e)
    out = command('test '+w.id)
    assert 'Would run now: no — waiting out an earlier hold until ' in out and 'retry' not in out.splitlines()[1]
    assert snapshot(e) == before


def test_an_unacknowledged_occurrence_previews_as_dropped(engine, command):
    e = engine
    w, o = queued(e, command)
    e.store.set_meta('accepted:'+o.id, '3')
    before = snapshot(e)
    out = command('test '+w.id)
    assert 'Would run now: no — not acknowledged after three attempts (it would be dropped)' in out
    assert snapshot(e) == before


def test_the_preview_skips_an_occurrence_dispatch_would_drop(engine, command):
    e = engine
    w, o = queued(e, command)
    e.store.set_meta('accepted:'+o.id, '3')
    e.store.add_occurrence(w.id, 't:second')
    out = command('test '+w.id)
    # The second, scheduled occurrence is the one shown, not the dropped /pulse run one.
    assert 'Would run now: yes' in out and 'The owner asked to run this now' not in out


def test_another_wake_earlier_in_the_tick_takes_the_turn(engine, command):
    e = engine
    first, _ = queued(e, command)
    second, _ = queued(e, command)
    out = command('test '+second.id)
    assert f'Would run now: no — another wake ({first.id}) takes the turn first (held; it would wait)' in out
    assert 'Would run now: yes' in command('test '+first.id)


def test_a_drop_that_pauses_the_wake_previews_as_no(engine, command):
    # Review (Astra): each drop is an error; at the threshold the wake pauses before the next one is sent.
    e = engine
    w, o = queued(e, command, error_streak=e.policy.error_pause_after-1)
    e.store.set_meta('accepted:'+o.id, '3')
    e.store.add_occurrence(w.id, 't:second')
    assert 'Would run now: no — an earlier unacknowledged turn pauses the wake first (it would be skipped)' in command('test '+w.id)


def test_a_one_shot_still_sends_its_next_turn_after_a_drop(engine, command):
    # Review (scout): a drop ends a one-shot only once nothing else waits, and the previewed turn still waits.
    e = engine
    w = arm(e, 'at', next_due_at=e.clock.now()+3600)
    for key in ('t:first', 't:second'):
        e.store.add_occurrence(w.id, key)
    e.store.set_meta('accepted:'+e.store.get_occurrence(w.id, 't:first').id, '3')
    assert 'Would run now: yes' in command('test '+w.id)


def test_a_broken_other_wake_does_not_break_the_preview(engine, command, monkeypatch):
    # Review (scout): a tick isolates one wake's bad data; the preview must too.
    e = engine
    first, _ = queued(e, command)
    second, _ = queued(e, command)
    real = e.gate
    def gate(w, o, session):
        if w.id == first.id: raise ValueError('bad stored data')
        return real(w, o, session)
    monkeypatch.setattr(e, 'gate', gate)
    assert 'Would run now: yes' in command('test '+second.id)


def test_a_wake_retrying_after_an_error_neither_runs_nor_goes_first(engine, command):
    # Review (CodeRabbit): a tick skips a wake until its evaluation retry time.
    e = engine
    first, _ = queued(e, command)
    second, _ = queued(e, command)
    e.store.set_meta('eval_not_before:'+first.id, str(e.clock.now()+300))
    assert 'Would run now: yes' in command('test '+second.id)
    assert 'Would run now: no — retrying after an error until ' in command('test '+first.id)


def test_evaluation_backoff_comes_before_any_other_verdict(engine, command):
    # Review (Astra): Poller.evaluate leaves the wake alone until its retry time, before gate or the drop check.
    e = engine
    w, o = queued(e, command)
    e.store.set_meta('accepted:'+o.id, '3')
    e.store.set_meta('eval_not_before:'+w.id, str(e.clock.now()+300))
    before = snapshot(e)
    assert 'Would run now: no — retrying after an error until ' in command('test '+w.id)
    assert snapshot(e) == before


def test_a_turn_already_held_stays_held_when_a_drop_pauses_the_wake(engine, command):
    # Review (scout): Poller.gate holds a paused wake's occurrence that already has held_since, as for a watcher.
    e = engine
    w, o = queued(e, command, error_streak=e.policy.error_pause_after-1)
    e.store.set_meta('accepted:'+o.id, '3')
    e.store.add_occurrence(w.id, 't:second')
    assert e.store.update_occurrence(w.id, 't:second', 'pending', held_since=e.clock.now()-60, held_reason='held_daily_cap')
    assert ('Would run now: no — an earlier unacknowledged turn pauses the wake first (held; it would wait)'
            in command('test '+w.id))


def claimed(e, w):
    return [o.dedupe_key for o in e.store.list_occurrences(w.id) if o.state in {'claimed', 'started'}]


def competitor_one_drop_from_pausing(e, command, **changes):
    # #167: the competitor's first waiting turn is dropped unacknowledged, which pauses it before its second is sent.
    first, o = queued(e, command, error_streak=e.policy.error_pause_after-1, **changes)
    e.store.set_meta('accepted:'+o.id, '3')
    e.store.add_occurrence(first.id, 't:second')
    second, _ = queued(e, command)
    return first, second


def test_a_competitor_paused_by_its_own_drop_does_not_take_the_turn(engine, command):
    e = engine
    first, second = competitor_one_drop_from_pausing(e, command)
    before = snapshot(e)
    assert 'Would run now: yes' in command('test '+second.id)
    assert snapshot(e) == before
    e.tick()  # What dispatch really does: the drop pauses the competitor, so the previewed wake is sent.
    assert e.store.get_wake(first.id).status == 'paused' and claimed(e, first) == []
    assert claimed(e, second) != []


def test_a_competitor_whose_drop_does_not_pause_it_still_goes_first(engine, command):
    e = engine
    first, o = queued(e, command, error_streak=e.policy.error_pause_after-2)
    e.store.set_meta('accepted:'+o.id, '3')
    e.store.add_occurrence(first.id, 't:second')
    second, _ = queued(e, command)
    assert f'Would run now: no — another wake ({first.id}) takes the turn first (held; it would wait)' in command('test '+second.id)
    e.tick()
    assert e.store.get_wake(first.id).status == 'active' and claimed(e, first) == ['t:second'] and claimed(e, second) == []


def test_a_held_turn_of_a_paused_competitor_waits_and_does_not_go_first(engine, command):
    # Poller.gate holds a paused wake's turn that already has held_since; it waits rather than taking the turn.
    e = engine
    first, second = competitor_one_drop_from_pausing(e, command)
    assert e.store.update_occurrence(first.id, 't:second', 'pending', held_since=e.clock.now()-60, held_reason='held_daily_cap')
    assert 'Would run now: yes' in command('test '+second.id)
    e.tick()
    assert claimed(e, first) == [] and claimed(e, second) != []
    held = e.store.get_occurrence(first.id, 't:second')
    assert held.state == 'pending' and held.error == 'held_paused'  # Held while paused, not skipped.


def test_a_report_back_is_never_paused_by_its_drops(engine, command, monkeypatch):
    # governor.error never pauses the report-back (#185), so after a drop its next turn is still sent.
    e = engine
    w = arm(e, 'at', next_due_at=e.clock.now()+3600, error_streak=e.policy.error_pause_after-1,
            trigger_json={'report_back': True})
    for key in ('t:first', 't:second'):
        e.store.add_occurrence(w.id, key)
    e.store.set_meta('accepted:'+e.store.get_occurrence(w.id, 't:first').id, '3')
    source = arm(e, 'at', next_due_at=e.clock.now()+86400)
    held = Occurrence(source.id, 't:held', state='done', outcome='progress', notify=True, summary='The build finished.',
                      held_reason='message_held:quiet_hours', ended_at=e.clock.now()-60)
    monkeypatch.setattr(e.store, 'held_reports', lambda session_key: [held])
    assert 'Would run now: yes' in command('test '+w.id)
    e.tick()
    assert e.store.get_wake(w.id).status == 'active' and claimed(e, w) == ['t:second']
