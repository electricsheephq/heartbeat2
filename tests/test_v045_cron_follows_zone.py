"""Cron and `every … at` times follow a time-zone change (#170).

A stored time that is not one of the wake's own local times in the zone in use was set in another zone. It moves to the
nearest of them: later after a move west, earlier after a move east, or, when that local time has already passed unserved,
it fires once now. A time already on the zone's schedule is never moved, so nothing records where a time was set.
"""
from dataclasses import replace
from datetime import datetime

from heartbeat2.model import Wake
from heartbeat2.poller import Poller
from heartbeat2.schedule import local_neighbours, next_cron, next_every, zone
from test_engine import engine, arm, KEY  # noqa: F401

SEOUL, BANGKOK, NEW_YORK = 'Asia/Seoul', 'Asia/Bangkok', 'America/New_York'  # UTC+9, UTC+7 (no DST), US Eastern


def _zone(e, name):
    e.policy = replace(e.policy, timezone=name)
    return e.policy.resolved_timezone()


def _at(name, *parts):
    return datetime(*parts, tzinfo=zone(name)).timestamp()


def _local(stamp, name):
    return datetime.fromtimestamp(stamp, zone(name)).strftime('%Y-%m-%d %H:%M')


def _cron(e, expression):
    due = next_cron(expression, now=e.clock.now(), timezone=e.policy.resolved_timezone())
    return arm(e, 'cron', trigger_json={'cron': expression}, next_due_at=due)


def _fires(e, w):
    return len(e.store.list_occurrences(w.id))


def _to(e, stamp):
    e.clock.advance(stamp - e.clock.now())


def test_a_move_west_waits_for_the_local_time(engine):
    # The live report: the stored next time, set in the old zone, would have fired two hours early.
    e = engine
    _zone(e, SEOUL)
    w = _cron(e, '0 9 * * *')
    seoul = e.store.get_wake(w.id).next_due_at
    assert _local(seoul, SEOUL) == '2025-10-13 09:00'
    _zone(e, BANGKOK)
    e.tick()
    assert _local(e.store.get_wake(w.id).next_due_at, BANGKOK) == '2025-10-13 09:00'
    _to(e, seoul)  # 07:00 in Bangkok
    e.tick()
    assert _fires(e, w) == 0
    _to(e, _at(BANGKOK, 2025, 10, 13, 9, 0))
    e.tick()
    assert _fires(e, w) == 1
    assert _local(e.store.get_wake(w.id).next_due_at, BANGKOK) == '2025-10-14 09:00'


def test_a_move_east_moves_back_to_the_local_time(engine):
    e = engine
    _zone(e, BANGKOK)
    w = _cron(e, '0 9 * * *')
    bangkok = e.store.get_wake(w.id).next_due_at  # 11:00 in Seoul
    _zone(e, SEOUL)
    e.tick()
    nine = _at(SEOUL, 2025, 10, 13, 9, 0)
    assert e.store.get_wake(w.id).next_due_at == nine
    _to(e, nine)
    e.tick()
    _to(e, bangkok)
    e.tick()
    assert _fires(e, w) == 1
    assert _local(e.store.get_wake(w.id).next_due_at, SEOUL) == '2025-10-14 09:00'


def test_a_move_east_after_the_local_time_fires_once_now(engine):
    e = engine
    _zone(e, BANGKOK)
    w = _cron(e, '0 9 * * *')
    bangkok = e.store.get_wake(w.id).next_due_at
    _to(e, _at(SEOUL, 2025, 10, 13, 10, 0))  # 08:00 in Bangkok: not due yet there
    e.tick()
    assert _fires(e, w) == 0
    _zone(e, SEOUL)  # 09:00 in Seoul has passed with no fire: it fires on this check, once
    e.tick()
    assert [o.dedupe_key for o in e.store.list_occurrences(w.id)] == [f't:{e.clock.now():.17g}']
    assert _local(e.store.get_wake(w.id).next_due_at, SEOUL) == '2025-10-14 09:00'
    _to(e, bangkok)
    e.tick()
    assert _fires(e, w) == 1


def test_a_local_time_already_served_or_too_late_waits_for_the_next(engine):
    e = engine
    _zone(e, BANGKOK)
    nine = _at(SEOUL, 2025, 10, 13, 9, 0)
    _to(e, _at(SEOUL, 2025, 10, 13, 10, 0))
    served = _cron(e, '0 9 * * *')
    assert e.store.cas_wake(served.id, 'active', expected_version=served.version, last_fired_at=nine)
    keyed = _cron(e, '0 9 * * *')
    e.store.add_occurrence(keyed.id, f't:{nine:.17g}', state='skipped')
    late = _cron(e, '0 9 * * *')
    _zone(e, SEOUL)
    e.policy = replace(e.policy, catch_up_hours=0)
    e.tick()
    tomorrow = _at(SEOUL, 2025, 10, 14, 9, 0)
    assert [e.store.get_wake(w.id).next_due_at for w in (served, keyed, late)] == [tomorrow] * 3
    assert [_fires(e, w) for w in (served, keyed, late)] == [0, 1, 0]


def test_a_move_west_just_after_a_fire_does_not_fire_again_that_day(engine):
    e = engine
    _zone(e, SEOUL)
    w = _cron(e, '0 9 * * *')
    _to(e, e.store.get_wake(w.id).next_due_at)  # 09:00 in Seoul: it fires
    e.tick()
    assert _fires(e, w) == 1
    e.clock.advance(60)
    _zone(e, BANGKOK)  # 07:01 in Bangkok: 09:00 there today is not a second fire
    e.tick()
    _to(e, _at(BANGKOK, 2025, 10, 13, 9, 0))
    e.tick()
    assert _fires(e, w) == 1
    assert _local(e.store.get_wake(w.id).next_due_at, BANGKOK) == '2025-10-14 09:00'


def test_a_time_set_in_the_new_zone_is_never_moved(engine):
    # The engine that answers tools and commands may still hold the old zone; whichever wrote a time, the check is idempotent.
    e = engine
    _zone(e, BANGKOK)
    w = _cron(e, '30 8 * * 1-5')
    weekly = _cron(e, '0 9 * * 1')
    e.tick()
    before = [e.store.get_wake(x.id).next_due_at for x in (w, weekly)]
    for _ in range(3):
        e.clock.advance(600)
        e.tick()
    assert [e.store.get_wake(x.id).next_due_at for x in (w, weekly)] == before


def test_a_slowed_cron_keeps_its_day(engine):
    # A governor slow-down leaves the next time days out; the move keeps that day, at the local time in the new zone.
    e = engine
    _zone(e, SEOUL)
    later = _at(SEOUL, 2025, 10, 16, 9, 0)
    w = arm(e, 'cron', trigger_json={'cron': '0 9 * * *'}, next_due_at=later, eff_interval_s=3 * 86400)
    _zone(e, BANGKOK)
    e.tick()
    assert _local(e.store.get_wake(w.id).next_due_at, BANGKOK) == '2025-10-16 09:00'


def test_every_at_keeps_its_local_time_and_its_stride(engine):
    e = engine
    seoul = _zone(e, SEOUL)
    daily = arm(e, 'every', trigger_json={'at': '09:00'}, base_interval_s=86400, eff_interval_s=86400,
                next_due_at=next_every(e.clock.now(), 86400, anchor='09:00', timezone=seoul))
    other = arm(e, 'every', trigger_json={'at': '09:00'}, base_interval_s=2 * 86400, eff_interval_s=2 * 86400,
                next_due_at=_at(SEOUL, 2025, 10, 14, 9, 0))
    _zone(e, BANGKOK)
    e.tick()
    assert _local(e.store.get_wake(daily.id).next_due_at, BANGKOK) == '2025-10-13 09:00'
    assert _local(e.store.get_wake(other.id).next_due_at, BANGKOK) == '2025-10-14 09:00'


def test_whole_hour_crons_intervals_and_one_time_reminders_stay_put(engine):
    e = engine
    _zone(e, SEOUL)
    hourly = _cron(e, '0 * * * *')  # The same instants in both zones
    interval = arm(e, 'every', next_due_at=e.clock.now() + 3600)
    short = arm(e, 'every', trigger_json={'at': '09:00'}, base_interval_s=6 * 3600, eff_interval_s=6 * 3600,
                next_due_at=_at(SEOUL, 2025, 10, 13, 3, 0))
    reminder = arm(e, 'at', next_due_at=_at(SEOUL, 2025, 10, 13, 9, 0))
    paused = _cron(e, '0 9 * * *')
    assert e.store.cas_wake(paused.id, 'active', expected_version=paused.version, status='paused', status_reason='owner')
    wakes = (hourly, interval, short, reminder, paused)
    before = [e.store.get_wake(w.id).next_due_at for w in wakes]
    _zone(e, BANGKOK)
    e.tick()
    assert [e.store.get_wake(w.id).next_due_at for w in wakes] == before


def test_dst_gaps_and_repeated_hours_are_on_schedule():
    tz = zone(NEW_YORK)
    spring, autumn = _at(NEW_YORK, 2026, 3, 7, 12, 0), _at(NEW_YORK, 2026, 10, 31, 12, 0)
    for expression, now in (('30 2 * * *', spring), ('0 2 * * *', spring), ('30 1 * * *', autumn)):
        wake = Wake(KEY, 'x', 'cron', trigger_json={'cron': expression})
        first = next_cron(expression, now=now, timezone=tz)
        second = next_cron(expression, now=first, timezone=tz)
        assert local_neighbours(wake, first, tz) is None and local_neighbours(wake, second, tz) is None
    daily = Wake(KEY, 'x', 'every', trigger_json={'at': '02:30'}, eff_interval_s=86400)
    gap = next_every(spring, 86400, anchor='02:30', timezone=tz)
    assert local_neighbours(daily, gap, tz) is None


def test_the_new_zone_applies_after_a_restart(engine):
    e = engine
    _zone(e, SEOUL)
    w = _cron(e, '30 8 * * 1-5')
    e.close()
    restarted = Poller(e.ctx, e.store, replace(e.policy, timezone=BANGKOK), e.clock, session_key_fn=lambda: KEY,
                       paused=lambda: False, jitter=lambda: 0, compression_tip=lambda sid: sid)
    try:
        restarted.tick()
        assert _local(e.store.get_wake(w.id).next_due_at, BANGKOK) == '2025-10-13 08:30'
    finally:
        restarted.close()


def test_a_live_settings_change_is_picked_up(engine):
    e = engine
    _zone(e, SEOUL)
    w = _cron(e, '0 9 * * *')
    e.refresh_policy = lambda: replace(e.policy, timezone='Europe/London')
    e.tick()
    assert _local(e.store.get_wake(w.id).next_due_at, 'Europe/London') == '2025-10-13 09:00'


def test_a_failed_hermes_zone_lookup_keeps_the_last_zone(tmp_path, clock, monkeypatch):
    # A passing config read error must not move every cron time to the system zone and back.
    import sys
    import types
    from heartbeat2.registration import Runtime
    from test_engine import GatewayCtx
    fake = types.ModuleType('hermes_time')
    fake.get_timezone_name = lambda: SEOUL
    monkeypatch.setitem(sys.modules, 'hermes_time', fake)
    rt = Runtime(GatewayCtx(tmp_path), clock=clock)
    assert rt.policy().profile_timezone == SEOUL

    def broken():
        raise RuntimeError('config read failed')
    fake.get_timezone_name = broken
    assert rt.policy().profile_timezone == SEOUL
