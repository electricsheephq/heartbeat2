import json
import sqlite3
import subprocess
import pytest
from heartbeat2.store import Store
from heartbeat2.model import Session, Wake
from bench.soak.client import CommandClient, ClientError
from bench.soak.evidence import Transcript, Pulse, frozen_state, gateway_starts


def test_client_argv_json_and_failures(monkeypatch):
    calls = []
    message = dict(id=1, date='2026-10-15T08:30:00+00:00', edit_date=None, out=True, text='hello')
    def run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, json.dumps(message) + '\n')
    monkeypatch.setattr(subprocess, 'run', run)
    client = CommandClient(['fake', '--json'], 'bot')
    assert client.send('hello') == message
    assert client.read(5, 100) == [message]
    assert calls[0][0] == ['fake', '--json', 'send', 'bot', 'hello']
    assert calls[1][0] == ['fake', '--json', 'read', 'bot', '--limit', '100', '--after-id', '5']
    assert calls[0][1]['timeout'] == 60
    for output, code in [('no JSON', 0), ('{}', 0), (json.dumps(message), 1)]:
        monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, code, output))
        with pytest.raises(ClientError): client.send('hello')
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired('fake', 60)))
    with pytest.raises(ClientError): client.read(None, 100)


def test_readonly_transcript_and_pulse(tmp_path, clock):
    state = tmp_path/'state.db'
    with sqlite3.connect(state) as con:
        con.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY)')
        con.execute("INSERT INTO sessions VALUES ('s')")
        con.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL, tool_calls TEXT, tool_name TEXT, tool_call_id TEXT, platform_message_id TEXT)')
        rows = [('s', 'user', 'request', 100, None, None, None, '17'),
                ('s', 'assistant', '', 101, json.dumps([{'id': 'c', 'function': {'name': 'pulse', 'arguments': '{"action":"create"}'}}]), None, None, None),
                ('s', 'tool', '{"error":"refused","receipt":{"due":200}}', 102, None, 'pulse', 'c', None),
                ('s', 'assistant', 'not set', 103, None, None, None, None),
                ('s', 'user', 'next', 120, None, None, None, '18')]
        con.executemany('INSERT INTO messages (session_id,role,content,timestamp,tool_calls,tool_name,tool_call_id,platform_message_id) VALUES (?,?,?,?,?,?,?,?)', rows)
    transcript = Transcript(state, now=lambda: 115)
    assert transcript.owner('s', 'request',100)['timestamp'] == 100
    assert len(transcript.turn('s', 100)) == 3
    assert transcript.pulse_results('s', 100) == [{'args': {'action': 'create'}, 'result': {'error': 'refused', 'receipt': {'due': 200}}}]
    assert transcript.turn_done('s', 100)
    transcript.now = lambda: 105
    assert not transcript.turn_done('s', 100)
    db = tmp_path/'pulse.db'
    with Store(db, clock) as store:
        store.upsert_session(Session(session_key='chat', current_session_id='s'))
        store.create_wake(Wake(session_key='chat', note='bank', kind='at', id='w'))
    pulse = Pulse(db)
    assert pulse.session('chat')['current_session_id'] == 's'
    assert pulse.wake('w')['note'] == 'bank'
    assert len(pulse.wakes('chat')) == 1
    assert pulse.occurrences('w') == []
    assert set(pulse.snapshot()) == {'wakes', 'occurrences', 'sessions'}
    with pytest.raises(sqlite3.OperationalError): pulse.connection.execute('DELETE FROM wakes')
    with pytest.raises(sqlite3.OperationalError): transcript.connection.execute('DELETE FROM messages')
    pulse.close(); transcript.close()


def test_frozen_state_and_starts(tmp_path):
    plugin = tmp_path/'plugins/heartbeat2'; plugin.mkdir(parents=True)
    (plugin/'plugin.yaml').write_text('version: 0.1.15\n')
    config = tmp_path/'config.yaml'
    config.write_text('model: {default: fake, provider: fake}\nplugins:\n  entries:\n    heartbeat2: {enabled: true, timezone: UTC}\n')
    a = frozen_state(tmp_path)
    assert a['plugin_version'] == '0.1.15'
    assert a['model'] == {'default': 'fake', 'provider': 'fake'}
    config.write_text('plugins:\n  entries:\n    heartbeat2: {timezone: UTC, enabled: true}\nmodel: {provider: fake, default: fake}\n')
    assert a == frozen_state(tmp_path)
    (tmp_path/'plugins/extra').mkdir()
    assert a['plugin_set_sha256'] != frozen_state(tmp_path)['plugin_set_sha256']
    (tmp_path/'gateway-starts.log').write_text('1.5\n2.5\n')
    assert gateway_starts(tmp_path) == [1.5, 2.5]
