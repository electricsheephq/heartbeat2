"""Read-only pulse/transcript evidence. No live profile discovery."""
import json
from pathlib import Path
import sqlite3
import subprocess
import re
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

    def session_rows(self, session_id):
        return self.rows('SELECT * FROM messages WHERE session_id=? ORDER BY timestamp,id', (session_id,))

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

    def snapshot(self, session_key=None):
        if session_key is None: return {name: self.rows(f'SELECT * FROM {name}') for name in ('wakes', 'occurrences', 'sessions')}
        return dict(wakes=self.wakes(session_key), sessions=self.rows('SELECT * FROM sessions WHERE session_key=?', (session_key,)),
                    occurrences=self.rows('SELECT o.* FROM occurrences o JOIN wakes w ON w.id=o.wake_id WHERE w.session_key=?', (session_key,)))

    def baseline_state(self, session_key, day, default_quiet):
        quiet = self.rows('SELECT value FROM meta WHERE key=?', ('quiet:'+session_key,))
        override = decoded(quiet[0]['value']) if quiet else {}
        usage = self.rows('SELECT l.* FROM ledger l JOIN wakes w ON w.id=l.wake_id WHERE w.session_key=? AND l.day=?', (session_key, day))
        pending = self.rows("SELECT o.* FROM occurrences o JOIN wakes w ON w.id=o.wake_id WHERE w.session_key=? AND o.state IN ('claimed','started')", (session_key,))
        return dict(effective_quiet=default_quiet if not override else override.get('range'), quiet_override=override, usage=usage, pending=pending)


def hermes_revision(source, version_file):
    if source is None or version_file is None: return dict(hermes_version=None, hermes_commit=None)
    text = Path(version_file).read_text().strip()
    match = re.search(r"(?:__version__|version)\s*=\s*['\"]([^'\"]+)['\"]", text)
    version = match[1] if match else text if '\n' not in text else None
    result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=source, capture_output=True, text=True, timeout=5)
    commit = result.stdout.strip() if result.returncode == 0 else None
    return dict(hermes_version=version, hermes_commit=commit if commit and re.fullmatch('[0-9a-f]{40,64}', commit) else None)


def frozen_state(profile_home, hermes_source=None, hermes_version_file=None):
    home = Path(profile_home)
    config = yaml.safe_load((home/'config.yaml').read_text()) or {}
    entries = config.get('plugins', {}).get('entries', {})
    version = yaml.safe_load((home/'plugins/heartbeat2/plugin.yaml').read_text())['version']
    enabled = {k: v for k, v in entries.items() if v is True or isinstance(v, dict) and v.get('enabled', False)}
    entry = entries.get('heartbeat2', {})
    settings = entry.get('settings', entry) if isinstance(entry, dict) else {}
    return dict(**hermes_revision(hermes_source, hermes_version_file),
                allow_gateway_injection=isinstance(entry, dict) and entry.get('allow_gateway_injection') is True,
                quiet_hours=settings.get('quiet_hours', '22:00-08:00'), plugin_version=str(version), settings_sha256=digest(entries.get('heartbeat2', {})),
                model=config.get('model'), plugin_set_sha256=digest(dict(directories=sorted(p.name for p in (home/'plugins').iterdir() if p.is_dir()), enabled=enabled)))


def gateway_starts(profile_home):
    path = Path(profile_home)/'gateway-starts.log'
    return [float(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
