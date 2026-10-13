"""Busy ownership survives wall-clock jumps; offline gateway/storage proof."""
from pathlib import Path
import sqlite3

import pytest

from heartbeat2.intake import Intake
from heartbeat2.poller import LOCAL_EPOCHS, Poller
from heartbeat2.store import Store
from test_engine import engine, arm, session, start, end, admit, KEY
from test_v013b import replay


@pytest.fixture
def busy_engine(engine):
    # Red on the unchanged base: expose storage for ownership without fixing
    # model, migration, stamps or clears. On the fixed code this is a no-op.
    columns = {r[1] for r in engine.store.connection.execute('PRAGMA table_info(sessions)')}
    if 'busy_epoch' not in columns:
        engine.store.connection.execute('ALTER TABLE sessions ADD COLUMN busy_epoch TEXT')
    return engine


def busy_pair(e):
    return tuple(e.store.connection.execute(
        'SELECT busy_since,busy_epoch FROM sessions WHERE session_key=?', (KEY,)).fetchone())


def stamp(e, since, epoch):
    session(e, busy_since=since)
    e.store.connection.execute('UPDATE sessions SET busy_epoch=? WHERE session_key=?', (epoch, KEY))


def recover(e):
    runner = Poller(e.ctx, e.store, e.policy, e.clock, paused=lambda: False, boot_at=e.boot_at)
    try:
        runner.reconcile()
    finally:
        runner.close()


@pytest.mark.parametrize('offset', [-60, 3600], ids=['past', 'future'])
def test_reconcile_clears_foreign_busy_regardless_of_wall_time(busy_engine, offset):
    e = busy_engine
    stamp(e, e.boot_at + offset, 'crashed-process')
    e.clock.advance(5)
    recover(e)
    assert busy_pair(e) == (None, None)


@pytest.mark.parametrize('offset', [3605, -60], ids=['future_after_step_back', 'before_boot'])
def test_reconcile_preserves_another_local_pollers_busy_flag(busy_engine, offset):
    e = busy_engine
    assert e.epoch in LOCAL_EPOCHS
    stamp(e, e.boot_at + offset, e.epoch)
    e.clock.advance(5)
    before = busy_pair(e)
    recover(e)
    assert busy_pair(e) == before


@pytest.mark.parametrize('offset', [-60, 3600], ids=['past', 'future'])
def test_reconcile_clears_legacy_busy_without_epoch(busy_engine, offset):
    e = busy_engine
    stamp(e, e.boot_at + offset, None)
    e.clock.advance(5)
    recover(e)
    assert busy_pair(e) == (None, None)


@pytest.mark.parametrize('local', [True, False], ids=['local', 'foreign'])
@pytest.mark.parametrize('offset', [-60, 3600], ids=['past', 'future'])
def test_restart_interrupt_uses_busy_ownership_not_time(busy_engine, local, offset):
    e = busy_engine
    w = arm(e)
    e.tick()
    start(e)
    o, = e.store.list_occurrences(w.id)
    assert e.epoch in LOCAL_EPOCHS
    stamp(e, e.boot_at + offset, e.epoch if local else 'crashed-process')
    before = busy_pair(e)
    assert e.interrupt(o, 'restart')
    assert busy_pair(e) == (before if local else (None, None))


def test_human_turn_stamps_own_epoch_and_end_clears_both(busy_engine):
    e = busy_engine
    admit(e)
    e.intake.pre_llm_call(session_id='sid', turn_id='human', platform='telegram', user_message='hello')
    assert busy_pair(e) == (e.clock.now(), e.epoch)
    e.intake.on_session_end(session_id='sid', turn_id='human', completed=True)
    assert busy_pair(e) == (None, None)


@pytest.mark.parametrize('order', ['replay_first', 'reconcile_first'])
def test_replay_turn_stamps_own_epoch_and_survives_recovery(busy_engine, order):
    e = busy_engine
    w = arm(e)
    e.tick()
    start(e)
    o, = e.store.list_occurrences(w.id)
    e.store.connection.execute('UPDATE occurrences SET epoch=? WHERE wake_id=?', ('crashed-process', w.id))
    stamp(e, e.boot_at + 3600, 'crashed-process')
    e.intake = Intake(e)
    e.clock.wall -= 60
    if order == 'reconcile_first':
        recover(e)
    replay(e, w, o)
    assert e.intake.flights['sid'].replay
    assert busy_pair(e) == (e.clock.now(), e.epoch)
    if order == 'replay_first':
        recover(e)
        assert busy_pair(e) == (e.clock.now(), e.epoch)
    e.intake.on_session_end(session_id='sid', turn_id='resumed', completed=True)
    assert busy_pair(e) == (None, None)


def test_recovery_of_foreign_future_busy_allows_due_wake_delivery(busy_engine):
    e = busy_engine
    w = arm(e)
    stamp(e, e.boot_at + 3600, 'crashed-process')
    e.clock.advance(5)
    e.reconcile()
    e.tick()
    assert len(e.ctx.calls) == 1
    start(e)
    end(e, outcome='done', notify=True, summary='Ready.')
    o, = e.store.list_occurrences(w.id)
    assert o.state == 'done' and o.delivered
    assert e.store.get_wake(w.id).delivered == 1
    assert busy_pair(e) == (None, None)


def test_legacy_store_adds_nullable_busy_epoch_idempotently(tmp_path, clock):
    path = tmp_path / 'legacy.db'
    fixture = Path(__file__).parent / 'fixtures/pulse-v0.1.0.sql'
    with sqlite3.connect(path) as db:
        db.executescript(fixture.read_text())
    for _ in range(2):
        with Store(path, clock) as s:
            columns = {r[1]: r[2] for r in s.connection.execute('PRAGMA table_info(sessions)')}
            assert columns.get('busy_epoch') == 'TEXT'
            assert s.get_session('unit').busy_epoch is None
            assert s.get_meta('schema_version') == '1'
            assert s.get_wake('w_v010').note == 'old note'
            assert s.get_occurrence('w_v010', 'old:match').state == 'done'


def test_busy_ttl_clear_remains_wall_clock_and_clears_epoch(busy_engine, monkeypatch):
    e = busy_engine
    monkeypatch.setenv('HERMES_AGENT_TIMEOUT', '1800')
    stamp(e, e.clock.now(), e.epoch)
    e.clock.wall -= 60
    e.acks()
    assert busy_pair(e) == (e.boot_at, e.epoch)
    e.clock.advance(18060)
    e.acks()
    assert busy_pair(e) == (None, None)


@pytest.mark.parametrize('builtin', [True, False])
def test_session_reset_clears_busy_and_epoch(busy_engine, builtin):
    e = busy_engine
    arm(e, trigger_json={'builtin': True} if builtin else {})
    stamp(e, e.clock.now(), e.epoch)
    e.intake.on_session_reset(old_session_id='sid', new_session_id='new-sid')
    assert busy_pair(e) == (None, None)
