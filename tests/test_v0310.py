"""#147: undeliverable built-ins and settings-owned wake capacity."""
from dataclasses import replace
import hashlib
import logging
from types import SimpleNamespace

import pytest

from heartbeat2 import commands
from heartbeat2.model import Session, Wake
from test_engine import engine, arm, pending, session, KEY


def enable(e):
    e.policy = replace(e.policy, owner_silent_checkin=True, own_time_hours=6)


def arm_in(e, key, kind='at', **changes):
    if key == KEY:
        return arm(e, kind, **changes)
    w = Wake(key, 'Check the task.', kind, platform='telegram', chat_type='dm',
             chat_id='fixture', armed_session_id='sid', next_due_at=e.clock.now(),
             expires_at=e.clock.now()+14*86400)
    if kind == 'self':
        w = replace(w, base_interval_s=3600, eff_interval_s=3600)
    return e.store.create_wake(replace(w, **changes))


def builtin(e, family, key=KEY, **changes):
    own = family == 'own_time'
    digest = (hashlib.sha256 if own else hashlib.sha1)(key.encode()).hexdigest()[:16]
    trigger = {'builtin': True, **({'own_time': True, 'hours': 6} if own else {'after': 86400})}
    return arm_in(e, key, 'self' if own else 'owner_silent',
               id=('owntime_' if own else 'checkin_')+digest,
               creator='user', trigger_json=trigger, **changes)


def create(e):
    return e.tool.handle({'action': 'create', 'note': 'Fixture reminder.',
                          'trigger': {'kind': 'at', 'at': 'in 5m'}}, session_id='sid')


def fill(e, count, *, builtins=False, same_chat=False, status='active'):
    for i in range(count):
        key = KEY if same_chat else f'agent:telegram:dm:fixture-{i}'
        if builtins:
            e.store.upsert_session(Session(key, 'telegram', 'dm', f'fixture-{i}', 'sid'))
            builtin(e, 'silence', key, status=status)
        else:
            arm_in(e, key, status=status)


@pytest.mark.parametrize('family', ['silence', 'own_time'])
@pytest.mark.parametrize('platform', ['cli', 'tui', 'desktop', 'api_server', 'cron', 'local', 'unknown_adapter'])
def test_builtins_only_created_for_deliverable_sessions_and_gate_agrees(engine, family, platform):
    e = engine
    enable(e)
    # A positive control also proves that settings still arm messaging chats.
    getattr(e, family)()
    gateway, = e.store.list_wakes(KEY)
    assert gateway.trigger_json.get('builtin') and gateway.platform == 'telegram'
    local_key = 'local-fixture'
    local = Session(local_key, platform, 'dm', 'local', 'local-sid', e.clock.now(), e.clock.now(), 'local-episode')
    e.store.upsert_session(local)
    normal = arm_in(e, local_key, platform=platform)
    assert e.gate(normal, pending(e, normal), local) == ('non_gateway', False, None)
    getattr(e, family)()
    assert not [w for w in e.store.list_wakes(local_key) if w.trigger_json.get('builtin')]


@pytest.mark.parametrize('family', ['silence', 'own_time'])
@pytest.mark.parametrize('status', ['active', 'proposed'])
def test_cleanup_cancels_pending_preserves_started_and_does_not_resurrect(engine, family, status):
    e = engine
    enable(e)
    gateway = builtin(e, family)
    local_key = 'local-fixture'
    # Cleanup must also work without a human timestamp or episode.
    e.store.upsert_session(Session(local_key, 'cli', 'dm', 'local', 'local-sid'))
    local = builtin(e, family, local_key, status=status, platform='cli')
    waiting = pending(e, local, 'pending')
    running = pending(e, local, 'started')
    assert e.store.claim(local.id, running.dedupe_key, epoch=e.epoch, pid=1)
    assert e.store.start(local.id, running.dedupe_key, attempt=1)
    ordinary = arm_in(e, local_key, platform='cli')
    missing = builtin(e, family, 'missing-session')
    getattr(e, family)()
    cleaned = e.store.get_wake(local.id)
    assert (cleaned.status, cleaned.status_reason) == ('cancelled', 'non_gateway')
    cancelled = e.store.get_occurrence(local.id, waiting.dedupe_key)
    assert (cancelled.state, cancelled.error) == ('superseded', 'non_gateway')
    assert e.store.get_occurrence(local.id, running.dedupe_key).state == 'started'
    assert e.store.get_wake(gateway.id).status == 'active'
    assert e.store.get_wake(ordinary.id).status == 'active'
    assert e.store.get_wake(missing.id).status == 'active'
    getattr(e, family)()
    assert e.store.get_wake(local.id) == cleaned
    # A session that never had a human message doesn't bring it back, even on a messaging platform.
    old = e.store.get_session(local_key)
    assert e.store.cas_session(local_key, {'platform': 'cli'}, platform='telegram')
    assert e.store.get_session(local_key).platform == 'telegram' and old.last_human_at is None
    getattr(e, family)()
    assert e.store.get_wake(local.id) == cleaned


@pytest.mark.parametrize('family', ['silence', 'own_time'])
def test_builtin_returns_when_the_chat_is_deliverable_again(engine, family):
    # A session's platform follows its latest human message; a chat seen once from a local client keeps its built-in.
    e = engine
    enable(e)
    session(e, last_human_at=e.clock.now(), episode_id='ep1')
    getattr(e, family)()
    w, = [x for x in e.store.list_wakes(KEY) if x.trigger_json.get('builtin') and bool(x.trigger_json.get('own_time')) == (family == 'own_time')]
    assert w.status == 'active'
    session(e, platform='cli')
    getattr(e, family)()
    assert (e.store.get_wake(w.id).status, e.store.get_wake(w.id).status_reason) == ('cancelled', 'non_gateway')
    session(e, platform='telegram')
    getattr(e, family)()
    back = e.store.get_wake(w.id)
    assert (back.status, back.status_reason) == ('active', '')


@pytest.mark.parametrize('family,status,reason', [
    ('silence', 'done', ''), ('silence', 'orphaned', ''),
    ('own_time', 'done', ''), ('own_time', 'orphaned', ''), ('own_time', 'cancelled', 'settings'),
])
def test_non_gateway_builtins_cannot_reactivate(engine, family, status, reason):
    enable(engine)
    session(engine, platform='cli')
    w = builtin(engine, family, status=status, status_reason=reason, platform='cli')
    getattr(engine, family)()
    assert engine.store.get_wake(w.id) == w


@pytest.mark.parametrize('family', ['silence', 'own_time'])
def test_cleanup_obeys_settings_then_cleans_when_enabled(engine, family):
    e = engine
    session(e, platform='cli')
    w = builtin(e, family, platform='cli')
    o = pending(e, w)
    getattr(e, family)()
    assert e.store.get_wake(w.id).status_reason == ('settings' if family == 'own_time' else '')
    if family == 'silence':
        e.policy = replace(e.policy, owner_silent_checkin=True, source_silence=False)
        e.silence()
        assert e.store.get_wake(w.id).status == 'active'
    else:
        # Reproduce an old active wake once own time is enabled again.
        e.store.cas_wake(w.id, 'cancelled', status='active', status_reason='')
        e.store.add_occurrence(w.id, 'new-pending')
    enable(e)
    e.policy = replace(e.policy, source_silence=True)
    getattr(e, family)()
    assert e.store.get_wake(w.id).status_reason == 'non_gateway'
    assert e.store.get_occurrence(w.id, o.dedupe_key if family == 'silence' else 'new-pending').state == 'superseded'


def test_fifty_gateway_builtins_do_not_block_create(engine):
    fill(engine, engine.policy.max_active_profile, builtins=True)
    result = create(engine)
    assert result.get('armed'), result


@pytest.mark.parametrize('scope', ['chat', 'profile'])
@pytest.mark.parametrize('status', ['active', 'proposed'])
def test_user_wake_caps_explain_recovery_and_log_safely(engine, caplog, scope, status):
    e = engine
    fill(e, e.policy.max_active if scope == 'chat' else e.policy.max_active_profile, same_chat=scope == 'chat', status=status)
    with caplog.at_level(logging.WARNING, logger='heartbeat2'):
        result = create(e)
    reason = 'max_active' if scope == 'chat' else 'global_max_active'
    assert result['error'] == reason
    assert result['detail'] and '/pulse cancel' in result['detail']
    limit = e.policy.max_active if scope == 'chat' else e.policy.max_active_profile
    assert f'reached its limit of {limit} active or proposed wakes' in result['detail'] and "don't count" in result['detail']
    records = [r for r in caplog.records if r.name == 'heartbeat2' and r.levelno == logging.WARNING]
    assert len(records) == 1 and reason in records[0].getMessage()
    assert hashlib.sha256(KEY.encode()).hexdigest()[:16] in records[0].getMessage()
    assert KEY not in records[0].getMessage() and 'Fixture reminder.' not in records[0].getMessage()


def test_same_chat_builtins_do_not_consume_last_create_slot(engine):
    fill(engine, engine.policy.max_active-1, same_chat=True)
    builtin(engine, 'silence')
    builtin(engine, 'own_time')
    result = create(engine)
    assert result.get('armed'), result


@pytest.mark.parametrize('action', ['ok', 'resume'])
@pytest.mark.parametrize('scope', ['chat', 'profile'])
def test_activation_paths_exclude_builtins(engine, monkeypatch, action, scope):
    e = engine
    if scope == 'profile':
        fill(e, e.policy.max_active_profile, builtins=True)
    else:
        fill(e, e.policy.max_active-1, same_chat=True)
        builtin(e, 'silence')
        builtin(e, 'own_time')
    w = arm(e, status='proposed' if action == 'ok' else 'paused')
    if action == 'ok':
        monkeypatch.setattr(commands, 'session_env', lambda name: KEY if name == 'HERMES_SESSION_KEY' else '')
        result = commands.handle('ok '+w.id, SimpleNamespace(get_engine=lambda: e))
        assert not result.startswith('Error:'), result
    else:
        result = e.tool.handle({'action': 'resume', 'id': w.id}, session_id='sid')
        assert result.get('ok'), result
    assert e.store.get_wake(w.id).status == 'active'


@pytest.mark.parametrize('scope', ['chat', 'profile'])
def test_resume_cap_refusal_has_matching_detail(engine, scope):
    e = engine
    fill(e, e.policy.max_active if scope == 'chat' else e.policy.max_active_profile, same_chat=scope == 'chat')
    w = arm(e, status='paused')
    refused = e.tool.handle({'action': 'resume', 'id': w.id}, session_id='sid')
    assert refused['error'] == 'max_active'
    assert refused['detail'] == create(e)['detail'] and '/pulse cancel' in refused['detail']


@pytest.mark.parametrize('family', ['silence', 'own_time'])
@pytest.mark.parametrize('reason', ['governor:noop', ''])
def test_paused_builtin_keeps_its_pause_through_a_local_visit(engine, family, reason):
    # A governor or owner pause must survive the chat being seen from a local client and then a messaging one.
    e = engine
    enable(e)
    session(e, last_human_at=e.clock.now(), episode_id='ep1')
    w = builtin(e, family, status='paused', status_reason=reason)
    session(e, platform='cli')
    getattr(e, family)()
    assert e.store.get_wake(w.id) == w
    session(e, platform='telegram')
    getattr(e, family)()
    after = e.store.get_wake(w.id)
    assert (after.status, after.status_reason) == ('paused', reason)


def test_issue_147_repro_leaked_cli_checkins_then_telegram_create(engine):
    # The reported state: 57 check-ins left by CLI launches (one per session key), then a create in a Telegram DM.
    e = engine
    enable(e)
    leaked = []
    for i in range(57):
        key = f'cli-launch-{i}'
        e.store.upsert_session(Session(key, 'cli', 'dm', f'local-{i}', f'sid-{i}', e.clock.now(), e.clock.now(), f'ep-{i}'))
        leaked.append(builtin(e, 'silence', key, platform='cli'))
    result = create(e)
    assert result.get('armed'), result
    e.silence()
    assert all((e.store.get_wake(w.id).status, e.store.get_wake(w.id).status_reason) == ('cancelled', 'non_gateway') for w in leaked)
    assert not [w for w in e.store.list_wakes() if w.trigger_json.get('builtin') and w.platform == 'cli' and w.status == 'active']
