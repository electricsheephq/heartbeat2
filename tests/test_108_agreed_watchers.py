"""A watcher the owner asked for in their own message is not held by the unanswered mute (#108 item 1).

The daily caps, the cost cap and the spend class are unchanged: it is still the agent's work.
"""
from dataclasses import replace

import pytest

from heartbeat2 import policy
from test_engine import engine, session, KEY, pending
from test_v0113 import owner_turn, create, finish, flag


def muted(e):
    finish(e)
    session(e, unanswered_count=e.policy.max_unanswered)
    return e.store.get_session(KEY)


@pytest.mark.parametrize('kind', ['subagent_done', 'file'])
def test_an_agreed_watcher_speaks_through_the_mute(engine, kind, tmp_path):
    owner_turn(engine)
    extra = {'trigger': {'kind': 'file', 'path': str(tmp_path/'gate.txt')}} if kind == 'file' else {}
    if kind == 'file': (tmp_path/'gate.txt').write_text('on time\n')
    w, result = create(engine, kind=kind, **extra)
    assert flag(w) is True and 'owner_asked_ignored' not in result
    s = muted(engine)
    assert engine.policy.unanswered_gate(w, s.unanswered_count) is None
    assert engine.message_hold_reason(w, s) == ''  # Its message is not held for the owner's reply.
    assert engine.gate(w, pending(engine, w), s)[0] == ''


def test_an_agreed_watcher_is_still_the_agents_work(engine):
    owner_turn(engine)
    w, _ = create(engine, kind='subagent_done')
    assert flag(w) is True
    assert not policy.owner_class(w) and not policy.owner_requested(w)  # No cap, cost-cap or spend bypass.
    s = muted(engine)
    engine.policy = replace(engine.policy, max_messages_per_day=0)
    assert engine.message_hold_reason(w, s) == 'daily_message_cap'


def test_a_watcher_the_owner_did_not_ask_for_stays_muted(engine):
    owner_turn(engine)
    w, result = create(engine, requested=False, kind='subagent_done')
    assert flag(w) is False
    assert engine.message_hold_reason(w, muted(engine)) == 'unanswered'


def test_a_watcher_armed_outside_an_owner_turn_is_not_agreed(engine):
    engine.intake.pre_llm_call(session_id='sid', turn_id='internal', user_message='Internal event', platform='telegram')
    w, result = create(engine, kind='subagent_done')
    assert flag(w) is False and result['owner_asked_ignored'] == 'not_owner_turn'
    assert engine.message_hold_reason(w, muted(engine)) == 'unanswered'


def test_the_receipt_says_the_mute_does_not_hold_it(engine):
    owner_turn(engine)
    w, result = create(engine, kind='subagent_done')
    s = muted(engine)
    receipt = engine.tool.receipt(engine.store.get_wake(w.id), s)
    assert 'Muted' not in receipt and 'You asked for this watcher: not muted; daily caps still apply.' in receipt


def test_an_agent_edit_without_a_fresh_ask_drops_the_exemption(engine):
    owner_turn(engine)
    w, _ = create(engine, kind='subagent_done')
    assert flag(w) is True
    finish(engine)
    engine.intake.pre_llm_call(session_id='sid', turn_id='internal', user_message='Internal event', platform='telegram')
    assert engine.tool.handle({'action': 'update', 'id': w.id, 'note': 'Check the gate number too.'}, session_id='sid').get('ok')
    w = engine.store.get_wake(w.id)
    assert flag(w) is False and engine.message_hold_reason(w, muted(engine)) == 'unanswered'


@pytest.mark.parametrize('reason', ['quiet_hours', 'daily_message_cap'])
def test_an_agreed_result_held_by_another_limit_is_not_then_held_by_the_mute(engine, reason):
    # Review (Astra): quiet hours or the message cap hold an agreed watcher's alert as a report; once that hold ends,
    # the unanswered mute must not keep the report-back from passing it on. Unrelated held results stay held.
    import test_v049_watchers_while_held as held
    e = engine
    held.hold(e, reason)
    e.policy = replace(e.policy, wake_spacing_minutes=0)  # Two watcher turns back to back.
    agreed = held.watch(e, '90', owner_requested=True)
    other = held.watch(e, '91')
    mine = held.finish_watch(e, agreed, summary='Your flight is delayed two hours.', muted=True)
    theirs = held.finish_watch(e, other, summary='The nightly build is green.', muted=True)
    assert mine.held_reason == 'message_held:'+reason and not mine.delivered and theirs.held_reason.startswith('message_held:')
    back, pending = held.queued_report(e)
    e.clock.advance(86400 if reason == 'daily_message_cap' else 1200)
    s = e.store.get_session(KEY)
    assert s.unanswered_count >= e.policy.max_unanswered
    assert e.gate(back, e.store.get_occurrence(back.id, pending.dedupe_key), s)[0] == ''
    e.dispatch(e.store.get_occurrence(back.id, pending.dedupe_key))
    text = e.ctx.calls[-1][0]
    assert 'Your flight is delayed two hours.' in text and 'The nightly build is green.' not in text
    held.start(e)
    assert held.end(e, 'done', True, summary='Your flight is delayed two hours.') is None
    assert e.store.held_reports(KEY) == [e.store.get_occurrence(other.id, theirs.dedupe_key)]


def test_a_muted_report_back_with_only_unrelated_results_still_waits(engine):
    import test_v049_watchers_while_held as held
    e = engine
    held.hold(e, 'quiet_hours')
    held.finish_watch(e, held.watch(e, '91'), muted=True)
    back, pending = held.queued_report(e)
    e.clock.advance(1200)
    assert e.gate(back, e.store.get_occurrence(back.id, pending.dedupe_key), e.store.get_session(KEY))[0] == 'unanswered'


def test_the_owner_can_grant_an_existing_watcher_on_update(engine):
    # Review (scout): the update path grants too.
    owner_turn(engine)
    w, _ = create(engine, requested=False, kind='subagent_done')
    assert flag(w) is False
    result = engine.tool.handle({'action': 'update', 'id': w.id, 'note': 'Tell me if it fails.', 'owner_asked': True}, session_id='sid')
    assert result.get('ok'), result
    w = engine.store.get_wake(w.id)
    assert flag(w) is True and engine.message_hold_reason(w, muted(engine)) == ''


def test_the_desk_says_agreed_watchers_still_send(engine):
    # Review (scout, Astra): the chat-wide mute line must not tell the agent an agreed watcher's alert is held.
    from test_v03_u3 import desk
    owner_turn(engine)
    create(engine, requested=False, kind='subagent_done')
    muted(engine)
    out = desk(engine)
    assert "Messages held now: the owner hasn't replied yet" in out and 'watchers the owner asked for' not in out
    owner_turn(engine, 'again')
    create(engine, kind='subagent_done')
    muted(engine)
    assert "Messages held now: the owner hasn't replied yet (watchers the owner asked for still send their alerts)" in desk(engine)
