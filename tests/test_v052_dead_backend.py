import os

import pytest

from heartbeat2 import serving
from test_backend_wakes import CHAT, hosts, notice_sends
from test_engine import KEY, arm, engine
from test_v052_notices import PARTIAL, flush_notices
from test_watchdog_retry import NOTICE, reminder, start


@pytest.mark.parametrize('owner', [True, False], ids=['owner', 'urgent'])
@pytest.mark.parametrize('api_calls', [0, 1], ids=['before-model', 'after-model'])
def test_started_wake_recovers_when_peer_dies_after_takeover(reminder, monkeypatch, owner, api_calls):
    backend, wid, sends = reminder(owner=owner, urgent=not owner)
    if api_calls:
        backend.intake.post_api_request(session_id=CHAT, turn_id='wake', usage={'input_tokens': 1})
    store = backend.store
    pid = os.getpid() + 1
    epoch = f'{pid}:10:peer'
    old_backend_alive = True
    monkeypatch.setattr(serving, 'backend_epoch_alive',
                        lambda candidate, *, pid=None: candidate == epoch and old_backend_alive)
    # Model an old backend releasing the lock while its turn is still running.
    backend.release()
    store.connection.execute('UPDATE occurrences SET pid=?,epoch=? WHERE wake_id=?', (pid, epoch, wid))
    store.set_meta('epoch', epoch)
    assert backend.acquire()
    assert not backend.process_start
    before, = store.list_occurrences(wid)
    assert (before.state, before.api_calls) == ('started', api_calls)

    old_backend_alive = False
    backend.acks()

    settled, = store.list_occurrences(wid)
    assert (settled.state, settled.error) == ('interrupted', 'restart')
    wake = store.get_wake(wid)
    assert (wake.status, wake.status_reason) == ('failed', 'interrupted:restart')
    assert store.get_meta('recovered:' + before.id) is None
    flush_notices(backend)
    backend.acks()
    flush_notices(backend)
    assert sends == [('owner', PARTIAL if api_calls else NOTICE.format(surface='Desktop'))]


@pytest.mark.parametrize('claim', ['alive-foreign', 'own', 'dead-foreign-own-only'])
def test_started_backend_claim_is_preserved(reminder, monkeypatch, claim):
    backend, wid, sends = reminder()
    store = backend.store
    if claim != 'own':
        pid = os.getpid() + 1
        store.connection.execute('UPDATE occurrences SET pid=?,epoch=? WHERE wake_id=?',
                                 (pid, f'{pid}:10:peer', wid))
        monkeypatch.setattr(serving, 'backend_epoch_alive', lambda epoch, *, pid=None: claim == 'alive-foreign')
    before, = store.list_occurrences(wid)
    assert before.state == 'started'

    backend.acks(own_only=claim == 'dead-foreign-own-only')

    assert store.list_occurrences(wid) == [before]
    assert store.get_wake(wid).status == 'active'
    flush_notices(backend)
    assert sends == []


def test_started_gateway_claim_is_preserved(engine):
    wake = arm(engine, 'at', armed_session_id=KEY)
    start(engine, KEY)
    store = engine.store
    pid = os.getpid() + 1
    store.connection.execute('UPDATE occurrences SET pid=?,epoch=? WHERE wake_id=?',
                             (pid, f'{pid}:10:peer', wake.id))
    before, = store.list_occurrences(wake.id)
    assert before.state == 'started'

    engine.acks()

    assert store.list_occurrences(wake.id) == [before]
    assert store.get_wake(wake.id).status == 'active'


def test_desktop_session_interrupt_stays_silent(reminder):
    backend, wid, sends = reminder()
    backend.intake.agent_loop_stopped(session_key=CHAT, invalidation_reason='session_interrupt')
    backend.acks()

    settled, = backend.store.list_occurrences(wid)
    assert (settled.state, settled.error) == ('interrupted', 'app_invalidation')
    assert backend.store.get_wake(wid).status == 'failed'
    flush_notices(backend)
    assert sends == []
