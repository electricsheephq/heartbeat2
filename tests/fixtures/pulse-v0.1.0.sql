-- Frozen HeartBeat 2.0 v0.1.0 (06c188b) database, dumped with sqlite3 iterdump after
-- v0.1.0 Store created one wake, a finished occurrence, a session and a ledger row.
-- Used by tests/test_v012.py::test_store_additive_migration_from_v010.
BEGIN TRANSACTION;
CREATE TABLE events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, session_key TEXT,
 chat_id TEXT, payload_json TEXT NOT NULL CHECK(length(CAST(payload_json AS BLOB))<=4096),
 dedupe_key TEXT NOT NULL UNIQUE, created_at REAL NOT NULL, consumed_at REAL);
CREATE TABLE ledger (
 day TEXT NOT NULL, wake_id TEXT NOT NULL REFERENCES wakes(id),
 fires INTEGER NOT NULL DEFAULT 0, gated_skips INTEGER NOT NULL DEFAULT 0,
 governor_skips INTEGER NOT NULL DEFAULT 0, model_turns INTEGER NOT NULL DEFAULT 0,
 noops INTEGER NOT NULL DEFAULT 0, valuable INTEGER NOT NULL DEFAULT 0,
 delivered INTEGER NOT NULL DEFAULT 0, engaged INTEGER NOT NULL DEFAULT 0,
 ignored INTEGER NOT NULL DEFAULT 0, tokens_in INTEGER NOT NULL DEFAULT 0,
 tokens_out INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0, provider_deferrals INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(day,wake_id));
INSERT INTO "ledger" VALUES('2026-10-13','w_v010',1,0,0,0,0,0,0,0,0,100,0,0,0);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO "meta" VALUES('schema_version','1');
CREATE TABLE occurrences (
 wake_id TEXT NOT NULL REFERENCES wakes(id), dedupe_key TEXT NOT NULL,
 state TEXT NOT NULL, attempt INTEGER NOT NULL, epoch TEXT, pid INTEGER,
 claimed_at REAL, started_at REAL, ended_at REAL,
 payload_json TEXT NOT NULL CHECK(length(CAST(payload_json AS BLOB))<=8192),
 outcome TEXT, notify INTEGER NOT NULL, summary TEXT NOT NULL, summary_hash TEXT NOT NULL,
 next_check_s REAL, force_silent INTEGER NOT NULL, delivered INTEGER NOT NULL,
 tokens_in INTEGER NOT NULL, tokens_out INTEGER NOT NULL, error TEXT NOT NULL,
 PRIMARY KEY(wake_id,dedupe_key));
INSERT INTO "occurrences" VALUES('w_v010','old:match','done',1,'v010',1,1760000000.0,1760000000.0,1760000000.0,'[]',NULL,0,'','',NULL,0,0,100,0,'');
CREATE TABLE sessions (
 session_key TEXT PRIMARY KEY, platform TEXT NOT NULL, chat_type TEXT NOT NULL,
 chat_id TEXT NOT NULL, current_session_id TEXT NOT NULL, last_human_at REAL,
 last_activity_at REAL, episode_id TEXT NOT NULL, unanswered_count INTEGER NOT NULL,
 busy_since REAL, enabled INTEGER NOT NULL, paused_until REAL,
 first_notice_sent INTEGER NOT NULL, last_wake_turn_at REAL, limits_fingerprint TEXT NOT NULL);
INSERT INTO "sessions" VALUES('unit','','','','',NULL,NULL,'',0,NULL,1,NULL,0,NULL,'');
CREATE TABLE wakes (
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
INSERT INTO "wakes" VALUES('w_v010','unit','','','','','agent','human_turn',NULL,0,'','old note','','','at','{}','null',NULL,NULL,NULL,NULL,NULL,0,0,0,NULL,NULL,'active','',NULL,NULL,0,0,0,0,0,0,0,0,0,0,1760000000.0,1760000000.0,0);
CREATE INDEX wakes_session_status ON wakes(session_key,status);
CREATE INDEX wakes_due ON wakes(status,next_due_at);
CREATE INDEX occurrences_state ON occurrences(state,claimed_at);
CREATE INDEX events_pending ON events(consumed_at,seq);
CREATE INDEX sessions_chat ON sessions(platform,chat_id);
DELETE FROM "sqlite_sequence";
COMMIT;
