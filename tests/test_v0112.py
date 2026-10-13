"""v0.1.12: a one-shot skipped because the owner turned pulse off reads as cancelled, not failed (#41)."""
from test_engine import arm, engine, session  # noqa: F401


def test_one_shot_skipped_by_pulse_off_is_cancelled(engine):
    e = engine
    e.tick()
    session(e, enabled=False)
    w = arm(e)
    e.tick()
    w = e.store.get_wake(w.id)
    assert not e.ctx.calls
    assert (w.status, w.status_reason) == ('cancelled', 'session_off')
    assert [o.error for o in e.store.list_occurrences(w.id)] == ['session_off']


def test_recurring_wake_stays_armed_through_pulse_off(engine):
    e = engine
    e.tick()
    session(e, enabled=False)
    w = arm(e, 'every')
    e.tick()
    assert e.store.get_wake(w.id).status == 'active'
    assert not e.ctx.calls


def test_other_final_skips_still_read_as_failed(engine):
    e = engine
    e.tick()
    session(e, chat_type='group')
    w = arm(e)
    e.tick()
    w = e.store.get_wake(w.id)
    assert (w.status, w.status_reason) == ('failed', 'skipped:dm_only')
