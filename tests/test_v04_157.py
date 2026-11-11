"""An owner counted during a queued wake still engaged, without folding into it."""
from types import SimpleNamespace

import pytest

from heartbeat2 import governor
from test_engine import engine, arm, start, end, session, admit, KEY


@pytest.mark.parametrize('when', ['before', 'at_start', 'during'])
def test_owner_counted_around_wake(engine, when):
    e = engine
    session(e, unanswered_count=1)
    e.clock.advance(60)
    if when != 'before':
        w = arm(e); e.tick(); start(e)
    if when == 'during': e.clock.advance(1)
    e.intake.pre_gateway_dispatch(event=SimpleNamespace(
        internal=False, text='hello', message_id='owner-157',
        source=SimpleNamespace(platform=SimpleNamespace(value='telegram'),
                               chat_type='dm', chat_id='unit', is_bot=False)))
    assert any(row['kind'] == 'human_message' for row in e.store.pending_events())
    assert e.store.get_session(KEY).last_human_at == e.clock.now()
    assert e.store.get_session(KEY).unanswered_count == 0
    if when == 'before':
        e.clock.advance(60)
        w = arm(e); e.tick(); start(e)
    end(e, 'progress', True)
    after = e.store.get_session(KEY)
    # The same instant as the start counts as before it (strictly after = during the turn).
    assert after.unanswered_count == (0 if when == 'during' else 1)
    assert after.first_notice_sent is True
    assert e.store.list_occurrences(w.id)[0].delivered == 1
    # The governor agrees: an owner who wrote during the turn engaged, so the wake is not marked ignored later.
    e.clock.advance(e.policy.engagement_window_hours*3600+1)
    governor.settle_ignored(e)
    wake = e.store.get_wake(w.id)
    assert (wake.engaged, wake.ignored_streak) == ((1, 0) if when == 'during' else (0, 1))


def test_owner_during_one_turn_does_not_cover_the_next(engine):
    e = engine
    arm(e); e.tick(); start(e)
    e.clock.advance(1)
    admit(e, 'owner-157-next')
    end(e, 'progress', True)
    assert e.store.get_session(KEY).unanswered_count == 0
    e.clock.advance(601)
    w = arm(e); e.tick(); start(e); end(e, 'progress', True)
    assert e.store.list_occurrences(w.id)[0].delivered == 1
    assert e.store.get_session(KEY).unanswered_count == 1


def test_owner_during_turn_keeps_the_requested_next_check(engine):
    e = engine
    w = arm(e, 'self', pacing_min_s=3600, pacing_max_s=6*3600)
    e.tick(); start(e)
    e.clock.advance(1)
    admit(e, 'owner-157-pace')
    end(e, 'progress', True, next_check='4h')
    wake = e.store.get_wake(w.id)
    assert wake.engaged == 1 and wake.eff_interval_s == 4*3600
    assert wake.next_due_at == e.clock.now()+4*3600


def test_owner_during_turn_never_tips_a_noop_pause(engine):
    e = engine
    w = arm(e, 'every', noop_streak=e.policy.noop_pause_after-1)
    e.tick(); start(e)
    e.clock.advance(1)
    admit(e, 'owner-157-noop')
    end(e, 'no_change', True)
    wake = e.store.get_wake(w.id)
    assert wake.status == 'active' and wake.noop_streak == 0 and wake.engaged == 1
    assert e.store.get_session(KEY).unanswered_count == 0


def test_no_owner_counts_each_delivered_wake(engine):
    e = engine
    for count in (1, 2):
        e.clock.advance(601)
        w = arm(e); e.tick(); start(e); end(e, 'progress', True)
        assert e.store.get_session(KEY).unanswered_count == count
        assert e.store.list_occurrences(w.id)[0].delivered == 1
