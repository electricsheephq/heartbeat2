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
    if platform in policy.LOCAL_SURFACES: platform = 'local'
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
    # An unknown/failed later resolution must retain the known app surface.
    engine.store.cas_session(key, {'platform':expected}, platform='desktop')
    assert engine.tool.identity().platform == ('desktop' if expected == 'local' else expected)


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
    assert not policy.is_direct_chat(surface, 'dm', 'unit')
    assert engine.session_can_receive(engine.store.get_session(KEY))
    engine.own_time(); engine.silence()
    wakes = engine.store.list_wakes(KEY)
    assert wakes == []
    bind(monkeypatch, surface)
    runtime = SimpleNamespace(get_engine=lambda:engine)
    commands.handle('owntime on', runtime)
    engine.own_time(); engine.silence()
    wakes = engine.store.list_wakes(KEY)
    assert len(wakes) == 1 and wakes[0].trigger_json.get('own_time')
    commands.handle('owntime off', runtime)
    assert engine.store.get_wake(wakes[0].id).status == 'cancelled'
    reply = commands.handle('warm on', runtime)
    assert 'closed app window makes it pointless' in reply
    assert not any(policy.is_keep_warm(w) for w in engine.store.list_wakes(KEY))


@pytest.mark.parametrize('surface', ['desktop', 'tui'])
def test_closed_app_holds_without_failure_then_runs(engine, monkeypatch, surface):
    monkeypatch.setattr(policy, 'local_injection_supported', lambda: True)
    session(engine, platform=surface)
    w = arm(engine, platform=surface)
    o = pending(engine,w)
    engine.ctx.accept = False
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
        assert engine.gate(w,held,engine.store.get_session(KEY))[0] == 'app_closed'
        engine.clock.advance(60)
    runtime = SimpleNamespace(get_engine=lambda:engine)
    bind(monkeypatch,surface)
    for text in (commands.handle('status',runtime), commands.handle('desk',runtime), commands.handle('why '+w.id,runtime)):
        assert 'app window is closed' in text and commands._time(engine,first) in text
    monkeypatch.setattr(engine.ctx,'inject_message',lambda text, **kw: engine.ctx.calls.append((text,KEY)) or True)
    engine.dispatch(o,cache_line='')
    assert engine.store.get_occurrence(w.id,o.dedupe_key).state == 'claimed'
    assert not engine.store.get_meta('held_since:'+o.id)
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


@pytest.mark.parametrize('surface', ['desktop', 'tui'])
def test_local_explicit_roles_and_silence_wakes_still_work(engine, monkeypatch, surface):
    monkeypatch.setattr(policy, 'local_injection_supported', lambda: True)
    session(engine, platform=surface)
    bind(monkeypatch, surface)
    reply = commands.handle('role every 1h Keep the report current.', SimpleNamespace(get_engine=lambda:engine))
    assert 'w_' in reply, reply
    explicit = arm(engine, kind='owner_silent', platform=surface, trigger_json={'after':60})
    builtin = arm(engine, kind='owner_silent', platform=surface, trigger_json={'builtin':True})
    engine.policy = replace(engine.policy, owner_silent_checkin=True)
    engine.clock.advance(61)
    engine.silence()
    assert engine.store.get_wake(builtin.id).status == 'cancelled'
    occurrences = engine.store.list_occurrences(explicit.id)
    assert len(occurrences) == 1 and occurrences[0].state == 'pending'
    assert engine.gate(explicit, occurrences[0], engine.store.get_session(KEY))[0] == ''


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


def test_accepted_injection_clears_app_hold_timestamp(engine, monkeypatch):
    monkeypatch.setattr(policy, 'local_injection_supported', lambda: True)
    session(engine, platform='desktop')
    w = arm(engine, platform='desktop')
    o = pending(engine, w)
    engine.ctx.accept = False
    engine.dispatch(o, cache_line='')
    assert engine.store.get_meta('held_since:'+o.id)
    engine.clock.advance(60)
    engine.ctx.accept = True
    engine.dispatch(o, cache_line='')
    assert engine.store.get_occurrence(w.id, o.dedupe_key).state == 'claimed'
    assert not engine.store.get_meta('held_since:'+o.id)


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


@pytest.mark.parametrize('surface', ['desktop', 'tui'])
@pytest.mark.parametrize('mode', ['absent', 'raising', 'unknown'])
def test_commands_use_stored_app_surface(engine, monkeypatch, surface, mode):
    monkeypatch.setattr(policy, 'local_injection_supported', lambda: True)
    session(engine, platform=surface)
    bind(monkeypatch, surface)
    if mode != 'absent':
        def resolve():
            if mode == 'raising': raise RuntimeError('no policy scope')
            return 'local'
        monkeypatch.setattr(engine.ctx, 'session_surface', resolve, raising=False)
    runtime = SimpleNamespace(get_engine=lambda:engine)
    assert 'HeartBeat 2.0: on' in commands.handle('status', runtime)
    assert engine.store.get_session(KEY).platform == surface
    # Turning a known app chat off remains available even on an incapable host.
    monkeypatch.setattr(policy, 'local_injection_supported', lambda: False)
    commands.handle('off', runtime)
    assert engine.store.get_session(KEY).enabled is False


def test_command_without_stored_local_session_asks_for_message(engine, monkeypatch):
    bind(monkeypatch, 'local', key='agent:main:local:dm:fresh')
    assert commands.handle('status', SimpleNamespace(get_engine=lambda:engine)) == (
        'Send a message in this chat first, then try /pulse again.')
    assert engine.store.get_session('agent:main:local:dm:fresh') is None


@pytest.mark.parametrize('action', ['run', 'test'])
@pytest.mark.parametrize('platform', ['cli', 'api_server', 'local'])
def test_run_and_test_use_stored_session_before_environment(engine, monkeypatch, action, platform):
    w = arm(engine, kind='every')
    bind(monkeypatch, platform)
    reply = commands.handle(action+' '+w.id, SimpleNamespace(get_engine=lambda:engine))
    assert ('Queued ' if action == 'run' else 'Test ') + w.id in reply
    assert engine.store.get_session(KEY).platform == 'telegram'


def test_closed_app_three_hour_hold_keeps_label_and_lateness(engine, monkeypatch):
    monkeypatch.setattr(policy, 'local_injection_supported', lambda: True)
    session(engine, platform='desktop')
    w = arm(engine, platform='desktop')
    first = engine.clock.now()
    o = pending(engine, w, key=f't:{first:.17g}')
    engine.ctx.accept = False
    bind(monkeypatch, 'desktop')
    runtime = SimpleNamespace(get_engine=lambda:engine)
    monkeypatch.setattr(engine, 'cache_line', lambda wake, **kwargs: '')
    for _ in range(3*3600//5):
        engine.tick()
        assert not engine.store.get_meta('last_error')
        held = engine.store.get_occurrence(w.id, o.dedupe_key)
        assert held.error == 'app_closed'
        assert float(engine.store.get_meta('held_since:'+o.id)) == first
        engine.clock.advance(5)
    preview = commands.handle('test '+w.id, runtime)
    assert 'error backoff' not in preview and 'closed app window' in preview
    engine.ctx.accept = True
    engine.dispatch(o, cache_line='')
    text = engine.ctx.calls[-1][0]
    assert 'late by 3h' in text and 'error backoff' not in text
    assert not engine.store.get_meta('held_since:'+o.id)


@pytest.mark.parametrize('surface', ['desktop', 'tui', 'telegram'])
def test_injection_exceptions_retry_then_die_without_payload(engine, monkeypatch, caplog, surface):
    monkeypatch.setattr(policy, 'local_injection_supported', lambda: True)
    session(engine, platform=surface)
    w = arm(engine, platform=surface)
    o = pending(engine, w)
    def broken(*args, **kwargs): raise ValueError('PRIVATE PAYLOAD MUST NOT APPEAR')
    monkeypatch.setattr(engine.ctx, 'inject_message', broken)
    for index, delay in enumerate((30,60,300,900,3600), 1):
        engine.dispatch(o, cache_line='')
        current = engine.store.get_occurrence(w.id, o.dedupe_key)
        assert current.state == ('dead' if index == 5 else 'pending')
        assert current.error == 'inject_failed'
        assert engine.store.get_meta('inject_failures:'+o.id) == str(index)
        assert not engine.store.get_meta('held_since:'+o.id)
        engine.clock.advance(delay)
    assert 'ValueError' in caplog.text
    assert 'PRIVATE PAYLOAD' not in caplog.text and 'Check the task.' not in caplog.text


def test_desk_bounds_closed_app_rows(engine, monkeypatch):
    from heartbeat2.desk import render_desk
    monkeypatch.setattr(policy, 'local_injection_supported', lambda: True)
    session(engine, platform='desktop')
    for _ in range(40):
        w = arm(engine, platform='desktop')
        o = pending(engine, w)
        engine.store.update_occurrence(w.id, o.dedupe_key, 'pending', error='app_closed')
        engine.store.set_meta('held_since:'+o.id, str(engine.clock.now()))
    text = render_desk(engine, engine.store.get_session(KEY), clock=engine.clock,
                       timezone='UTC', policy=engine.policy)
    assert len(text) <= 1800
    assert text.count('held: app window is closed') == 3
    assert '- 37 more held while their app window is closed' in text
    assert 'Owner last wrote' in text and 'Items' in text and 'quiet hours' in text


HELD_APP_LINE = ('This app window shows anything you write, so reply with exactly [SILENT] and nothing else, '
                 'even when you respond with notify=true: HeartBeat keeps the summary, not your reply.')


@pytest.mark.parametrize('held', ['quiet_hours', 'unanswered', 'daily_message_cap'])
@pytest.mark.parametrize('stop_hint', ['once', 'always', 'first_per_chat'])
def test_held_app_turn_replies_silent_and_offers_no_stop_hint(clock, held, stop_hint):
    from heartbeat2.wake_text import offers_stop_hint
    w,o,s=records(clock)
    s=replace(s,first_notice_sent=False)
    messaging=render_wake(w,o,replace(s,platform='telegram'),clock=clock,message_held=held,stop_hint=stop_hint)
    assert HELD_APP_LINE not in messaging
    for platform in ('desktop','tui'):
        app=replace(s,platform=platform)
        text=render_wake(w,o,app,clock=clock,message_held=held,stop_hint=stop_hint)
        assert HELD_APP_LINE in text
        assert 'stop checking in' not in text
        assert not offers_stop_hint(w,app,stop_hint=stop_hint,message_held=held)
        # Messages flowing again: the app chat gets the ordinary not-notifying rule, no held line.
        assert HELD_APP_LINE not in render_wake(w,o,app,clock=clock,stop_hint=stop_hint)


@pytest.mark.parametrize('stored,expected', [('cli', CLI), ('api_server', API)])
@pytest.mark.parametrize('action', ['status', 'off', 'quiet off'])
def test_command_in_stored_cli_or_api_chat_gives_its_refusal(engine, monkeypatch, stored, expected, action):
    session(engine, platform=stored)
    bind(monkeypatch, 'local')
    assert commands.handle(action, SimpleNamespace(get_engine=lambda:engine)) == expected


@pytest.mark.parametrize('source,expected', [('desktop', tool.APP_BACKEND_REFUSAL), ('tui', tool.APP_BACKEND_REFUSAL), ('', '')])
def test_desktop_chat_backend_refusal_says_why(engine, monkeypatch, source, expected):
    """#233: Hermes's Desktop chat backend gives plugin tools an empty platform and surface, only the source. Wakes
    can't be delivered there yet, so create stays refused, now with a reason and the workaround."""
    key = '20261124_120000_abcd'
    engine.tool.session_key_fn = lambda: key
    values = {'KEY': key, 'PLATFORM': '', 'SOURCE': source, 'CHAT_TYPE': '', 'CHAT_ID': '', 'ID': 'sid'}
    env = lambda name: values.get(name.removeprefix('HERMES_SESSION_'), '')
    monkeypatch.setattr(tool, 'session_env', env)
    monkeypatch.setattr(commands, 'session_env', env)
    monkeypatch.setattr(engine.ctx, 'session_surface', lambda: '', raising=False)
    result = engine.tool.handle(CREATE)
    assert result['error'] == 'non_gateway' and result.get('detail', '') == expected
