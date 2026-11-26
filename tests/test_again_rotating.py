import json

import pytest

from heartbeat2.store import FAILURE_NOTICE_PREFIX
from test_backend_wakes import CHAT, bind, hosts, notice_sends
from test_v052_notices import flush_notices, requeued
from test_watchdog_retry import NOTICE, reminder


@pytest.mark.parametrize('source, case', [
    pytest.param('desktop', 'rotated', id='desktop'),
    pytest.param('tui', 'rotated', id='tui'),
    pytest.param('desktop', 'unrelated', id='unrelated'),
    pytest.param('desktop', 'expired', id='expired'),
    pytest.param('desktop', 'unsent', id='unsent'),
])
def test_again_after_rotating_compaction(reminder, monkeypatch, source, case):
    engine, wid, sends = reminder(source=source)
    store = engine.store
    engine.intake.on_session_end(session_id=CHAT, turn_id='wake', failed=True)
    occurrence, = store.list_occurrences(wid)
    notice_key = FAILURE_NOTICE_PREFIX + occurrence.id
    assert store.get_wake(wid).status == 'failed'
    if case != 'unsent':
        flush_notices(engine)
        assert sends == [('owner', NOTICE.format(surface='TUI' if source == 'tui' else 'Desktop'))]
    assert ('queued' in json.loads(store.get_meta(notice_key))) == (case != 'unsent')

    # The new Hermes ID is also its tool session key. The hook follows the
    # compression lineage before it creates the new row under that key.
    tip = 'fixture-compacted-chat'
    assert store.get_session(tip) is None
    monkeypatch.setattr(engine, 'compression_tip', lambda sid: tip if sid == CHAT else sid)
    bind(monkeypatch, engine, tip, source=source)
    engine.intake.pre_llm_call(session_id=tip, turn_id='compaction', user_message='continue')
    engine.intake.on_session_end(session_id=tip, turn_id='compaction')
    assert store.get_session(CHAT).current_session_id == store.get_session(tip).current_session_id == tip
    failed = store.get_wake(wid)
    assert (failed.session_key, failed.armed_session_id) == (CHAT, tip)

    sid = tip
    if case == 'unrelated':
        sid = 'fixture-unrelated-chat'
        bind(monkeypatch, engine, sid, source=source)
    if case == 'expired':
        engine.clock.advance(86401)
    result = engine.intake.pre_llm_call(session_id=sid, turn_id='owner', user_message='again')
    assert store.get_session(sid).current_session_id == sid

    if case != 'rotated':
        assert result is None
        assert [wake.id for wake in store.list_wakes()] == [wid]
        assert not json.loads(store.get_meta(notice_key)).get('rearmed')
        return

    assert result == requeued('Reminder')
    rearmed, = store.list_wakes(status='active')
    assert (rearmed.session_key, rearmed.armed_session_id) == (CHAT, tip)
    assert json.loads(store.get_meta(notice_key))['rearmed'] is True

    engine.intake.on_session_end(session_id=tip, turn_id='owner')
    assert engine.intake.pre_llm_call(session_id=tip, turn_id='duplicate', user_message='again') is None
    assert {wake.id for wake in store.list_wakes()} == {wid, rearmed.id}
