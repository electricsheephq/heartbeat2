from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import sqlite3
import threading

import pytest

from heartbeat2.model import Wake, Session
from heartbeat2.store import Store


@pytest.fixture
def store(tmp_path, clock):
    with Store(tmp_path / "pulse.db", clock) as value:
        yield value


def seed(store):
    wake = store.create_wake(Wake("s", "Check work.", "every", id="w_abcdefgh",
                                  next_due_at=100, eff_interval_s=900))
    assert store.add_occurrence(wake.id, "t:100")
    return wake


def test_schema_migration_idempotence_durability_and_clock(tmp_path, clock):
    path = tmp_path / "nested" / "pulse.db"
    with Store(path, clock) as s:
        wake = seed(s)
        assert wake.created_at == clock.now()
        assert s.get_meta("schema_version") == "1"
        s.set_meta("epoch", "test-epoch")
    with Store(path, clock) as s:
        assert s.get_wake(wake.id) == wake
        assert s.get_meta("epoch") == "test-epoch"
        assert s.get_occurrence(wake.id, "t:100").state == "pending"
        assert s.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert s.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    with sqlite3.connect(path) as db:
        assert {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= {"wakes", "occurrences", "sessions", "events", "ledger", "meta"}


def test_future_schema_refused(tmp_path, clock):
    path = tmp_path / "pulse.db"
    with Store(path, clock) as s:
        s.set_meta("schema_version", "99")
    with pytest.raises(ValueError, match="schema"):
        Store(path, clock)


def test_CAS_claim_start_refund_stale_attempt_and_finish(store, clock):
    wake = seed(store)
    assert store.claim(wake.id, "t:100", epoch="one", pid=42)
    assert not store.claim(wake.id, "t:100", epoch="two", pid=43)
    occ = store.get_occurrence(wake.id, "t:100")
    assert (occ.state, occ.attempt, occ.claimed_at) == ("claimed", 1, clock.now())
    assert not store.refund(wake.id, "t:100", attempt=0)
    assert store.refund(wake.id, "t:100", attempt=1, error="inject false")
    assert not store.refund(wake.id, "t:100", attempt=1)
    assert store.claim(wake.id, "t:100", epoch="two", pid=43)
    assert not store.start(wake.id, "t:100", attempt=1)
    assert store.start(wake.id, "t:100", attempt=2)
    assert not store.refund(wake.id, "t:100", attempt=2)
    clock.advance(20)
    assert store.finish(wake.id, "t:100", attempt=2, outcome="progress", notify=True,
                         summary="Build passed", delivered=True)
    assert not store.finish(wake.id, "t:100", attempt=2, outcome="done")
    occ = store.get_occurrence(wake.id, "t:100")
    assert occ.ended_at == clock.now() and occ.delivered and occ.summary_hash


def test_CAS_race_two_connections_threads(tmp_path, clock):
    path = tmp_path / "pulse.db"
    with Store(path, clock) as s:
        wake = seed(s)
    barrier = threading.Barrier(2)
    def contender(pid):
        with Store(path, clock) as s:
            barrier.wait(timeout=5)
            return s.claim(wake.id, "t:100", epoch="race", pid=pid)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(contender, [41, 42])) == [False, True]
    with Store(path, clock) as s:
        assert s.get_occurrence(wake.id, "t:100").attempt == 1


def test_wake_CAS_immutable_target_and_version(store, clock):
    wake = seed(store)
    assert store.cas_wake(wake.id, "active", expected_version=0, status="paused")
    assert not store.cas_wake(wake.id, "active", status="cancelled")
    assert not store.cas_wake(wake.id, "paused", expected_version=0, status="active")
    assert store.get_wake(wake.id).version == 1
    with pytest.raises(ValueError, match="immutable"):
        store.cas_wake(wake.id, "paused", session_key="other")


def test_occurrence_terminal_transitions_never_rerun(store):
    wake = seed(store)
    assert store.claim(wake.id, "t:100", epoch="one", pid=1)
    assert store.start(wake.id, "t:100", attempt=1)
    assert store.cas_occurrence(wake.id, "t:100", "started", "interrupted", attempt=1)
    assert not store.claim(wake.id, "t:100", epoch="two", pid=2)
    with pytest.raises(ValueError, match="transition"):
        store.cas_occurrence(wake.id, "t:100", "interrupted", "pending")


def test_dedupe_and_coalesce_bounds(store):
    wake = seed(store)
    assert not store.add_occurrence(wake.id, "t:100")
    assert store.add_occurrence(wake.id, "e:one", payload=[{"seq": 0}])
    for n in range(1, 20):
        assert store.coalesce(wake.id, "e:one", [{"seq": n}])
    assert not store.coalesce(wake.id, "e:one", [{"seq": 20}])
    assert len(store.get_occurrence(wake.id, "e:one").payload_json) == 20
    assert store.claim(wake.id, "e:one", epoch="e", pid=1)
    assert not store.coalesce(wake.id, "e:one", [{"seq": 21}])
    with pytest.raises(ValueError, match="payload"):
        store.add_occurrence(wake.id, "e:huge", payload=["é" * 5000])


def test_sessions_events_ledger_and_atomic_rollback(store, clock):
    session = Session("s", platform="telegram", chat_type="dm", last_human_at=clock.now())
    store.upsert_session(session)
    assert store.get_session("s") == session
    assert store.cas_session("s", {"unanswered_count": 0}, unanswered_count=2)
    assert not store.cas_session("s", {"unanswered_count": 0}, unanswered_count=3)
    assert store.add_event("human_message", "unique", {"text": "hello"}, session_key="s")
    assert not store.add_event("human_message", "unique", {}, session_key="s")
    event = store.pending_events()[0]
    assert event["created_at"] == clock.now() and event["payload_json"] == {"text": "hello"}
    assert store.consume_event(event["seq"])
    assert not store.consume_event(event["seq"])
    assert store.pending_events() == []
    with pytest.raises(ValueError, match="payload"):
        store.add_event("bad", "big", {"text": "x" * 4097})
    wake = seed(store)
    store.add_ledger("2025-10-12", wake.id, fires=1, model_turns=1, tokens_in=123)
    store.add_ledger("2025-10-12", wake.id, fires=1)
    assert store.get_ledger("2025-10-12", wake.id)["fires"] == 2
    with pytest.raises(RuntimeError):
        with store.atomic():
            store.cas_wake(wake.id, "active", status="paused")
            raise RuntimeError("rollback")
    assert store.get_wake(wake.id).status == "active"


def test_gc_keeps_live_occurrences_200_terminal_and_retention(store, clock):
    wake = seed(store)
    for n in range(202):
        key = f"t:{n + 1000}"
        store.add_occurrence(wake.id, key)
        store.cas_occurrence(wake.id, key, "pending", "missed")
    store.add_event("old", "old", {})
    store.consume_event(store.pending_events()[0]["seq"])
    store.add_event("live", "live", {})
    store.add_ledger("2020-01-01", wake.id, fires=1)
    clock.advance(8 * 86400)
    store.gc(ledger_days=30)
    assert len(store.list_occurrences(wake.id)) == 200
    assert store.get_occurrence(wake.id, "t:100").state == "pending"
    assert len(store.pending_events()) == 1
    assert store.get_ledger("2020-01-01", wake.id) is None
    assert store.get_meta("last_gc_at") == str(clock.now())


def test_response_and_usage_CAS_while_started(store):
    wake = seed(store)
    store.claim(wake.id, "t:100", epoch="one", pid=1)
    store.start(wake.id, "t:100", attempt=1)
    assert store.update_occurrence(wake.id, "t:100", "started", attempt=1,
                                   outcome="no_change", tokens_in=50)
    assert not store.update_occurrence(wake.id, "t:100", "started", attempt=0,
                                       outcome="done")
    assert store.update_occurrence(wake.id, "t:100", "started", attempt=1,
                                   outcome="progress", notify=True, summary="Passed")
    assert store.get_occurrence(wake.id, "t:100").outcome == "progress"
    store.finish(wake.id, "t:100", attempt=1)
    assert not store.update_occurrence(wake.id, "t:100", "started", attempt=1,
                                       outcome="no_change")


def test_one_shared_connection_claim_race_and_byte_coalesce(tmp_path, clock):
    with Store(tmp_path / "pulse.db", clock) as s:
        wake = seed(s)
        barrier = threading.Barrier(2)
        def contender(pid):
            barrier.wait(timeout=5)
            return s.claim(wake.id, "t:100", epoch="race", pid=pid)
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(contender, [1, 2])) == [False, True]
        assert s.add_occurrence(wake.id, "e:bytes", payload=["x" * 8000])
        assert not s.coalesce(wake.id, "e:bytes", ["é" * 100])
        assert s.get_occurrence(wake.id, "e:bytes").payload_json == ["x" * 8000]


def test_ledger_gc_retains_exact_local_calendar_days(store, clock):
    wake = seed(store)
    for day in ("2025-09-12", "2025-09-13", "2025-10-12"):
        store.add_ledger(day, wake.id, fires=1)
    store.gc(ledger_days=30, timezone="UTC")
    assert store.get_ledger("2025-09-12", wake.id) is None
    assert store.get_ledger("2025-09-13", wake.id)["fires"] == 1
    assert store.get_ledger("2025-10-12", wake.id)["fires"] == 1
