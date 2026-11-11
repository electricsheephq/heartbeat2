"""An owner counted during a queued wake still engaged, without folding into it."""
from types import SimpleNamespace

import pytest

from test_engine import engine, arm, start, end, session, KEY


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


def test_no_owner_counts_each_delivered_wake(engine):
    e = engine
    for count in (1, 2):
        e.clock.advance(601)
        w = arm(e); e.tick(); start(e); end(e, 'progress', True)
        assert e.store.get_session(KEY).unanswered_count == count
        assert e.store.list_occurrences(w.id)[0].delivered == 1
