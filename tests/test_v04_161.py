"""An owner writing after a wake finished undoes the idle streaks' effects, never the agent's own pace (#161)."""
from heartbeat2 import governor
from heartbeat2.schedule import next_cron
from test_engine import engine, arm, start, end, admit, KEY


def _fire(e, outcome='progress', **args):
    e.tick(); start(e)
    return end(e, outcome, True, **args)


def test_owner_after_the_turn_keeps_the_requested_next_check(engine):
    e = engine
    w = arm(e, 'self', pacing_min_s=3600, pacing_max_s=6*3600)
    _fire(e, next_check='4h')
    due = e.clock.now()+4*3600
    assert e.store.get_wake(w.id).next_due_at == due
    e.clock.advance(300)
    admit(e, 'owner-161-pace')
    wake = e.store.get_wake(w.id)
    assert wake.engaged == 1 and wake.noop_streak == 0
    assert (wake.next_due_at, wake.eff_interval_s) == (due, 4*3600)


def test_owner_after_the_turn_clears_a_backoff_down_to_the_agents_pace(engine):
    e = engine
    p = e.policy
    w = arm(e, 'self', pacing_min_s=3600, pacing_max_s=6*3600, noop_streak=p.noop_slowdown_after-1)
    _fire(e, 'no_change', next_check='1h')
    finished = e.clock.now()
    wake = e.store.get_wake(w.id)
    assert wake.noop_streak == p.noop_slowdown_after and wake.next_due_at == finished+2*3600  # backed off
    e.clock.advance(300)
    admit(e, 'owner-161-backoff')
    wake = e.store.get_wake(w.id)
    # The agent's 1h, counted from the owner's message: sooner than the backoff, never sooner than asked.
    assert wake.noop_streak == 0 and wake.next_due_at == e.clock.now()+3600


def test_owner_late_in_a_backoff_never_moves_the_wake_later(engine):
    e = engine
    p = e.policy
    w = arm(e, 'self', pacing_min_s=3600, pacing_max_s=6*3600, noop_streak=p.noop_slowdown_after-1)
    _fire(e, 'no_change', next_check='1h')
    due = e.store.get_wake(w.id).next_due_at
    e.clock.advance(2*3600-600)
    admit(e, 'owner-161-late')
    assert e.store.get_wake(w.id).next_due_at == due


def test_owner_reply_lifts_a_noop_pause_they_have_not_seen(engine):
    e = engine
    w = arm(e, 'every', noop_streak=e.policy.noop_pause_after-1)
    _fire(e, 'no_change')
    wake = e.store.get_wake(w.id)
    assert (wake.status, wake.status_reason) == ('paused', 'governor:noop') and e.store.get_meta('card:'+w.id)
    e.clock.advance(60)
    admit(e, 'owner-161-unseen')
    wake = e.store.get_wake(w.id)
    assert (wake.status, wake.status_reason, wake.noop_streak) == ('active', '', 0)
    assert not e.store.get_meta('card:'+w.id)  # The owner's turn shows no card for a pause that no longer holds.
    assert wake.next_due_at > e.clock.now()


def test_a_pause_card_already_shown_stays_for_the_owner(engine):
    e = engine
    w = arm(e, 'every', noop_streak=e.policy.noop_pause_after-1)
    _fire(e, 'no_change')
    e.store.set_meta('card:'+w.id, '')  # Shown on an owner turn.
    e.clock.advance(60)
    admit(e, 'owner-161-seen')
    wake = e.store.get_wake(w.id)
    assert (wake.status, wake.status_reason, wake.noop_streak) == ('paused', 'governor:noop', 0)


def test_an_error_pause_is_never_lifted_by_engagement(engine):
    e = engine
    w = arm(e, 'every')
    _fire(e)
    wake = e.store.get_wake(w.id)
    assert e.store.cas_wake(w.id, 'active', expected_version=wake.version, status='paused',
                            status_reason='governor:error', error_streak=e.policy.error_pause_after)
    e.store.set_meta('card:'+w.id, 'error')
    e.clock.advance(60)
    admit(e, 'owner-161-error')
    wake = e.store.get_wake(w.id)
    assert (wake.status, wake.status_reason) == ('paused', 'governor:error') and e.store.get_meta('card:'+w.id)


def test_owner_reply_returns_skipped_cron_times(engine):
    e = engine
    p = e.policy
    # A minimum interval below the cron period, so a re-apply soon after the turn can still reach the next cron time.
    w = arm(e, 'cron', trigger_json={'cron': '0 * * * *'}, base_interval_s=1800, eff_interval_s=1800,
            noop_streak=p.noop_slowdown_after-1)
    _fire(e, 'no_change')
    base = e.store.get_wake(w.id).base_interval_s
    first = next_cron('0 * * * *', now=e.clock.now(), timezone=p.resolved_timezone(), minimum_interval_s=base)
    skipped = [o for o in e.store.list_occurrences(w.id, state='skipped') if o.error == 'governor']
    assert [o.dedupe_key for o in skipped] == [f't:{first:.17g}']
    day = governor.local_day(e.clock.now(), p.resolved_timezone())
    assert e.store.get_ledger(day, w.id)['governor_skips'] == 1
    e.clock.advance(60)
    admit(e, 'owner-161-cron')
    wake = e.store.get_wake(w.id)
    assert wake.noop_streak == 0 and wake.next_due_at == first
    assert not [o for o in e.store.list_occurrences(w.id, state='skipped') if o.error == 'governor']
    e.clock.set(first) if hasattr(e.clock, 'set') else e.clock.advance(first-e.clock.now())
    e.tick()
    assert e.store.get_occurrence(w.id, f't:{first:.17g}').state != 'skipped'


def test_a_reapply_never_counts_the_same_cron_skip_twice(engine):
    e = engine
    p = e.policy
    w = arm(e, 'cron', trigger_json={'cron': '0 * * * *'}, noop_streak=p.noop_slowdown_after)
    _fire(e, 'no_change')
    day = governor.local_day(e.clock.now(), p.resolved_timezone())
    before = e.store.get_ledger(day, w.id)['governor_skips']
    keys = sorted(o.dedupe_key for o in e.store.list_occurrences(w.id, state='skipped'))
    with e.store.atomic():
        governor.apply(e, e.store.get_wake(w.id))  # e.g. an ignored delivery settling re-applies the same backoff
    assert e.store.get_ledger(day, w.id)['governor_skips'] == before
    assert sorted(o.dedupe_key for o in e.store.list_occurrences(w.id, state='skipped')) == keys
