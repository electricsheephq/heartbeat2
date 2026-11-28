"""Desktop/TUI compaction with a transport that accepts only Hermes' live key."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import json
import sqlite3
import sys
from threading import Event
from types import SimpleNamespace

import pytest

from heartbeat2 import commands, policy, tui_probe
from heartbeat2.poller import Poller
from heartbeat2.registration import Runtime
from test_backend_wakes import ARM, CHAT, bind, hosts, notice_sends
from test_engine import GatewayCtx
from test_watchdog_retry import reminder

K0, K1, K2 = CHAT, 'rotation-one', 'rotation-two'


@pytest.fixture
def lineage(monkeypatch):
    rows = {K0: {'parent_session_id': None}}
    next_key = {}
    reads = []

    class SessionDB:
        def __init__(self, *, db_path, read_only):
            assert read_only

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get_session(self, key):
            reads.append(key)
            return rows.get(key)

        def get_compression_tip(self, key):
            reads.append(key)
            while key in next_key:
                key = next_key[key]
            return key

    def child(parent, key, *, kind='compression'):
        rows[key] = {'parent_session_id': parent}
        if kind == 'compression':
            next_key[parent] = key
        elif kind in {'branch', 'reset', 'delegate'}:
            marker = 'branched' if kind == 'branch' else kind
            rows[key]['model_config'] = {f'_{marker}_from': parent}
        else:
            assert kind == 'stale-sibling' and parent in next_key

    monkeypatch.setitem(sys.modules, 'hermes_state', SimpleNamespace(SessionDB=SessionDB))
    return SimpleNamespace(child=child, db=SessionDB, rows=rows, reads=reads)


def arm(e, **fields):
    result = e.tool.handle({**ARM, **fields})
    assert result.get('armed'), result
    return result['wake']['id']


def pulse(e, text):
    return commands.handle(text, SimpleNamespace(get_engine=lambda: e))


def strict_inject(e, live, monkeypatch):
    def inject(text, *, session_key):
        e.ctx.calls.append((text, session_key))
        return session_key == live

    monkeypatch.setattr(e.ctx, 'inject_message', inject)


def deliver(e, wid, live, turn, monkeypatch, *, surface='desktop', advance=0, outcome='done'):
    strict_inject(e, live, monkeypatch)
    before = len(e.ctx.calls)
    e.clock.advance(advance)
    e.tick()
    assert [key for _, key in e.ctx.calls[before:]] == [live]
    e.intake.pre_llm_call(session_id=live, turn_id=turn, platform=surface, user_message=e.ctx.calls[-1][0])
    assert e.intake.flights[live].wake_id == wid
    assert e.store.list_occurrences(wid)[-1].state == 'started'
    e.intake.post_api_request(session_id=live, turn_id=turn, usage={})
    response = e.tool.handle({'action': 'respond', 'outcome': outcome, 'summary': 'Checked.', 'notify': False},
                             session_id=live)
    assert response.get('ok'), response
    e.intake.on_session_end(session_id=live, turn_id=turn, completed=True)
    e.tick()
    assert len(e.ctx.calls) == before + 1
    assert e.store.list_occurrences(wid)[-1].state == 'done'


@pytest.mark.parametrize('surface,co_resident', [('desktop', False), ('tui', False), ('desktop', True)])
def test_live_rotation_runs_once(hosts, lineage, monkeypatch, surface, co_resident):
    gateway, backend = hosts
    e = gateway if co_resident else backend
    if co_resident:
        e.ctx._manager.has_tui_message_injector = True
        monkeypatch.setattr(policy, 'local_injection_supported', lambda: True)
    bind(monkeypatch, e, K0, platform=surface if co_resident else '', source=surface)
    wid = arm(e)
    lineage.child(K0, K1)
    bind(monkeypatch, e, K1, platform=surface if co_resident else '', source=surface)

    deliver(e, wid, K1, 'wake', monkeypatch, surface=surface, advance=61)
    assert e.store.get_wake(wid).fires == 1
    assert e.tool.identity().session_key == K0
    assert e.store.get_session(K1) is None
    assert e.store.get_meta('host:' + K1) is None


@pytest.mark.parametrize('restart', [False, True], ids=['running', 'restarted'])
def test_double_rotation_without_intermediate_hook(hosts, lineage, monkeypatch, restart):
    _, old = hosts
    bind(monkeypatch, old, K0)
    wid = arm(old)
    old.tick()
    lineage.child(K0, K1)
    lineage.child(K1, K2)
    with closing(Poller(old.ctx, old.store, old.policy, old.clock, paused=lambda: False)) as fresh:
        e = fresh if restart else old
        if restart:
            old.close()
            assert not e.intake.bindings
        bind(monkeypatch, e, K2)
        deliver(e, wid, K2, 'wake', monkeypatch, advance=61)
        assert e.intake.bindings[K2] == K0
        assert [session.session_key for session in e.store.list_sessions()] == [K0]


def test_rotation_after_wake_turn_needs_no_owner_hook(hosts, lineage, monkeypatch):
    _, e = hosts
    bind(monkeypatch, e, K0)
    wid = arm(e, trigger={'kind': 'every', 'every': '10m'})
    e.clock.advance(e.store.get_wake(wid).next_due_at - e.clock.now() + 1)
    deliver(e, wid, K0, 'first', monkeypatch, outcome='progress')

    lineage.child(K0, K1)
    bind(monkeypatch, e, K1)
    e.clock.advance(e.store.get_wake(wid).next_due_at - e.clock.now() + 1)
    deliver(e, wid, K1, 'second', monkeypatch)
    assert e.store.get_wake(wid).fires == 2


@pytest.mark.parametrize('enabled', [True, False], ids=['deliver-once', 'keep-off'])
def test_branch_then_rotate(hosts, lineage, monkeypatch, enabled):
    _, e = hosts
    branch, tip = 'branch', 'branch-tip'
    bind(monkeypatch, e, K0)
    assert e.tool.identity().session_key == K0
    lineage.child(K0, K1)
    lineage.child(K1, branch, kind='branch')
    bind(monkeypatch, e, branch)
    wid = arm(e)
    if not enabled:
        assert pulse(e, 'off') == 'HeartBeat 2.0 off for this chat.'
    lineage.child(branch, tip)
    bind(monkeypatch, e, tip)

    if enabled:
        deliver(e, wid, tip, 'branch-wake', monkeypatch, advance=61)
        assert e.intake.bindings[tip] == branch
        assert e.store.get_wake(wid).fires == 1
    else:
        strict_inject(e, tip, monkeypatch)
        e.clock.advance(61)
        e.tick()
        assert not e.ctx.calls
    assert e.tool.identity().session_key == branch
    assert e.store.get_wake(wid).session_key == branch
    assert e.store.get_session(branch).enabled == enabled
    assert e.store.get_session(tip) is None


def test_delivery_holds_on_lineage_disagreement(hosts, lineage, monkeypatch):
    _, e = hosts
    bind(monkeypatch, e, K0)
    wid = arm(e)
    # The forward read selects a live key whose visible lineage has no path back to K0.
    lineage.rows[K1] = {'parent_session_id': None}
    compression_tip = e.compression_tip
    monkeypatch.setattr(e, 'compression_tip', lambda key: K1 if key == K0 else compression_tip(key))
    monkeypatch.setattr(tui_probe, 'queued_or_inflight', lambda key, marker: False)
    bind(monkeypatch, e, K1)
    strict_inject(e, K1, monkeypatch)
    accepted = []
    for i in range(40):
        before = len(e.ctx.calls)
        e.clock.advance(600)
        e.tick()
        for message, key in e.ctx.calls[before:]:
            if key == K1:
                accepted.append(key)
                turn = f'wrong-chat-{i}'
                e.intake.pre_llm_call(session_id=K1, turn_id=turn, platform='desktop', user_message=message)
                e.intake.on_session_end(session_id=K1, turn_id=turn, completed=True)

    assert not accepted
    assert 0 < len(e.ctx.calls) <= 40
    assert {key for _, key in e.ctx.calls} == {K0}
    occurrence, = e.store.list_occurrences(wid)
    assert (occurrence.state, occurrence.error) == ('pending', 'app_closed')
    assert e.store.get_wake(wid).fires == 0


def test_hook_can_lag_the_state_db_tip(hosts, lineage, monkeypatch):
    _, e = hosts
    bind(monkeypatch, e, K0)
    wid = arm(e)
    lineage.child(K0, K1)
    lineage.child(K1, K2)
    bind(monkeypatch, e, K1)
    e.intake.pre_llm_call(session_id=K1, turn_id='owner', user_message='Hello.')
    e.intake.on_session_end(session_id=K1, turn_id='owner', completed=True)
    assert e.intake.bindings[K1] == K0
    assert e.store.get_wake(wid).status == 'active'
    bind(monkeypatch, e, K2)
    deliver(e, wid, K2, 'wake', monkeypatch, advance=61)


def test_again_after_rotation_rearms_exactly_once(reminder, lineage, monkeypatch):
    e, wid, sends = reminder()
    e.intake.on_session_end(session_id=K0, failed=True)
    e.tick()
    assert e.notifications.idle.wait(2) and len(sends) == 1
    lineage.child(K0, K1)
    bind(monkeypatch, e, K1)
    e.clock.advance(e.policy.wake_spacing_minutes * 60)

    result = e.intake.pre_llm_call(session_id=K1, turn_id='owner', user_message='again')
    assert result and 're-queued' in result.get('context', '')
    e.intake.on_session_end(session_id=K1, turn_id='owner')
    rearmed, = e.store.list_wakes(K0, status='active')
    deliver(e, rearmed.id, K1, 'retry', monkeypatch)
    assert e.intake.pre_llm_call(session_id=K1, turn_id='duplicate', user_message='again') is None
    e.intake.on_session_end(session_id=K1, turn_id='duplicate')
    e.tick()
    assert len(e.ctx.calls) == 2
    assert {w.id for w in e.store.list_wakes(K0)} == {wid, rearmed.id}


@pytest.mark.parametrize('setting', ['off', 'pause'])
@pytest.mark.parametrize('split', [False, True], ids=['rotation', 'fold'])
def test_restrictions_survive_rotation(hosts, lineage, monkeypatch, setting, split):
    _, e = hosts
    bind(monkeypatch, e, K0)
    wid = arm(e)
    if setting == 'off':
        assert pulse(e, 'off') == 'HeartBeat 2.0 off for this chat.'
    else:
        assert e.store.cas_session(K0, {'paused_until': None}, paused_until=e.clock.now() + 600)
        assert 'paused' in pulse(e, 'pause ' + wid).lower()
    if split:
        bind(monkeypatch, e, K1)
        arm(e)
    lineage.child(K0, K1)
    bind(monkeypatch, e, K1)
    strict_inject(e, K1, monkeypatch)
    e.clock.advance(61)
    e.tick()
    assert not e.ctx.calls
    assert e.tool.identity().session_key == K0
    assert wid in pulse(e, 'list all')
    assert e.store.get_session(K1) is None
    if setting == 'off':
        assert not e.store.get_session(K0).enabled
        assert pulse(e, 'on') == 'HeartBeat 2.0 on for this chat.'
        assert e.store.get_session(K0).enabled
    else:
        assert e.store.get_session(K0).paused_until > e.clock.now()
        assert e.store.get_wake(wid).status == 'paused'


def split_store(e, lineage, monkeypatch):
    bind(monkeypatch, e, K0)
    older = arm(e)
    bind(monkeypatch, e, K1)
    younger = arm(e)
    e.store.add_event('human_message', 'split-event', {}, session_key=K1)
    assert e.store.cas_session(K0, {'enabled': True}, enabled=False, paused_until=e.clock.now() + 90)
    assert e.store.cas_session(K1, {'paused_until': None}, paused_until=e.clock.now() + 120)
    for key, value in {
        'quiet:' + K0: '{"until": 1760270700, "range": "12:00-13:00"}',
        'quiet:' + K1: '{"range": "22:00-07:00"}',
        'own_time_off:' + K1: '1',
        'own_time_on:' + K0: '1',
        'notice:' + K0: '["old"]',
        'notice:' + K1: '["new"]',
        'human_context:' + K1: '1',
        'switched_notice:' + K0: '2',
        'switched_notice:' + K1: '3',
        'nonhuman:' + K1 + ':42': '1',
        'backend:notice_in_flight:' + K1: '',
        'agent_spend:' + K0 + ':2025-10-12': '{"cost_microusd":2,"unpriced_calls":1}',
        'agent_spend:' + K1 + ':2025-10-12': '{"cost_microusd":3,"unpriced_calls":2}',
    }.items():
        e.store.set_meta(key, value)
    lineage.child(K0, K1)
    return older, younger


def snapshot(store):
    return tuple(tuple(tuple(row) for row in store.connection.execute(
        f'SELECT * FROM {table} ORDER BY rowid')) for table in ('sessions', 'wakes', 'events', 'meta'))


def test_startup_folds_split_store_once(hosts, lineage, monkeypatch):
    gateway, e = hosts
    older, younger = split_store(e, lineage, monkeypatch)
    e.reconcile()
    s = e.store
    chat, = s.list_sessions()
    assert (chat.session_key, chat.enabled, chat.paused_until, chat.current_session_id) == (
        K0, False, e.clock.now() + 120, K1)
    assert {w.id for w in s.list_wakes(K0)} == {older, younger}
    event, = s.pending_events()
    assert event['session_key'] == K0
    assert not gateway.store.list_sessions() and not gateway.store.list_wakes() and not gateway.store.pending_events()
    assert s.get_meta('host:' + K1) is None
    assert json.loads(s.get_meta('quiet:' + K0)) == {
        'until': 1760270700, 'range': '12:00-13:00', 'ranges': ['22:00-07:00']}
    for hour, release in ((12, 13), (22, 31)):
        midnight = int(e.clock.now() // 86400) * 86400
        assert commands.quiet_until(e, K0, now=midnight + hour * 3600) == midnight + release * 3600
    assert s.get_meta('own_time_off:' + K0) == '1'
    assert s.get_meta('own_time_on:' + K0) in (None, '')
    assert json.loads(s.get_meta('notice:' + K0)) == ['old', 'new']
    assert s.get_meta('human_context:' + K0) == '1'
    assert s.get_meta('switched_notice:' + K0) == '5'
    assert json.loads(s.get_meta('agent_spend:' + K0 + ':2025-10-12')) == {
        'cost_microusd': 5, 'unpriced_calls': 3}
    assert s.get_meta('nonhuman:' + K1 + ':42') is None
    assert s.get_meta('backend:notice_in_flight:' + K1) is None
    before = snapshot(s)
    e.process_start = True
    e.reconcile()
    assert snapshot(s) == before


def test_fold_keeps_one_own_time_schedule_and_off_controls_it(hosts, lineage, monkeypatch):
    e, _ = hosts
    e.ctx._manager.has_tui_message_injector = True
    monkeypatch.setattr(policy, 'local_injection_supported', lambda: True)
    s = e.store
    for key, age in ((K0, 10), (K1, 0)):
        bind(monkeypatch, e, key, platform='desktop')
        assert e.tool.identity()
        assert s.cas_session(key, {'episode_id': ''}, episode_id=key, last_human_at=e.clock.now()-age)
    assert 'on in this chat' in pulse(e, 'owntime on')
    e.own_time()
    existing, = s.list_wakes(K1)
    assert not s.list_wakes(K0)
    lineage.child(K0, K1)

    e.tick()
    assert s.get_session(K1) is None
    assert [w.id for w in s.list_wakes(K0)] == [existing.id]
    assert s.get_session(K0).episode_id == K1
    assert 'off in this chat' in pulse(e, 'owntime off')
    assert s.get_wake(existing.id).status == 'cancelled'


@pytest.mark.parametrize('root_status', ['active', 'cancelled'])
def test_fold_deduplicates_own_time(hosts, lineage, monkeypatch, root_status):
    e, _ = hosts
    e.ctx._manager.has_tui_message_injector = True
    monkeypatch.setattr(policy, 'local_injection_supported', lambda: True)
    s = e.store
    for key in (K0, K1):
        bind(monkeypatch, e, key, platform='desktop')
        assert e.tool.identity().session_key == key
        assert s.cas_session(key, {'episode_id': ''}, episode_id=key, last_human_at=e.clock.now())
        assert 'on in this chat' in pulse(e, 'owntime on')
        e.own_time()
    root, = s.list_wakes(K0)
    child, = s.list_wakes(K1)
    if root_status == 'cancelled':
        assert s.cas_wake(root.id, 'active', status='cancelled', status_reason='chat_off')
    assert s.add_occurrence(child.id, 'own-time-due')
    lineage.child(K0, K1)

    e.reconcile()
    e.own_time()
    active, = s.list_wakes(K0, status='active')
    assert active.id == (root.id if root_status == 'active' else child.id)
    expected = 'superseded' if root_status == 'active' else 'pending'
    assert s.get_occurrence(child.id, 'own-time-due').state == expected


@pytest.mark.parametrize('override_key', [K0, K1], ids=['root-override', 'child-override'])
@pytest.mark.parametrize('override', ['', '16:00-17:00'], ids=['off', 'range'])
def test_fold_quiet_preserves_inherited_settings(hosts, lineage, monkeypatch, override_key, override):
    _, e = hosts
    e.policy = replace(e.policy, quiet_hours='11:00-13:00')
    for key in (K0, K1):
        bind(monkeypatch, e, key)
        assert e.tool.identity().session_key == key
    e.store.set_meta('quiet:' + override_key, json.dumps({'range': override}))
    lineage.child(K0, K1)

    e.reconcile()
    assert commands.quiet_until(e, K0) == e.clock.now() + 3600
    e.policy = replace(e.policy, quiet_hours='11:00-14:00')
    assert commands.quiet_until(e, K0) == e.clock.now() + 7200
    if override:
        assert commands.quiet_until(e, K0, now=e.clock.now() + 4 * 3600) == e.clock.now() + 5 * 3600


def test_interrupted_fold_rolls_back_and_retries(hosts, lineage, monkeypatch):
    _, e = hosts
    _, younger = split_store(e, lineage, monkeypatch)
    bind(monkeypatch, e, K2)
    youngest = arm(e)
    lineage.child(K1, K2)
    s = e.store
    s.connection.execute("""CREATE TEMP TRIGGER abort_fold BEFORE DELETE ON sessions
                         WHEN OLD.session_key = 'rotation-two'
                         BEGIN SELECT RAISE(ABORT, 'fixture interruption'); END""")
    before = snapshot(s)
    with pytest.raises(sqlite3.IntegrityError, match='fixture interruption'):
        e.reconcile()
    assert snapshot(s) == before
    s.connection.execute('DROP TRIGGER abort_fold')
    e.reconcile()
    assert s.get_wake(younger).session_key == s.get_wake(youngest).session_key == K0
    assert s.get_session(K1) is None and s.get_session(K2) is None


def test_stale_cas_fails_after_fold(hosts, lineage, monkeypatch):
    _, e = hosts
    _, younger = split_store(e, lineage, monkeypatch)
    old = e.store.get_wake(younger)
    e.reconcile()
    assert not e.store.cas_wake(old.id, old.status, expected_version=old.version, note='stale')
    assert e.store.get_wake(old.id).version == old.version + 1


def test_inline_fold_remaps_hooks_and_delivers(hosts, lineage, monkeypatch):
    _, e = hosts
    bind(monkeypatch, e, K0)
    assert e.tool.identity()
    bind(monkeypatch, e, K1)
    wid = arm(e, trigger={'kind': 'every', 'every': '10m'})
    e.clock.advance(e.store.get_wake(wid).next_due_at - e.clock.now() + 1)
    strict_inject(e, K1, monkeypatch)
    e.tick()
    assert [key for _, key in e.ctx.calls] == [K1]
    e.intake.pre_llm_call(session_id=K1, turn_id='first', user_message=e.ctx.calls[-1][0])
    flight = e.intake.flights[K1]
    assert flight.session_key == K1
    e.intake.inbounds.add(K1)

    lineage.child(K0, K1)
    assert e.tool.identity().session_key == K0
    assert e.intake.bindings[K1] == flight.session_key == K0
    assert e.intake.inbounds == {K0}
    assert e.store.get_wake(wid).session_key == K0
    e.intake.agent_loop_stopped(session_key=K1, platform='tui', invalidation_reason='stop_command')
    assert e.store.list_occurrences(wid)[-1].state == 'interrupted'
    e.clock.advance(e.store.get_wake(wid).next_due_at - e.clock.now() + 1)
    deliver(e, wid, K1, 'second', monkeypatch)


def test_startup_fold_racing_identity_does_not_recreate_split(hosts, lineage, monkeypatch):
    _, e = hosts
    split_store(e, lineage, monkeypatch)
    scanning, release = Event(), Event()
    get_session = lineage.db.get_session

    def read(db, key):
        if key == K1:
            scanning.set()
            assert release.wait(4)
        return get_session(db, key)

    monkeypatch.setattr(lineage.db, 'get_session', read)
    with ThreadPoolExecutor(max_workers=2) as threads:
        folding = threads.submit(e.reconcile)
        try:
            assert scanning.wait(2)
            hook = threads.submit(e.tool.identity)
        finally:
            release.set()
        folding.result(timeout=4)
        assert hook.result(timeout=4).session_key == K0
    assert e.store.get_session(K1) is None
    assert e.store.get_meta('host:' + K1) is None


def test_bootstrap_folds_on_hook_engine(tmp_path, clock, lineage, monkeypatch):
    ctx = GatewayCtx(tmp_path, settings={'quiet_hours': ''})
    ctx._manager.has_gateway_message_injector = False
    ctx._manager.has_tui_message_injector = True
    stop = Event()

    def reconcile_once(runner):
        try:
            runner.reconcile()
        finally:
            stop.set()

    monkeypatch.setattr(Poller, 'tick', reconcile_once)
    with closing(Runtime(ctx, clock=clock)) as runtime:
        e = runtime.get_engine()
        _, younger = split_store(e, lineage, monkeypatch)
        e.intake.bindings[K1] = K1
        e.intake.inbounds.add(K1)

        runtime._bootstrap(stop)

        assert runtime._runner is not e
        assert e.store.get_wake(younger).session_key == K0
        assert e.intake.bindings[K1] == K0
        assert e.intake.inbounds == {K0}
        assert runtime._runner.chat_key == e.chat_key


@pytest.mark.parametrize('kind', ['branch', 'reset', 'delegate', 'stale-sibling'])
def test_noncontinuation_children_keep_their_own_chat(hosts, lineage, monkeypatch, kind):
    _, e = hosts
    bind(monkeypatch, e, K0)
    older = arm(e)
    if kind == 'stale-sibling':
        lineage.child(K0, K2)
    lineage.child(K0, K1, kind=kind)
    bind(monkeypatch, e, K1)
    younger = arm(e, note='A separate conversation.')
    assert e.tool.identity().session_key == K1
    assert e.store.get_wake(older).session_key == K0
    assert e.store.get_wake(younger).session_key == K1
    assert e.store.get_meta('host:' + K1) == 'backend'


def test_messaging_chat_never_resolves_folds_or_retargets(hosts, lineage, monkeypatch):
    e, _ = hosts
    e.ctx._manager.has_tui_message_injector = True
    monkeypatch.setattr(policy, 'local_injection_supported', lambda: True)
    bind(monkeypatch, e, K0, platform='desktop')
    assert e.tool.identity().platform == 'desktop'
    bind(monkeypatch, e, K1, platform='telegram', source='telegram')
    wid = arm(e)
    lineage.child(K0, K1)
    deliver(e, wid, K1, 'wake', monkeypatch, surface='telegram', advance=61)
    assert e.tool.identity().session_key == K1
    assert e.store.get_wake(wid).session_key == K1
    assert K1 not in lineage.reads


def test_in_place_compaction_delivers_same_key(hosts, lineage, monkeypatch):
    _, e = hosts
    bind(monkeypatch, e, K0)
    wid = arm(e)
    assert e.tool.identity().session_key == K0
    lineage.reads.clear()
    assert e.tool.identity().session_key == K0
    assert not lineage.reads
    deliver(e, wid, K0, 'wake', monkeypatch, advance=61)
    assert e.store.get_wake(wid).fires == 1


def test_lineage_read_error_is_not_memoized(hosts, lineage, monkeypatch):
    _, e = hosts
    bind(monkeypatch, e, K0)
    wid = arm(e)
    lineage.child(K0, K1)
    bind(monkeypatch, e, K1)
    with monkeypatch.context() as broken:
        def unreadable(db, key):
            raise sqlite3.OperationalError('fixture read error')

        broken.setattr(lineage.db, 'get_session', unreadable)
        assert e.tool.identity().session_key == K1
        assert e.store.get_wake(wid).session_key == K0
    assert e.tool.identity().session_key == K0
    assert e.store.get_session(K1) is None


def test_unknown_desktop_key_is_memoized_until_state_changes(hosts, lineage, monkeypatch):
    _, e = hosts
    state = e.ctx._manager.home_path / 'state.db'
    state.touch()
    bind(monkeypatch, e, K0)
    assert e.tool.identity().session_key == K0
    bind(monkeypatch, e, K1)
    assert e.tool.identity().session_key == K1
    lineage.reads.clear()
    assert e.tool.identity().session_key == K1
    assert not lineage.reads

    lineage.child(K0, K1)
    state.with_name('state.db-wal').write_bytes(b'new lineage')
    assert e.tool.identity().session_key == K0


@pytest.mark.parametrize('platform', ['desktop', 'telegram'])
def test_unbound_end_does_not_open_write_transaction(hosts, platform):
    gateway, backend = hosts
    e = backend if platform == 'desktop' else gateway
    statements = []
    e.store.connection.set_trace_callback(statements.append)
    try:
        e.intake.on_session_end(session_id='unbound', turn_id='turn', platform=platform)
    finally:
        e.store.connection.set_trace_callback(None)
    assert not any(sql.startswith('BEGIN IMMEDIATE') for sql in statements)


def test_ack_probe_checks_live_queue_after_rotation(hosts, lineage, monkeypatch):
    _, e = hosts
    bind(monkeypatch, e, K0)
    wid = arm(e)
    strict_inject(e, K0, monkeypatch)
    e.clock.advance(61)
    e.tick()
    assert [key for _, key in e.ctx.calls] == [K0]
    lineage.child(K0, K1)
    strict_inject(e, K1, monkeypatch)
    probes = []

    def queued(key, marker):
        probes.append((key, marker))
        return True if key == K1 else None

    monkeypatch.setattr(tui_probe, 'queued_or_inflight', queued)
    e.clock.advance(31)
    e.tick()
    assert [key for key, _ in probes] == [K1]
    assert e.store.list_occurrences(wid)[-1].state == 'claimed'
    assert len(e.ctx.calls) == 1
