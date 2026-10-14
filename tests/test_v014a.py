"""Settings, pause/restart preservation and owner cache display regressions."""
from dataclasses import fields, replace
import json
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest
import yaml

from heartbeat2 import commands, governor
from heartbeat2.model import Session
from heartbeat2.policy import Policy
from heartbeat2.poller import Poller
from heartbeat2.registration import Runtime, register
from test_engine import engine, arm, pending, session, start, end, admit, GatewayCtx, KEY


BAD_SETTINGS = [('quiet_hours', '10pm-6am'), ('max_messages_per_day', 1.5),
                ('min_interval_minutes', 0), ('timezone', 'Europe/Pari'), ('max_active', -1)]


@pytest.fixture
def runtime(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(Runtime, 'start', lambda self: None)
    ctx = GatewayCtx(tmp_path, settings={'quiet_hours': '', 'timezone': 'UTC'})
    rt = register(ctx, clock=clock)
    monkeypatch.setattr(commands, 'session_env', lambda n: KEY if n == 'HERMES_SESSION_KEY' else '')
    yield rt
    rt.close()


def bind(rt):
    e = rt.get_engine()
    e.paused = lambda: False
    e.compression_tip = lambda sid: sid
    e.tool.session_key_fn = lambda: KEY
    e.store.upsert_session(Session(KEY, 'telegram', 'dm', 'unit', 'sid',
                                  e.clock.now(), e.clock.now(), 'episode'))
    return e


@pytest.mark.parametrize('key,value', BAD_SETTINGS)
def test_bad_live_setting_keeps_policy_and_fires(runtime, key, value):
    e = bind(runtime)
    old = e.policy
    arm(e, next_due_at=e.clock.now() + 60)
    runtime.ctx.settings.update({key: value, 'max_unanswered': 9})
    e.clock.advance(120)
    e.tick()
    assert len(e.ctx.calls) == 1
    assert e.policy == replace(old, max_unanswered=9)
    notice = e.store.get_meta('settings_error')
    assert key in notice
    status = commands.handle('status', runtime)
    assert 'Setting ignored: ' + key in status
    assert '; using ' in status
    assert e.tool.handle({'action': 'list'})['settings_error'] in status
    runtime.ctx.settings[key] = getattr(old, key)
    e.refresh()
    assert not e.store.get_meta('settings_error')
    assert e.policy.max_unanswered == 9
    assert 'Setting ignored:' not in commands.handle('status', runtime)
    assert 'settings_error' not in e.tool.handle({'action': 'list'})


@pytest.mark.parametrize('key,value', BAD_SETTINGS)
def test_bad_startup_setting_builds_engine_and_hooks(runtime, key, value):
    runtime.ctx.settings[key] = value
    runtime.ctx.settings['max_messages_per_day'] = value if key == 'max_messages_per_day' else 3
    e = bind(runtime)
    assert getattr(e.policy, key) == getattr(Policy(), key)
    if key != 'max_messages_per_day':
        assert e.policy.max_messages_per_day == 3
    hook, = runtime.ctx.hooks['pre_llm_call']
    hook(session_id='sid', turn_id='owner', user_message='hello', platform='telegram')
    assert e.store.get_session(KEY).busy_since is not None
    result = json.loads(runtime.tool({'action': 'list'}))
    assert result['ok'] and key in result['settings_error']


def test_multiple_bad_settings_report_each_default(runtime):
    runtime.ctx.settings.update(dict(BAD_SETTINGS))
    e = bind(runtime)
    status = commands.handle('status', runtime)
    for key, _ in BAD_SETTINGS:
        assert 'Setting ignored: ' + key in status
        assert getattr(e.policy, key) == getattr(Policy(), key)


def test_refresh_build_exception_does_not_abort_due_wake(engine):
    e = engine
    old = e.policy
    e.refresh_policy = lambda: Policy.from_settings({'quiet_hours': '10pm-6am'}, clock=e.clock)
    arm(e)
    e.tick()
    assert len(e.ctx.calls) == 1 and e.policy == old
    assert e.store.get_meta('settings_error')
    e.refresh_policy = lambda: old
    e.refresh()
    assert not e.store.get_meta('settings_error')


def test_runtime_bootstrap_shares_last_good_policy(runtime, monkeypatch):
    e = bind(runtime)
    stop = threading.Event()
    observed = {}
    monkeypatch.setattr(Poller, 'acquire', lambda self: True)
    def refresh_sequence(runner):
        runtime.ctx.settings.update({'quiet_hours': '23:00-07:00',
                                     'max_model_wakes_per_day': 5})
        runner.refresh()
        observed['before'] = (e.policy.quiet_hours, runner.policy.quiet_hours)
        runtime.ctx.settings['quiet_hours'] = '11pm-7am'
        e.refresh()
        runner.refresh()
        observed['policies'] = (e.policy, runner.policy)
        observed['notice'] = json.loads(e.store.get_meta('settings_error'))
        stop.set()
    monkeypatch.setattr(Poller, 'tick', refresh_sequence)
    runtime._bootstrap(stop)  # Exercise the actual runner refresh wiring.
    assert observed['before'] == ('', '23:00-07:00')
    engine_policy, runner_policy = observed['policies']
    assert engine_policy == runner_policy
    assert runner_policy.quiet_hours == '23:00-07:00'
    assert runner_policy.max_model_wakes_per_day == 5
    assert observed['notice'][0]['effective'] == runner_policy.quiet_hours


def test_valid_disable_applies_with_rejected_quiet_hours(runtime):
    e = bind(runtime)
    old_quiet = e.policy.quiet_hours
    arm(e)
    runtime.ctx.settings.update({'enabled': False, 'quiet_hours': '10pm-6am'})
    e.tick()
    assert e.policy.enabled is False
    assert e.policy.quiet_hours == old_quiet
    assert not e.ctx.calls
    assert json.loads(e.store.get_meta('settings_error'))[0]['effective'] == old_quiet


def test_invalid_refresh_never_clears_or_rewrites_unchanged_notice(runtime, monkeypatch):
    e = bind(runtime)
    runtime.ctx.settings['quiet_hours'] = '10pm-6am'
    e.refresh()
    writes = []
    set_meta = e.store.set_meta
    def record(key, value):
        if key == 'settings_error':
            writes.append(value)
        return set_meta(key, value)
    monkeypatch.setattr(e.store, 'set_meta', record)
    for _ in range(3):
        e.refresh()
        assert e.settings_notice()
    assert '' not in writes
    assert writes == []


@pytest.mark.parametrize('nondefault', [False, True], ids=['defaults', 'nondefaults'])
def test_every_policy_field_round_trips(clock, nondefault):
    defaults = Policy()
    settings = {}
    if nondefault:
        strings = {'agent_can_create': 'ask_first', 'timezone': 'Asia/Tokyo',
                   'quiet_hours': '21:00-06:00', 'probe_commands': 'git status'}
        for field in fields(Policy):
            value = getattr(defaults, field.name)
            if isinstance(value, bool):
                settings[field.name] = not value
            elif isinstance(value, int):
                settings[field.name] = value + 1
            elif field.name in strings:
                settings[field.name] = strings[field.name]
    policy = Policy.from_settings(settings, clock=clock, profile_timezone='UTC')
    for field in fields(Policy):
        rebuilt = Policy.from_settings({field.name: getattr(policy, field.name)},
            clock=policy.clock, profile_timezone=policy.profile_timezone)
        assert getattr(rebuilt, field.name) == getattr(policy, field.name), field.name


def test_governor_pause_holds_pending_watcher_until_keep(engine):
    e = engine
    e.policy = replace(e.policy, wake_spacing_minutes=0)
    w = arm(e, 'url', trigger_json={'check_every': 60}, probe_state_json={'fingerprint': 'v1'})
    e.store.cas_wake(w.id, 'active', noop_streak=e.policy.noop_pause_after - 1)
    e.probe_runner = lambda _: {'fire': True, 'identity': 'v2', 'state': {'fingerprint': 'v2'}, 'message': 'v2'}
    e.tick()
    start(e)
    e.clock.advance(61)
    e.probe_runner = lambda _: {'fire': True, 'identity': 'v3', 'state': {'fingerprint': 'v3'}, 'message': 'v3'}
    e.tick()
    o, = e.store.list_occurrences(w.id, state='pending')
    assert o.error == 'busy' and o.held_since is None
    end(e, outcome='no_change')
    assert e.store.get_wake(w.id).status_reason == 'governor:noop'
    e.clock.advance(1)
    e.tick()
    held = e.store.get_occurrence(w.id, o.dedupe_key)
    assert (held.state, held.error, held.held_reason) == ('pending', 'held_paused', 'held_paused')
    assert held.held_since is not None and held.held_matches == 1
    admit(e, text='keep it')
    assert governor.owner_answer(e, e.store.get_wake(w.id), 'keep')['ok']
    e.probe_runner = lambda _: {'fire': False, 'state': {'fingerprint': 'v3'}}
    for _ in range(3):
        e.clock.advance(61)
        e.tick()
    assert len(e.ctx.calls) == 2
    assert e.store.get_occurrence(w.id, o.dedupe_key).attempt == 1


def test_paused_time_occurrence_is_not_held(engine):
    w = arm(engine)
    o = pending(engine, w)
    engine.store.cas_wake(w.id, 'active', status='paused')
    assert engine.gate(engine.store.get_wake(w.id), o, engine.store.get_session(KEY))[:2] == ('wake_inactive', False)


@pytest.mark.parametrize('reconcile_first', [False, True])
def test_restart_preserves_early_turn_busy_fence(engine, reconcile_first):
    e = engine
    w = arm(e)
    o = pending(e, w, 't:old')
    assert e.store.claim(w.id, o.dedupe_key, epoch='crashed-process', pid=1)
    if reconcile_first:
        e.acquire()
    e.intake.pre_llm_call(session_id='sid', turn_id='resume', user_message='hello', platform='telegram')
    busy = e.store.get_session(KEY).busy_since
    for _ in range(4):
        e.tick()
        e.clock.advance(600)
    assert e.store.get_session(KEY).busy_since == busy
    assert e.ctx.calls == []
    assert e.store.get_occurrence(w.id, o.dedupe_key).attempt == 1
    e.intake.on_session_end(session_id='sid', turn_id='resume', completed=True)
    e.tick()
    assert len(e.ctx.calls) == 1


@pytest.mark.parametrize('live', [False, True])
def test_restart_interrupt_only_clears_preboot_busy(engine, live):
    e = engine
    w = arm(e)
    e.tick()
    start(e)
    e.close()
    e.clock.advance(1)
    runner = Poller(e.ctx, e.store, e.policy, e.clock, paused=lambda: False)
    if live:
        session(e, busy_since=e.clock.now(), busy_epoch=runner.epoch)
    else:
        # The flag belongs to the crashed process, as in test_v013b_review.crashed().
        session(e, busy_epoch='crashed-process')
    busy = e.store.get_session(KEY).busy_since
    try:
        runner.reconcile()
        assert e.store.get_session(KEY).busy_since == (busy if live else None)
        assert e.store.list_occurrences(w.id)[0].state == 'interrupted'
    finally:
        runner.close()


def cache_session(e):
    session(e, last_route='openai|gpt-6.1-sol|api.openai.com',
            last_cache_touch_at=e.clock.now(), last_prompt_tokens=50000)


@pytest.mark.parametrize('add', ['in 2h Owner one-shot', 'when silent 24h Check in'])
def test_owner_add_cache_matches_tool(engine, monkeypatch, add):
    cache_session(engine)
    monkeypatch.setattr(commands, 'session_env', lambda n: KEY if n == 'HERMES_SESSION_KEY' else '')
    out = commands.handle('add ' + add, SimpleNamespace(get_engine=lambda: engine))
    w, = engine.store.list_wakes(KEY)
    assert out.splitlines()[-2] == engine.tool.cache_result(w)
    assert out.splitlines()[-1] == engine.tool.receipt(w, engine.store.get_session(KEY))
    assert 'this wake warm' not in out
    if w.kind == 'at':
        assert '120m cold' in out
    else:
        assert 'warm' not in out.splitlines()[-2] and 'cold' not in out.splitlines()[-2]


@pytest.mark.parametrize('kind', ['command', 'at', 'owner_silent'])
def test_governor_card_uses_tool_cache_label(engine, kind):
    cache_session(engine)
    w = arm(engine, kind, next_due_at=engine.clock.now() + 7200)
    with engine.store.atomic():
        governor.pause(engine, w, 'noop')
    card = engine.store.get_meta('card:' + w.id)
    assert card == 'noop · ' + engine.tool.cache_result(engine.store.get_wake(w.id))
    assert 'this wake warm' not in card


def test_settings_descriptions_and_draft_version():
    root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((root / 'plugin.yaml').read_text())
    # CONTRIBUTING.md invites notes under '## Unreleased'; the first versioned heading must match the manifest.
    top = next(l for l in (root / 'CHANGELOG.md').read_text().splitlines() if l.startswith('## ') and l != '## Unreleased')
    assert top.split()[1] == manifest['version'], (top, manifest['version'])
    for key, field in manifest['config_schema'].items():
        assert field['description'].rstrip('.') != field['label'], key


def test_command_watcher_docs_do_not_offer_proxy_config_workaround():
    root = Path(__file__).resolve().parents[1]
    assert 'configure those in gh/git config instead' not in (root / 'CHANGELOG.md').read_text()
    for name in ('README.md', 'SECURITY.md'):
        text = (root / name).read_text()
        assert 'does not pass proxy or custom-CA variables yet' in text
