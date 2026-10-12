"""Real helper contract at the pinned Hermes checkout; no remote network."""
from concurrent.futures import ThreadPoolExecutor
import importlib
import ipaddress
import os
import subprocess
import sys

import pytest

from heartbeat2 import probes
from heartbeat2.registration import home_scope
from test_engine import engine


@pytest.fixture
def real_helpers():
    try:
        return tuple(importlib.import_module(name) for name in (
            'tools.environments.local', 'tools.url_safety', 'agent.file_safety', 'hermes_constants'))
    except ImportError:
        if os.environ.get('HEARTBEAT2_REQUIRE_HERMES') == '1':
            pytest.fail('Hermes helper is not importable at the required pin')
        pytest.skip('Hermes is not importable in this venv')


def test_real_subprocess_env_builder(real_helpers, tmp_path, monkeypatch):
    local, _, _, constants = real_helpers
    calls, environments = [], []
    real = local.hermes_subprocess_env
    def record(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)
    monkeypatch.setattr(local, 'hermes_subprocess_env', record)
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture-only')
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'fixture-only')
    monkeypatch.setenv('GH_TOKEN', 'fixture-only')
    popen = subprocess.Popen
    def capture(*args, **kwargs):
        environments.append(kwargs['env'])
        return popen(*args, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', capture)
    # Also exercise the real sanitizer with keys present in its curated base.
    cleaned = real(inherit_credentials=False, base_env={
        'PATH': os.environ['PATH'], 'HOME': str(tmp_path),
        'OPENAI_API_KEY': 'fixture-only', 'TELEGRAM_BOT_TOKEN': 'fixture-only'})
    assert not {'OPENAI_API_KEY', 'TELEGRAM_BOT_TOKEN'} & cleaned.keys()
    with home_scope(tmp_path):
        assert probes._run_command([sys.executable, '-I', '-c', "print('ok')"], tmp_path) == (0, b'ok\n')
    assert len(calls) == 1 and calls[0]['inherit_credentials'] is False
    assert not {'OPENAI_API_KEY', 'TELEGRAM_BOT_TOKEN', 'GH_TOKEN'} & calls[0]['base_env'].keys()
    assert not {'OPENAI_API_KEY', 'TELEGRAM_BOT_TOKEN', 'GH_TOKEN'} & environments[0].keys()
    assert environments[0]['HERMES_HOME'] == str(tmp_path)


def test_real_url_classification(real_helpers, monkeypatch):
    _, safety, _, _ = real_helpers
    monkeypatch.setenv('HERMES_ALLOW_PRIVATE_URLS', 'false')
    monkeypatch.setattr(safety, '_getaddrinfo', lambda *a: [(2, 1, 6, '', ('127.0.0.1', 80))])
    safety._reset_allow_private_cache()
    assert safety.is_safe_url('http://127.0.0.1/') is False
    assert safety.is_always_blocked_url('http://169.254.169.254/') is True
    assert safety._resolved_ip_block_reason(ipaddress.ip_address('127.0.0.1'), False)
    assert safety._resolved_ip_block_reason(ipaddress.ip_address('127.0.0.1'), True) is None
    for allow in (False, True):
        assert safety._resolved_ip_block_reason(ipaddress.ip_address('169.254.169.254'), allow)


def test_real_file_read_block(real_helpers, tmp_path):
    _, _, files, _ = real_helpers
    assert files.get_read_block_error(str(tmp_path / '.env'))


def test_real_profile_scope_on_pool_thread(real_helpers, tmp_path, engine, monkeypatch):
    _, _, _, constants = real_helpers
    def scoped():
        with home_scope(tmp_path): return constants.get_hermes_home()
    seen = []
    monkeypatch.setattr(probes, 'check', lambda *a, **k: seen.append(constants.get_hermes_home()))
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(scoped).result(timeout=2) == tmp_path
        pool.submit(engine._probe, None).result(timeout=2)
    assert seen == [engine.ctx._manager.home_path]
