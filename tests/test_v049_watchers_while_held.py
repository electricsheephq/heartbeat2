"""Agent watchers work through message holds and share one report-back turn per chat."""
from dataclasses import replace
import hashlib
import json

import pytest

from heartbeat2.policy import cap_counts
from heartbeat2.schedule import local_day, parse_at
from test_engine import KEY, admit, arm, end, engine, session, start
from test_v038 import react


SUMMARY = 'The build failed; I prepared the fix.'


def hold(e, reason):
    if reason == 'quiet_hours':
        e.store.set_meta('quiet:' + KEY, json.dumps({'until': e.clock.now() + 1200}))
    elif reason == 'unanswered':
        session(e, unanswered_count=e.policy.max_unanswered)
    else:
        # A positive cap another wake already spent today; the policy stays as it is across midnight.
        e.policy = replace(e.policy, max_messages_per_day=1)
        sent = arm(e, 'at', next_due_at=None, status='done')
        e.store.add_ledger(local_day(e.clock.now(), 'UTC'), sent.id, delivered=1)


def watch(e, message='90', **changes):
    return arm(e, 'reaction', next_due_at=None,
               trigger_json={'message_id': message, 'emojis': ['yes']}, **changes)


def match(e, w, *, muted=False):
    react(e, w.trigger_json['message_id'])
    # An owner reaction is engagement; establish the mute after event intake.
    if muted: session(e, unanswered_count=e.policy.max_unanswered)
    o = e.store.list_occurrences(w.id)[-1]
    e.dispatch(o)
    return e.store.get_occurrence(w.id, o.dedupe_key)


def report(e):
    wakes = [w for w in e.store.list_wakes(KEY) if w.trigger_json.get('report_back')]
    assert len(wakes) == 1, 'a held watcher report must arm one report-back wake'
    return wakes[0]


def queued_report(e):
    w = report(e)
    e.times()
    items = e.store.list_occurrences(w.id, state='pending')
    assert len(items) == 1
    return e.store.get_wake(w.id), items[0]


def finish_watch(e, w, *, outcome='progress', notify=True, summary=SUMMARY, muted=False):
    o = match(e, w, muted=muted)
    assert o.state == 'claimed' and o.force_silent
    start(e)
    assert end(e, outcome, notify, summary=summary) == '[SILENT]'
    return e.store.get_occurrence(w.id, o.dedupe_key)


@pytest.mark.parametrize('reason', ['quiet_hours', 'unanswered', 'daily_message_cap'])
def test_agent_match_runs_silently_and_report_back_obeys_hold(engine, reason):
    e = engine
    hold(e, reason)
    w = watch(e)
    o = finish_watch(e, w, muted=reason == 'unanswered')
    assert o.held_reason == 'message_held:' + reason
    assert o.notify and not o.delivered and o.summary == SUMMARY
    assert e.store.held_reports(KEY) == [o]
    back, pending = queued_report(e)
    assert back.id == 'report_' + hashlib.sha1(KEY.encode()).hexdigest()[:16]
    assert back.kind == 'at' and back.creator == 'agent' and back.created_in == 'command'
    assert back.title == 'Report back' and back.armed_session_id == 'sid'
    assert (back.platform, back.chat_type, back.chat_id) == ('telegram', 'dm', 'unit')
    assert back.trigger_json['builtin'] and back.parent_wake_id is None
    assert parse_at(back.trigger_json['at'], now=e.clock.now(), timezone='UTC') == e.clock.now()
    assert cap_counts(e.store, KEY) == (1, 1)
    assert not e.store.get_meta(f'children:{w.id}/{o.dedupe_key}:{o.attempt}')
    expected = {'quiet_hours': ('quiet_hours', True, e.clock.now() + 1200),
                'unanswered': ('unanswered', False, None),
                'daily_message_cap': ('held_daily_cap', True, None)}[reason]
    assert e.gate(back, pending, e.store.get_session(KEY)) == expected
    e.dispatch(pending)
    pending = e.store.get_occurrence(back.id, pending.dedupe_key)
    assert len(e.ctx.calls) == 1
    assert pending.state == ('skipped' if reason == 'unanswered' else 'pending')
    if reason == 'unanswered':
        # The mute skips it; the report waits for the owner's next message.
        assert e.store.get_wake(back.id).status == 'failed'
        assert e.store.held_reports(KEY) == [o]
        return
    if reason == 'daily_message_cap':
        # The message cap holds it to the next local day, like a watcher, so the result still arrives.
        assert pending.held_reason == 'held_daily_cap'
        e.clock.advance(86400)
        assert e.gate(back, e.store.get_occurrence(back.id, pending.dedupe_key), e.store.get_session(KEY))[0] == ''
    else:
        e.clock.advance(1200)
    e.dispatch(e.store.get_occurrence(back.id, pending.dedupe_key))
    text = e.ctx.calls[-1][0]
    assert SUMMARY in text and 'Held for the owner' in text
    assert 'stop checking in' not in text
    started = e.store.get_occurrence(back.id, pending.dedupe_key)
    assert started.state == 'claimed' and not started.force_silent
    start(e)
    assert end(e, 'done', True, summary='The fix is ready.') is None
    assert e.store.get_occurrence(back.id, pending.dedupe_key).delivered
    assert e.store.get_wake(back.id).status == 'done'
    assert not e.store.held_reports(KEY)


def test_owner_watcher_still_waits_for_quiet_end(engine):
    e = engine
    hold(e, 'quiet_hours')
    w = watch(e, creator='user')
    o = match(e, w)
    assert o.state == 'pending' and o.error == 'quiet_hours' and not o.force_silent
    assert not e.ctx.calls
    e.clock.advance(1200)
    e.dispatch(o)
    assert len(e.ctx.calls) == 1
    assert not e.store.get_occurrence(w.id, o.dedupe_key).force_silent


def test_agent_watcher_still_waits_for_model_capacity(engine):
    hold(engine, 'quiet_hours')
    engine.policy = replace(engine.policy, max_model_wakes_per_day=0)
    o = match(engine, watch(engine))
    assert o.state == 'pending' and o.error == o.held_reason == 'held_daily_cap'
    assert not engine.ctx.calls


def test_two_reports_share_pending_occurrence_and_deliver_together(engine):
    e = engine
    hold(e, 'quiet_hours')
    finish_watch(e, watch(e), summary='Build failure investigated.')
    back, pending = queued_report(e)
    e.dispatch(pending)
    e.clock.advance(601)
    finish_watch(e, watch(e, '91'), summary='Release notes prepared.')
    assert report(e).id == back.id
    assert len(e.store.list_occurrences(back.id)) == 1
    assert e.store.get_occurrence(back.id, pending.dedupe_key).state == 'pending'
    e.clock.advance(600)
    e.dispatch(e.store.get_occurrence(back.id, pending.dedupe_key))
    text = e.ctx.calls[-1][0]
    assert 'Build failure investigated.' in text and 'Release notes prepared.' in text
    start(e)
    assert end(e, 'done', True) is None
    assert not e.store.held_reports(KEY)


def test_owner_receives_reports_before_quiet_end_so_report_back_skips(engine):
    e = engine
    hold(e, 'quiet_hours')
    finish_watch(e, watch(e))
    back, pending = queued_report(e)
    e.clock.advance(1)
    admit(e, mid='back', text="I'm back.")
    result = e.intake.pre_llm_call(session_id='sid', turn_id='back',
                                  user_message="I'm back.", platform='telegram')
    assert SUMMARY in result['context']
    e.intake.on_session_end(session_id='sid', turn_id='back', completed=True)
    assert not e.store.held_reports(KEY)
    e.dispatch(pending)
    skipped = e.store.get_occurrence(back.id, pending.dedupe_key)
    assert skipped.state == 'skipped' and skipped.error == 'nothing_to_report'
    assert e.store.get_wake(back.id).status == 'failed'
    assert len(e.ctx.calls) == 1 and not skipped.delivered


def test_silent_untrusted_watcher_cannot_arm_recurring_work(engine):
    hold(engine, 'quiet_hours')
    w = watch(engine)
    o = match(engine, w)
    assert o.state == 'claimed' and o.force_silent
    assert '<wake-payload' in engine.ctx.calls[-1][0]
    start(engine)
    assert engine.intake.flights['sid'].untrusted
    result = engine.tool.handle(dict(action='create', note='Keep working.',
                                    trigger={'kind': 'every', 'every': '1h'}), session_id='sid')
    assert result['error'] == 'untrusted_rearm'


@pytest.mark.parametrize('outcome,notify,summary', [
    ('progress', False, SUMMARY), ('no_change', True, SUMMARY), ('progress', True, '   '),
])
def test_nonreports_never_arm_report_back(engine, outcome, notify, summary):
    hold(engine, 'quiet_hours')
    finish_watch(engine, watch(engine), outcome=outcome, notify=notify, summary=summary)
    assert not any(w.trigger_json.get('report_back') for w in engine.store.list_wakes())


@pytest.mark.parametrize('reason,wording', [
    ('quiet_hours', 'Quiet hours hold its messages'),
    ('unanswered', 'Muted: messages are held; work still runs.'),
])
def test_receipt_holds_messages_rather_than_agent_watcher(engine, reason, wording):
    hold(engine, reason)
    w = watch(engine)
    receipt = engine.tool.receipt(w, engine.store.get_session(KEY))
    assert wording in receipt
    assert 'Quiet hours hold it until' not in receipt
    assert 'Muted: it waits' not in receipt


@pytest.mark.parametrize('status', ['done', 'failed', 'cancelled', 'expired'])
def test_report_back_reactivates_after_terminal_state(engine, status):
    hold(engine, 'quiet_hours')
    finish_watch(engine, watch(engine))
    back = report(engine)
    assert engine.store.cas_wake(back.id, 'active', status=status, status_reason='previous')
    session(engine, current_session_id='new')
    engine.clock.advance(1)
    engine.arm_report_back(KEY)
    current = report(engine)
    assert current.status == 'active' and current.status_reason == ''
    assert current.next_due_at == engine.clock.now() and current.armed_session_id == 'new'


@pytest.mark.parametrize('state', ['pending', 'claimed', 'started'])
def test_report_back_never_duplicates_or_rearms_live_occurrence(engine, state):
    hold(engine, 'quiet_hours')
    finish_watch(engine, watch(engine))
    back, o = queued_report(engine)
    if state != 'pending':
        assert engine.store.claim(back.id, o.dedupe_key, epoch=engine.epoch, pid=1)
    if state == 'started':
        assert engine.store.start(back.id, o.dedupe_key, attempt=1)
    engine.clock.advance(1)
    engine.arm_report_back(KEY)
    assert report(engine) == back
    assert len(engine.store.list_occurrences(back.id)) == 1


def test_report_back_is_not_agent_mutable(engine):
    hold(engine, 'quiet_hours')
    finish_watch(engine, watch(engine))
    back = report(engine)
    detail = "A report-back turn is HeartBeat's; it ends on its own."
    for request in [{'action': 'pause'}, {'action': 'cancel'},
                    {'action': 'update', 'trigger': {'kind': 'at', 'at': 'in 1h'}}]:
        result = engine.tool.handle(dict(id=back.id, **request), session_id='sid')
        assert result == {'error': 'builtin_settings_owned', 'detail': detail}
    assert engine.store.get_wake(back.id) == back


def test_report_back_ended_silently_settles_what_it_saw(engine):
    e = engine
    hold(e, 'quiet_hours')
    w = watch(e)
    o = finish_watch(e, w)
    back, pending = queued_report(e)
    e.clock.advance(1200)
    e.dispatch(e.store.get_occurrence(back.id, pending.dedupe_key))
    assert SUMMARY in e.ctx.calls[-1][0]
    start(e)
    # Nothing still mattered: the turn ends silently, and the owner's next message isn't handed the same report again.
    assert end(e, 'no_change', False) == '[SILENT]'
    assert not e.store.held_reports(KEY)
    assert e.store.get_occurrence(w.id, o.dedupe_key).held_reason == 'reported:quiet_hours'


@pytest.mark.parametrize('reason,promised', [('quiet_hours', True), ('daily_message_cap', True), ('unanswered', False)])
def test_watcher_text_promises_report_back_only_where_one_can_run(engine, reason, promised):
    e = engine
    hold(e, reason)
    w = watch(e)
    match(e, w, muted=reason == 'unanswered')
    text = e.ctx.calls[-1][0]
    assert 'Messages are held right now' in text
    assert ('one report-back turn once messages can be sent' in text) is promised
