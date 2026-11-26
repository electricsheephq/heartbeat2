from dataclasses import replace
import json

import pytest

from heartbeat2.notifier import EPISODE_PREFIX, NOTIFY_AFTER_S, notice_text
from heartbeat2.store import FAILURE_NOTICE_PREFIX, HEADS_UP_PREFIX
from test_backend_wakes import CHAT, hosts, notice_sends, tui_queue
from test_watchdog_retry import NOTICE, WATCHDOG, reminder, start


PARTIAL = "Reminder may not have finished; say 'again' in its Desktop chat to run it again"


def flush_notices(engine):
    engine.notifications.tick(engine)
    assert engine.notifications.idle.wait(2)


def fail_watchdog(engine, *, turn='wake', api_calls=0):
    if api_calls:
        engine.intake.post_api_request(session_id=CHAT, turn_id=turn, usage={'input_tokens': 1})
    engine.intake.on_session_end(session_id=CHAT, turn_id=turn, interrupted=True, turn_exit_reason=WATCHDOG)


def again(engine):
    return engine.intake.pre_llm_call(session_id=CHAT, turn_id='owner', user_message='again')


def requeued(title):
    return {'context': f"HeartBeat re-queued '{title}'; it runs as its own turn next, so only acknowledge."}


@pytest.mark.parametrize('cause', ['no_session_end', 'restart'])
@pytest.mark.parametrize('api_calls', [0, 1], ids=['before-model', 'after-model'])
def test_recovery_notice_and_again(reminder, monkeypatch, cause, api_calls):
    engine, wid, sends = reminder()
    store = engine.store
    if api_calls:
        engine.intake.post_api_request(session_id=CHAT, turn_id='wake', usage={'input_tokens': 1})
    occurrence, = store.list_occurrences(wid)
    assert occurrence.api_calls == api_calls
    if cause == 'no_session_end':
        monkeypatch.setenv('HERMES_AGENT_TIMEOUT', '600')
        engine.clock.advance(7201)
        engine.acks()
    else:
        # Simulate a started claim abandoned by a previous process.
        monkeypatch.setattr(engine, '_own_claim', lambda o: False)
        engine.process_start = True
        engine.reconcile()
    settled, = store.list_occurrences(wid)
    assert (settled.state, settled.error) == ('interrupted', cause)
    flush_notices(engine)
    assert sends == [('owner', PARTIAL if api_calls else NOTICE.format(surface='Desktop'))]
    assert again(engine) == requeued('Reminder')
    assert len(store.list_wakes(CHAT, status='active')) == 1


def test_watchdog_after_model_notice(reminder):
    engine, wid, sends = reminder()
    fail_watchdog(engine, api_calls=1)
    settled, = engine.store.list_occurrences(wid)
    assert (settled.state, settled.error, settled.api_calls) == ('interrupted', 'watchdog', 1)
    flush_notices(engine)
    assert sends == [('owner', PARTIAL)]


def retry_after_heads_up(engine, wid, sends, tui_queue, title):
    store = engine.store
    engine.tick()
    occurrence, = store.list_occurrences(wid)
    tui_queue['queued_prompt'] = {'text': engine.ctx.calls[-1][0]}
    engine.clock.advance(NOTIFY_AFTER_S)  # The injector has held the accepted prompt for ten minutes.
    engine.tick()
    assert engine.notifications.idle.wait(2)
    assert sends == [('owner', f"{title} is due but its Desktop chat isn't ready; "
                               "it'll run on your next message there.")]
    data = json.loads(store.get_meta(EPISODE_PREFIX + CHAT))
    assert occurrence.id in data['accepted_noticed']

    tui_queue['queued_prompt'] = None
    engine.tick()  # First absent read.
    engine.clock.advance(5)
    start(engine, turn='retry')  # Second absent read, re-injection, then turn start.
    retried, = store.list_occurrences(wid)
    assert (retried.id, retried.attempt, retried.state) == (occurrence.id, 2, 'started')
    assert len(engine.ctx.calls) == 2
    return retried.id


@pytest.mark.parametrize('api_calls', [0, 1], ids=['before-model', 'after-model'])
def test_heads_up_then_watchdog_correction(reminder, tui_queue, api_calls):
    engine, wid, sends = reminder(start_turn=False)
    oid = retry_after_heads_up(engine, wid, sends, tui_queue, 'Reminder')
    fail_watchdog(engine, turn='retry', api_calls=api_calls)
    settled, = engine.store.list_occurrences(wid)
    # A zero-call watchdog may retry once. Cut that last attempt too so it settles.
    if not api_calls:
        assert settled.state == 'pending'
        engine.clock.advance(engine.policy.wake_spacing_minutes * 60)
        start(engine, turn='last')
        fail_watchdog(engine, turn='last')
        settled, = engine.store.list_occurrences(wid)
    assert (settled.id, settled.state, settled.error) == (oid, 'interrupted', 'watchdog')
    assert engine.store.get_meta(FAILURE_NOTICE_PREFIX + oid)
    episode = json.loads(engine.store.get_meta(EPISODE_PREFIX + CHAT) or '{}')
    assert oid not in episode.get('accepted_noticed', [])  # The actual start ends the chat episode.
    flush_notices(engine)
    expected = ("may not have finished after all; say 'again' in its Desktop chat to run it again"
                if api_calls else "didn't run after all; say 'again' in its Desktop chat to retry")
    assert sends[1:] == [('owner', f'Update: Reminder {expected}')]


def test_untitled_wake_uses_clipped_note_in_notices_and_again(reminder, tui_queue):
    note = 'Check\n' + 'x' * 70
    title = 'Check…'
    engine, wid, sends = reminder(start_turn=False, title='', note=note)
    retry_after_heads_up(engine, wid, sends, tui_queue, title)
    fail_watchdog(engine, turn='retry', api_calls=1)
    flush_notices(engine)
    context = again(engine)
    assert sends[1:] == [('owner', f"Update: {title} may not have finished after all; "
                                   "say 'again' in its Desktop chat to run it again")]
    assert context == requeued(title)


def test_legacy_failure_record_renders_v051_text(reminder):
    engine, wid, _ = reminder()
    engine.intake.on_session_end(session_id=CHAT, failed=True)
    occurrence, = engine.store.list_occurrences(wid)
    name = FAILURE_NOTICE_PREFIX + occurrence.id
    engine.store.set_meta(name, json.dumps({'id': 'failed:' + occurrence.id, 'attempts': 0}))
    now = engine.clock.now()
    rows = [(engine.store.get_wake(wid), occurrence, now)]
    assert notice_text(engine, engine.store.get_session(CHAT), rows, now) == NOTICE.format(surface='Desktop')


def test_heads_up_receipt_after_failure_queued_still_corrects(reminder):
    # The heads-up send can finish after terminal_one_shot queued the failure record without the flag.
    engine, wid, _ = reminder()
    engine.intake.on_session_end(session_id=CHAT, failed=True)
    occurrence, = engine.store.list_occurrences(wid)
    engine.store.set_meta(FAILURE_NOTICE_PREFIX + occurrence.id, json.dumps({'id': 'failed:' + occurrence.id, 'attempts': 0}))
    engine.store.set_meta(HEADS_UP_PREFIX + occurrence.id, '1')
    now = engine.clock.now()
    rows = [(engine.store.get_wake(wid), occurrence, now)]
    assert notice_text(engine, engine.store.get_session(CHAT), rows, now) == (
        "Update: Reminder didn't run after all; say 'again' in its Desktop chat to retry")


@pytest.mark.parametrize('gate', ['no-notify-via', 'quiet-hours'])
def test_gated_failure_record_is_not_rewritten(reminder, monkeypatch, gate):
    engine, wid, sends = reminder()
    store = engine.store
    engine.intake.on_session_end(session_id=CHAT, failed=True)
    occurrence, = store.list_occurrences(wid)
    name = FAILURE_NOTICE_PREFIX + occurrence.id
    if gate == 'no-notify-via':
        engine.policy = replace(engine.policy, notify_via='')
    else:
        store.set_meta('quiet:' + CHAT, json.dumps({'until': engine.clock.now() + 60}))
    flush_notices(engine)
    original = store.get_meta(name)
    writes = []
    set_meta = store.set_meta

    def record_write(key, value):
        if key == name:
            writes.append(value)
        return set_meta(key, value)

    monkeypatch.setattr(store, 'set_meta', record_write)
    records = []
    for _ in range(2):
        engine.clock.advance(5)
        flush_notices(engine)
        records.append(store.get_meta(name))
    gated_writes = writes.copy()
    assert sends == []
    assert 'queued' not in json.loads(records[-1])
    if gate == 'no-notify-via':
        assert again(engine) is None
        assert [wake.id for wake in store.list_wakes(CHAT)] == [wid]
    else:
        engine.clock.advance(50)
        flush_notices(engine)
        assert sends == [('owner', NOTICE.format(surface='Desktop'))]
        assert json.loads(store.get_meta(name))['queued'] == engine.clock.now()
    assert records == [original, original]
    assert gated_writes == []
