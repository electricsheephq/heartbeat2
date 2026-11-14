"""A cron or daily `every … at HH:MM` wake follows a timezone change on its next time, not only after it fires (#170)."""
from dataclasses import replace

from heartbeat2.poller import Poller
from heartbeat2.schedule import next_cron, next_every
from test_engine import engine, arm, KEY  # noqa: F401


def _zone(e, name):
    e.policy = replace(e.policy, timezone=name)
    return e.policy.resolved_timezone()


def _cron(e, expression, **changes):
    tz = e.policy.resolved_timezone()
    due = next_cron(expression, now=e.clock.now(), timezone=tz, minimum_interval_s=3600)
    return arm(e, 'cron', trigger_json={'cron': expression}, next_due_at=due, **changes)


def test_a_timezone_change_moves_the_next_cron_time(engine):
    e = engine
    _zone(e, 'Asia/Seoul')
    w = _cron(e, '0 9 * * *')
    seoul = e.store.get_wake(w.id).next_due_at
    bangkok = next_cron('0 9 * * *', now=e.clock.now(), timezone=_zone(e, 'Asia/Bangkok'), minimum_interval_s=3600)
    assert bangkok - seoul == 2 * 3600  # 09:00 in Bangkok is two hours after 09:00 in Seoul
    e.tick()
    assert e.store.get_wake(w.id).next_due_at == bangkok


def test_the_new_zone_applies_after_a_restart(engine):
    e = engine
    _zone(e, 'Asia/Seoul')
    w = _cron(e, '30 8 * * 1-5')
    later = replace(e.policy, timezone='Asia/Bangkok')
    e.close()
    restarted = Poller(e.ctx, e.store, later, e.clock, session_key_fn=lambda: KEY,
                       paused=lambda: False, jitter=lambda: 0, compression_tip=lambda sid: sid)
    try:
        restarted.tick()
        expected = next_cron('30 8 * * 1-5', now=e.clock.now(), timezone=later.resolved_timezone(), minimum_interval_s=3600)
        assert e.store.get_wake(w.id).next_due_at == expected
    finally:
        restarted.close()


def test_a_live_settings_change_is_picked_up(engine):
    e = engine
    _zone(e, 'Asia/Seoul')
    w = _cron(e, '0 9 * * *')
    e.refresh_policy = lambda: replace(e.policy, timezone='Europe/London')
    e.tick()
    expected = next_cron('0 9 * * *', now=e.clock.now(), timezone=e.policy.resolved_timezone(), minimum_interval_s=3600)
    assert str(e.policy.resolved_timezone()) == 'Europe/London'
    assert e.store.get_wake(w.id).next_due_at == expected


def test_a_daily_every_at_follows_the_new_zone(engine):
    e = engine
    seoul = _zone(e, 'Asia/Seoul')
    due = next_every(e.clock.now(), 86400, anchor='09:00', timezone=seoul)
    w = arm(e, 'every', trigger_json={'at': '09:00'}, base_interval_s=86400, eff_interval_s=86400, next_due_at=due)
    bangkok = _zone(e, 'Asia/Bangkok')
    e.tick()
    assert e.store.get_wake(w.id).next_due_at == next_every(e.clock.now(), 86400, anchor='09:00', timezone=bangkok)


def test_times_both_zones_share_stay_put(engine):
    e = engine
    _zone(e, 'Asia/Seoul')
    hourly = _cron(e, '0 * * * *')
    interval = arm(e, 'every', next_due_at=e.clock.now() + 5400)
    once = arm(e, 'at', next_due_at=e.clock.now() + 7200)
    before = {w.id: (e.store.get_wake(w.id).next_due_at, e.store.get_wake(w.id).version) for w in (hourly, interval, once)}
    _zone(e, 'Asia/Bangkok')  # whole hours apart, so every hourly cron time is a cron time in both zones
    e.tick()
    assert {w.id: (e.store.get_wake(w.id).next_due_at, e.store.get_wake(w.id).version) for w in (hourly, interval, once)} == before


def test_an_unchanged_zone_writes_nothing(engine):
    e = engine
    _zone(e, 'Asia/Seoul')
    w = _cron(e, '0 9 * * *')
    before = e.store.get_wake(w.id)
    e.tick()
    after = e.store.get_wake(w.id)
    assert (after.next_due_at, after.version) == (before.next_due_at, before.version)


def test_a_due_cron_time_still_fires_once(engine):
    e = engine
    _zone(e, 'Asia/Seoul')
    w = arm(e, 'cron', trigger_json={'cron': '0 9 * * *'}, next_due_at=e.clock.now() - 60)
    _zone(e, 'Asia/Bangkok')
    e.tick()
    occurrences = e.store.list_occurrences(w.id)
    assert [o.dedupe_key for o in occurrences] == [f't:{e.clock.now() - 60:.17g}']
    assert e.store.get_wake(w.id).next_due_at == next_cron('0 9 * * *', now=e.clock.now(),
                                                           timezone=e.policy.resolved_timezone(), minimum_interval_s=3600)


def test_old_zone_governor_skips_are_dropped(engine):
    e = engine
    _zone(e, 'Asia/Seoul')
    w = _cron(e, '0 9,11 * * *')
    seoul_eleven = next_cron('0 9,11 * * *', now=e.store.get_wake(w.id).next_due_at, timezone=e.policy.resolved_timezone())
    e.store.add_occurrence(w.id, f't:{seoul_eleven:.17g}', state='skipped')
    e.store.connection.execute("UPDATE occurrences SET error='governor' WHERE wake_id=?", (w.id,))
    _zone(e, 'Asia/Bangkok')
    e.tick()
    # 11:00 in Seoul is 09:00 in Bangkok: the old skip must not swallow the time the wake now fires at.
    assert e.store.get_wake(w.id).next_due_at == seoul_eleven
    assert not e.store.list_occurrences(w.id, state='skipped')
