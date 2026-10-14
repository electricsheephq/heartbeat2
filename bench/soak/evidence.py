"""Read-only pulse/transcript evidence. No live profile discovery."""
import json
from pathlib import Path
import sqlite3
import subprocess
import re
import time
import yaml
from .schedule import digest


def whitespace(text):
    return ' '.join((text or '').split())


def display(text):
    text = re.sub(r'```[^\n`]*\n', '', text or '')
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    for marker in ('**', '__', '`', '~~'): text = text.replace(marker, '')
    for marker in ('*', '_'):
        text = re.sub(r'(?<!\w)' + re.escape(marker) + r'([^' + re.escape(marker) + r'\n]+)' + re.escape(marker) + r'(?!\w)', r'\1', text)
    return whitespace(text)


def command_matches(text, reply):
    reply = display(reply)
    if text == '/new': return 'Confirm /new' in reply and '/approve' in reply
    if text == '/approve': return reply.startswith(('✨ Session reset! Starting fresh.', '✨ New session started!'))
    if text == '/pulse quiet default': return reply == 'Quiet hours follow your settings again: 22:00-08:00'
    if text == '/pulse on': return reply == 'HeartBeat 2.0 on for this chat.'
    if text.startswith('/pulse cancel '): return bool(re.match(re.escape(text.split()[-1])+r' cancelled\b', reply))
    return False


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
        # Deployed Hermes stores BLOB columns (messages.display_identity); the log is JSON, so bytes become hex.
        return [{k: v.hex() if isinstance(v, bytes) else v for k, v in dict(r).items()} for r in self.connection.execute(query, params)]

    def close(self):
        self.connection.close()


class Transcript(Reader):
    def __init__(self, path, now=time.time):
        super().__init__(path); self.now = now

    def current_session(self, chat_key):
        rows = self.rows('SELECT id FROM sessions WHERE session_key=? AND ended_at IS NULL ORDER BY started_at DESC', (chat_key,))
        return rows[0]['id'] if len(rows) == 1 else None

    def owner(self, session_id, text, sent_at, claimed=()):
        rows = self.rows("SELECT * FROM messages WHERE session_id=? AND role='user' AND timestamp BETWEEN ? AND ? ORDER BY timestamp,id", (session_id, sent_at-5, sent_at+120))
        return next((r for r in rows if r['id'] not in claimed and whitespace(r['content']) == whitespace(text)), None)

    def turn(self, session_id, after_ts):
        owner = self.rows("SELECT id FROM messages WHERE session_id=? AND role='user' AND timestamp=? ORDER BY id LIMIT 1", (session_id, after_ts))
        rows = self.rows('SELECT * FROM messages WHERE session_id=? AND (timestamp>? OR (timestamp=? AND id>?)) ORDER BY timestamp,id', (session_id, after_ts, after_ts, owner[0]['id'] if owner else 0))
        end = next((i for i, r in enumerate(rows) if r['role'] == 'user'), len(rows))
        return rows[:end]

    def pulse_results(self, session_id, after_ts):
        rows, results = self.turn(session_id, after_ts), []
        outputs = {r.get('tool_call_id'): decoded(r.get('content')) for r in rows if r['role'] == 'tool'}
        for row in rows:
            for call in decoded(row.get('tool_calls')) or []:
                name = call['function']['name']; args = decoded(call['function']['arguments']); output = outputs.get(call['id'])
                if name == 'pulse': results.append(dict(args=args,result=output))
                elif name == 'tool_call' and isinstance(args,dict):
                    nested = args.get('calls',[])
                    pulses = [c for c in nested if isinstance(c,dict) and c.get('name') == 'pulse']
                    if len(nested) == len(pulses) == 1:
                        results.append(dict(args=decoded(pulses[0].get('arguments')),result=output))
                    elif isinstance(output,dict) and output.get('error'):
                        results.append(dict(args=args,result=output,dispatch='tool_call'))
                    else:
                        # A shared wrapper id cannot prove which result belongs to each call.
                        results.extend(dict(args=decoded(c.get('arguments')),result=None) for c in pulses)
        return results

    def turn_done(self, session_id, after_ts, quiet_s=10):
        # Deployed Hermes appends a 'session_meta' row after a session's first turn; only conversation rows end a turn.
        rows = [r for r in self.turn(session_id, after_ts) if r['role'] in ('user', 'assistant', 'tool')]
        return bool(rows and rows[-1]['role'] == 'assistant' and not decoded(rows[-1].get('tool_calls')) and self.now() - rows[-1]['timestamp'] >= quiet_s)

    def session_rows(self, session_id):
        return self.rows('SELECT * FROM messages WHERE session_id=? ORDER BY timestamp,id', (session_id,))

    def occurrence_turn(self, session_id, occurrence):
        # The injection marker precedes the plugin's started_at by a fraction of a second.
        owners = self.rows("SELECT * FROM messages WHERE session_id=? AND role='user' AND timestamp BETWEEN ? AND ? AND content LIKE ? ORDER BY timestamp,id",
                           (session_id, occurrence['started_at']-5, occurrence['ended_at'], '⟦pulse o:'+occurrence['wake_id']+'/%'))
        owners = [r for r in owners if r['content'].startswith('⟦pulse o:'+occurrence['wake_id']+'/')]
        if len(owners) != 1: return []
        rows = self.turn(session_id, owners[0]['timestamp'])
        final = [r for r in rows if r['role'] == 'assistant' and r.get('content') and not decoded(r.get('tool_calls'))]
        if not final or not occurrence['started_at'] <= final[-1]['timestamp'] <= occurrence['ended_at']: return []
        return [owners[0]] + [r for r in rows if r['timestamp'] <= occurrence['ended_at']]


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
    if source is None or version_file is None: return dict(hermes_version=None, hermes_commit=None, hermes_source_commit=None)
    text = Path(version_file).read_text().strip()
    try: stamp = json.loads(text)
    except ValueError: stamp = None
    match = re.search(r"(?:__version__|version)\s*=\s*['\"]([^'\"]+)['\"]", text)
    version = (stamp.get('baseVersion') or None if isinstance(stamp, dict) else
               match[1] if match else text if '\n' not in text else None)
    try:
        result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=source, capture_output=True, text=True, timeout=5)
        commit = result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError): commit = None
    commit = commit if commit and re.fullmatch('[0-9a-fA-F]{40,64}', commit) else None
    installed = stamp.get('commit') if isinstance(stamp, dict) else None
    installed = installed if isinstance(installed, str) and re.fullmatch('[0-9a-fA-F]{40,64}', installed) else commit
    return dict(hermes_version=version, hermes_commit=installed, hermes_source_commit=commit)


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
                quiet_hours=settings.get('quiet_hours', '22:00-08:00'), resolved_timezone=settings.get('timezone') or config.get('timezone'),
                max_messages_per_day=settings.get('max_messages_per_day', 6), plugin_version=str(version), settings_sha256=digest(entries.get('heartbeat2', {})),
                nondefault_settings=sorted(k for k in settings if k not in ('timezone', 'enabled', 'allow_gateway_injection', 'settings')),
                model=config.get('model'), plugin_set_sha256=digest(dict(directories=sorted(p.name for p in (home/'plugins').iterdir() if p.is_dir()), enabled=enabled)))


def gateway_starts(profile_home):
    path = Path(profile_home)/'gateway-starts.log'
    return [float(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
