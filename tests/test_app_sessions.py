"""Local surfaces use the ordinary policy; a retired window refunds unsent claims."""
from dataclasses import replace
import hashlib
from types import SimpleNamespace

import pytest

from heartbeat2 import commands, policy, tool
from heartbeat2.model import Session
from heartbeat2.wake_text import render_wake
from test_engine import KEY, engine, arm, pending, session, end
from test_wake_text import records


CLI = "Wakes can't run in the Hermes CLI: it doesn't show plugin turns. Use the Desktop app, the TUI or a messaging chat."
API = "Wakes can't run in API-server sessions yet: Hermes drops plugin turns there. Arm it from the Desktop app, the TUI or a messaging chat."
OLD = "This Hermes version can't deliver wakes to app sessions. Update Hermes, or use a messaging chat."
CREATE = dict(action='create', note='Check the task.', trigger={'kind':'at', 'at':'in 1 minute'})


def bind(monkeypatch, platform, *, key=KEY):
    values = {'KEY':key, 'PLATFORM':platform, 'CHAT_TYPE':'dm', 'CHAT_ID':'unit', 'ID':'sid'}
    env = lambda name: values.get(name.removeprefix('HERMES_SESSION_'), '')
    monkeypatch.setattr(tool, 'session_env', env)
    monkeypatch.setattr(commands, 'session_env', env)


@pytest.mark.parametrize('marker,expected', [(lambda: None, True), (None, False), ('unknown', False)])
def test_host_marker_is_feature_detected_and_cached(monkeypatch, marker, expected):
    calls = []
    def load(name):
        calls.append(name)
        return SimpleNamespace(GatewayPluginInjectionMixin=SimpleNamespace(_plugin_local_session=marker))
    monkeypatch.setattr(policy.importlib, 'import_module', load)
    policy.local_injection_supported.cache_clear()
    try:
        assert policy.local_injection_supported() is expected
        assert policy.local_injection_supported() is expected
        assert calls == ['gateway.run_plugin_injection']
    finally:
        policy.local_injection_supported.cache_clear()


def test_host_gate_fails_closed_on_import_error(monkeypatch):
    def missing(name): raise ImportError(name)
    monkeypatch.setattr(policy.importlib, 'import_module', missing)
    policy.local_injection_supported.cache_clear()
    try:
        assert not policy.local_injection_supported()
    finally:
        policy.local_injection_supported.cache_clear()


@pytest.mark.parametrize('surface', ['desktop', 'tui', 'cli', 'unknown', ''])
def test_hook_surface_creates_and_updates_local_identity(engine, monkeypatch, surface):
    key = 'agent:main:local:dm:local-hook'
    engine.tool.session_key_fn = lambda: key
    bind(monkeypatch, 'local', key=key)
    engine.intake.pre_llm_call(session_id='sid', turn_id='owner', platform=surface)
    assert engine.store.get_session(key).platform == (surface if surface in {'desktop','tui','cli'} else 'local')
    engine.intake.pre_llm_call(session_id='sid', turn_id='next', platform='tui')
    assert engine.store.get_session(key).platform == 'tui'


@pytest.mark.parametrize('mode,expected', [('desktop','desktop'), ('tui','tui'), ('cli','cli'),
                                          ('absent','local'), ('raising','local'), ('unknown','local')])
def test_tool_surface_resolution(engine, monkeypatch, mode, expected):
    key = 'agent:main:local:dm:local-tool'
    engine.tool.session_key_fn = lambda: key
    bind(monkeypatch, 'local', key=key)
    if mode == 'raising':
        def resolve(): raise RuntimeError('surface unavailable')
    else:
        resolve = lambda: mode
    if mode != 'absent': monkeypatch.setattr(engine.ctx, 'session_surface', resolve, raising=False)
    assert engine.tool.identity().platform == expected
    # An unknown/failed later resolution must not retain an admitted surface.
    engine.store.cas_session(key, {'platform':expected}, platform='desktop')
    assert engine.tool.identity().platform == expected


def test_surface_resolution_does_not_rewrite_messaging(engine, monkeypatch):
    bind(monkeypatch, 'telegram')
    monkeypatch.setattr(engine.ctx, 'session_surface', lambda: 'desktop', raising=False)
    engine.intake.pre_llm_call(session_id='sid', platform='desktop')
    assert engine.tool.identity().platform == 'telegram'


@pytest.mark.parametrize('platform,capable,detail', [('desktop',True,''), ('tui',True,''),
    ('cli',True,CLI), ('api_server',True,API), ('local',True,''), ('desktop',False,OLD), ('tui',False,OLD)])
def test_creation_and_command_gates(engine, monkeypatch, platform, capable, detail):
    monkeypatch.setattr(policy, 'local_injection_supported', lambda: capable)
    session(engine, platform=platform)
    bind(monkeypatch, platform)
    result = engine.tool.handle(CREATE)
    reply = commands.handle('add in 1m Check the task.', SimpleNamespace(get_engine=lambda: engine))
    if platform in {'desktop','tui'} and capable:
        assert result['armed'], result
        assert 'w_' in reply, reply
        wid=result['wake']['id']
        assert engine.tool.handle({'action':'update','id':wid,'note':'Updated task.'})['ok']
        assert engine.tool.handle({'action':'pause','id':wid})['ok']
        assert engine.tool.handle({'action':'resume','id':wid})['ok']
        assert policy.deliverable_platform(platform)
        gate = engine.policy.creation_gate(session_key=KEY,platform=platform,chat_type='group',allow_gateway_injection=True)
        assert gate.reason == 'dm_only'
        engine.ctx.allowed = False
        assert engine.tool.handle(CREATE)['error'] == 'injection_disabled'
    else:
        assert result['error'] == 'non_gateway'
        assert result.get('detail','') == detail
        assert not policy.deliverable_platform(platform)
        assert reply == detail if detail else 'session' in reply


@pytest.mark.parametrize('surface', ['desktop', 'tui'])
def test_local_own_time_checkin_and_keep_warm(engine, monkeypatch, surface):
    monkeypatch.setattr(policy, 'local_injection_supported', lambda: True)
    session(engine, platform=surface)
    engine.policy = replace(engine.policy, own_time_hours=1, owner_silent_checkin=True)
    assert policy.is_direct_chat(surface, 'dm', 'unit')
    assert engine.session_can_receive(engine.store.get_session(KEY))
    engine.own_time(); engine.silence()
    wakes = engine.store.list_wakes(KEY)
    assert len(wakes) == 2
    assert any(w.trigger_json.get('own_time') for w in wakes)
    assert any(w.kind == 'owner_silent' for w in wakes)
    bind(monkeypatch, surface)
    reply = commands.handle('warm on', SimpleNamespace(get_engine=lambda:engine))
    assert 'closed app window makes it pointless' in reply
    assert not any(policy.is_keep_warm(w) for w in engine.store.list_wakes(KEY))


@pytest.mark.parametrize('surface,raises', [('desktop',False), ('tui',False), ('desktop',True)])
def test_closed_app_holds_without_failure_then_runs(engine, monkeypatch, surface, raises):
    monkeypatch.setattr(policy, 'local_injection_supported', lambda: True)
    session(engine, platform=surface)
    w = arm(engine, platform=surface)
    o = pending(engine,w)
    engine.ctx.accept = False
    if raises:
        def inject(*args, **kwargs): raise RuntimeError('window retired')
        monkeypatch.setattr(engine.ctx,'inject_message',inject)
    first = engine.clock.now()
    for _ in range(7):
        engine.dispatch(o,cache_line='')
        held = engine.store.get_occurrence(w.id,o.dedupe_key)
        assert (held.state,held.error) == ('pending','app_closed')
        assert engine.store.get_meta('inject_failures:'+o.id) is None
        assert float(engine.store.get_meta('held_since:'+o.id)) == first
        assert float(engine.store.get_meta('not_before:'+o.id)) == engine.clock.now()+60
        assert engine.daily(KEY)['model_turns'] == 0
        assert engine.store.dispatches_today(w.id,engine.policy.resolved_timezone()) == 0
        # Retry interval is respected without changing the first waiting timestamp.
        assert engine.gate(w,held,engine.store.get_session(KEY))[0] == 'not_before'
        engine.clock.advance(60)
    runtime = SimpleNamespace(get_engine=lambda:engine)
    bind(monkeypatch,surface)
    for text in (commands.handle('status',runtime), commands.handle('desk',runtime), commands.handle('why '+w.id,runtime)):
        assert 'app window is closed' in text and commands._time(engine,first) in text
    monkeypatch.setattr(engine.ctx,'inject_message',lambda text, **kw: engine.ctx.calls.append((text,KEY)) or True)
    engine.dispatch(o,cache_line='')
    assert engine.store.get_occurrence(w.id,o.dedupe_key).state == 'claimed'
    engine.intake.pre_llm_call(session_id='sid',turn_id='turn',user_message=engine.ctx.calls[-1][0],platform=surface)
    assert end(engine,'progress',True,'Window reopened.') is None
    assert engine.store.get_occurrence(w.id,o.dedupe_key).state == 'done'
    assert 'app window is closed' not in commands.handle('status',runtime)


def test_closed_app_expiry_and_messaging_failure_budget(engine, monkeypatch):
    monkeypatch.setattr(policy,'local_injection_supported',lambda:True)
    session(engine,platform='desktop')
    w=arm(engine,platform='desktop',expires_at=engine.clock.now()+60)
    o=pending(engine,w)
    engine.ctx.accept=False
    engine.dispatch(o,cache_line='')
    engine.clock.advance(60)
    engine.dispatch(o,cache_line='')
    assert engine.store.get_occurrence(w.id,o.dedupe_key).state == 'skipped'
    assert engine.store.get_occurrence(w.id,o.dedupe_key).error == 'expired'
    session(engine,platform='telegram')
    w=arm(engine)
    o=pending(engine,w)
    for delay in (30,60,300,900,3600):
        engine.dispatch(o,cache_line='')
        engine.clock.advance(delay)
    assert engine.store.get_occurrence(w.id,o.dedupe_key).state == 'dead'
    assert engine.store.get_meta('inject_failures:'+o.id) == '5'
    assert engine.store.get_meta('held_since:'+o.id) is None


def test_app_hold_meta_is_garbage_collected(engine):
    w=arm(engine)
    o=pending(engine,w)
    engine.store.set_meta('held_since:'+o.id,str(engine.clock.now()))
    engine.store.cas_occurrence(w.id,o.dedupe_key,'pending','skipped')
    newer=pending(engine,w,key='t:newer')
    engine.store.cas_occurrence(w.id,newer.dedupe_key,'pending','skipped')
    engine.clock.advance(40*86400)
    engine.store.gc(ledger_days=30,keep_occurrences=1)
    assert engine.store.get_occurrence(w.id,o.dedupe_key) is None
    assert engine.store.get_meta('held_since:'+o.id) is None


def test_local_prompt_line_and_byte_identical_messaging(clock):
    w,o,s=records(clock)
    messaging=render_wake(w,o,replace(s,platform='telegram'),clock=clock)
    # Pins the complete base renderer output, including whitespace; local support must not alter messaging text.
    assert hashlib.sha256(messaging.encode()).hexdigest() == '172c77311768b353c848dd0a5a0076fd57198c624d8760ecbff4963d91bd971d'
    line='Anything you write appears in the app window. When you are not notifying, reply with exactly [SILENT] and nothing else.'
    for platform in ('desktop','tui'):
        local=render_wake(w,o,replace(s,platform=platform),clock=clock)
        assert line in local
        assert local.replace(line+'\n','') == messaging
    assert line not in messaging
