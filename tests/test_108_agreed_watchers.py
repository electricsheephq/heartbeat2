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
