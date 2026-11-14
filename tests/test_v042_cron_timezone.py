"""Cron and `every … at` times are local wall times: after a time-zone change each future one keeps its local date and time
in the new zone, on the next check rather than only after it fires (#170)."""
from dataclasses import replace
from datetime import datetime

from heartbeat2 import governor
from heartbeat2.poller import Poller
from heartbeat2.schedule import next_cron, next_every, zone
from test_engine import engine, arm, start, end, KEY  # noqa: F401

SEOUL, BANGKOK = 'Asia/Seoul', 'Asia/Bangkok'


def _zone(e, name):
    e.policy = replace(e.policy, timezone=name)
    return e.policy.resolved_timezone()


def _local(stamp, name):
    return datetime.fromtimestamp(stamp, zone(name)).strftime('%Y-%m-%d %H:%M')


def _settled(e, name):
    """The poller has seen `name` as the zone (the first check after an upgrade records it)."""
    _zone(e, name)
    e.tick()


def _cron(e, expression, **changes):
    due = next_cron(expression, now=e.clock.now(), timezone=e.policy.resolved_timezone(), minimum_interval_s=3600)
    return arm(e, 'cron', trigger_json={'cron': expression}, next_due_at=due, **changes)


def test_a_zone_change_keeps_the_local_time_of_the_next_cron_time(engine):
    e = engine
    _settled(e, SEOUL)
    w = _cron(e, '0 9 * * *')
    seoul = e.store.get_wake(w.id).next_due_at
    _zone(e, BANGKOK)
    e.tick()
    moved = e.store.get_wake(w.id).next_due_at
    assert _local(moved, BANGKOK) == _local(seoul, SEOUL) and moved - seoul == 2 * 3600


def test_the_new_zone_applies_after_a_restart(engine):
    e = engine
    _settled(e, SEOUL)
    w = _cron(e, '30 8 * * 1-5')
    seoul = e.store.get_wake(w.id).next_due_at
    later = replace(e.policy, timezone=BANGKOK)
    e.close()
    restarted = Poller(e.ctx, e.store, later, e.clock, session_key_fn=lambda: KEY,
                       paused=lambda: False, jitter=lambda: 0, compression_tip=lambda sid: sid)
    try:
        restarted.tick()
        assert _local(e.store.get_wake(w.id).next_due_at, BANGKOK) == _local(seoul, SEOUL)
    finally:
        restarted.close()


def test_a_live_settings_change_is_picked_up(engine):
    e = engine
    _settled(e, SEOUL)
    w = _cron(e, '0 9 * * *')
    seoul = e.store.get_wake(w.id).next_due_at
    e.refresh_policy = lambda: replace(e.policy, timezone='Europe/London')
    e.tick()
    assert _local(e.store.get_wake(w.id).next_due_at, 'Europe/London') == _local(seoul, SEOUL)


def test_every_at_keeps_its_local_time_and_its_stride(engine):
    e = engine
    seoul = _zone(e, SEOUL)
    e.tick()
    daily = arm(e, 'every', trigger_json={'at': '09:00'}, base_interval_s=86400, eff_interval_s=86400,
                next_due_at=next_every(e.clock.now(), 86400, anchor='09:00', timezone=seoul))
    # Every two days: two days after its last fire, not the next 09:00 in the new zone.
    stride = next_every(e.clock.now(), 2 * 86400, anchor='09:00', timezone=seoul) + 86400
    other = arm(e, 'every', trigger_json={'at': '09:00'}, base_interval_s=2 * 86400, eff_interval_s=2 * 86400, next_due_at=stride)
    before = {w.id: e.store.get_wake(w.id).next_due_at for w in (daily, other)}
    _zone(e, BANGKOK)
    e.tick()
    for w in (daily, other):
        assert _local(e.store.get_wake(w.id).next_due_at, BANGKOK) == _local(before[w.id], SEOUL)


def test_a_slowed_cron_keeps_its_skipped_times(engine):
    e = engine
    p = e.policy
    _settled(e, SEOUL)
    w = _cron(e, '0 9 * * *', base_interval_s=3600, eff_interval_s=3600, noop_streak=p.noop_slowdown_after)
    with e.store.atomic():
        governor.apply(e, e.store.get_wake(w.id))
    skips = sorted(float(o.dedupe_key[2:]) for o in e.store.list_occurrences(w.id, state='skipped') if o.error == 'governor')
    due = e.store.get_wake(w.id).next_due_at
    assert skips and due > skips[-1]
    _zone(e, BANGKOK)
    e.tick()
    after = sorted(float(o.dedupe_key[2:]) for o in e.store.list_occurrences(w.id, state='skipped') if o.error == 'governor')
    assert [_local(t, BANGKOK) for t in after] == [_local(t, SEOUL) for t in skips]
    assert _local(e.store.get_wake(w.id).next_due_at, BANGKOK) == _local(due, SEOUL)


def test_a_move_east_fires_a_time_it_passed_once(engine):
    e = engine
    _settled(e, BANGKOK)
    # 09:00 today in Bangkok is still ahead; 09:00 today in Seoul is already past: it fires on this check, once.
    seoul_nine = datetime(2025, 10, 13, 9, 0, tzinfo=zone(SEOUL)).timestamp()
    e.clock.advance(seoul_nine + 3600 - e.clock.now())  # 10:00 Seoul, 08:00 Bangkok
    w = arm(e, 'cron', trigger_json={'cron': '0 9 * * *'}, next_due_at=seoul_nine + 2 * 3600)
    e.tick()
    _zone(e, SEOUL)
    e.tick()
    keys = [o.dedupe_key for o in e.store.list_occurrences(w.id)]
    assert keys == [f't:{seoul_nine:.17g}']
    assert _local(e.store.get_wake(w.id).next_due_at, SEOUL) == '2025-10-14 09:00'


def test_a_move_west_after_a_fire_does_not_fire_again_that_day(engine):
    e = engine
    _settled(e, SEOUL)
    w = _cron(e, '0 9 * * *')
    e.clock.advance(e.store.get_wake(w.id).next_due_at - e.clock.now())  # 09:00 Seoul: it fires
    e.tick(); start(e); end(e, 'progress', False)
    fired = len(e.store.list_occurrences(w.id))
    tomorrow = e.store.get_wake(w.id).next_due_at
    e.clock.advance(60)
    _zone(e, BANGKOK)  # 07:01 Bangkok: 09:00 Bangkok today must not become a second fire
    e.tick()
    assert len(e.store.list_occurrences(w.id)) == fired
    assert _local(e.store.get_wake(w.id).next_due_at, BANGKOK) == _local(tomorrow, SEOUL)


def test_a_paused_cron_keeps_its_local_time_too(engine):
    e = engine
    _settled(e, SEOUL)
    w = _cron(e, '0 9 * * *', status='paused', status_reason='command')
    seoul = e.store.get_wake(w.id).next_due_at
    _zone(e, BANGKOK)
    e.tick()
    assert _local(e.store.get_wake(w.id).next_due_at, BANGKOK) == _local(seoul, SEOUL)


def test_intervals_one_time_reminders_and_an_unchanged_zone_stay_put(engine):
    e = engine
    _settled(e, SEOUL)
    cron = _cron(e, '0 9 * * *')
    interval = arm(e, 'every', next_due_at=e.clock.now() + 5400)
    once = arm(e, 'at', next_due_at=e.clock.now() + 7200)
    snapshot = lambda: {w.id: (e.store.get_wake(w.id).next_due_at, e.store.get_wake(w.id).version) for w in (cron, interval, once)}
    before = snapshot()
    e.tick()
    assert snapshot() == before  # unchanged zone: no write at all
    _zone(e, BANGKOK)
    e.tick()
    after = snapshot()
    assert after[interval.id] == before[interval.id] and after[once.id] == before[once.id]
    assert after[cron.id] != before[cron.id]


def test_the_first_check_after_an_upgrade_records_the_zone_and_moves_nothing(engine):
    e = engine
    _zone(e, BANGKOK)
    w = _cron(e, '0 9 * * *')
    before = e.store.get_wake(w.id)
    assert e.store.get_meta('schedule_zone') is None
    e.tick()
    after = e.store.get_wake(w.id)
    assert e.store.get_meta('schedule_zone') == BANGKOK
    assert (after.next_due_at, after.version) == (before.next_due_at, before.version)
