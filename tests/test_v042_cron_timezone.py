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


def test_a_wake_written_before_the_upgrade_takes_the_current_zone_and_does_not_move(engine):
    e = engine
    _zone(e, BANGKOK)
    w = _cron(e, '0 9 * * *')
    e.store.set_meta('zone:' + w.id, '')  # as a 0.4.1 store has it: no record
    before = e.store.get_wake(w.id)
    e.tick()
    after = e.store.get_wake(w.id)
    assert e.store.get_meta('zone:' + w.id) == BANGKOK
    assert (after.next_due_at, after.version) == (before.next_due_at, before.version)


def test_a_time_computed_in_the_new_zone_is_not_moved_again(engine):
    # The zone changes, then a cron is armed (or updated, or re-applied by the governor) before the next check:
    # its time is already in the new zone.
    e = engine
    _settled(e, SEOUL)
    _zone(e, BANGKOK)
    w = _cron(e, '0 9 * * *')
    due = e.store.get_wake(w.id).next_due_at
    e.tick()
    assert e.store.get_wake(w.id).next_due_at == due and _local(due, BANGKOK).endswith('09:00')


def test_a_due_time_not_yet_fired_moves_too(engine):
    e = engine
    _settled(e, SEOUL)
    nine = datetime(2025, 10, 13, 9, 0, tzinfo=zone(SEOUL)).timestamp()
    e.clock.advance(nine + 60 - e.clock.now())  # 09:01 Seoul: today's 09:00 is due but no check has run yet
    w = arm(e, 'cron', trigger_json={'cron': '0 9 * * *'}, next_due_at=nine)
    _zone(e, BANGKOK)  # 07:01 Bangkok
    e.tick()
    assert not e.store.list_occurrences(w.id)  # no fire at 07:01 Bangkok...
    assert _local(e.store.get_wake(w.id).next_due_at, BANGKOK) == '2025-10-13 09:00'  # ...it fires once, at 09:00 there


def test_a_moved_time_never_lands_on_a_stale_skip(engine):
    e = engine
    _settled(e, BANGKOK)
    w = _cron(e, '0 * * * *')
    due = e.store.get_wake(w.id).next_due_at
    seoul_same = datetime.fromtimestamp(due, zone(BANGKOK)).replace(tzinfo=zone(SEOUL)).timestamp()
    # An old governor skip sits exactly where the moved time lands.
    e.store.add_occurrence(w.id, f't:{seoul_same:.17g}', state='skipped')
    e.store.connection.execute("UPDATE occurrences SET error='governor' WHERE wake_id=?", (w.id,))
    _zone(e, SEOUL)
    e.tick()
    assert e.store.get_wake(w.id).next_due_at == seoul_same
    assert e.store.get_occurrence(w.id, f't:{seoul_same:.17g}') is None  # the skip moved with its zone too


def test_overlapping_skips_all_survive_a_move(engine):
    e = engine
    _settled(e, SEOUL)
    w = _cron(e, '0 * * * *')
    first = e.store.get_wake(w.id).next_due_at
    keys = [first + 3600 * n for n in range(7)]  # 7 hourly skips: moving 2h lands some on others' old keys
    for k in keys:
        e.store.add_occurrence(w.id, f't:{k:.17g}', state='skipped')
    e.store.connection.execute("UPDATE occurrences SET error='governor' WHERE wake_id=?", (w.id,))
    e.store.cas_wake(w.id, 'active', next_due_at=keys[-1] + 3600)
    _zone(e, BANGKOK)
    e.tick()
    after = sorted(float(o.dedupe_key[2:]) for o in e.store.list_occurrences(w.id, state='skipped') if o.error == 'governor')
    assert after == [k + 2 * 3600 for k in keys]


def test_a_fixed_offset_has_no_zone_name():
    from datetime import timedelta, timezone as fixed
    from heartbeat2.schedule import zone_key
    assert zone_key(fixed(timedelta(hours=9))) == '' and zone_key(zone(SEOUL)) == SEOUL


def test_a_repeated_hour_keeps_its_fold():
    from heartbeat2.schedule import same_local_time
    second = datetime(2025, 11, 2, 1, 30, tzinfo=zone('America/New_York'), fold=1).timestamp()
    assert same_local_time(second, 'America/New_York', 'America/Toronto') == second


def test_one_bad_wake_does_not_stop_the_others(engine, monkeypatch):
    from heartbeat2 import poller as module
    e = engine
    _settled(e, SEOUL)
    bad, good = _cron(e, '0 9 * * *'), _cron(e, '0 10 * * *')
    real = module.same_local_time
    monkeypatch.setattr(module, 'same_local_time',
                        lambda stamp, old, new: (_ for _ in ()).throw(OverflowError('x')) if stamp == e.store.get_wake(bad.id).next_due_at else real(stamp, old, new))
    before = e.store.get_wake(bad.id).next_due_at
    _zone(e, BANGKOK)
    e.tick()
    assert e.store.get_wake(bad.id).next_due_at == before and e.store.get_meta('zone:' + bad.id) == BANGKOK
    assert _local(e.store.get_wake(good.id).next_due_at, BANGKOK).endswith('10:00')
    assert e.store.get_meta('last_error') == ''


def test_a_zone_move_catch_up_is_not_reported_as_a_stall(engine):
    e = engine
    _settled(e, BANGKOK)
    seoul_nine = datetime(2025, 10, 13, 9, 0, tzinfo=zone(SEOUL)).timestamp()
    e.clock.advance(seoul_nine + 3600 - e.clock.now())
    arm(e, 'cron', trigger_json={'cron': '0 9 * * *'}, next_due_at=seoul_nine + 2 * 3600)
    e.ctx.calls.clear() if hasattr(e.ctx, 'calls') else None
    _zone(e, SEOUL)
    e.tick()
    assert not any('scheduler was stalled' in str(call) for call in getattr(e.ctx, 'calls', []))


def test_a_failed_hermes_zone_lookup_keeps_the_last_zone(tmp_path, clock, monkeypatch):
    import sys, types
    from heartbeat2.registration import Runtime
    from test_engine import GatewayCtx
    fake = types.ModuleType('hermes_time')
    fake.get_timezone_name = lambda: SEOUL
    monkeypatch.setitem(sys.modules, 'hermes_time', fake)
    rt = Runtime(GatewayCtx(tmp_path), clock=clock)
    assert rt.policy().profile_timezone == SEOUL
    fake.get_timezone_name = lambda: (_ for _ in ()).throw(RuntimeError('config read failed'))
    assert rt.policy().profile_timezone == SEOUL
