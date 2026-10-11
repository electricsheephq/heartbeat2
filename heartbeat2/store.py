"""SQLite persistence primitives for the engine, not a scheduler.

Claim/refund CAS follows the idea in Hermes hermes_cli/heartbeat.py:216–246.
All writes serialize through BEGIN IMMEDIATE; methods compose inside atomic().
Use a Store per worker or share one Store (its connection is protected by a lock).
No operation starts threads, reads a Hermes profile, or calls a network service.
"""
from contextlib import contextmanager
from dataclasses import fields, replace, asdict
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any

from .clock import Clock
from .model import (Wake, Occurrence, Session, KINDS, WAKE_STATUSES,
                    TERMINAL_OCCURRENCE_STATES)
from .schedule import local_day

SCHEMA_VERSION = 1
LEDGER_FIELDS = frozenset({"fires", "gated_skips", "governor_skips", "model_turns",
    "noops", "valuable", "delivered", "engaged", "ignored", "tokens_in", "tokens_out", "errors"})
_JSON_FIELDS = {"trigger_json", "probe_state_json", "payload_json"}
_BOOL_FIELDS = {"babysit", "urgent", "cancel_on_reply", "notify", "force_silent",
                "enabled", "first_notice_sent"}
_TRANSITIONS = {
    "pending": {"claimed", "skipped", "missed", "dead", "orphaned", "superseded"},
    "claimed": {"pending", "started", "dead", "interrupted", "orphaned", "superseded"},
    "started": {"done", "interrupted", "orphaned", "superseded"},
}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wakes (
 id TEXT PRIMARY KEY, session_key TEXT NOT NULL, platform TEXT NOT NULL,
 chat_type TEXT NOT NULL, chat_id TEXT NOT NULL, armed_session_id TEXT NOT NULL,
 creator TEXT NOT NULL CHECK(creator IN ('user','agent')),
 created_in TEXT NOT NULL CHECK(created_in IN ('human_turn','wake_turn','command')),
 parent_wake_id TEXT, depth INTEGER NOT NULL, title TEXT NOT NULL CHECK(length(title)<=80),
 note TEXT NOT NULL CHECK(length(note)<=2000), reason TEXT NOT NULL CHECK(length(reason)<=280),
 scratch TEXT NOT NULL CHECK(length(scratch)<=4000), kind TEXT NOT NULL,
 trigger_json TEXT NOT NULL, probe_state_json TEXT, deadline_at REAL,
 base_interval_s REAL, eff_interval_s REAL, pacing_min_s REAL, pacing_max_s REAL,
 babysit INTEGER NOT NULL, urgent INTEGER NOT NULL, cancel_on_reply INTEGER NOT NULL,
 max_fires INTEGER, expires_at REAL, status TEXT NOT NULL,
 status_reason TEXT NOT NULL, next_due_at REAL, last_fired_at REAL,
 fires INTEGER NOT NULL, model_turns INTEGER NOT NULL, gated_skips INTEGER NOT NULL,
 noop_streak INTEGER NOT NULL, ignored_streak INTEGER NOT NULL, error_streak INTEGER NOT NULL,
 delivered INTEGER NOT NULL, engaged INTEGER NOT NULL, tokens_in INTEGER NOT NULL,
 tokens_out INTEGER NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 version INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS wakes_session_status ON wakes(session_key,status);
CREATE INDEX IF NOT EXISTS wakes_due ON wakes(status,next_due_at);
CREATE TABLE IF NOT EXISTS occurrences (
 wake_id TEXT NOT NULL REFERENCES wakes(id), dedupe_key TEXT NOT NULL,
 state TEXT NOT NULL, attempt INTEGER NOT NULL, epoch TEXT, pid INTEGER,
 claimed_at REAL, started_at REAL, ended_at REAL,
 payload_json TEXT NOT NULL CHECK(length(CAST(payload_json AS BLOB))<=8192),
 outcome TEXT, notify INTEGER NOT NULL, summary TEXT NOT NULL, summary_hash TEXT NOT NULL,
 next_check_s REAL, force_silent INTEGER NOT NULL, delivered INTEGER NOT NULL,
 tokens_in INTEGER NOT NULL, tokens_out INTEGER NOT NULL, error TEXT NOT NULL,
 PRIMARY KEY(wake_id,dedupe_key));
CREATE INDEX IF NOT EXISTS occurrences_state ON occurrences(state,claimed_at);
CREATE TABLE IF NOT EXISTS events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, session_key TEXT,
 chat_id TEXT, payload_json TEXT NOT NULL CHECK(length(CAST(payload_json AS BLOB))<=4096),
 dedupe_key TEXT NOT NULL UNIQUE, created_at REAL NOT NULL, consumed_at REAL);
CREATE INDEX IF NOT EXISTS events_pending ON events(consumed_at,seq);
CREATE TABLE IF NOT EXISTS sessions (
 session_key TEXT PRIMARY KEY, platform TEXT NOT NULL, chat_type TEXT NOT NULL,
 chat_id TEXT NOT NULL, current_session_id TEXT NOT NULL, last_human_at REAL,
 last_activity_at REAL, episode_id TEXT NOT NULL, unanswered_count INTEGER NOT NULL,
 busy_since REAL, enabled INTEGER NOT NULL, paused_until REAL,
 first_notice_sent INTEGER NOT NULL, last_wake_turn_at REAL, limits_fingerprint TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sessions_chat ON sessions(platform,chat_id);
CREATE TABLE IF NOT EXISTS ledger (
 day TEXT NOT NULL, wake_id TEXT NOT NULL REFERENCES wakes(id),
 fires INTEGER NOT NULL DEFAULT 0, gated_skips INTEGER NOT NULL DEFAULT 0,
 governor_skips INTEGER NOT NULL DEFAULT 0, model_turns INTEGER NOT NULL DEFAULT 0,
 noops INTEGER NOT NULL DEFAULT 0, valuable INTEGER NOT NULL DEFAULT 0,
 delivered INTEGER NOT NULL DEFAULT 0, engaged INTEGER NOT NULL DEFAULT 0,
 ignored INTEGER NOT NULL DEFAULT 0, tokens_in INTEGER NOT NULL DEFAULT 0,
 tokens_out INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(day,wake_id));
"""


def _json(value: Any, limit: int | None = None) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if limit is not None and len(text.encode("utf-8")) > limit:
        raise ValueError("payload exceeds byte limit")
    return text


def _encode(key: str, value: Any) -> Any:
    return _json(value) if key in _JSON_FIELDS else int(value) if isinstance(value, bool) else value


def _record(cls, row):
    if row is None:
        return None
    data = dict(row)
    for name in _JSON_FIELDS & data.keys():
        data[name] = json.loads(data[name]) if data[name] is not None else None
    for name in _BOOL_FIELDS & data.keys():
        data[name] = bool(data[name])
    if cls is Occurrence:
        data["delivered"] = bool(data["delivered"])
    return cls(**data)


def _updates(cls, updates, forbidden=()):
    names = {f.name for f in fields(cls)}
    for key in updates:
        if key in forbidden:
            raise ValueError(f"immutable field: {key}")
        if key not in names:
            raise ValueError(f"unknown field: {key}")


def _validate_wake(wake: Wake):
    if not wake.id or not wake.session_key or not wake.note:
        raise ValueError("wake needs id, session_key and note")
    if wake.kind not in KINDS or wake.status not in WAKE_STATUSES:
        raise ValueError("invalid kind or status")
    if wake.creator not in {"user", "agent"} or wake.created_in not in {"human_turn", "wake_turn", "command"}:
        raise ValueError("invalid wake provenance")
    for name, limit in (("title", 80), ("note", 2000), ("reason", 280), ("scratch", 4000)):
        if not isinstance(getattr(wake, name), str) or len(getattr(wake, name)) > limit:
            raise ValueError(f"{name} exceeds length limit")
    _json(wake.probe_state_json, 16384)


class Store:
    def __init__(self, path: str | Path, clock: Clock):
        self.path, self.clock = Path(path), clock
        self._lock = threading.RLock()
        self._savepoint = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), timeout=5, isolation_level=None,
                                          check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA busy_timeout=5000")
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA journal_mode=WAL")
            with self.atomic():
                self.connection.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                current = self.get_meta("schema_version")
                if current is not None and int(current) != SCHEMA_VERSION:
                    raise ValueError(f"unsupported schema version: {current}")
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        self.connection.execute(statement)
                self.set_meta("schema_version", str(SCHEMA_VERSION))
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        with self._lock:
            self.connection.close()

    @contextmanager
    def atomic(self):
        """Compose table writes atomically; nested calls use rollback savepoints."""
        with self._lock:
            nested = self.connection.in_transaction
            self._savepoint += 1
            name = f"pulse_{self._savepoint}"
            self.connection.execute(f"SAVEPOINT {name}" if nested else "BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                if nested:
                    self.connection.execute(f"ROLLBACK TO {name}")
                    self.connection.execute(f"RELEASE {name}")
                else:
                    self.connection.rollback()
                raise
            else:
                if nested:
                    self.connection.execute(f"RELEASE {name}")
                else:
                    self.connection.commit()

    def _insert_record(self, table, record):
        data = asdict(record)
        columns = ",".join(data)
        return self.connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({','.join('?' for _ in data)})",
            [_encode(k, v) for k, v in data.items()])

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self.connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def set_meta(self, key: str, value: str):
        with self.atomic():
            self.connection.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                    (key, str(value)))

    def create_wake(self, wake: Wake) -> Wake:
        _validate_wake(wake)
        now = self.clock.now()
        wake = replace(wake, created_at=now, updated_at=now, version=0)
        with self.atomic():
            self._insert_record("wakes", wake)
        return wake

    def get_wake(self, wake_id: str) -> Wake | None:
        with self._lock:
            return _record(Wake, self.connection.execute("SELECT * FROM wakes WHERE id=?", (wake_id,)).fetchone())

    def list_wakes(self, session_key: str | None = None, *, status: str | None = None) -> list[Wake]:
        clauses, values = [], []
        if session_key is not None:
            clauses.append("session_key=?")
            values.append(session_key)
        if status is not None:
            clauses.append("status=?")
            values.append(status)
        with self._lock:
            return [_record(Wake, row) for row in self.connection.execute(
                "SELECT * FROM wakes" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY created_at,id", values)]

    def cas_wake(self, wake_id: str, expected_status: str, *, expected_version: int | None = None,
                 **updates) -> bool:
        _updates(Wake, updates, {"id", "session_key", "created_at", "updated_at", "version"})
        with self.atomic():
            wake = self.get_wake(wake_id)
            if wake is None or wake.status != expected_status or (expected_version is not None and wake.version != expected_version):
                return False
            _validate_wake(replace(wake, **updates))
            updates["updated_at"] = self.clock.now()
            assignments = ",".join(f"{name}=?" for name in updates)
            values = [_encode(k, v) for k, v in updates.items()]
            return self.connection.execute(f"UPDATE wakes SET {assignments},version=version+1 WHERE id=? AND status=? AND version=?",
                values + [wake_id, expected_status, wake.version]).rowcount == 1

    @staticmethod
    def _payload(payload):
        if not isinstance(payload, list) or len(payload) > 20:
            raise ValueError("occurrence payload must be a list of at most 20 items")
        return _json(payload, 8192)

    def add_occurrence(self, wake_id: str, dedupe_key: str, *, payload: list | None = None,
                       state: str = "pending") -> bool:
        if state not in {"pending", "missed", "skipped"}:
            raise ValueError("invalid initial occurrence state")
        if not dedupe_key:
            raise ValueError("dedupe_key is required")
        payload = [] if payload is None else payload
        self._payload(payload)
        occurrence = Occurrence(wake_id, dedupe_key, payload_json=payload, state=state,
                                ended_at=self.clock.now() if state != "pending" else None)
        with self.atomic():
            # Do not mask unrelated integrity failures as a successful dedupe.
            if self.get_occurrence(wake_id, dedupe_key) is not None:
                return False
            self._insert_record("occurrences", occurrence)
            return True

    def get_occurrence(self, wake_id: str, dedupe_key: str) -> Occurrence | None:
        with self._lock:
            return _record(Occurrence, self.connection.execute(
                "SELECT * FROM occurrences WHERE wake_id=? AND dedupe_key=?", (wake_id, dedupe_key)).fetchone())

    def list_occurrences(self, wake_id: str | None = None, *, state: str | None = None,
                         session_key: str | None = None) -> list[Occurrence]:
        clauses, values = [], []
        for column, value in (("o.wake_id", wake_id), ("o.state", state), ("w.session_key", session_key)):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(value)
        with self._lock:
            return [_record(Occurrence, row) for row in self.connection.execute(
                "SELECT o.* FROM occurrences o JOIN wakes w ON w.id=o.wake_id" +
                (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY o.rowid", values)]

    def cas_occurrence(self, wake_id: str, dedupe_key: str, expected_state: str,
                       new_state: str, *, attempt: int | None = None, **updates) -> bool:
        if new_state not in _TRANSITIONS.get(expected_state, set()):
            raise ValueError(f"invalid occurrence transition: {expected_state} → {new_state}")
        _updates(Occurrence, updates, {"wake_id", "dedupe_key", "state", "attempt"})
        if "payload_json" in updates:
            self._payload(updates["payload_json"])
        if expected_state in {"claimed", "started"} and attempt is None:
            raise ValueError("attempt is required for an in-flight transition")
        updates["state"] = new_state
        if new_state in TERMINAL_OCCURRENCE_STATES:
            updates["ended_at"] = self.clock.now()
        clauses = "wake_id=? AND dedupe_key=? AND state=?"
        values = [wake_id, dedupe_key, expected_state]
        if attempt is not None:
            clauses += " AND attempt=?"
            values.append(attempt)
        with self.atomic():
            return self.connection.execute(
                "UPDATE occurrences SET " + ",".join(f"{k}=?" for k in updates) + " WHERE " + clauses,
                [_encode(k, v) for k, v in updates.items()] + values).rowcount == 1

    def claim(self, wake_id: str, dedupe_key: str, *, epoch: str, pid: int) -> bool:
        with self.atomic():
            return self.connection.execute("""UPDATE occurrences SET state='claimed',
                attempt=attempt+1,epoch=?,pid=?,claimed_at=?,started_at=NULL,ended_at=NULL
                WHERE wake_id=? AND dedupe_key=? AND state='pending'""",
                (epoch, pid, self.clock.now(), wake_id, dedupe_key)).rowcount == 1

    def refund(self, wake_id: str, dedupe_key: str, *, attempt: int, error: str = "") -> bool:
        return self.cas_occurrence(wake_id, dedupe_key, "claimed", "pending", attempt=attempt,
                                   epoch=None, pid=None, claimed_at=None, error=error)

    def start(self, wake_id: str, dedupe_key: str, *, attempt: int) -> bool:
        return self.cas_occurrence(wake_id, dedupe_key, "claimed", "started", attempt=attempt,
                                   started_at=self.clock.now())

    def finish(self, wake_id: str, dedupe_key: str, *, attempt: int, **updates) -> bool:
        if "summary" in updates:
            if len(updates["summary"]) > 280:
                raise ValueError("summary exceeds length limit")
            updates["summary_hash"] = hashlib.sha256(updates["summary"].encode("utf-8")).hexdigest()
        return self.cas_occurrence(wake_id, dedupe_key, "started", "done", attempt=attempt, **updates)

    def update_occurrence(self, wake_id: str, dedupe_key: str, expected_state: str, *,
                          attempt: int | None = None, **updates) -> bool:
        """CAS metadata without advancing state (respond, usage, force_silent).

        Totals are absolute values; compose a read/modify/write in atomic() for
        increments. The engine commits scratch/next_check only after success.
        """
        if expected_state not in {"pending", "claimed", "started"} or not updates:
            raise ValueError("metadata updates require a live occurrence")
        _updates(Occurrence, updates, {"wake_id", "dedupe_key", "state", "attempt",
                                      "epoch", "pid", "claimed_at", "started_at", "ended_at"})
        if expected_state != "pending" and attempt is None:
            raise ValueError("attempt is required for an in-flight update")
        if "payload_json" in updates:
            self._payload(updates["payload_json"])
        if "summary" in updates:
            if len(updates["summary"]) > 280:
                raise ValueError("summary exceeds length limit")
            updates["summary_hash"] = hashlib.sha256(updates["summary"].encode("utf-8")).hexdigest()
        where = "wake_id=? AND dedupe_key=? AND state=?"
        params = [wake_id, dedupe_key, expected_state]
        if attempt is not None:
            where += " AND attempt=?"
            params.append(attempt)
        with self.atomic():
            return self.connection.execute(
                "UPDATE occurrences SET " + ",".join(f"{k}=?" for k in updates) + " WHERE " + where,
                [_encode(k, v) for k, v in updates.items()] + params).rowcount == 1

    def coalesce(self, wake_id: str, dedupe_key: str, payload: list) -> bool:
        self._payload(payload)
        with self.atomic():
            occurrence = self.get_occurrence(wake_id, dedupe_key)
            if occurrence is None or occurrence.state != "pending":
                return False
            try:
                encoded = self._payload(occurrence.payload_json + payload)
            except ValueError:
                return False
            return self.connection.execute("UPDATE occurrences SET payload_json=? WHERE wake_id=? AND dedupe_key=? AND state='pending'",
                (encoded, wake_id, dedupe_key)).rowcount == 1

    def get_session(self, session_key: str) -> Session | None:
        with self._lock:
            return _record(Session, self.connection.execute("SELECT * FROM sessions WHERE session_key=?", (session_key,)).fetchone())

    def upsert_session(self, session: Session):
        """Insert or replace metadata using a full expected-record CAS on update.

        Engine hooks should normally use cas_session for read/modify/write so
        stale snapshots cannot overwrite a newer unanswered/activity state.
        """
        with self.atomic():
            current = self.get_session(session.session_key)
            if current is None:
                self._insert_record("sessions", session)
            else:
                before, after = asdict(current), asdict(session)
                before.pop("session_key")
                after.pop("session_key")
                return self.cas_session(session.session_key, before, **after)
            return True

    def cas_session(self, session_key: str, expected: dict, **updates) -> bool:
        if not expected or not updates:
            raise ValueError("session CAS needs expected values and updates")
        _updates(Session, expected, {"session_key"})
        _updates(Session, updates, {"session_key"})
        with self.atomic():
            return self.connection.execute(
                "UPDATE sessions SET " + ",".join(f"{k}=?" for k in updates) +
                " WHERE session_key=? AND " + " AND ".join(f"{k} IS ?" for k in expected),
                [_encode(k, v) for k, v in updates.items()] + [session_key] +
                [_encode(k, v) for k, v in expected.items()]).rowcount == 1

    def list_sessions(self) -> list[Session]:
        with self._lock:
            return [_record(Session, row) for row in self.connection.execute("SELECT * FROM sessions ORDER BY session_key")]

    def add_event(self, kind: str, dedupe_key: str, payload: Any, *,
                  session_key: str | None = None, chat_id: str | None = None) -> bool:
        encoded = _json(payload, 4096)
        with self.atomic():
            return self.connection.execute("""INSERT INTO events(kind,session_key,chat_id,
                payload_json,dedupe_key,created_at) VALUES (?,?,?,?,?,?)
                ON CONFLICT(dedupe_key) DO NOTHING""",
                (kind, session_key, chat_id, encoded, dedupe_key, self.clock.now())).rowcount == 1

    def pending_events(self, *, limit: int = 100) -> list[dict]:
        with self._lock:
            result = [dict(row) for row in self.connection.execute(
                "SELECT * FROM events WHERE consumed_at IS NULL ORDER BY seq LIMIT ?", (limit,))]
        for row in result:
            row["payload_json"] = json.loads(row["payload_json"])
        return result

    def consume_event(self, seq: int) -> bool:
        with self.atomic():
            return self.connection.execute("UPDATE events SET consumed_at=? WHERE seq=? AND consumed_at IS NULL",
                                           (self.clock.now(), seq)).rowcount == 1

    def add_ledger(self, day: str, wake_id: str, **counts):
        if not counts or counts.keys() - LEDGER_FIELDS:
            raise ValueError("unknown or empty ledger counters")
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in counts.values()):
            raise ValueError("ledger counters must be nonnegative integers")
        with self.atomic():
            self.connection.execute(
                f"INSERT INTO ledger(day,wake_id,{','.join(counts)}) VALUES ({','.join('?' for _ in range(len(counts)+2))}) "
                "ON CONFLICT(day,wake_id) DO UPDATE SET " + ",".join(f"{k}=ledger.{k}+excluded.{k}" for k in counts),
                [day, wake_id] + list(counts.values()))

    def get_ledger(self, day: str, wake_id: str) -> dict | None:
        with self._lock:
            row = self.connection.execute("SELECT * FROM ledger WHERE day=? AND wake_id=?", (day, wake_id)).fetchone()
            return dict(row) if row else None

    def gc(self, *, ledger_days: int, timezone="UTC", keep_occurrences: int = 200):
        if ledger_days < 1 or keep_occurrences < 1:
            raise ValueError("retention limits must be positive")
        now = self.clock.now()
        ledger_cutoff = (datetime.fromisoformat(local_day(now, timezone)).date() - timedelta(days=ledger_days - 1)).isoformat()
        terminal = sorted(TERMINAL_OCCURRENCE_STATES)
        placeholders = ",".join("?" for _ in terminal)
        with self.atomic():
            # Live rows count toward the limit but are never discarded by GC.
            self.connection.execute(f"""WITH ranked AS (
                SELECT rowid AS rid,state,
                ROW_NUMBER() OVER (PARTITION BY wake_id,state IN ({placeholders})
                    ORDER BY ended_at DESC,rowid DESC) AS rank,
                SUM(CASE WHEN state IN ({placeholders}) THEN 0 ELSE 1 END)
                    OVER (PARTITION BY wake_id) AS live FROM occurrences)
                DELETE FROM occurrences WHERE rowid IN (SELECT rid FROM ranked
                    WHERE state IN ({placeholders}) AND rank>MAX(0,?-live))""",
                terminal * 3 + [keep_occurrences])
            self.connection.execute("DELETE FROM events WHERE consumed_at IS NOT NULL AND consumed_at<?", (now - 7 * 86400,))
            self.connection.execute("DELETE FROM ledger WHERE day<?", (ledger_cutoff,))
            self.set_meta("last_gc_at", str(now))
