"""FakeCtx follows fish-audio-hermes's Hermes-loader package pattern."""
import importlib.util
import sys
import socket
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


@pytest.fixture
def plugin():
    return load_plugin_package()
