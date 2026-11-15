"""SPEC-162: synchronous, namespaced, deepcopy bus; no sleeps or delivery."""
import copy
from dataclasses import FrozenInstanceError
import json
from types import SimpleNamespace as NS

import pytest

from conftest import FakeCtx
from heartbeat2.registration import Runtime, register
from heartbeat2.voice_bridge import FIELDS, VoiceBridgeRegistry, voice_bridge_for


def payload(stamp=1):
    return dict(version=1, registered_at=stamp, **{name: lambda *a: None for name in FIELDS})


class Bus:
    def __init__(self):
        self.subscriptions = {}
        self.emits = []

    def emit(self, plugin_id, event, data):
        self.emits.append((plugin_id, event, data))
        for callback in list(self.subscriptions.get(plugin_id+':'+event, [])):
            callback(**copy.deepcopy(data))


class Context(FakeCtx):
    def __init__(self, home, bus, settings=None, plugin_id='heartbeat2'):
        super().__init__()
        self._manager = NS(home_path=home, scope_key=str(home))
        self.bus, self.settings, self.plugin_id = bus, settings or {}, plugin_id
        self.unloads = []

    def get_config(self, key, default=None): return self.settings.get(key, default)
    def on_unload(self, callback): self.unloads.append(callback)
    def emit(self, event, data): self.bus.emit(self.plugin_id, event, data)
    def subscribe(self, event, callback): self.bus.subscriptions.setdefault(event, []).append(callback)


@pytest.fixture
def setup(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(Runtime, 'start', lambda self: None)
    bus = Bus()
    ctx = Context(tmp_path, bus, {'voice_bridge': ' bridge ', 'timezone': 'UTC'})
    rt = register(ctx, clock=clock)
    yield rt, ctx, bus
    rt.close()


def test_valid_immutable_view_and_accessor(setup):
    rt, ctx, bus = setup
    data = payload()
    ctx.bus.emit('bridge', 'heartbeat2_voice_bridge', data)
    view = rt.voice_bridge()
    assert view.voice_target is data['voice_target']
    with pytest.raises(FrozenInstanceError): view.voice_target = None
    assert voice_bridge_for(rt.get_engine()) is view
    assert [(pid, event, data) for pid, event, data in bus.emits if pid == 'heartbeat2'] == [
        ('heartbeat2', 'voice_bridge_wanted', {'version': 1})]
    assert rt.voice_bridge() is view  # lookup emits nothing
    assert len(bus.emits) == 2


@pytest.mark.parametrize('change', [
    {'version': 2}, {'version': True}, {'version': 1.0}, {'version': None},
    {'registered_at': '1'}, {'registered_at': True}, {'registered_at': 0},
    {'registered_at': -1}, {'registered_at': 1.5},
    *({name: None} for name in FIELDS),
])
def test_invalid_payload_logged_once(change, caplog):
    registry = VoiceBridgeRegistry()
    data = payload() | change
    registry.announce(**data)
    registry.announce(**data)
    assert registry.get() is None
    assert len(caplog.records) == 1 and caplog.records[0].levelname == 'WARNING'


@pytest.mark.parametrize('field', ['version', 'registered_at', *FIELDS])
def test_missing_fields(field):
    registry = VoiceBridgeRegistry()
    data = payload()
    del data[field]
    registry.announce(**data)
    assert registry.get() is None


@pytest.mark.parametrize('order', [(1, 2), (2, 1)])
def test_newest_wins_and_equal_stamp_replaces(order):
    registry = VoiceBridgeRegistry()
    data = {stamp: payload(stamp) for stamp in order}
    for stamp in order: registry.announce(**data[stamp])
    assert registry.get().voice_target is data[2]['voice_target']
    newer = payload(2)
    registry.announce(**newer)
    assert registry.get().voice_target is newer['voice_target']


def test_withdrawal_tombstones_and_stale_announces():
    registry = VoiceBridgeRegistry()
    registry.announce(**payload(2))
    registry.announce(version=1, registered_at=1, withdrawn=True)
    assert registry.get() is not None
    registry.announce(version=1, registered_at=2, withdrawn=True)
    registry.announce(**payload(2))
    registry.announce(**payload(1))
    assert registry.get() is None
    registry.announce(version=1, registered_at=3, withdrawn=True)
    registry.announce(**payload(3))
    assert registry.get() is None
    registry.announce(**payload(4))
    assert registry.get() is not None


@pytest.mark.parametrize('setting', ['', '  ', 'invalid:id', '../bridge', 'a'*65, True, 123])
def test_blank_invalid_settings_do_nothing(tmp_path, monkeypatch, setting, clock):
    monkeypatch.setattr(Runtime, 'start', lambda self: None)
    bus = Bus()
    rt = register(Context(tmp_path, bus, {'voice_bridge': setting}), clock=clock)
    try:
        assert not bus.subscriptions and not bus.emits
        assert rt.voice_bridge() is None and rt.policy().voice_bridge == ''
        if setting not in ('', '  '):
            assert json.loads(rt.settings_error)[0]['key'] == 'voice_bridge'
    finally: rt.close()


@pytest.mark.parametrize('missing', ['emit', 'subscribe', 'both'])
def test_bus_feature_detection(tmp_path, monkeypatch, missing, clock):
    monkeypatch.setattr(Runtime, 'start', lambda self: None)
    bus = Bus()
    ctx = Context(tmp_path, bus, {'voice_bridge': 'bridge'})
    for name in (('emit', 'subscribe') if missing == 'both' else (missing,)):
        setattr(ctx, name, None)
    rt = register(ctx, clock=clock)
    try:
        assert rt.voice_bridge() is None and not bus.emits and not bus.subscriptions
    finally: rt.close()


def test_recording_context_has_no_handshake():
    ctx = FakeCtx()
    ctx.emit = lambda *a: pytest.fail('recording must not emit')
    ctx.subscribe = lambda *a: pytest.fail('recording must not subscribe')
    assert register(ctx) is None


def test_closed_runtime_and_unconfigured_emitter(setup):
    rt, ctx, bus = setup
    bus.emit('other', 'heartbeat2_voice_bridge', payload(100))
    assert rt.voice_bridge() is None
    bus.emit('bridge', 'heartbeat2_voice_bridge', payload())
    assert rt.voice_bridge() is not None
    rt.close()
    bus.emit('bridge', 'heartbeat2_voice_bridge', payload(2))
    assert rt.voice_bridge() is None


def test_setting_frozen_until_restart(setup):
    rt, ctx, bus = setup
    ctx.settings['voice_bridge'] = 'other'
    assert rt.policy().voice_bridge == rt.voice_bridge_id == 'bridge'
    assert list(bus.subscriptions) == ['bridge:heartbeat2_voice_bridge']


def test_accessor_never_raises():
    class Broken:
        @property
        def voice_bridge(self): raise RuntimeError('unavailable')
    def broken(): raise RuntimeError('unloaded')
    for engine in (None, object(), Broken(), NS(voice_bridge=broken)):
        assert voice_bridge_for(engine) is None
    assert Runtime.voice_bridge(object()) is None


def test_background_poller_accessor(setup, monkeypatch):
    from heartbeat2 import serving
    from heartbeat2.poller import Poller
    rt, ctx, bus = setup
    bus.emit('bridge', 'heartbeat2_voice_bridge', payload())
    monkeypatch.setattr(serving, 'state', lambda manager: serving.SERVED)
    monkeypatch.setattr(Poller, 'acquire', lambda self: False)
    # One bootstrap iteration, deterministically stopped at its first wait.
    monkeypatch.setattr(rt._stop, 'wait', lambda timeout: rt._stop.set())
    rt._bootstrap(rt._stop)
    assert voice_bridge_for(rt._runner) is rt.voice_bridge()


def test_status_line(setup, monkeypatch):
    from heartbeat2 import commands
    from heartbeat2.commands import handle
    from heartbeat2.model import Session
    rt, ctx, bus = setup
    key = 'agent:telegram:dm:fixture'
    monkeypatch.setattr(commands, 'session_env', lambda name: key if name == 'HERMES_SESSION_KEY' else '')
    rt.get_engine().store.upsert_session(Session(key, platform='telegram', chat_type='dm', chat_id='fixture'))
    assert 'voice bridge: bridge (not connected)' in handle('status', rt)
    bus.emit('bridge', 'heartbeat2_voice_bridge', payload())
    assert 'voice bridge: bridge (connected)' in handle('status', rt)
    rt.voice_bridge_id = ''
    assert 'voice bridge:' not in handle('status', rt)
