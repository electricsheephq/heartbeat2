"""Read-only pulse/transcript evidence. No live profile discovery."""
import json
from pathlib import Path
import sqlite3
import time
import yaml
from .schedule import digest


def decoded(value):
    if isinstance(value, str):
        try: return json.loads(value)
        except ValueError: return value
    return value


class Reader:
    def __init__(self, path):
        self.connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
        self.connection.row_factory = sqlite3.Row

    def rows(self, query, params=()):
        return [dict(r) for r in self.connection.execute(query, params)]

    def close(self):
        self.connection.close()


class Transcript(Reader):
    def __init__(self, path, now=time.time):
        super().__init__(path); self.now = now

    def owner(self, session_id, message_id):
        rows = self.rows("SELECT * FROM messages WHERE session_id=? AND role='user' AND CAST(platform_message_id AS TEXT)=? ORDER BY timestamp,id", (session_id, str(message_id)))
        return rows[0] if rows else None

    def turn(self, session_id, after_ts):
        rows = self.rows('SELECT * FROM messages WHERE session_id=? AND timestamp>? ORDER BY timestamp,id', (session_id, after_ts))
        end = next((i for i, r in enumerate(rows) if r['role'] == 'user'), len(rows))
        return rows[:end]

    def pulse_results(self, session_id, after_ts):
        rows, calls, outputs = self.turn(session_id, after_ts), {}, {}
        for row in rows:
            for call in decoded(row.get('tool_calls')) or []:
                if call['function']['name'] == 'pulse': calls[call['id']] = decoded(call['function']['arguments'])
            if row['role'] == 'tool': outputs[row.get('tool_call_id')] = decoded(row.get('content'))
        return [dict(args=args, result=outputs.get(cid)) for cid, args in calls.items()]

    def turn_done(self, session_id, after_ts, quiet_s=10):
        rows = self.turn(session_id, after_ts)
        return bool(rows and rows[-1]['role'] == 'assistant' and not decoded(rows[-1].get('tool_calls')) and self.now() - rows[-1]['timestamp'] >= quiet_s)

    def occurrence_turn(self, session_id, occurrence):
        rows = self.rows('SELECT * FROM messages WHERE session_id=? AND timestamp>=? AND timestamp<=? ORDER BY timestamp,id',
                         (session_id, occurrence['started_at'], occurrence['ended_at']))
        return rows


class Pulse(Reader):
    def session(self, chat_key):
        rows = self.rows('SELECT * FROM sessions WHERE session_key=?', (chat_key,))
        return rows[0] if rows else None

    def wakes(self, session_key):
        return self.rows('SELECT * FROM wakes WHERE session_key=? ORDER BY created_at,id', (session_key,))

    def wake(self, id):
        rows = self.rows('SELECT * FROM wakes WHERE id=?', (id,)); return rows[0] if rows else None

    def occurrences(self, wake_id):
        return self.rows('SELECT * FROM occurrences WHERE wake_id=? ORDER BY created_at,dedupe_key', (wake_id,))

    def snapshot(self):
        return {name: self.rows(f'SELECT * FROM {name}') for name in ('wakes', 'occurrences', 'sessions')}


def frozen_state(profile_home):
    home = Path(profile_home)
    config = yaml.safe_load((home/'config.yaml').read_text()) or {}
    entries = config.get('plugins', {}).get('entries', {})
    version = yaml.safe_load((home/'plugins/heartbeat2/plugin.yaml').read_text())['version']
    enabled = {k: v for k, v in entries.items() if v is True or isinstance(v, dict) and v.get('enabled', False)}
    return dict(plugin_version=str(version), settings_sha256=digest(entries.get('heartbeat2', {})),
                model=config.get('model'), plugin_set_sha256=digest(dict(directories=sorted(p.name for p in (home/'plugins').iterdir() if p.is_dir()), enabled=enabled)))


def gateway_starts(profile_home):
    path = Path(profile_home)/'gateway-starts.log'
    return [float(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
