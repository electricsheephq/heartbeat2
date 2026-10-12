"""FakeCtx follows fish-audio-hermes's Hermes-loader package pattern."""
import importlib.util
import sys
import socket
import ipaddress
import urllib.parse
from types import ModuleType
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))


class FakeClock:
    def __init__(self, wall=1760270400.0, elapsed=100.0):
        self.wall = wall
        self.elapsed = elapsed

    def now(self):
        return self.wall

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds):
        self.wall += seconds
        self.elapsed += seconds


class FakeCtx:
    def __init__(self):
        self.tools = {}
        self.hooks = {}
        self.commands = {}
        self.cli_commands = {}
        self.skills = {}
        self.skill_descriptions = {}

    def register_tool(self, name, **kwargs):
        self.tools[name] = kwargs

    def register_hook(self, name, fn):
        self.hooks.setdefault(name, []).append(fn)

    def register_command(self, name, **kwargs):
        self.commands[name] = kwargs

    def register_cli_command(self, name, **kwargs):
        self.cli_commands[name] = kwargs

    def register_skill(self, name, path, description=""):
        self.skills[name] = Path(path)
        self.skill_descriptions[name] = description


def load_plugin_package():
    name = "hermes_plugins_test.heartbeat2"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_ROOT / "__init__.py", submodule_search_locations=[str(PLUGIN_ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("hermes_plugins_test", type(sys)("hermes_plugins_test"))
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("U1 unit tests must not use network")
    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(socket.socket, "connect_ex", refused)
    monkeypatch.setattr(socket.socket, "sendto", refused)
    monkeypatch.setattr(socket, "create_connection", refused)
    monkeypatch.setattr(socket, "getaddrinfo", refused)


@pytest.fixture
def fake_ctx():
    return FakeCtx()


def install_probe_helpers(monkeypatch):
    """Hermes seams for stdlib-only units; integration always uses real modules."""
    def module(name):
        result = ModuleType(name)
        monkeypatch.setitem(sys.modules, name, result)
        return result
    for name in ('tools', 'agent'):
        if name not in sys.modules:
            module(name).__path__ = []
    safety = module('tools.url_safety')
    safety._BLOCKED_HOSTNAMES = frozenset({'metadata.google.internal', 'metadata.goog'})
    def reason(ip, allow_private):
        if str(ip) == '169.254.169.254': return 'cloud metadata address'
        if not allow_private and not ip.is_global: return 'private/internal address'
        return None
    def blocked(url):
        host = urllib.parse.urlsplit(url).hostname.rstrip('.').lower()
        if host in safety._BLOCKED_HOSTNAMES: return True
        try: return reason(ipaddress.ip_address(host), True) is not None
        except ValueError: return False
    def safe(url):
        host = urllib.parse.urlsplit(url).hostname.rstrip('.').lower()
        if blocked(url) or host == 'localhost': return False
        try: return reason(ipaddress.ip_address(host), False) is None
        except ValueError: return True
    safety._resolved_ip_block_reason = reason
    safety.is_always_blocked_url = blocked
    safety.is_safe_url = safe
    files = module('agent.file_safety')
    files.get_read_block_error = lambda path: None
    return safety, files


@pytest.fixture(autouse=True)
def hermes_probe_helpers(request, monkeypatch):
    if 'integration' not in request.node.path.parts:
        return install_probe_helpers(monkeypatch)


@pytest.fixture
def plugin():
    return load_plugin_package()
