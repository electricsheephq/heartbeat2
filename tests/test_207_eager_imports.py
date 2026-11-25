"""#207: plugin files replaced under a running process must stay inert until a restart."""
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from heartbeat2 import commands, registration
from test_engine import engine, session, KEY

ROOT = Path(__file__).resolve().parents[1]


def test_module_list_covers_every_module():
    on_disk = {p.stem for p in (ROOT/'heartbeat2').glob('*.py')} - {'__init__', 'registration'}
    assert set(registration.MODULES) == on_disk


# The unit suite's network denial doesn't reach a child interpreter, so each child installs its own.
NO_NETWORK = ('import socket\n'
              'def _denied(*a, **k): raise AssertionError("unit tests must not use network")\n'
              'for _n in ("connect", "connect_ex", "sendto"): setattr(socket.socket, _n, _denied)\n'
              'socket.create_connection = socket.getaddrinfo = _denied\n')


def test_gateway_load_never_resolves_hermes_tui_server():
    subprocess.run([sys.executable, '-B', '-c', NO_NETWORK +
                    'from heartbeat2.registration import load_all; import sys; load_all(); '
                    'assert "heartbeat2.tui_probe" in sys.modules; '
                    'assert "tui_gateway.server" not in sys.modules'],
                   cwd=ROOT, check=True, timeout=60)


SWAP = r'''
import sys
sys.path.insert(0, sys.argv[1])
import heartbeat2.registration as r
if sys.argv[2] == 'load_all': r.load_all()
from pathlib import Path
root = Path(sys.argv[1])
(root/'heartbeat2'/'wake_text.py').write_text("raise ImportError('a newer file')\n")
(root/'plugin.yaml').write_text((root/'plugin.yaml').read_text().replace('version: "'+r.LOADED_VERSION+'"', 'version: "9.9.9"'))
try:
    from heartbeat2 import poller            # the old poller imports wake_text lazily inside the wake path
    from heartbeat2.wake_text import render_wake
    print('ok')
except ImportError as exc:
    print('ImportError', exc)
print(r.version_skew())
'''


@pytest.mark.parametrize('mode,expected', [('load_all', 'ok'), ('lazy', 'ImportError a newer file')])
def test_swapped_files_stay_inert_after_load_all(tmp_path, mode, expected):
    shutil.copytree(ROOT/'heartbeat2', tmp_path/'heartbeat2', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy(ROOT/'plugin.yaml', tmp_path/'plugin.yaml')
    out = subprocess.run([sys.executable, '-B', '-c', NO_NETWORK + SWAP, str(tmp_path), mode], capture_output=True, text=True,
                         timeout=60, check=True).stdout.splitlines()
    assert out[0] == expected
    assert out[1].startswith('Upgrade pending: 9.9.9 is installed but ') and out[1].endswith('restart Hermes to finish the upgrade.')


def test_register_loads_every_module_before_the_runtime(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(registration, 'load_all', lambda: calls.append('load_all'))
    class Stop(Exception): pass
    def runtime(ctx, **kw):
        calls.append('runtime')
        raise Stop
    monkeypatch.setattr(registration, 'Runtime', runtime)
    ctx = SimpleNamespace(register_tool=lambda *a, **k: None,
                          _manager=SimpleNamespace(home_path=tmp_path, scope_key='scope-207'))
    with pytest.raises(Stop):
        registration.register(ctx)
    assert calls == ['load_all', 'runtime']


@pytest.fixture
def pulse(monkeypatch):
    monkeypatch.setattr(commands, 'session_env', lambda name: KEY if name == 'HERMES_SESSION_KEY' else '')
    return lambda e, args: commands.handle(args, SimpleNamespace(get_engine=lambda: e))


def test_status_says_when_an_upgrade_waits_for_a_restart(engine, pulse, monkeypatch):
    session(engine, last_human_at=engine.clock.now())
    assert 'Upgrade pending' not in pulse(engine, 'status')
    monkeypatch.setattr(registration, 'LOADED_VERSION', '0.0.1')
    reply = pulse(engine, 'status')
    assert f'Upgrade pending: {registration.installed_version()} is installed but 0.0.1 is running' in reply


def test_installed_version_reads_utf8_whatever_the_locale(monkeypatch):
    # A legacy-locale host (EUC-JP, GBK, Shift-JIS) can't decode plugin.yaml's non-ASCII bytes with the default codec.
    original = Path.read_text
    def legacy_locale(self, encoding=None, errors=None):
        if encoding is None: raise UnicodeDecodeError('euc_jp', b'\xe2\x80', 0, 1, 'illegal multibyte sequence')
        return original(self, encoding=encoding, errors=errors)
    monkeypatch.setattr(Path, 'read_text', legacy_locale)
    assert registration.installed_version() == registration.LOADED_VERSION


def test_undecodable_manifest_never_stops_the_plugin_loading(tmp_path, monkeypatch):
    (tmp_path/'plugin.yaml').write_bytes(b'version: "\xff\xfe"\n')
    monkeypatch.setattr(registration, 'PLUGIN_ROOT', tmp_path)
    assert registration.installed_version() is None
    assert registration.version_skew() == ''
