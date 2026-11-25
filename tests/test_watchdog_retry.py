import pytest

from test_backend_wakes import CHAT, arm_backend_wake, hosts, notice_sends
from test_engine import KEY, arm, engine

WATCHDOG = 'interrupted_by_system(turn_liveness_watchdog)'
WATCHDOG_API = 'interrupted_during_api_call(turn_liveness_watchdog)'
NOTICE = ('owner', "Reminder didn't run; reply 'again' to retry")

def start(engine, chat=CHAT, turn='wake'):
    engine.tick()
    engine.intake.pre_llm_call(session_id=chat, turn_id=turn, user_message=engine.ctx.calls[-1][0])

@pytest.fixture
def reminder(hosts, monkeypatch, notice_sends):
    _, backend = hosts

    def run(*, owner=True, urgent=False, **fields):
        wid = arm_backend_wake(backend, monkeypatch, title='Reminder', urgent=urgent, **fields)
        wake = backend.store.get_wake(wid)
        backend.store.cas_wake(wid, wake.status, expected_version=wake.version, owner_requested=owner)
        sends = notice_sends(backend)
        backend.clock.advance(wake.next_due_at - backend.clock.now() + 601)
        start(backend)
        return backend, wid, sends

    return run

@pytest.mark.parametrize('case,reason', [
    ('W1', WATCHDOG), ('W2', WATCHDOG_API), ('W3', WATCHDOG), ('W5', WATCHDOG),
    ('W4', 'interrupted_by_user'), ('W4', 'interrupted_during_api_call'), ('W4', 'stop_command'), ('W4', 'shutdown'),
    ('W7', 'provider'), ('W8-owner', 'provider'), ('W8-urgent', 'provider'),
])
def test_watchdog(reminder, case, reason):
    e, wid, sends = reminder(owner=case not in {'W7', 'W8-urgent'}, urgent=case=='W8-urgent', until={'max_fires': 1})
    o, = e.store.list_occurrences(wid)
    if case=='W3': e.intake.post_api_request(session_id=CHAT, turn_id='wake', usage={'input_tokens': 1})
    assert e.store.get_occurrence(wid, o.dedupe_key).api_calls == int(case=='W3')
    if reason=='provider':
        e.store.set_meta('provider_error:'+o.id, 'auth')
        e.intake.on_session_end(session_id=CHAT, failed=True)
    elif reason.startswith('interrupted'):
        e.intake.on_session_end(session_id=CHAT, interrupted=True, turn_exit_reason=reason)
    else:
        e.intake.agent_loop_stopped(session_key=CHAT, invalidation_reason=reason)
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
    if case=='W4' and reason!='shutdown': assert w.status == 'cancelled'
    if case in {'W1','W2'}: assert w.status == 'done'
    notified=case in {'W3','W5','W8-owner','W8-urgent'}
    if notified: assert w.status == 'failed'
    assert sends == ([NOTICE] if notified else [])

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
    e, _, sends = reminder()
    e.intake.on_session_end(session_id=CHAT, failed=True)
    e.tick()
    assert e.notifications.idle.wait(2) and sends == [NOTICE]
    e.clock.advance(age)
    for turn in ('owner', 'duplicate'):
        e.intake.pre_llm_call(session_id=CHAT, turn_id=turn, user_message=message)
        e.intake.on_session_end(session_id=CHAT, turn_id=turn)
        e.tick()
    assert len(e.store.list_occurrences(session_key=CHAT)) == len(e.ctx.calls) == 1 + rearm
