"""#217: a watcher result that a message hold silenced must not stay silent because the owner paused and resumed its wake.

A held result whose turn a message hold silenced, and whose delivery failed, returns to pending still marked silent with a
message_held: reason. Pausing the wake replaced that reason with held_paused and kept the silence, so after the resume the
result ran as [SILENT] even with no message hold left. Pausing now drops the silence with the reason, as a held_ deferral
does (#216); dispatch checks the message holds again before the turn.
"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from heartbeat2 import commands
from test_engine import engine, session, KEY, pending, arm


def run(e, text, monkeypatch):
    monkeypatch.setattr(commands, 'session_env', lambda n: KEY if n == 'HERMES_SESSION_KEY' else '')
    return commands.handle(text, SimpleNamespace(get_engine=lambda: e))


def silenced_held_result(e, reason='message_held:quiet_hours'):
    w = arm(e, 'subagent_done')
    o = pending(e, w)
    e.store.update_occurrence(w.id, o.dedupe_key, 'pending', held_since=e.clock.now(), held_reason=reason,
                              force_silent=True, error='')
    return w, o


def test_pause_then_resume_lets_the_result_speak_once_no_hold_applies(engine, monkeypatch):
    w, o = silenced_held_result(engine)
    run(engine, f'pause {w.id}', monkeypatch)
    o = engine.store.get_occurrence(w.id, o.dedupe_key)
    assert o.state == 'pending' and o.held_reason == 'held_paused' and not o.force_silent
    run(engine, f'resume {w.id}', monkeypatch)
    engine.clock.advance(61)
    engine.tick()
    o = engine.store.get_occurrence(w.id, o.dedupe_key)
    assert o.state != 'pending' and not o.force_silent
    assert engine.ctx.calls  # the held result ran as a normal turn


def test_a_message_hold_still_in_force_after_the_resume_silences_it_again(engine, monkeypatch):
    w, o = silenced_held_result(engine, 'message_held:daily_message_cap')
    run(engine, f'pause {w.id}', monkeypatch)
    run(engine, f'resume {w.id}', monkeypatch)
    engine.policy = replace(engine.policy, max_messages_per_day=0)
    assert engine.message_hold_reason(engine.store.get_wake(w.id), engine.store.get_session(KEY)) == 'daily_message_cap'
    engine.clock.advance(61)
    engine.tick()
    o = engine.store.get_occurrence(w.id, o.dedupe_key)
    assert o.force_silent and o.held_reason == 'message_held:daily_message_cap'


def test_pausing_a_result_held_for_another_reason_keeps_its_silence_unchanged(engine, monkeypatch):
    # Only a message hold's silence goes with the pause; nothing else is touched.
    w, o = silenced_held_result(engine, 'held_unanswered')
    run(engine, f'pause {w.id}', monkeypatch)
    assert engine.store.get_occurrence(w.id, o.dedupe_key).force_silent
