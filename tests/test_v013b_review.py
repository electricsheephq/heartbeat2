"""Regression tests from the cross-model review of the v0.1.3 resume-silence change."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import clock  # noqa: F401
from test_engine import engine, arm, session, start, admit, KEY  # noqa: F401
from test_v013b import NOTE, interrupted, replay
from heartbeat2 import commands
from heartbeat2.intake import Intake
from heartbeat2.poller import Poller


def hist(w, o, cur):
    text = f'⟦pulse o:{w.id}/{o.dedupe_key} a:{o.attempt}⟧\nWake instructions'
    return [{'role': 'user', 'content': 'owner'}, {'role': 'assistant', 'content': 'reply'},
            {'role': 'user', 'content': text}, cur]


def test_owner_captionless_photo_with_note_is_never_silenced(engine):
    w, o = interrupted(engine)
    admit(engine, 'm-photo', '')
    engine.intake.pre_llm_call(session_id='sid', turn_id='resume', platform='telegram', user_message=NOTE,
        conversation_history=hist(w, o, {'role': 'user', 'content': NOTE, 'display_kind': 'internal_notification'}))
    engine.intake.on_session_end(session_id='sid', turn_id='resume', interrupted=True)
    cur = {'role': 'user', 'content': [{'type': 'text', 'text': NOTE}, {'type': 'image_url', 'image_url': {'url': 'x'}}]}
    engine.intake.pre_llm_call(session_id='sid', turn_id='owner', platform='telegram', user_message=NOTE,
        conversation_history=hist(w, o, cur))
    assert engine.intake.transform_llm_output(session_id='sid', turn_id='owner', response_text='Nice photo') is None


def test_non_string_user_message_does_not_abort_turn_start(engine):
    interrupted(engine)
    engine.intake.pre_llm_call(session_id='sid2', turn_id='t', platform='telegram',
        user_message=[{'type': 'text', 'text': 'x'}],
        conversation_history=[{'role': 'user', 'content': 'x', 'display_kind': 'internal_notification'}])
    after = engine.store.get_session(KEY)
    assert after.current_session_id == 'sid2' and after.busy_since is not None


def test_replay_never_rewrites_a_paused_wake_reason(engine):
    engine.policy = replace(engine.policy, error_pause_after=1)
    w, o = interrupted(engine, kind='every', trigger_json={'kind': 'every', 'every': '1h'}, base_interval_s=3600, eff_interval_s=3600)
    assert w.status == 'paused' and w.status_reason.startswith('governor:')
    replay(engine, w, o)
    w2 = engine.store.get_wake(w.id)
    assert w2.status_reason == w.status_reason and w2.model_turns == w.model_turns + 1
    gate = engine.policy.mutation_gate(w2, action='resume', session_key=KEY, created_in='wake_turn',
                                       new_kind=None, babysit=None, untrusted_payload=False)
    assert not gate.allowed


def test_policy_reclamp_on_title_update_keeps_owner_status(engine):
    w = arm(engine, 'every', creator='user', created_in='command', trigger_json={'kind': 'every', 'every': '1h'},
            expires_at=engine.clock.now() + 20 * 86400)
    engine.policy = replace(engine.policy, max_lifetime_days=10)
    assert engine.tool.handle({'action': 'update', 'id': w.id, 'title': 'renamed'}, session_id='sid').get('ok')
    assert not engine.store.get_wake(w.id).agent_edited


def test_first_fire_cache_line_describes_this_wake(engine, monkeypatch):
    now = engine.clock.now()
    session(engine, last_route='openai|gpt-6.1-sol|api.openai.com', last_cache_touch_at=now - 300, last_prompt_tokens=50000)
    monkeypatch.setattr(commands, 'session_env', lambda n: KEY if n == 'HERMES_SESSION_KEY' else '')
    added = commands.handle('add every 20m Owner wake', SimpleNamespace(get_engine=lambda: engine))
    assert '20.0m warm' in added
    w, = engine.store.list_wakes(KEY)
    engine.clock.advance(w.next_due_at - now + 1)
    engine.tick()
    lines = [l for t in (c[0] for c in engine.ctx.calls) for l in t.splitlines() if 'Cache' in l]
    assert lines and all('this wake warm' in l for l in lines), lines


# PR #30 bot-review round: startup ordering, inbound admission, owner-reply lifetime, output tokens.


def crashed(engine, state='started'):
    """An attempt the crashed gateway claimed or started; this process has not reconciled yet."""
    w = arm(engine)
    engine.tick()
    if state == 'started':
        start(engine)
    o, = engine.store.list_occurrences(w.id)
    engine.store.connection.execute('UPDATE occurrences SET epoch=? WHERE wake_id=?', ('crashed-process', w.id))
    engine.intake = Intake(engine)
    return engine.store.get_wake(w.id), engine.store.get_occurrence(w.id, o.dedupe_key)


def reconcile(engine):
    Poller(engine.ctx, engine.store, engine.policy, engine.clock, paused=lambda: False).reconcile()


@pytest.mark.parametrize('order', ['replay_first', 'reconcile_first'])
def test_replay_settles_the_same_whichever_runs_first(engine, order):
    w, o = crashed(engine)
    assert o.state == 'started'
    steps = [lambda: replay(engine, w, o), lambda: reconcile(engine)]
    for step in (steps if order == 'replay_first' else steps[::-1]):
        step()
        if order == 'replay_first' and step is steps[0]:
            assert engine.store.get_session(KEY).busy_since is not None
    assert engine.intake.transform_llm_output(session_id='sid', turn_id='resumed', response_text='Restored') == '[SILENT]'
    o2, w2 = engine.store.get_occurrence(w.id, o.dedupe_key), engine.store.get_wake(w.id)
    assert (o2.state, o2.error, o2.delivered) == ('interrupted', 'restart', False)
    assert (w2.fires, w2.delivered, w2.model_turns, w2.error_streak) == (w.fires, 0, w.model_turns + 1, w.error_streak + 1)
    assert (w2.status, w2.status_reason) == ('failed', 'resumed_after_restart')


def test_replay_of_a_claimed_crashed_attempt_leaves_it_for_refund(engine):
    w, o = crashed(engine, state='claimed')
    assert o.state == 'claimed'
    replay(engine, w, o)
    assert engine.intake.transform_llm_output(session_id='sid', turn_id='resumed', response_text='Restored') == '[SILENT]'
    assert engine.store.get_occurrence(w.id, o.dedupe_key).state == 'claimed'
    reconcile(engine)
    o2 = engine.store.get_occurrence(w.id, o.dedupe_key)
    assert (o2.state, o2.error) == ('pending', 'restart_refund')
    assert engine.store.get_wake(w.id).model_turns == w.model_turns + 1


def test_replay_never_silences_an_attempt_this_process_claimed(engine):
    w, o = crashed(engine)
    engine.store.connection.execute('UPDATE occurrences SET epoch=? WHERE wake_id=?', (engine.epoch, w.id))
    replay(engine, w, o)
    assert engine.intake.transform_llm_output(session_id='sid', turn_id='resumed', response_text='Normal') is None
    assert engine.store.get_occurrence(w.id, o.dedupe_key).state == 'started'
    assert engine.store.get_wake(w.id).model_turns == w.model_turns


@pytest.mark.parametrize('sender,silenced', [('bot', True), ('unauthorized', True), ('owner', False)])
def test_only_an_admitted_inbound_vetoes_replay(engine, sender, silenced):
    w, o = interrupted(engine)
    source = SimpleNamespace(platform=SimpleNamespace(value='telegram'), chat_type='dm', chat_id='unit',
                             message_id='x1', is_bot=sender == 'bot')
    gateway = SimpleNamespace(_session_key_for_source=lambda _: KEY,
                              _is_user_authorized_for_source=lambda _: sender != 'unauthorized')
    engine.intake.pre_gateway_dispatch(event=SimpleNamespace(internal=False, source=source, message_id='x1', text='hi'),
                                       gateway=gateway)
    replay(engine, w, o)
    out = engine.intake.transform_llm_output(session_id='sid', turn_id='resumed', response_text='Restored')
    assert (out == '[SILENT]') is silenced


@pytest.mark.parametrize('usage,out', [({}, 0), ({'completion_tokens': 5}, 5), (SimpleNamespace(output_tokens=7), 7)])
def test_output_tokens_count_without_prompt_counts(engine, usage, out):
    w = arm(engine)
    engine.tick()
    start(engine)
    engine.intake.post_api_request(session_id='sid', turn_id='turn', usage=usage, provider='openai', model='gpt-6.1-sol')
    o, = engine.store.list_occurrences(w.id)
    assert (o.tokens_out, o.unpriced_calls, o.warm_wakes, o.cold_wakes, o.cost_microusd) == (out, 1, 0, 0, 0)
