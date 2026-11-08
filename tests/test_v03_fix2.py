"""FIX2 regressions with synthetic gateway sessions and network denied."""
from dataclasses import replace

import pytest

from heartbeat2 import governor
from heartbeat2.desk import render_desk
from heartbeat2.tool import DESCRIPTION
from test_engine import KEY, arm, end, engine, pending, session, start
from test_v03_fix1 import command, own


CONTRACT = ('- outcome: progress when you did something useful, even silently '
            '(always include a summary of what you did; silent progress without one counts as idle); '
            'no_change when there was nothing to do; done when the work is finished for good; '
            'blocked or needs_attention when you need the owner.')


def test_g1_desk_uses_last_dispatched_turn(engine):
    e = engine
    w = arm(e, 'self', role=True, budget_turns_per_day=1)
    e.dispatch(pending(e, w))
    start(e)
    end(e, 'progress', False, summary='drafted notes')
    e.clock.advance(1)
    e.dispatch(pending(e, e.store.get_wake(w.id), 'budget-skip'))
    assert e.store.get_occurrence(w.id, 'budget-skip').error == 'role_budget'
    text = render_desk(e, e.store.get_session(KEY), clock=e.clock,
                       timezone='UTC', policy=e.policy)
    assert 'last: drafted notes' in text
    assert 'last: skipped' not in text


def test_g2_rendered_contract_requires_progress_summary(engine):
    e = engine
    e.dispatch(pending(e, arm(e)))
    assert CONTRACT in e.ctx.calls[-1][0].splitlines()


def test_g2_tool_description_requires_progress_summary():
    assert ('outcome=progress with notify=false and a summary is real work, '
            'never counted as idle') in DESCRIPTION


@pytest.mark.parametrize('kind', ['role', 'own_time'])
def test_g3_watch_refuses_recurring_responsibilities(engine, monkeypatch, kind):
    e = engine
    w = arm(e, 'self', role=True, next_due_at=e.clock.now()+3600) if kind == 'role' else own(e)
    before = e.store.get_wake(w.id)
    assert command(e, monkeypatch, 'watch '+w.id) == (
        'Error: roles and own time stay recurring; use /pulse slow, /pulse budget or /pulse cancel')
    assert e.store.get_wake(w.id) == before
    ordinary = arm(e)
    assert not command(e, monkeypatch, 'watch '+ordinary.id).startswith('Error:')
    assert e.store.get_wake(ordinary.id).status == 'paused'


@pytest.mark.parametrize('state', ['claimed', 'started'])
def test_g4_force_silent_does_not_reserve_message_slot(engine, state):
    e = engine
    e.policy = replace(e.policy, max_messages_per_day=1)
    w = arm(e, 'every')
    o = pending(e, w)
    e.store.update_occurrence(w.id, o.dedupe_key, 'pending', force_silent=True)
    e.dispatch(o)
    if state == 'started': start(e)
    due = arm(e, creator='agent')
    e.dispatch(pending(e, due))
    o = e.store.get_occurrence(due.id, 't:test')
    assert o.state == 'pending' and o.error != 'daily_message_cap'
    assert e.store.get_wake(due.id).status == 'active'
    assert e.daily(KEY) == {'model_turns': 1, 'delivered': 0}


def test_g5_gc_preserves_daily_role_budget(engine):
    e = engine
    e.policy = replace(e.policy, min_interval_minutes=1, agent_ungated_floor_minutes=1)
    w = arm(e, 'self', role=True, budget_turns_per_day=1,
            base_interval_s=60, eff_interval_s=60)
    e.tick()
    start(e)
    end(e, 'progress', False)
    first = e.store.list_occurrences(w.id)[0]
    for _ in range(201):
        e.clock.advance(60)
        e.tick()
    assert sum(o.error == 'role_budget' for o in e.store.list_occurrences(w.id)) == 201
    e.store.gc(ledger_days=30)
    assert e.store.dispatches_today(w.id, e.policy.resolved_timezone()) == 1
    e.clock.advance(60)
    e.tick()
    assert e.store.list_occurrences(w.id)[-1].error == 'role_budget'
    assert len(e.ctx.calls) == 1
    # Retention protects the exact two-day boundary, then permits normal pruning.
    e.clock.advance(first.claimed_at + 2*86400 - e.clock.now())
    e.store.gc(ledger_days=30)
    assert e.store.get_occurrence(w.id, first.dedupe_key) is not None
    e.clock.advance(1)
    e.store.gc(ledger_days=30)
    assert e.store.get_occurrence(w.id, first.dedupe_key) is None


@pytest.mark.parametrize('kind', ['role', 'builtin'])
@pytest.mark.parametrize('switch', ['reset', 'session'])
def test_g6_rebind_supersedes_claim_and_fences_late_turn(engine, kind, switch):
    e = engine
    # A claimed turn has not started; it must not hold the chat after /new.
    e.policy = replace(e.policy, wake_spacing_minutes=1)
    w = arm(e, 'self', role=True, next_due_at=e.clock.now()+3600) if kind == 'role' else own(e)
    e.dispatch(pending(e, w))
    old_text = e.ctx.calls[-1][0]
    if switch == 'reset':
        e.intake.on_session_reset(old_session_id='sid', new_session_id='new')
    else:
        e.intake._session_switch(e.store.get_wake(w.id), 'new')
        session(e, current_session_id='new')
    o = e.store.get_occurrence(w.id, 't:test')
    assert (o.state, o.error, o.force_silent) == (
        'superseded', 'session_reset' if switch == 'reset' else 'session_switched', True)
    assert e.store.get_wake(w.id).armed_session_id == 'new'
    # Past the ordinary spacing gate, well before the stale-claim timeout.
    e.clock.advance(e.policy.wake_spacing_minutes*60)
    other = arm(e, armed_session_id='new')
    e.tick()
    assert e.store.list_occurrences(other.id)[0].state == 'claimed'
    assert len(e.ctx.calls) == 2
    start(e, sid='sid', message=old_text)
    # The existing invalid-flight sentinel suppresses output; no live flight starts.
    assert e.intake.flights['sid'].invalid
    assert e.store.get_occurrence(w.id, 't:test').state == 'superseded'
    assert e.store.get_wake(w.id).armed_session_id == 'new'
    assert e.tool.handle({'action': 'respond', 'outcome': 'progress', 'notify': True,
                          'summary': 'late update'}, session_id='sid').get('error')
    assert e.intake.transform_llm_output(response_text='late update', session_id='sid') == '[SILENT]'
    e.intake.on_session_end(session_id='sid', completed=True)
    assert not e.store.get_occurrence(w.id, 't:test').delivered
    assert e.store.get_wake(w.id).fires == 0


def test_g7_useful_own_time_never_faster_than_base(engine):
    e = engine
    w = own(e, 48)
    e.clock.advance(48*3600)
    e.tick()
    start(e)
    end(e, 'progress', False)
    updated = e.store.get_wake(w.id)
    assert updated.eff_interval_s == 48*3600
    assert updated.next_due_at == pytest.approx(e.clock.now()+48*3600, abs=1)
    ordinary = arm(e, 'every')
    for streak, expected in ((0, 3600), (3, 7200), (4, 14400)):
        e.store.cas_wake(ordinary.id, 'active', noop_streak=streak)
        governor.apply(e, e.store.get_wake(ordinary.id))
        assert e.store.get_wake(ordinary.id).eff_interval_s == expected


@pytest.mark.parametrize('switch', ['reset', 'session'])
def test_g6_rebind_supersedes_a_started_turn(engine, switch):
    e = engine
    # A role turn already running when the owner starts a new conversation.
    e.policy = replace(e.policy, wake_spacing_minutes=1)
    w = arm(e, 'self', role=True, next_due_at=e.clock.now()+3600)
    e.dispatch(pending(e, w))
    start(e)
    assert e.store.get_occurrence(w.id, 't:test').state == 'started'
    if switch == 'reset':
        e.intake.on_session_reset(old_session_id='sid', new_session_id='new')
    else:
        e.intake._session_switch(e.store.get_wake(w.id), 'new')
        session(e, current_session_id='new')
    o = e.store.get_occurrence(w.id, 't:test')
    assert (o.state, o.force_silent) == ('superseded', True)
    # The old turn can't respond or deliver, and it no longer holds the chat.
    assert e.tool.handle({'action': 'respond', 'outcome': 'progress', 'notify': True,
                          'summary': 'late update'}, session_id='sid').get('error')
    assert e.intake.transform_llm_output(response_text='late update', session_id='sid') == '[SILENT]'
    e.intake.on_session_end(session_id='sid', completed=True)
    assert not e.store.get_occurrence(w.id, 't:test').delivered
    e.clock.advance(e.policy.wake_spacing_minutes*60)
    other = arm(e, armed_session_id='new')
    e.tick()
    assert e.store.list_occurrences(other.id)[0].state == 'claimed'


@pytest.mark.parametrize('kind', ['role', 'agent'])
def test_switch_keeps_wake_turn_limits_for_the_old_turn(engine, kind):
    e = engine
    # A non-lineage switch during a running turn: its later tool calls are still a wake turn's, not the owner's.
    owner_wake = arm(e, 'every', creator='user')
    w = (arm(e, 'self', role=True, budget_turns_per_day=2, next_due_at=e.clock.now()+3600) if kind == 'role'
         else arm(e, 'every', next_due_at=e.clock.now()+3600))
    e.dispatch(pending(e, w))
    start(e)
    e.intake._session_switch(e.store.get_wake(w.id), 'new')
    session(e, current_session_id='new')
    edit = e.tool.handle({'action': 'update', 'id': owner_wake.id, 'note': 'changed by an old turn'}, session_id='sid')
    assert edit.get('error') == 'owner_item', edit
    if kind == 'role':
        e.tool.handle({'action': 'update', 'id': w.id, 'budget': {'turns_per_day': 12}}, session_id='sid')
        assert e.store.get_wake(w.id).budget_turns_per_day == 2


def test_own_time_lifetime_is_settings_owned(engine):
    e = engine
    w = own(e)
    for until in ({'expires': 'in 1h'}, {'owner_replies': True}, {'max_fires': 1}):
        result = e.tool.handle({'action': 'update', 'id': w.id, 'until': until}, session_id='sid')
        assert result.get('error') == 'builtin_settings_owned', result
    assert e.store.get_wake(w.id).expires_at is None


def test_desk_ignores_a_claim_superseded_before_it_started(engine):
    e = engine
    w = arm(e, 'self', role=True, next_due_at=e.clock.now()+3600)
    e.dispatch(pending(e, w))
    start(e)
    end(e, 'progress', False, summary='drafted notes')
    e.clock.advance(601)
    e.dispatch(pending(e, e.store.get_wake(w.id), 'second'))
    e.intake.on_session_reset(old_session_id='sid', new_session_id='new')
    assert e.store.get_occurrence(w.id, 'second').state == 'superseded'
    from heartbeat2.desk import render_desk
    text = render_desk(e, e.store.get_session(KEY), clock=e.clock, timezone=e.policy.resolved_timezone(), policy=e.policy)
    assert 'last: drafted notes' in text, text


def test_own_time_babysit_and_pacing_are_settings_owned(engine):
    e = engine
    w = own(e)
    for extra in ({'babysit': True}, {'pacing': {'max': '7d'}}):
        result = e.tool.handle({'action': 'update', 'id': w.id, **extra}, session_id='sid')
        assert result.get('error') == 'builtin_settings_owned', result
    # A wake that did end (for example after reaching max_fires) comes back clean on the next re-arm.
    e.store.cas_wake(w.id, 'active', status='done', fires=24, max_fires=24, babysit=True)
    e.own_time()
    back = e.store.get_wake(w.id)
    assert (back.status, back.fires, back.max_fires, back.babysit) == ('active', 0, None, False)
