"""#191: a changed trigger retires an unstarted claim when it is refunded."""
from dataclasses import replace

import pytest

from test_engine import KEY, arm, end, engine, pending, start
from test_v039 import command


RUN_LINE = 'The owner asked to run this now (/pulse run).'


def update(e, w, trigger):
    result = e.tool.handle(dict(action='update', id=w.id, trigger=trigger), session_id='sid2')
    assert result.get('ok'), result
    return e.store.get_wake(w.id)


def assert_superseded(e, o):
    actual = e.store.get_occurrence(o.wake_id, o.dedupe_key)
    assert (actual.state, actual.error, actual.force_silent) == ('superseded', 'trigger_changed', True)
    assert actual.claimed_at is None and actual.epoch is None and actual.pid is None
    assert actual.started_at is None and actual.api_calls == 0
    return actual


def test_claimed_run_timeout_leaves_first_watcher_event_on_new_row(engine, command):
    e = engine
    assert e.acquire()  # Complete startup before manually dispatching the run.
    w = arm(e, 'every', next_due_at=e.clock.now()+3600)
    assert command('run '+w.id).startswith('Queued ')
    old, = e.store.list_occurrences(w.id)
    e.dispatch(old)
    claimed = e.store.get_occurrence(w.id, old.dedupe_key)
    assert claimed.state == 'claimed' and RUN_LINE in e.ctx.calls[-1][0]
    update(e, w, {'kind': 'reaction'})
    assert e.store.get_occurrence(w.id, old.dedupe_key) == claimed
    e.clock.advance(601)
    e.acks()
    retired = assert_superseded(e, old)
    assert e.daily(KEY) == {'model_turns': 0, 'delivered': 0}
    payload = {'text': 'A matching reaction arrived.'}
    assert e.store.add_event('reaction', 'event1', payload, session_key=KEY)
    e.tick()
    rows = e.store.list_occurrences(w.id)
    assert len(rows) == 2 and rows[0] == retired
    new = rows[1]
    assert new.dedupe_key.startswith('e:') and new.state == 'claimed'
    assert new.payload_json == [payload] and len(e.ctx.calls) == 2
    assert payload['text'] in e.ctx.calls[-1][0] and RUN_LINE not in e.ctx.calls[-1][0]
    start(e)
    assert e.intake.flights['sid'].dedupe_key == new.dedupe_key
    end(e)
    assert e.store.get_occurrence(w.id, old.dedupe_key) == retired


@pytest.mark.parametrize('site', ['restart', 'ack_timeout', 'held', 'inject_failed'])
def test_refund_sites_preserve_control_then_supersede_changed_claim(engine, command, monkeypatch, site):
    e = engine
    assert e.acquire()
    e.policy = replace(e.policy, wake_spacing_minutes=0)
    w = arm(e, 'every', next_due_at=e.clock.now()+3600)
    assert command('run '+w.id).startswith('Queued ')
    old, = e.store.list_occurrences(w.id)
    render = e.render_occurrence

    # Each site first proves the unchanged refund, then retries the same row
    # with a trigger edit while it is claimed (including dispatch's two races).
    for changed in (False, True):
        def render_and_hold(*args, **kwargs):
            text = render(*args, **kwargs)
            assert e.store.get_occurrence(w.id, old.dedupe_key).state == 'claimed'
            if changed:
                update(e, w, {'kind': 'reaction'})
            if site == 'held':
                e.ctx.runner.served = False
            return text

        if site in {'held', 'inject_failed'}:
            monkeypatch.setattr(e, 'render_occurrence', render_and_hold)
            e.ctx.accept = site != 'inject_failed'
        e.dispatch(e.store.get_occurrence(w.id, old.dedupe_key))
        e.ctx.runner.served = True
        if site in {'restart', 'ack_timeout'}:
            assert e.store.get_occurrence(w.id, old.dedupe_key).state == 'claimed'
            if changed:
                update(e, w, {'kind': 'reaction'})
            if site == 'restart':
                e.process_start = True
                e.reconcile()
            else:
                e.clock.advance(601)
                e.acks()
        actual = e.store.get_occurrence(w.id, old.dedupe_key)
        if changed:
            assert_superseded(e, actual)
            assert e.store.get_meta('supersede_on_refund:'+actual.id) == 'trigger_changed'
        else:
            error = {'restart': 'restart_refund', 'ack_timeout': 'ack_timeout',
                     'held': '', 'inject_failed': 'inject_failed'}[site]
            assert (actual.state, actual.error) == ('pending', error)
            assert e.store.get_meta('supersede_on_refund:'+actual.id) is None
            assert actual.claimed_at is None and actual.epoch is None and actual.pid is None
        assert actual.attempt == (2 if changed else 1)
        assert e.daily(KEY) == {'model_turns': 0, 'delivered': 0}
        assert e.store.dispatches_today(w.id, e.policy.resolved_timezone()) == 0
        assert e.store.get_wake(w.id).model_turns == 0
        assert e.store.get_wake(w.id).cost_microusd == 0
        assert e.store.agent_cost_today(KEY, e.policy.resolved_timezone()) == {'cost_microusd': 0, 'unpriced_calls': 0}
        if site == 'restart':
            assert e.store.get_meta('recovered:'+actual.id) == '1'
        if site == 'inject_failed':
            assert e.store.get_meta('inject_failures:'+actual.id) == ('2' if changed else '1')
            assert float(e.store.get_meta('not_before:'+actual.id)) == e.clock.now()+(60 if changed else 30)
            e.clock.advance(61)


def test_watcher_deadline_before_old_claim_refund_keeps_fallback(engine, command):
    e = engine
    assert e.acquire()
    w = arm(e, 'every', next_due_at=e.clock.now()+3600)
    assert command('run '+w.id).startswith('Queued ')
    old, = e.store.list_occurrences(w.id)
    e.dispatch(old)
    assert e.store.get_occurrence(w.id, old.dedupe_key).state == 'claimed'
    watcher = update(e, w, {'kind': 'reaction', 'deadline': 'in 1m'})
    assert e.store.get_meta('supersede_on_refund:'+old.id) == 'trigger_changed'
    e.clock.advance(61)
    e.tick()
    assert e.store.get_occurrence(w.id, old.dedupe_key).state == 'claimed'
    fallback = e.store.get_occurrence(w.id, f't:{watcher.deadline_at:.17g}')
    assert fallback is not None
    assert fallback.state in {'pending', 'claimed'}
    assert fallback.payload_json == [{'message': 'deadline passed — nothing happened'}]
    assert e.store.get_wake(w.id).deadline_at is None
    e.clock.advance(540)
    e.acks()
    assert_superseded(e, old)
    e.tick()
    assert_superseded(e, old)
    rows = e.store.list_occurrences(w.id)
    assert len(rows) == 2 and rows[1].id == fallback.id
    assert rows[1].state == 'claimed'
    assert 'deadline passed — nothing happened' in e.ctx.calls[-1][0]
    assert RUN_LINE not in e.ctx.calls[-1][0]


@pytest.mark.parametrize('state', ['claimed', 'pending'])
def test_watcher_deadline_with_unmarked_trigger_occurrence_has_no_fallback(engine, state):
    e = engine
    assert e.acquire()
    w = update(e, arm(e, 'reaction'), {'kind': 'reaction', 'deadline': 'in 1m'})
    old = pending(e, w, 'e:matching-reaction')
    if state == 'claimed':
        e.dispatch(old)
    assert e.store.get_occurrence(w.id, old.dedupe_key).state == state
    assert e.store.get_meta('supersede_on_refund:'+old.id) is None
    e.clock.advance(61)
    e.tick()
    rows = e.store.list_occurrences(w.id)
    assert len(rows) == 1 and rows[0].id == old.id
    assert e.store.get_occurrence(w.id, f't:{w.deadline_at:.17g}') is None
    assert e.store.get_wake(w.id).deadline_at is None


def test_claimed_probe_target_change_refunds_and_keeps_deadline_fallback(engine):
    e = engine
    assert e.acquire()
    trigger = {'kind': 'file', 'path': 'old.txt', 'check_every': 60}
    w = arm(e, 'file', trigger_json=trigger)
    old = pending(e, w, 'e:old-observation')
    e.dispatch(old)
    update(e, w, {**trigger, 'path': 'new.txt', 'deadline': 'in 10m'})
    e.clock.advance(601)
    e.acks()
    assert_superseded(e, old)
    e.times()
    rows = e.store.list_occurrences(w.id)
    assert len(rows) == 2 and rows[1].dedupe_key.startswith('t:')
    assert rows[1].payload_json == [{'message': 'deadline passed — nothing happened'}]


@pytest.mark.parametrize('settlement', ['done', 'restart'])
def test_marked_claim_that_starts_settles_normally(engine, settlement):
    e = engine
    assert e.acquire()
    w = arm(e, 'every', next_due_at=e.clock.now()+3600)
    old = pending(e, w, 'r:old')
    e.dispatch(old)
    update(e, w, {'kind': 'reaction'})
    assert e.store.get_meta('supersede_on_refund:'+old.id) == 'trigger_changed'
    start(e)
    assert e.store.get_occurrence(w.id, old.dedupe_key).state == 'started'
    if settlement == 'done':
        end(e)
    else:
        e.process_start = True
        e.reconcile()
    actual = e.store.get_occurrence(w.id, old.dedupe_key)
    expected = ('done', '') if settlement == 'done' else ('interrupted', 'restart')
    assert (actual.state, actual.error) == expected
    assert actual.started_at is not None
    assert e.store.get_meta('supersede_on_refund:'+old.id) == 'trigger_changed'


def test_claimed_same_kind_refilter_has_no_marker_and_refunds(engine):
    e = engine
    assert e.acquire()
    w = arm(e, 'reaction', trigger_json={'kind': 'reaction', 'emojis': ['yes', 'no']})
    payload = {'emojis': ['no']}
    assert e.store.add_occurrence(w.id, 'e:old-reaction', payload=[payload])
    old = e.store.list_occurrences(w.id)[0]
    e.dispatch(old)
    claimed = e.store.get_occurrence(w.id, old.dedupe_key)
    assert claimed.state == 'claimed'
    update(e, w, {'kind': 'reaction', 'emojis': ['yes']})
    assert e.store.get_meta('supersede_on_refund:'+old.id) is None
    assert e.store.get_occurrence(w.id, old.dedupe_key) == claimed
    e.clock.advance(601)
    e.acks()
    actual = e.store.get_occurrence(w.id, old.dedupe_key)
    assert (actual.state, actual.error) == ('pending', 'ack_timeout')
    assert actual.payload_json == [payload] and not actual.force_silent
    assert actual.claimed_at is None and actual.epoch is None and actual.pid is None
    assert e.store.get_meta('supersede_on_refund:'+old.id) is None


@pytest.mark.parametrize('site', ['ack_timeout', 'inject_failed'])
@pytest.mark.parametrize('changed', [True, False], ids=['marked', 'unmarked'])
def test_attempt_caps_supersede_marked_claim_and_preserve_deadline(engine, monkeypatch, site, changed):
    e = engine
    assert e.acquire()
    e.policy = replace(e.policy, wake_spacing_minutes=0)
    w = arm(e, 'every' if changed else 'reaction', next_due_at=e.clock.now()+3600)
    trigger = {'kind': 'reaction', 'deadline': 'in 1h'}
    if not changed:
        w = update(e, w, trigger)
    old = pending(e, w, 'r:old')
    cap = 3 if site == 'ack_timeout' else 5
    e.ctx.accept = site == 'ack_timeout'
    render = e.render_occurrence
    terminal_calls = []
    terminal = e.terminal_one_shot

    def record_terminal(wake):
        terminal_calls.append(wake.id)
        return terminal(wake)

    def render_and_change(wake, occurrence, *args, **kwargs):
        text = render(wake, occurrence, *args, **kwargs)
        if changed and occurrence.attempt == cap:
            update(e, w, trigger)
            assert e.store.get_meta('supersede_on_refund:'+old.id) == 'trigger_changed'
        return text

    monkeypatch.setattr(e, 'terminal_one_shot', record_terminal)
    monkeypatch.setattr(e, 'render_occurrence', render_and_change)
    for attempt in range(1, cap+1):
        e.dispatch(e.store.get_occurrence(w.id, old.dedupe_key))
        if site == 'ack_timeout':
            assert e.store.get_occurrence(w.id, old.dedupe_key).state == 'claimed'
            e.clock.advance(601)
            e.acks()
            assert e.store.get_meta('accepted:'+old.id) == str(attempt)
        else:
            assert e.store.get_meta('inject_failures:'+old.id) == str(attempt)
            retry_at = float(e.store.get_meta('not_before:'+old.id))
            assert retry_at > e.clock.now()
            if attempt < cap:
                e.clock.advance(retry_at-e.clock.now()+1)
        actual = e.store.get_occurrence(w.id, old.dedupe_key)
        assert actual.attempt == attempt
        if attempt < cap:
            assert (actual.state, actual.error) == ('pending', site)

    assert len(e.ctx.calls) == cap
    if changed:
        assert_superseded(e, old)
        assert terminal_calls == []
        assert e.store.get_wake(w.id).error_streak == 0
        assert e.store.get_meta('notice:'+KEY) is None
        assert e.daily(KEY) == {'model_turns': 0, 'delivered': 0}
        assert e.store.dispatches_today(w.id, e.policy.resolved_timezone()) == 0
        assert e.store.get_wake(w.id).model_turns == 0
        assert e.store.get_wake(w.id).cost_microusd == 0
    else:
        assert (actual.state, actual.error) == ('dead', site)
        assert e.store.get_meta('supersede_on_refund:'+old.id) is None
        assert terminal_calls == [w.id]
        assert e.store.get_wake(w.id).error_streak == 1
        expected_notice = '["A wake was not acknowledged after three attempts."]' if site == 'ack_timeout' else None
        assert e.store.get_meta('notice:'+KEY) == expected_notice

    watcher = e.store.get_wake(w.id)
    assert watcher.status == 'active' and watcher.deadline_at > e.clock.now()
    e.clock.advance(watcher.deadline_at-e.clock.now()+1)
    e.times()
    rows = e.store.list_occurrences(w.id)
    if changed:
        assert len(rows) == 2 and rows[1].dedupe_key.startswith('t:')
        assert rows[1].state == 'pending'
        assert rows[1].payload_json == [{'message': 'deadline passed — nothing happened'}]
    else:
        assert rows == [actual]


def test_gc_removes_supersede_on_refund_marker_with_occurrence(engine):
    e = engine
    w = arm(e, 'every')
    old = pending(e, w, 'r:old')
    kept = pending(e, w, 'r:kept')
    for o in (old, kept):
        assert e.store.cas_occurrence(w.id, o.dedupe_key, 'pending', 'superseded', error='trigger_changed')
        e.store.set_meta('supersede_on_refund:'+o.id, 'trigger_changed')
    e.clock.advance(3*86400)
    e.store.gc(ledger_days=30, keep_occurrences=1)
    assert e.store.get_occurrence(w.id, old.dedupe_key) is None
    assert e.store.get_meta('supersede_on_refund:'+old.id) is None
    assert e.store.get_occurrence(w.id, kept.dedupe_key) is not None
    assert e.store.get_meta('supersede_on_refund:'+kept.id) == 'trigger_changed'
