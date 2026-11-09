"""Held results return on existing turns; recently finished roles stay visible."""
from dataclasses import replace

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
    assert not e.store.held_reports(KEY)
    assert e.store.reported_through(KEY) == o.ended_at
    admit(e, mid='again', text='Anything else?')
    again = e.intake.pre_llm_call(session_id='sid', turn_id='again',
                                 user_message='Anything else?', platform='telegram')
    assert SUMMARY not in (again or {}).get('context', '')
    assert OWNER_HEADER not in (again or {}).get('context', '')
    assert not e.store.held_reports(KEY)


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
        assert e.store.get_meta('report_shown:' + o.id) == str(held.ended_at)
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


def test_reports_bounded_sorted_and_watermark_monotonic(engine):
    from heartbeat2.desk import held_report_lines
    e = engine
    w = arm(e, 'self', role=True, title='Release `notes`\nready')
    items = []
    for n in range(7):
        e.clock.advance(1)
        items.append(recorded(e, w, str(n), summary=f'Result `{n}`\nready.'))
    assert e.store.held_reports(KEY) == items
    lines, through = held_report_lines(e.store, KEY)
    assert lines == [f"- Release 'notes' ready ({w.id}): Result '{n}' ready." for n in range(2, 7)] + ['- … 2 more earlier']
    assert through == items[-1].ended_at
    assert 'Held for you: 7 results' in desk(e)
    e.store.mark_reported(KEY, through)
    e.store.mark_reported(KEY, through - 1)
    assert e.store.reported_through(KEY) == through and not e.store.held_reports(KEY)
    assert held_report_lines(e.store, KEY) == ([], 0.0)


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


@pytest.mark.parametrize('value', [None, 'not a number'])
def test_bad_reported_through(engine, value):
    if value is not None: engine.store.set_meta('reported_through:' + KEY, value)
    assert engine.store.reported_through(KEY) == 0.0


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
