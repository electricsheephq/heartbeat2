from dataclasses import replace

import pytest

from test_backend_wakes import ARM, CHAT, bind, hosts, notice_sends
from test_engine import KEY, arm, engine

WATCHDOG = 'interrupted_by_system(turn_liveness_watchdog)'
WATCHDOG_API = 'interrupted_during_api_call(turn_liveness_watchdog)'
NOTICE = "Reminder didn't run; say 'again' in its {surface} chat to retry"

def start(engine, chat=CHAT, turn='wake'):
    engine.tick()
    engine.intake.pre_llm_call(session_id=chat, turn_id=turn, user_message=engine.ctx.calls[-1][0])

@pytest.fixture
def reminder(hosts, monkeypatch, notice_sends):
    _, backend = hosts

    def run(*, owner=True, urgent=False, source='desktop', title='Reminder', start_turn=True, **fields):
        bind(monkeypatch, backend, CHAT, source=source)
        result = backend.tool.handle({**ARM, 'urgent': urgent, **fields})
        assert result['armed'], result
        wake = backend.store.get_wake(result['wake']['id'])
        backend.store.cas_wake(wake.id, wake.status, expected_version=wake.version, owner_requested=owner, title=title)
        sends = notice_sends(backend)
        backend.clock.advance(wake.next_due_at - backend.clock.now() + 601)
        if start_turn: start(backend)
        return backend, wake.id, sends

    return run

@pytest.mark.parametrize('case,reason', [
    ('W1', WATCHDOG), ('W2', WATCHDOG_API), ('W3', WATCHDOG), ('W5', WATCHDOG),
    ('W4', 'interrupted_by_user'), ('W4', 'interrupted_during_api_call'), ('W4', 'stop_command'), ('W4', 'shutdown'),
    ('W4', 'session_interrupt'), ('watchdog-provider', WATCHDOG_API),
    ('W7', 'provider'), ('W8-owner', 'provider'), ('W8-urgent', 'provider'),
])
def test_watchdog(reminder, case, reason):
    tui = reason in {'shutdown', 'session_interrupt'} or case=='W8-urgent'
    e, wid, sends = reminder(owner=case not in {'W7', 'W8-urgent'}, urgent=case=='W8-urgent',
                            source='tui' if tui else 'desktop', until={'max_fires': 1})
    o, = e.store.list_occurrences(wid)
    if case=='W3': e.intake.post_api_request(session_id=CHAT, turn_id='wake', usage={'input_tokens': 1})
    assert e.store.get_occurrence(wid, o.dedupe_key).api_calls == int(case=='W3')
    if reason=='provider' or case=='watchdog-provider': e.store.set_meta('provider_error:'+o.id, 'auth')
    if reason=='provider':
        e.intake.on_session_end(session_id=CHAT, failed=True)
    elif reason.startswith('interrupted'):
        e.intake.on_session_end(session_id=CHAT, interrupted=True, turn_exit_reason=reason)
    else:
        e.intake.agent_loop_stopped(session_key=CHAT, invalidation_reason=reason)
    if case=='watchdog-provider':
        settled = e.store.get_occurrence(wid, o.dedupe_key)
        assert (settled.state, settled.error, settled.attempt) == ('interrupted', 'provider_error:auth', 1)
    resent=case in {'W1','W2','W5'}
    if resent:
        e.clock.advance(e.policy.wake_spacing_minutes * 60)
        start(e, turn='retry')
        assert '(held by a closed app window) (late by ' in e.ctx.calls[-1][0]
        assert e.store.backend_reinject_state(o.id)['count'] == 1
        assert e.store.get_wake(wid).status == 'active' and sends == []
        e.intake.on_session_end(session_id=CHAT, turn_id='retry', interrupted=case=='W5', turn_exit_reason=reason)
    for _ in range(2):
        e.tick()
        assert e.notifications.idle.wait(2)
    w = e.store.get_wake(wid)
    assert (len(e.ctx.calls), w.fires) == (1 + resent, 1)
    if case in {'W1','W2','W3','W5'}: assert w.error_streak == int(case in {'W3','W5'})
    if case=='W4': assert w.status == ('failed' if tui else 'cancelled')
    if case in {'W1','W2'}: assert w.status == 'done'
    notified=case in {'W3','W5','W8-owner','W8-urgent','watchdog-provider'}
    if notified: assert w.status == 'failed'
    assert sends == ([('owner', NOTICE.format(surface='TUI' if tui else 'Desktop'))] if notified else [])

def test_watchdog_W6_recurring_and_gateway(engine, reminder):
    e, _, sends = reminder(trigger={'kind': 'every', 'every': '10m'})
    e.intake.on_session_end(session_id=CHAT, interrupted=True, turn_exit_reason=WATCHDOG)
    e.tick()
    assert len(e.ctx.calls) == 1 and sends == []
    w = arm(engine, 'at', armed_session_id=KEY)
    start(engine, KEY)
    engine.intake.on_session_end(session_id=KEY, interrupted=True, turn_exit_reason=WATCHDOG)
    engine.tick()
    o, = engine.store.list_occurrences(w.id)
    assert (o.state, o.error, len(engine.ctx.calls)) == ('interrupted', 'interrupted', 1)

@pytest.mark.parametrize('message,age,rearm', [('  AgAiN  ', 86399, True), ('again', 86400, False), ('again please', 0, False)])
def test_again_rearms_once(reminder, message, age, rearm):
    e, _, sends = reminder(title='A\n reminder')
    e.intake.on_session_end(session_id=CHAT, failed=True)
    e.tick()
    assert e.notifications.idle.wait(2) and len(sends) == 1
    e.clock.advance(age)
    for turn in ('owner', 'duplicate'):
        result = e.intake.pre_llm_call(session_id=CHAT, turn_id=turn, user_message=message)
        context = "HeartBeat re-queued 'A reminder'; it runs as its own turn next, so only acknowledge."
        assert result == ({'context': context} if rearm and turn=='owner' else None)
        e.intake.on_session_end(session_id=CHAT, turn_id=turn)
        e.tick()
    assert len(e.store.list_occurrences(session_key=CHAT)) == len(e.ctx.calls) == 1 + rearm


@pytest.mark.parametrize('owner', [False, True])
def test_again_preserves_agent_creator_and_owner_request(reminder, owner):
    e, wid, sends = reminder(owner=owner, urgent=not owner)
    e.intake.on_session_end(session_id=CHAT, failed=True)
    e.tick()
    assert e.notifications.idle.wait(2) and len(sends) == 1
    e.intake.pre_llm_call(session_id=CHAT, turn_id='owner', user_message='again')
    rearmed, = [wake for wake in e.store.list_wakes(CHAT) if wake.id != wid]
    assert (rearmed.creator, rearmed.owner_requested) == ('agent', owner)


def test_asleep_urgent_wake_skips_without_notice(reminder):
    e, wid, sends = reminder(owner=False, urgent=True, start_turn=False)
    e.policy = replace(e.policy, sleep_after_days=1)
    chat = e.store.get_session(CHAT)
    e.store.cas_session(CHAT, {'last_human_at': chat.last_human_at},
                        last_human_at=e.clock.now() - e.policy.sleep_after_days * 86400)
    e.tick()
    assert e.notifications.idle.wait(2)
    o, = e.store.list_occurrences(wid)
    assert (o.state, o.error) == ('skipped', 'asleep')
    assert sends == []


def test_dead_owner_wake_notifies_once(reminder, monkeypatch):
    e, wid, sends = reminder(start_turn=False, title='')

    def fail_inject(*args, **kwargs):
        raise RuntimeError('injection failed')

    monkeypatch.setattr(e.ctx, 'inject_message', fail_inject)
    for delay in (0, 30, 60, 300, 900, 0):
        e.clock.advance(delay)
        e.tick()
        assert e.notifications.idle.wait(2)
    o, = e.store.list_occurrences(wid)
    assert (o.state, o.error) == ('dead', 'inject_failed')
    assert sends == [('owner', "Your wake didn't run; say 'again' in its Desktop chat to retry")]
