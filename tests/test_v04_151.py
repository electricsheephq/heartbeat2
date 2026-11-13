"""/pulse test preview matches what dispatch would do in the rare cases (#151 item 2)."""
from test_engine import engine, arm
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
    assert 'Would run now: yes' in out


def test_another_wake_earlier_in_the_tick_takes_the_turn(engine, command):
    e = engine
    first, _ = queued(e, command)
    second, _ = queued(e, command)
    out = command('test '+second.id)
    assert f'Would run now: no — another wake ({first.id}) takes the turn first (held; it would wait)' in out
    assert 'Would run now: yes' in command('test '+first.id)
