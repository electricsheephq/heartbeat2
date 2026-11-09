"""Held results return on existing turns; recently finished roles stay visible."""
from dataclasses import replace
import json

import pytest

from heartbeat2.desk import render_desk
from test_engine import KEY, admit, arm, end, engine, pending, session, start
from test_v0113 import owner_turn


SUMMARY = 'Prepared the release notes for the owner.'
OWNER_HEADER = ("HeartBeat 2.0: while the owner's limits held your messages, your wake turns "
                'finished these and wanted to tell the owner. In this reply, tell them what still matters, briefly:')
WAKE_HEADER = ('Held for the owner (their limits held these earlier). '
               'If you message them this turn, include what still matters:')


def desk(e):
    return render_desk(e.store, e.store.get_session(KEY), clock=e.clock,
                       timezone=e.policy.resolved_timezone(), policy=e.policy)


def held_turn(e):
    e.policy = replace(e.policy, quiet_hours='00:00-23:59')
    w = arm(e, 'self', role=True, title='Release notes')
    o = pending(e, w)
    e.dispatch(o)
    start(e)
    assert end(e, 'progress', True, summary=SUMMARY) == '[SILENT]'
    return w, e.store.get_occurrence(w.id, o.dedupe_key)


def recorded(e, w, key, **changes):
    """Finished store fixture, without spending turns or changing policy."""
    o = pending(e, w, key)
    assert e.store.claim(w.id, key, epoch=e.epoch, pid=1)
    assert e.store.start(w.id, key, attempt=1)
    values = dict(held_reason='message_held:quiet_hours', notify=True, delivered=False,
                  outcome='progress', summary=SUMMARY, force_silent=True)
    assert e.store.finish(w.id, key, attempt=1, **{**values, **changes})
    return e.store.get_occurrence(w.id, o.dedupe_key)


def test_quiet_role_retains_report_and_desk_hint(engine):
    w, o = held_turn(engine)
    assert o.state == 'done' and o.held_reason == 'message_held:quiet_hours'
    assert o.notify and not o.delivered
    assert engine.store.held_reports(KEY) == [o]
    assert f'- Held for you: 1 result your wake turns wanted to send (newest: {w.title})' in desk(engine)


def test_owner_reply_receives_report_once(engine):
    e = engine
    _, o = held_turn(e)
    e.clock.advance(1)
    admit(e, mid='back', text="I'm back.")
    result = e.intake.pre_llm_call(session_id='sid', turn_id='back',
                                  user_message="I'm back.", platform='telegram')
    assert OWNER_HEADER in result['context'] and SUMMARY in result['context']
    assert e.store.held_reports(KEY) == [o]
    assert json.loads(e.store.get_meta('owner_report:sid')) == {'turn':'back', 'epoch':e.epoch, 'items':[[o.wake_id, o.dedupe_key]]}
    e.intake.on_session_end(session_id='sid', turn_id='back', completed=True)
    assert not e.store.held_reports(KEY)
    assert e.store.get_occurrence(o.wake_id, o.dedupe_key).held_reason == 'reported:quiet_hours'
    assert not e.store.get_meta('owner_report:sid')
    admit(e, mid='again', text='Anything else?')
    again = e.intake.pre_llm_call(session_id='sid', turn_id='again',
                                 user_message='Anything else?', platform='telegram')
    assert SUMMARY not in (again or {}).get('context', '')
    assert OWNER_HEADER not in (again or {}).get('context', '')
    assert not e.store.held_reports(KEY)


@pytest.mark.parametrize('failure', [{'failed': True}, {'interrupted': True}, {'completed': False}])
def test_failed_owner_reply_keeps_report_for_next_turn(engine, failure):
    e = engine
    _, o = held_turn(e)
    for mid, ending in [('failed', failure), ('retry', {'completed': True})]:
        e.clock.advance(1)
        admit(e, mid=mid, text="I'm back.")
        result = e.intake.pre_llm_call(session_id='sid', turn_id=mid,
                                      user_message="I'm back.", platform='telegram')
        assert OWNER_HEADER in result['context'] and SUMMARY in result['context']
        assert e.store.held_reports(KEY) == [o]
        e.intake.on_session_end(session_id='sid', turn_id=mid, **ending)
        assert not e.store.get_meta('owner_report:sid')
        assert bool(e.store.held_reports(KEY)) is (mid == 'failed')


@pytest.mark.parametrize('kind', ['role', 'own_time'])
def test_sending_turn_reports_only_after_delivery(engine, kind):
    e = engine
    w, held = held_turn(e)
    e.policy = replace(e.policy, quiet_hours='')
    if kind == 'own_time':
        e.policy = replace(e.policy, own_time_hours=6)
        w = arm(e, 'self', trigger_json={'builtin': True, 'own_time': True}, creator='user')
    for n, notify in enumerate((False, True)):
        e.clock.advance(601)
        o = pending(e, e.store.get_wake(w.id), f'report:{n}')
        e.dispatch(o)
        assert e.store.get_occurrence(w.id, o.dedupe_key).state == 'claimed'
        text = e.ctx.calls[-1][0]
        assert WAKE_HEADER in text and SUMMARY in text
        assert text.index('Your desk:') < text.index(WAKE_HEADER) < text.index('Recent outcomes:')
        assert json.loads(e.store.get_meta('report_shown:' + o.id)) == [[held.wake_id, held.dedupe_key]]
        start(e)
        assert end(e, 'progress', notify, summary=f'Follow-up {n}.') == (None if notify else '[SILENT]')
        assert not e.store.get_meta('report_shown:' + o.id)
        assert bool(e.store.held_reports(KEY)) is not notify
        assert e.store.get_occurrence(w.id, o.dedupe_key).delivered is notify


def test_held_and_forced_silent_turns_never_receive_report_list(engine):
    e = engine
    w, held = held_turn(e)
    for n, forced in enumerate((False, True)):
        if forced: e.policy = replace(e.policy, quiet_hours='')
        e.clock.advance(601)
        o = pending(e, e.store.get_wake(w.id), f'silent:{n}')
        if forced:
            e.store.update_occurrence(w.id, o.dedupe_key, 'pending', force_silent=True, held_reason='restart')
            o = e.store.get_occurrence(w.id, o.dedupe_key)
        e.dispatch(o)
        assert WAKE_HEADER not in e.ctx.calls[-1][0]
        assert f'- {w.title} ({w.id}): {SUMMARY}' not in e.ctx.calls[-1][0]
        assert not e.store.get_meta('report_shown:' + o.id)
        start(e)
        end(e, 'no_change', False)
        assert e.store.held_reports(KEY) == [held]


@pytest.mark.parametrize('changes', [
    {'notify': False}, {'outcome': 'no_change'}, {'summary': '  \n '},
    {'delivered': True}, {'held_reason': 'restart'}, {'held_reason': ''},
])
def test_report_exclusions(engine, changes):
    w = arm(engine, 'self', role=True)
    recorded(engine, w, 'excluded', **changes)
    assert engine.store.held_reports(KEY) == []


@pytest.mark.parametrize('outcome', ['progress', 'done', 'needs_attention', 'blocked'])
def test_report_outcomes(engine, outcome):
    w = arm(engine, 'self', role=True)
    o = recorded(engine, w, 'included', outcome=outcome)
    assert engine.store.held_reports(KEY) == [o]


def test_reports_bounded_oldest_first_and_only_shown_acknowledged(engine):
    from heartbeat2.desk import held_report_lines
    e = engine
    w = arm(e, 'self', role=True, title='Release `notes`\nready')
    items = []
    for n in range(7):
        e.clock.advance(1)
        items.append(recorded(e, w, str(n), summary=f'Result `{n}`\nready.'))
    assert e.store.held_reports(KEY) == items
    lines, shown = held_report_lines(e.store, KEY)
    assert lines == [f"- Release 'notes' ready ({w.id}): Result '{n}' ready." for n in range(5)] + ['- … 2 more after these (they come back next time)']
    assert shown == [(o.wake_id, o.dedupe_key) for o in items[:5]]
    assert 'Held for you: 7 results' in desk(e)
    for mid, expected in [('first', items[:5]), ('second', items[5:])]:
        admit(e, mid=mid)
        result = e.intake.pre_llm_call(session_id='sid', turn_id=mid,
                                      user_message='hello', platform='telegram')
        assert json.loads(e.store.get_meta('owner_report:sid'))['items'] == [[o.wake_id, o.dedupe_key] for o in expected]
        for o in expected:
            assert o.summary.replace('`', "'").replace('\n', ' ') in result['context']
        e.intake.on_session_end(session_id='sid', turn_id=mid, completed=True)
        if mid == 'first':
            assert e.store.held_reports(KEY) == items[5:]
            assert 'Held for you: 2 results' in desk(e)
            lines, shown = held_report_lines(e.store, KEY)
            assert lines == [f"- Release 'notes' ready ({w.id}): Result '{n}' ready." for n in range(5, 7)]
            assert shown == [(o.wake_id, o.dedupe_key) for o in items[5:]]
    assert not e.store.held_reports(KEY)
    assert held_report_lines(e.store, KEY) == ([], [])


@pytest.mark.parametrize('last_human', ['earlier', 'never'])
def test_finished_role_visible_until_owner_writes(engine, last_human):
    e = engine
    if last_human == 'never': session(e, last_human_at=None)
    e.clock.advance(1)
    w = arm(e, 'self', role=True, title='Release notes')
    e.dispatch(pending(e, w))
    start(e)
    end(e, 'done', False, summary=SUMMARY)
    assert e.store.get_wake(w.id).status == 'done'
    out = desk(e)
    assert f'- done role {w.id} Release notes · finished ' in out
    assert f'· last: {SUMMARY}' in out and '- Other: 0 wakes, 0 watchers' in out
    e.clock.advance(1)
    owner_turn(e)
    assert f'done role {w.id}' not in desk(e)


def test_finished_role_expires_after_24_hours(engine):
    e = engine
    session(e, last_human_at=None)
    w = arm(e, 'self', role=True)
    e.dispatch(pending(e, w))
    start(e)
    end(e, 'done', False, summary=SUMMARY)
    e.clock.advance(86400)
    assert f'done role {w.id}' in desk(e)
    e.clock.advance(1)
    assert f'done role {w.id}' not in desk(e)


@pytest.mark.parametrize('step_back', [0, -10])
def test_reports_survive_equal_and_backward_finish_times(engine, step_back):
    from heartbeat2.desk import held_report_lines
    e = engine
    w = arm(e, 'self', role=True)
    first = recorded(e, w, 'first')
    _, shown = held_report_lines(e.store, KEY)
    e.store.mark_reported(shown)
    e.clock.advance(step_back)
    second = recorded(e, w, 'second')
    assert second.ended_at <= first.ended_at
    assert e.store.held_reports(KEY) == [second]
    _, shown = held_report_lines(e.store, KEY)
    assert shown == [(second.wake_id, second.dedupe_key)]
    e.store.mark_reported(shown)
    assert not e.store.held_reports(KEY)


def test_mark_reported_touches_only_finished_still_held_reasons(engine):
    e = engine
    w = arm(e, 'self', role=True)
    held = recorded(e, w, 'held')
    reported = recorded(e, w, 'reported', held_reason='reported:quiet_hours')
    other = recorded(e, w, 'other', held_reason='restart')
    live = pending(e, w, 'live')
    e.store.update_occurrence(w.id, live.dedupe_key, 'pending', held_reason='message_held:quiet_hours')
    live = e.store.get_occurrence(w.id, live.dedupe_key)
    pairs = [(o.wake_id, o.dedupe_key) for o in (held, reported, other, live)]
    e.store.mark_reported(pairs)
    assert e.store.get_occurrence(w.id, held.dedupe_key) == replace(held, held_reason='reported:quiet_hours')
    for o in (reported, other, live):
        assert e.store.get_occurrence(w.id, o.dedupe_key) == o
    e.store.mark_reported(pairs)
    assert e.store.get_occurrence(w.id, held.dedupe_key) == replace(held, held_reason='reported:quiet_hours')


def test_mark_reported_is_atomic(engine):
    e = engine
    w = arm(e, 'self', role=True)
    held = recorded(e, w, 'held')
    with pytest.raises(ValueError):
        e.store.mark_reported([(held.wake_id, held.dedupe_key), ('invalid',)])
    assert e.store.get_occurrence(w.id, held.dedupe_key) == held


def test_ceiling_above_a_lowered_profile_maximum(engine, monkeypatch):
    """#141: the clamp note names the number applied, and an owner-asked lowering records the owner's number."""
    from dataclasses import replace
    from types import SimpleNamespace
    from heartbeat2 import commands
    from test_v0113 import owner_turn
    e = engine
    monkeypatch.setattr(commands, 'session_env', lambda n: KEY if n == 'HERMES_SESSION_KEY' else '')
    role = arm(e, 'every', role=True, title='Notes', trigger_json={'kind': 'every', 'every': '2h'})
    e.policy = replace(e.policy, role_turns_per_day_max=5)
    commands.handle(f'budget {role.id} 8/day', SimpleNamespace(get_engine=lambda: e))
    assert e.store.owner_budget(role.id) == 8
    result = e.tool.handle(dict(action='update', id=role.id, budget={'turns_per_day': 9}), session_id='other')
    assert result['wake']['budget_turns_per_day'] == 5
    assert 'role_turns_per_day_max' in result['clamped']['budget.turns_per_day']
    assert '/pulse budget' not in result['clamped']['budget.turns_per_day']
    owner_turn(e)
    e.tool.handle(dict(action='update', id=role.id, budget={'turns_per_day': 6}, owner_asked=True), session_id='sid')
    assert e.store.owner_budget(role.id) == 6


@pytest.mark.parametrize('restart', [False, True])
def test_recovery_turn_never_acknowledges_a_list_it_did_not_see(engine, restart):
    e = engine
    _, o = held_turn(e)
    e.clock.advance(1)
    admit(e, mid='back', text="I'm back.")
    result = e.intake.pre_llm_call(session_id='sid', turn_id='back',
                                  user_message="I'm back.", platform='telegram')
    assert SUMMARY in result['context']
    if restart:
        # The owner turn's process died; the stored list belongs to the old process.
        batch = json.loads(e.store.get_meta('owner_report:sid'))
        e.store.set_meta('owner_report:sid', json.dumps({**batch, 'epoch':'old-process'}))
        turn = 'back'
    else:
        # A recovery turn in the same process: no owner context, so no list.
        turn = 'recovery'
        assert e.intake.pre_llm_call(session_id='sid', turn_id=turn, user_message='',
                                     platform='telegram') is None
    e.intake.on_session_end(session_id='sid', turn_id=turn, completed=True)
    assert e.store.held_reports(KEY) == [o]
    assert not e.store.get_meta('owner_report:sid')
    e.clock.advance(1)
    admit(e, mid='again', text='Hello?')
    again = e.intake.pre_llm_call(session_id='sid', turn_id='again', user_message='Hello?', platform='telegram')
    assert SUMMARY in again['context']
    e.intake.on_session_end(session_id='sid', turn_id='again', completed=True)
    assert not e.store.held_reports(KEY)


def test_upgrade_does_not_bring_back_results_held_before_037(engine):
    from heartbeat2.store import Store
    e = engine
    _, o = held_turn(e)
    assert e.store.held_reports(KEY) == [o]
    # Reopening a 0.3.7 database keeps what it holds.
    Store(e.store.path, e.clock).close()
    assert e.store.held_reports(KEY) == [o]
    # A database from before 0.3.7 has no marker: its old held rows count as reported on upgrade.
    with e.store.atomic():
        e.store.connection.execute("DELETE FROM meta WHERE key='held_reports_from'")
    Store(e.store.path, e.clock).close()
    assert e.store.held_reports(KEY) == []
    assert e.store.get_occurrence(o.wake_id, o.dedupe_key).held_reason == 'reported:quiet_hours'
    assert e.store.get_meta('held_reports_from') == '0.3.7'


def test_reports_wait_while_heartbeat_is_off_for_the_chat(engine):
    e = engine
    _, o = held_turn(e)
    session(e, enabled=False)
    e.clock.advance(1)
    admit(e, mid='off', text='Hi.')
    result = e.intake.pre_llm_call(session_id='sid', turn_id='off', user_message='Hi.', platform='telegram')
    assert SUMMARY not in (result or {}).get('context', '')
    assert not e.store.get_meta('owner_report:sid')
    e.intake.on_session_end(session_id='sid', turn_id='off', completed=True)
    assert e.store.held_reports(KEY) == [o]
    session(e, enabled=True)
    e.clock.advance(1)
    admit(e, mid='on', text='Back on.')
    result = e.intake.pre_llm_call(session_id='sid', turn_id='on', user_message='Back on.', platform='telegram')
    assert SUMMARY in result['context']


def test_history_pruning_keeps_results_still_held_for_the_owner(engine):
    e = engine
    w = arm(e, 'self', role=True)
    held = recorded(e, w, 'held')
    old = [recorded(e, w, f'old{n}', held_reason='', notify=False) for n in range(4)]
    old += [recorded(e, w, 'quiet-unsent', notify=False), recorded(e, w, 'delivered', delivered=True),
            recorded(e, w, 'reported', held_reason='reported:quiet_hours')]
    for n in range(4):
        e.clock.advance(1)
        recorded(e, w, f'new{n}', held_reason='', notify=False)
    with e.store.atomic():
        e.store.connection.execute('UPDATE occurrences SET claimed_at=?', (e.clock.now() - 3 * 86400,))
    e.store.gc(ledger_days=30, keep_occurrences=4)
    assert e.store.held_reports(KEY) == [e.store.get_occurrence(w.id, 'held')]
    assert all(e.store.get_occurrence(w.id, o.dedupe_key) is None for o in old)


def test_reports_wait_while_injection_consent_is_revoked(engine):
    e = engine
    _, o = held_turn(e)
    e.ctx.allowed = False
    e.clock.advance(1)
    admit(e, mid='revoked', text='Hi.')
    result = e.intake.pre_llm_call(session_id='sid', turn_id='revoked', user_message='Hi.', platform='telegram')
    assert SUMMARY not in (result or {}).get('context', '')
    assert e.store.held_reports(KEY) == [o]
    e.ctx.allowed = True
    e.clock.advance(1)
    admit(e, mid='granted', text='Hi again.')
    result = e.intake.pre_llm_call(session_id='sid', turn_id='granted', user_message='Hi again.', platform='telegram')
    assert SUMMARY in result['context']


def test_compressed_owner_reply_still_acknowledges_its_list(engine):
    e = engine
    _, o = held_turn(e)
    e.clock.advance(1)
    admit(e, mid='back', text="I'm back.")
    result = e.intake.pre_llm_call(session_id='sid', turn_id='back', user_message="I'm back.", platform='telegram')
    assert SUMMARY in result['context']
    e.compression_tip = lambda sid: 'tip' if sid == 'sid' else sid
    e.intake.on_session_end(session_id='tip', turn_id='back', completed=True)
    assert not e.store.held_reports(KEY)
    assert not e.store.get_meta('owner_report:sid') and not e.store.get_meta('owner_report:tip')


def test_reports_respect_dm_only_for_group_chats(engine):
    e = engine
    _, o = held_turn(e)
    e.policy = replace(e.policy, dm_only=True)
    e.clock.advance(1)
    admit(e, mid='group', text='Hi all.')
    session(e, chat_type='group')  # admit() records a DM; the chat is a group
    result = e.intake.pre_llm_call(session_id='sid', turn_id='group', user_message='Hi all.', platform='telegram')
    assert SUMMARY not in (result or {}).get('context', '')
    assert e.store.held_reports(KEY) == [o]
    e.policy = replace(e.policy, dm_only=False)
    e.clock.advance(1)
    admit(e, mid='group2', text='Hi again.')
    session(e, chat_type='group')
    result = e.intake.pre_llm_call(session_id='sid', turn_id='group2', user_message='Hi again.', platform='telegram')
    assert SUMMARY in result['context']
