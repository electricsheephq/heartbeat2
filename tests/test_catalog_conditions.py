"""Catalog listing requirements: fake Hermes policy, real bounded local I/O."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
from types import ModuleType

import pytest

from heartbeat2 import probes
from heartbeat2.model import Wake
from heartbeat2.policy import Policy
from heartbeat2.registration import home_scope
from test_engine import engine, arm

REAL_CONNECT = socket.socket.connect
REAL_DNS = socket.getaddrinfo
REAL_CREATE = socket.create_connection
SECRETS = ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'TELEGRAM_BOT_TOKEN',
           'GH_TOKEN', 'GITHUB_TOKEN', 'AWS_SECRET_ACCESS_KEY')


@pytest.fixture
def profile_accessor(monkeypatch):
    context = ContextVar('fixture_home', default=None)
    constants = ModuleType('hermes_constants')
    constants.set_hermes_home_override = context.set
    constants.reset_hermes_home_override = context.reset
    constants.get_hermes_home = lambda: Path(context.get() or 'launch-profile')
    monkeypatch.setitem(sys.modules, 'hermes_constants', constants)
    return constants.get_hermes_home


def test_profile_scope_reaches_pool_probe(engine, monkeypatch, profile_accessor):
    homes = []
    monkeypatch.setattr(probes, 'check', lambda *a, **k: homes.append(profile_accessor()))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(engine._probe, Wake('fixture', 'observe', 'file')).result(timeout=2)
        assert pool.submit(profile_accessor).result(timeout=2) == Path('launch-profile')
    assert homes == [engine.ctx._manager.home_path]


def test_profile_scope_reaches_url_worker(tmp_path, monkeypatch, profile_accessor, hermes_probe_helpers):
    homes = []
    safety, _ = hermes_probe_helpers
    def safe(url):
        homes.append((profile_accessor(), threading.current_thread().name))
        return False
    monkeypatch.setattr(safety, 'is_safe_url', safe)
    with home_scope(tmp_path), pytest.raises(ValueError):
        probes._get_url('https://public.example/')
    assert homes == [(tmp_path, 'pulse-url')]


FLAGS = [['--jq', 'env'], ['--jq=x'], ['-q', 'x'], ['-qx'], ['-q=x'],
         ['--template', 'env'], ['--template=x'], ['-t', 'x'], ['-tx'], ['-t=x']]
FLAGS += [[flag] for flag in ('-vqx', '-aqx', '-vtx', '-atx')]


@pytest.mark.parametrize('flags', FLAGS)
@pytest.mark.parametrize('before', [False, True])
def test_gh_output_shaping_refused(flags, before):
    prefix = ['gh', 'run', 'list' if flags[0].startswith('-a') else 'view'] if flags[0].startswith(('-a', '-v')) else ['gh', 'pr', 'view']
    positional = ['1', '--json', 'number'] if prefix[1] == 'pr' else ['1']
    argv = prefix + (flags + positional if before else positional + flags)
    assert not Policy().command_allowed(argv)


@pytest.mark.parametrize('argv', [['git', 'log', '-q'], ['git', 'log', '-t'],
    ['gh', 'pr', 'view', '1', '--json', 'number'], ['gh', 'pr', 'checks', '1']])
def test_legitimate_commands_remain_allowed(argv):
    assert Policy().command_allowed(argv)


@pytest.mark.parametrize('helper', ['working', 'missing', 'raises'])
def test_command_env_allowlisted_and_sanitized(tmp_path, monkeypatch, helper):
    monkeypatch.setattr(os, 'environ', dict(PATH='/bin', HOME=str(tmp_path), LANG='C',
        LC_ALL='C', GH_HOST='github.com', **dict.fromkeys(SECRETS, 'fixture-only')))
    bases, children = [], []
    if helper == 'missing':
        monkeypatch.setitem(sys.modules, 'tools.environments.local', None)
    else:
        local = ModuleType('tools.environments.local')
        def sanitize(*, inherit_credentials, base_env):
            assert inherit_credentials is False
            bases.append(dict(base_env))
            if helper == 'raises': raise RuntimeError('fixture')
            return {**base_env, 'HERMES_SANITIZED': '1'}
        local.hermes_subprocess_env = sanitize
        monkeypatch.setitem(sys.modules, 'tools.environments.local', local)
    popen = subprocess.Popen
    def capture(*args, **kwargs):
        children.append(dict(kwargs['env']))
        return popen(*args, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', capture)
    assert probes._run_command([sys.executable, '-I', '-c', "print('ok')"], tmp_path) == (0, b'ok\n')
    child = children[0]
    assert not set(SECRETS) & child.keys()
    assert child['PATH'] == '/bin' and child['HOME'] == str(tmp_path)
    assert child['LC_ALL'] == 'C' and child['GH_HOST'] == 'github.com'
    assert child['GIT_PAGER'] == child['GH_PAGER'] == child['PAGER'] == 'cat'
    assert child['GIT_OPTIONAL_LOCKS'] == child['GIT_TERMINAL_PROMPT'] == '0'
    assert child['GH_PROMPT_DISABLED'] == '1' and child['GCM_INTERACTIVE'] == 'never'
    if helper != 'missing':
        assert bases == [dict(os.environ.items() - {key: 'fixture-only' for key in SECRETS}.items())]
    assert ('HERMES_SANITIZED' in child) is (helper == 'working')


@pytest.mark.parametrize('url', ['http://127.0.0.1/', 'http://[::1]/', 'http://10.0.0.1/',
    'http://169.254.169.254/latest/meta-data', 'http://localhost/', 'http://metadata.google.internal/'])
def test_url_arm_refuses_internal_without_dns(engine, monkeypatch, url):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: pytest.fail('arm performed DNS'))
    result = engine.tool.handle({'action': 'create', 'note': 'observe', 'trigger': {'kind': 'url', 'url': url}})
    assert result.get('error') and not engine.store.list_wakes()


def test_url_setting_allows_private_but_never_metadata(engine):
    engine.policy = Policy.from_settings({'url_allow_private': 'true'})
    for url, allowed in [('http://127.0.0.1/', True), ('http://169.254.169.254/', False)]:
        result = engine.tool.handle({'action': 'create', 'note': 'observe', 'trigger': {'kind': 'url', 'url': url}})
        assert bool(result.get('ok')) is allowed
    assert Policy().url_allow_private is False
    assert Policy.from_settings({'url_allow_private': 'false'}).url_allow_private is False


def test_url_update_refuses_private_without_dns(engine, monkeypatch):
    result = engine.tool.handle({'action': 'create', 'note': 'observe', 'trigger': {'kind': 'url', 'url': 'https://public.example/'}})
    assert result.get('ok')
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: pytest.fail('update performed DNS'))
    update = engine.tool.handle({'action': 'update', 'id': result['wake']['id'], 'trigger': {'kind': 'url', 'url': 'http://127.0.0.1/'}})
    assert update.get('error')


def test_missing_url_safety_refused(engine, monkeypatch):
    monkeypatch.setitem(sys.modules, 'tools.url_safety', None)
    result = engine.tool.handle({'action': 'create', 'note': 'observe', 'trigger': {'kind': 'url', 'url': 'https://public.example/'}})
    assert result.get('error') and 'URL watchers need Hermes url_safety' in result['detail']


@pytest.fixture
def local_server(monkeypatch):
    hits = []
    def connect(sock, address):
        assert address[0] == '127.0.0.1'
        return REAL_CONNECT(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setattr(socket, 'getaddrinfo', REAL_DNS)
    monkeypatch.setattr(socket, 'create_connection', REAL_CREATE)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            hits.append(self.path)
            self.send_response(302 if self.path == '/redirect' else 200)
            if self.path == '/redirect':
                self.send_header('Location', f'http://127.0.0.1:{self.server.server_port}/target')
            self.end_headers()
            if self.path != '/redirect': self.wfile.write(b'ok')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port, hits
    server.shutdown(); server.server_close(); thread.join(2)


@pytest.mark.parametrize('ip,allow_private,allowed', [('127.0.0.1', False, False),
    ('127.0.0.1', True, True), ('169.254.169.254', False, False), ('169.254.169.254', True, False)])
def test_connect_time_classifies_vetted_ip(local_server, monkeypatch, hermes_probe_helpers, ip, allow_private, allowed):
    port, hits = local_server
    safety, _ = hermes_probe_helpers
    # Preflight looks public (also models Hermes' global allow-private toggle).
    monkeypatch.setattr(safety, 'is_safe_url', lambda url: True)
    monkeypatch.setattr(safety, 'is_always_blocked_url', lambda url: False)
    dns = socket.getaddrinfo
    def resolve(host, target_port, *args, **kwargs):
        if host == 'public.example':
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, target_port))]
        return dns(host, target_port, *args, **kwargs)
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    url = f'http://public.example:{port}/ok'
    if allowed: assert probes._get_url(url, allow_private=allow_private) == b'ok'
    else:
        with pytest.raises(ValueError): probes._get_url(url, allow_private=allow_private)
    assert hits == (['/ok'] if allowed else [])


def test_connect_time_rejects_any_blocked_answer(local_server, monkeypatch, hermes_probe_helpers):
    port, hits = local_server
    safety, _ = hermes_probe_helpers
    monkeypatch.setattr(safety, 'is_safe_url', lambda url: True)
    monkeypatch.setattr(safety, 'is_always_blocked_url', lambda url: False)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port)) for ip in ('8.8.8.8', '127.0.0.1')])
    with pytest.raises(ValueError): probes._get_url(f'http://public.example:{port}/ok')
    assert hits == []


def test_redirect_hop_refuses_private(local_server, monkeypatch, hermes_probe_helpers):
    port, hits = local_server
    safety, _ = hermes_probe_helpers
    classify = safety._resolved_ip_block_reason
    # Exempt ONLY the first fixture URL and its first connect, keeping the
    # redirect target on the default policy. No real remote endpoint is used.
    monkeypatch.setattr(safety, 'is_safe_url', lambda url: 'public.example' in url)
    reasons = []
    def reason(ip, allow_private):
        reasons.append(ip)
        return None if len(reasons) == 1 else classify(ip, allow_private)
    monkeypatch.setattr(safety, '_resolved_ip_block_reason', reason)
    dns = socket.getaddrinfo
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, port, *a, **k: dns('127.0.0.1', port, *a, **k))
    with pytest.raises(ValueError): probes._get_url(f'http://public.example:{port}/redirect')
    assert hits == ['/redirect']


def test_file_arm_and_update_use_absolute_profile_path(engine, hermes_probe_helpers):
    _, files = hermes_probe_helpers
    seen = []
    def block(path):
        seen.append(path)
        return 'Hermes fixture read block' if path.endswith('blocked') else None
    files.get_read_block_error = block
    result = engine.tool.handle({'action': 'create', 'note': 'observe', 'trigger': {'kind': 'file', 'path': 'blocked'}})
    assert result.get('error') and result['detail'] == 'Hermes fixture read block'
    assert seen == [str(engine.ctx._manager.home_path / 'blocked')]
    result = engine.tool.handle({'action': 'create', 'note': 'observe', 'trigger': {'kind': 'file', 'path': 'allowed'}})
    assert result.get('ok')
    update = engine.tool.handle({'action': 'update', 'id': result['wake']['id'], 'trigger': {'kind': 'file', 'path': 'blocked'}})
    assert update.get('error') and update['detail'] == 'Hermes fixture read block'


def test_file_becomes_blocked_after_arming(engine, tmp_path, monkeypatch, hermes_probe_helpers):
    _, files = hermes_probe_helpers
    path = tmp_path / 'build.log'; path.write_text('ok\n')
    result = engine.tool.handle({'action': 'create', 'note': 'observe', 'trigger': {'kind': 'file', 'path': str(path)}})
    assert result.get('ok')
    files.get_read_block_error = lambda path: 'Hermes fixture read block'
    monkeypatch.setattr(os, 'open', lambda *a, **k: pytest.fail('blocked path was opened'))
    for _ in range(5):
        engine.tick(); engine.clock.advance(61)
    current = engine.store.get_wake(result['wake']['id'])
    assert current.error_streak == 5 and current.status == 'paused'
    assert current.probe_state_json is None


@pytest.mark.parametrize('pattern', [None, 'new'])
def test_file_rotation_rechecks_block(tmp_path, hermes_probe_helpers, pattern):
    _, files = hermes_probe_helpers
    path = tmp_path / 'build.log'; path.write_text('old\n')
    trigger = {'path': str(path), **({'pattern': pattern} if pattern else {})}
    wake = Wake('fixture', 'observe', 'file', trigger_json=trigger)
    first = probes.check(wake)
    assert 'error' not in first
    path.rename(tmp_path / 'old.log'); path.write_text('new\n')
    seen = []
    def block(path):
        seen.append(path)
        return 'Hermes rotation block' if len(seen) > 1 else None
    files.get_read_block_error = block
    result = probes.check(replace(wake, probe_state_json=first['state']))
    assert result.get('error') == 'ValueError' and not result['fire']
    assert seen == [str(path), str(path)]


def test_file_symlink_swap_between_stat_and_open(tmp_path, monkeypatch):
    safe = tmp_path / 'safe'; safe.write_text('safe\n')
    other = tmp_path / 'other'; other.write_text('other\n')
    path = tmp_path / 'watch'; path.symlink_to(safe)
    real_open = os.open
    def swapped(name, *args, **kwargs):
        if Path(name) == path:
            path.unlink(); path.symlink_to(other)
        return real_open(name, *args, **kwargs)
    monkeypatch.setattr(os, 'open', swapped)
    result = probes.check(Wake('fixture', 'observe', 'file', trigger_json={'path': str(path)}))
    assert result == {'fire': False, 'error': 'ValueError'}


@pytest.mark.parametrize('failure', ['missing', 'raises'])
def test_file_helper_failures_refuse_arm_and_probe(engine, tmp_path, monkeypatch, hermes_probe_helpers, failure):
    if failure == 'missing': monkeypatch.setitem(sys.modules, 'agent.file_safety', None)
    else:
        def raised(path): raise RuntimeError('fixture')
        hermes_probe_helpers[1].get_read_block_error = raised
    path = tmp_path / 'build.log'; path.write_text('ok')
    result = engine.tool.handle({'action': 'create', 'note': 'observe', 'trigger': {'kind': 'file', 'path': str(path)}})
    assert result.get('error') and not engine.store.list_wakes()
    assert probes.check(Wake('fixture', 'observe', 'file', trigger_json={'path': str(path)})).get('error')
