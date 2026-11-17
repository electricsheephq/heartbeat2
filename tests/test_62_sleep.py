"""#62: after N days with no owner message, the agent's own wakes and watchers in that chat sleep until the owner writes.

Owner wakes, reminders the owner asked for, watchers the owner asked for and the keep-warm still run. Sleeping work runs
no model turn: one-shots are dropped, recurring wakes skip their turn, and watchers hold one merged result for the owner's
return.
"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from heartbeat2 import commands, policy
from heartbeat2.policy import Policy
from test_engine import engine, session, KEY, pending, arm, admit

DAY = 86400


def away(e, days=2.0, setting=1):
    e.policy = replace(e.policy, sleep_after_days=setting)
    session(e, last_human_at=e.clock.now() - days*DAY)
    return e.store.get_session(KEY)


def gate(e, w):
    return e.gate(w, pending(e, w), e.store.get_session(KEY))


def test_off_by_default_and_nothing_sleeps(engine):
    assert Policy().sleep_after_days == 0
    session(engine, last_human_at=engine.clock.now() - 365*DAY)
    assert gate(engine, arm(engine, 'at'))[0] == ''


@pytest.mark.parametrize('changes,sleeps', [
    (dict(kind='at', creator='user'), False),                       # owner one-shot
    (dict(kind='every', creator='user'), False),                    # owner recurring
    (dict(kind='subagent_done', creator='user'), False),            # owner watcher
    (dict(kind='at', creator='user', agent_edited=True), True),     # the agent edited an owner wake
    (dict(kind='at', owner_requested=True), False),                 # reminder the owner asked for
    (dict(kind='subagent_done', owner_requested=True), False),      # watcher the owner asked for
    (dict(kind='at'), True),                                        # agent one-shot
    (dict(kind='every'), True),                                     # agent recurring
    (dict(kind='subagent_done'), True),                             # agent watcher
    (dict(kind='every', role='researcher'), True),                  # role
    (dict(kind='every', trigger_json={'own_time': True}), True),    # own time
    (dict(kind='owner_silent', trigger_json={'builtin': True}), True),  # built-in check-in
    (dict(kind='every', trigger_json={'builtin': True, 'keep_warm': True}), False),  # the owner's keep-warm
    (dict(kind='every', trigger_json={'builtin': True, 'report_back': True}), True),  # report-back
])
def test_which_wakes_sleep(engine, changes, sleeps):
    kind = changes.pop('kind')
    w = arm(engine, kind, **changes)
    assert policy.sleeps(w) is sleeps


def test_asleep_from_exactly_n_days_and_awake_without_a_record(engine):
    s = away(engine, days=1)
    assert engine.policy.asleep(s, engine.clock.now())
    s = away(engine, days=1 - 1/DAY)
    assert not engine.policy.asleep(s, engine.clock.now())
    session(engine, last_human_at=None)
    assert not engine.policy.asleep(engine.store.get_session(KEY), engine.clock.now())


@pytest.mark.parametrize('changes,expected', [
    (dict(kind='at'), ('asleep', False)),
    (dict(kind='every'), ('asleep', False)),
    (dict(kind='subagent_done'), ('held_asleep', True)),
    (dict(kind='at', creator='user'), ('', None)),
    (dict(kind='at', owner_requested=True), ('', None)),
    (dict(kind='subagent_done', owner_requested=True), ('', None)),
])
def test_the_gate_while_asleep(engine, changes, expected):
    kind = changes.pop('kind')
    w = arm(engine, kind, **changes)
    away(engine)
    reason, defer, _ = gate(engine, w)
    assert reason == expected[0]
    if reason: assert defer is expected[1]


def test_an_agent_one_shot_is_dropped_and_ended(engine):
    w = arm(engine, 'at')
    away(engine)
    engine.tick()
    w = engine.store.get_wake(w.id)
    assert w.status != 'active' and not engine.ctx.calls
    assert [o.error for o in engine.store.list_occurrences(w.id)] == ['asleep']


def test_days_of_sleep_leave_recurring_work_and_roles_untouched(engine):
    far = engine.clock.now() + 90*DAY
    wakes = [arm(engine, 'every', expires_at=far), arm(engine, 'every', role='researcher', expires_at=far)]
    away(engine)
    for _ in range(13 * 24):  # inside the agent's 14-day lifetime cap, which sleep does not change
        engine.clock.advance(3600)
        engine.tick()
    assert not engine.ctx.calls
    for w in wakes:
        w = engine.store.get_wake(w.id)
        assert w.status == 'active' and (w.noop_streak, w.ignored_streak, w.error_streak) == (0, 0, 0)


def test_an_owner_message_wakes_the_chat_and_releases_a_held_watcher(engine):
    w = arm(engine, 'subagent_done')
    away(engine)
    o = pending(engine, w)
    engine.tick()
    assert engine.store.get_occurrence(w.id, o.dedupe_key).error == 'held_asleep' and not engine.ctx.calls
    admit(engine, mid='back', text='I am back')
    assert not engine.policy.asleep(engine.store.get_session(KEY), engine.clock.now())
    engine.clock.advance(61)
    engine.tick()
    assert engine.ctx.calls  # the held result runs once the owner has written


def test_a_pulse_command_does_not_wake_the_chat(engine):
    s = away(engine)
    admit(engine, mid='cmd', text='/pulse status')
    assert engine.store.get_session(KEY).last_human_at == s.last_human_at


def test_status_and_why_say_the_chat_is_asleep(engine, monkeypatch):
    monkeypatch.setattr(commands, 'session_env', lambda n: KEY if n == 'HERMES_SESSION_KEY' else '')
    w = arm(engine, 'at')
    away(engine, days=3)
    engine.tick()
    run = lambda text: commands.handle(text, SimpleNamespace(get_engine=lambda: engine))
    assert 'Asleep' in run('status')
    watcher = arm(engine, 'subagent_done')
    assert 'asleep until you write' in run('list')
    assert 'asleep' in run(f'why {w.id}')


def test_the_setting_changes_the_limits_fingerprint_only_when_set(engine):
    from heartbeat2.intake import limits_fingerprint
    assert limits_fingerprint(replace(engine.policy, sleep_after_days=0)) == limits_fingerprint(engine.policy)
    assert limits_fingerprint(replace(engine.policy, sleep_after_days=7)) != limits_fingerprint(engine.policy)


def test_the_test_preview_agrees_that_the_chat_is_asleep(engine, monkeypatch):
    monkeypatch.setattr(commands, 'session_env', lambda n: KEY if n == 'HERMES_SESSION_KEY' else '')
    w = arm(engine, 'every', expires_at=engine.clock.now() + 7*DAY)
    away(engine)
    out = commands.handle(f'test {w.id}', SimpleNamespace(get_engine=lambda: engine))
    assert 'asleep' in out, out
    assert not engine.ctx.calls


def test_a_held_watcher_that_was_silenced_by_a_message_hold_speaks_after_the_owner_returns(engine):
    # A message-held turn whose injection failed goes back to pending still marked silent; sleep then takes over its hold.
    w = arm(engine, 'subagent_done')
    o = pending(engine, w)
    engine.store.update_occurrence(w.id, o.dedupe_key, 'pending', force_silent=True, held_reason='message_held:unanswered')
    away(engine)
    engine.tick()
    o = engine.store.get_occurrence(w.id, o.dedupe_key)
    assert o.error == 'held_asleep' and not o.force_silent
    admit(engine, mid='back', text='I am back')
    engine.clock.advance(61)
    engine.tick()
    o = engine.store.get_occurrence(w.id, o.dedupe_key)
    assert o.state != 'pending' and not o.force_silent


def test_a_report_back_while_asleep_passes_on_only_the_owners_watcher_results(engine):
    # Review (scout): spec test 7. Quiet hours held both results; the chat then fell asleep.
    import test_v049_watchers_while_held as held
    e = engine
    held.hold(e, 'quiet_hours')
    e.policy = replace(e.policy, wake_spacing_minutes=0)
    agreed = held.watch(e, '90', owner_requested=True)
    other = held.watch(e, '91')
    held.finish_watch(e, agreed, summary='Your flight is delayed two hours.', muted=True)
    held.finish_watch(e, other, summary='The nightly build is green.', muted=True)
    back, pending = held.queued_report(e)
    e.clock.advance(1200)
    away(e)
    s = e.store.get_session(KEY)
    assert e.gate(back, e.store.get_occurrence(back.id, pending.dedupe_key), s)[0] == ''
    e.dispatch(e.store.get_occurrence(back.id, pending.dedupe_key))
    text = e.ctx.calls[-1][0]
    assert 'Your flight is delayed two hours.' in text and 'The nightly build is green.' not in text


def test_a_report_back_while_asleep_with_only_the_agents_results_sleeps(engine):
    import test_v049_watchers_while_held as held
    e = engine
    held.hold(e, 'quiet_hours')
    held.finish_watch(e, held.watch(e, '91'), muted=True)
    back, pending = held.queued_report(e)
    e.clock.advance(1200)
    away(e)
    assert e.gate(back, e.store.get_occurrence(back.id, pending.dedupe_key), e.store.get_session(KEY))[0] == 'asleep'


def test_several_held_watchers_come_back_spaced_when_the_owner_writes(engine):
    # Review (scout): spec test 4, with the default wake spacing.
    assert engine.policy.wake_spacing_minutes > 0
    wakes = [arm(engine, 'subagent_done'), arm(engine, 'subagent_done')]
    away(engine)
    for w in wakes: pending(engine, w)
    engine.tick()
    admit(engine, mid='back', text='I am back')
    engine.clock.advance(61)
    engine.tick()
    states = [[o.state for o in engine.store.list_occurrences(w.id)] for w in wakes]
    assert sum(s != ['pending'] for s in states) == 1, states  # one runs now; the other waits for the spacing
