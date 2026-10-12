"""Regression tests from the cross-model review of the v0.1.3 resume-silence change."""
from dataclasses import replace
from types import SimpleNamespace

from conftest import clock  # noqa: F401
from test_engine import engine, arm, session, admit, KEY  # noqa: F401
from test_v013b import NOTE, interrupted, replay
from heartbeat2 import commands


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
