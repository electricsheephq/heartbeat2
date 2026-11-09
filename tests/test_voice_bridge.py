"""Live voice wakes through the v1 registration seam, exercised without another plugin or model calls."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from heartbeat2 import commands
from heartbeat2.model import Session, Wake
from heartbeat2.poller import Poller, RETRY_DELAYS
from heartbeat2.policy import Policy
from heartbeat2.store import Store
from heartbeat2.voice_bridge import VoiceBridgeRegistry
from test_engine import GatewayCtx

KEY = 'heartbeat2:voice:voice-root'


def make_bridge(store):
    """A device double. Its transport is four plain closures announced through the real registry, as a bridge does."""
    device = SimpleNamespace(connected=True, busy=False, owner_text=None, calls=[], raises=set(),
                             refuse_next=0, inject_error=False, in_transaction=[])

    def enter(name):
        device.in_transaction.append((name, store.connection.in_transaction))
        if name in device.raises: raise RuntimeError('bridge down')

    def voice_target(sid):
        enter('voice_target')
        return 'voice-root' if device.connected and sid in {'voice-root', 'rotated-id'} else None

    def owner_message(conversation_id, text):
        enter('owner_message')
        return conversation_id == 'voice-root' and text == device.owner_text

    def wake_ready(conversation_id):
        enter('wake_ready')
        return device.connected and conversation_id == 'voice-root' and not device.busy

    def inject_wake(conversation_id, text):
        enter('inject_wake')
        if device.inject_error: raise RuntimeError('transport broke')
        if device.refuse_next:
            device.refuse_next -= 1
            return False
        if not device.connected or device.busy: return False
        device.calls.append((conversation_id, text))
        return True

    registry = VoiceBridgeRegistry()
    registry.announce(version=1, registered_at=1, voice_target=voice_target, owner_message=owner_message,
                      wake_ready=wake_ready, inject_wake=inject_wake)
    device.registry = registry
    return device


@pytest.fixture
def voice_engine(tmp_path, clock, monkeypatch):
    from heartbeat2 import tool
    ctx = GatewayCtx(tmp_path)
    state = {'HERMES_SESSION_PLATFORM': 'api_server', 'HERMES_SESSION_ID': 'voice-root'}
    env = lambda name: state.get(name, '')
    monkeypatch.setattr(tool, 'session_env', env)
    monkeypatch.setattr(commands, 'session_env', env)
    store = Store(tmp_path / 'pulse.db', clock)
    engine = Poller(ctx, store, Policy(clock=clock, quiet_hours='', timezone='UTC'), clock,
                    session_key_fn=lambda: state.get('run', 'run-1'), paused=lambda: False,
                    compression_tip=lambda sid: sid, jitter=lambda: 0)
    bridge = make_bridge(store)
    engine.voice_bridge = bridge.registry.get
    yield engine, bridge, state
    engine.close()
    store.close()


def runtime_for(engine):
    return SimpleNamespace(get_engine=lambda: engine, home=engine.ctx._manager.home_path, clock=engine.clock)


def arm(engine, bridge, *, owner=True, sid='voice-root'):
    text = 'Remind me in five minutes.'
    bridge.owner_text = text if owner else None
    engine.intake.pre_llm_call(session_id=sid, turn_id='owner',
                              user_message=text, platform='api_server')
    result = engine.tool.handle(dict(action='create', note='The timer is ready.',
                                trigger={'kind': 'at', 'at': 'in 5m'}, owner_asked=True),
                                session_id=sid)
    engine.intake.on_session_end(session_id=sid, turn_id='owner', completed=True)
    return result


def due(engine, wake):
    engine.store.add_occurrence(wake.id, 't:test')
    return engine.store.get_occurrence(wake.id, 't:test')


def pending(engine, wake_id):
    """The one occurrence a real tick queued for this wake."""
    occurrences = engine.store.list_occurrences(wake_id)
    assert len(occurrences) == 1
    return occurrences[0]


def test_stable_identity_and_trusted_owner_evidence(voice_engine):
    e, bridge, state = voice_engine
    first = e.tool.identity()
    state.update(run='run-2', HERMES_SESSION_ID='rotated-id')
    assert e.tool.identity().session_key == first.session_key == KEY
    assert first.platform == 'api_server' and first.chat_type == 'dm'
    result = arm(e, bridge)
    assert result['armed'] and result['wake']['owner_requested']
    assert e.store.get_session(first.session_key).last_human_at == e.clock.now()


def test_api_text_alone_does_not_grant_owner_privileges(voice_engine):
    e, bridge, _ = voice_engine
    result = arm(e, bridge, owner=False)
    assert result['armed'] and not result['wake']['owner_requested']
    assert result['owner_asked_ignored'] == 'not_owner_turn'


def test_raising_owner_message_gives_no_credit_and_keeps_turn_bookkeeping(voice_engine):
    e, bridge, _ = voice_engine
    bridge.raises.add('owner_message')
    result = arm(e, bridge)
    assert result['armed'] and not result['wake']['owner_requested']
    assert result['owner_asked_ignored'] == 'not_owner_turn'
    # The hook ran to its end: the turn was bound and counted as running, and no human turn was credited.
    assert e.intake.bindings['voice-root'] == KEY and e.intake.turns['voice-root'] == 'owner'
    assert e.store.get_session(KEY).last_human_at is None
    bridge.raises.clear()
    e.intake.pre_llm_call(session_id='voice-root', turn_id='next', user_message='hello', platform='api_server')
    assert e.store.get_session(KEY).busy_since == e.clock.now()


@pytest.mark.parametrize('notify', [False, True])
def test_dispatch_and_transformed_silent_verdict(voice_engine, notify):
    e, bridge, _ = voice_engine
    result = arm(e, bridge)
    wake = e.store.get_wake(result['wake']['id'])
    e.clock.advance(301)
    e.dispatch(due(e, wake), cache_line='')
    assert len(bridge.calls) == 1 and bridge.calls[0][0] == 'voice-root'
    assert e.ctx.calls == []
    e.intake.pre_llm_call(session_id='voice-root', turn_id='wake',
                          user_message=bridge.calls[0][1], platform='api_server')
    assert not e.intake.flights['voice-root'].invalid
    assert e.tool.handle(dict(action='respond', outcome='done', notify=notify,
                             summary='The timer is ready.'), session_id='voice-root')['ok']
    e.intake.post_api_request(session_id='voice-root', turn_id='wake',
                             usage={'prompt_tokens': 10, 'completion_tokens': 5})
    final = e.intake.transform_llm_output(response_text='The timer is ready.',
                                         session_id='voice-root', turn_id='wake')
    if notify:
        assert final is None or final == 'The timer is ready.'
    else:
        assert final == '[SILENT]'
    e.intake.on_session_end(session_id='voice-root', turn_id='wake', completed=True)
    occurrence = e.store.get_occurrence(wake.id, 't:test')
    assert occurrence.state == 'done' and occurrence.delivered == notify


@pytest.mark.parametrize('tip', ['unrelated', 'none'])
def test_rotated_voice_id_without_compression_lineage_keeps_the_wake(voice_engine, tip):
    e, bridge, state = voice_engine
    # Hermes knows no lineage from the old id to the new one; only the bridge says they are one conversation.
    e.compression_tip = (lambda sid: sid) if tip == 'unrelated' else (lambda sid: None)
    wake = e.store.get_wake(arm(e, bridge)['wake']['id'])
    assert wake.armed_session_id == 'voice-root'
    state['HERMES_SESSION_ID'] = 'rotated-id'
    # An owner turn on the rotated id leaves the wake armed in its conversation.
    e.intake.pre_llm_call(session_id='rotated-id', turn_id='chat', user_message='hello', platform='api_server')
    e.intake.on_session_end(session_id='rotated-id', turn_id='chat', completed=True)
    wake = e.store.get_wake(wake.id)
    assert wake.status == 'active' and wake.session_key == KEY and wake.armed_session_id == 'rotated-id'
    # Rotate back before the wake fires: its turn arrives on an id the wake was not last armed in.
    state['HERMES_SESSION_ID'] = 'voice-root'
    e.clock.advance(301)
    e.dispatch(due(e, wake), cache_line='')
    assert len(bridge.calls) == 1
    e.intake.pre_llm_call(session_id='voice-root', turn_id='wake',
                          user_message=bridge.calls[0][1], platform='api_server')
    assert not e.intake.flights['voice-root'].invalid
    occurrence = e.store.get_occurrence(wake.id, 't:test')
    assert occurrence.state == 'started' and occurrence.error != 'stale_turn'
    wake = e.store.get_wake(wake.id)
    assert wake.status == 'active' and wake.armed_session_id == 'voice-root'
    assert e.store.list_sessions() == [e.store.get_session(KEY)]


@pytest.mark.parametrize('condition,reason', [('offline', 'voice_disconnected'), ('busy', 'busy'),
                                               ('consent', 'injection_disabled'), ('quiet', 'quiet_hours')])
def test_defers_without_injection(voice_engine, condition, reason):
    e, bridge, _ = voice_engine
    wake = e.store.get_wake(arm(e, bridge)['wake']['id'])
    e.clock.advance(301)
    if condition == 'offline': bridge.connected = False
    if condition == 'busy': bridge.busy = True
    if condition == 'consent': e.ctx.allowed = False
    if condition == 'quiet': e.policy = replace(e.policy, quiet_hours='00:00-23:59')
    e.dispatch(due(e, wake), cache_line='')
    occurrence = e.store.get_occurrence(wake.id, 't:test')
    assert occurrence.state == 'pending' and occurrence.error == reason
    assert bridge.calls == e.ctx.calls == []


@pytest.mark.parametrize('condition,reason', [('offline', 'voice_disconnected'), ('busy', 'busy')])
def test_a_real_tick_holds_the_wake_while_the_device_cannot_take_it(voice_engine, condition, reason):
    e, bridge, _ = voice_engine
    wake = e.store.get_wake(arm(e, bridge)['wake']['id'])
    e.clock.advance(301)
    if condition == 'offline': bridge.connected = False
    if condition == 'busy': bridge.busy = True
    e.tick()
    occurrence = pending(e, wake.id)
    assert occurrence.state == 'pending' and occurrence.error == reason and occurrence.attempt == 0
    assert 'inject_wake' not in {name for name, _ in bridge.in_transaction}
    bridge.connected, bridge.busy = True, False
    e.tick()
    assert len(bridge.calls) == 1 and pending(e, wake.id).state == 'claimed'
    assert e.ctx.calls == []


@pytest.mark.parametrize('change', ['unattached', 'unregistered', 'withdrawn', 'failing_bridge', 'cli'])
def test_ordinary_api_and_other_sources_remain_refused(voice_engine, change):
    e, bridge, state = voice_engine
    if change == 'unattached': state['HERMES_SESSION_ID'] = 'unknown'
    if change == 'unregistered': e.voice_bridge = VoiceBridgeRegistry().get
    if change == 'withdrawn': bridge.registry.announce(version=1, registered_at=1, withdrawn=True)
    if change == 'failing_bridge': bridge.raises.add('voice_target')
    if change == 'cli': state['HERMES_SESSION_PLATFORM'] = 'cli'
    result = e.tool.handle(dict(action='create', note='Check.', trigger={'kind': 'at', 'at': 'in 5m'}))
    assert result['error'] == 'non_gateway'
    assert not bridge.calls
    assert e.store.get_session(KEY) is None


def test_bridge_lost_after_arming_holds_the_wake_until_it_returns(voice_engine):
    e, bridge, _ = voice_engine
    wake = e.store.get_wake(arm(e, bridge)['wake']['id'])
    e.clock.advance(301)
    bridge.registry.announce(version=1, registered_at=1, withdrawn=True)
    e.dispatch(due(e, wake), cache_line='')
    occurrence = e.store.get_occurrence(wake.id, 't:test')
    assert occurrence.state == 'pending' and occurrence.error == 'voice_disconnected'
    assert bridge.calls == e.ctx.calls == []


def test_consent_still_required_at_creation(voice_engine):
    e, bridge, _ = voice_engine
    e.ctx.allowed = False
    assert arm(e, bridge)['error'] == 'injection_disabled'


def test_busy_refusal_at_injection_defers_and_is_then_delivered(voice_engine):
    e, bridge, _ = voice_engine
    wake = e.store.get_wake(arm(e, bridge)['wake']['id'])
    e.clock.advance(301)
    # The device reads idle when the tick asks and turns busy before the wake lands, more often than the retry limit allows.
    bridge.refuse_next = 6
    for _ in range(6):
        e.tick()
        occurrence = pending(e, wake.id)
        assert occurrence.state == 'pending' and occurrence.error == 'busy'
        assert not e.store.get_meta('inject_failures:' + occurrence.id)
        e.clock.advance(RETRY_DELAYS[0] + 1)
    assert bridge.calls == [] and bridge.refuse_next == 0 and e.store.get_wake(wake.id).status == 'active'
    e.tick()
    assert len(bridge.calls) == 1 and bridge.calls[0][0] == 'voice-root'
    assert pending(e, wake.id).state == 'claimed'
    assert e.ctx.calls == []


def test_busy_refusal_still_ends_at_the_wakes_expiry(voice_engine):
    e, bridge, _ = voice_engine
    wake = e.store.get_wake(arm(e, bridge, owner=False)['wake']['id'])
    e.clock.advance(301)
    bridge.refuse_next = 1
    e.dispatch(due(e, wake), cache_line='')
    assert e.store.get_occurrence(wake.id, 't:test').error == 'busy'
    e.clock.advance(15*86400)
    e.dispatch(e.store.get_occurrence(wake.id, 't:test'), cache_line='')
    occurrence = e.store.get_occurrence(wake.id, 't:test')
    assert occurrence.state == 'skipped' and occurrence.error == 'expired'
    assert bridge.calls == []


def test_transport_error_uses_existing_retry_path(voice_engine):
    e, bridge, _ = voice_engine
    wake = e.store.get_wake(arm(e, bridge)['wake']['id'])
    bridge.inject_error = True
    e.clock.advance(301)
    e.dispatch(due(e, wake), cache_line='')
    occurrence = e.store.get_occurrence(wake.id, 't:test')
    assert occurrence.state == 'pending' and occurrence.error == 'inject_failed'
    assert e.store.get_meta('inject_failures:' + occurrence.id) == '1'
    assert e.ctx.calls == []


def test_disconnect_blocks_resume_but_allows_cancel(voice_engine):
    e, bridge, _ = voice_engine
    result = arm(e, bridge)
    wid = result['wake']['id']
    assert e.tool.handle({'action': 'pause', 'id': wid})['ok']
    bridge.connected = False
    # Restore the stable key a persisted gateway turn would carry after disconnect.
    e.tool.session_key_fn = lambda: result['wake']['session_key']
    assert e.tool.handle({'action': 'resume', 'id': wid})['error'] == 'non_gateway'
    assert e.tool.handle({'action': 'cancel', 'id': wid})['ok']


def test_forged_api_session_is_not_dispatched(voice_engine):
    e, bridge, _ = voice_engine
    key = 'ordinary-api'
    e.store.upsert_session(Session(key, 'api_server', 'dm', 'voice-root', 'voice-root'))
    wake = e.store.create_wake(Wake(key, 'Check.', 'at', platform='api_server', chat_type='dm',
        chat_id='voice-root', next_due_at=e.clock.now(), expires_at=e.clock.now()+3600))
    e.dispatch(due(e, wake), cache_line='')
    assert e.store.get_occurrence(wake.id, 't:test').error == 'non_gateway'
    assert not bridge.calls


def test_connected_voice_can_update_and_resume(voice_engine):
    e, bridge, _ = voice_engine
    wid = arm(e, bridge)['wake']['id']
    assert e.tool.handle({'action': 'pause', 'id': wid})['ok']
    assert e.tool.handle({'action': 'resume', 'id': wid})['ok']
    assert e.tool.handle({'action': 'update', 'id': wid, 'note': 'Updated reminder.'})['ok']
    assert e.store.get_wake(wid).note == 'Updated reminder.'


def test_voice_cannot_mutate_another_destination(voice_engine):
    e, bridge, _ = voice_engine
    e.tool.identity()
    other = e.store.create_wake(Wake('other-session', 'Private reminder.', 'at',
        platform='api_server', chat_type='dm', chat_id='other-voice'))
    result = e.tool.handle({'action': 'cancel', 'id': other.id})
    assert result['error'] and e.store.get_wake(other.id).status == 'active'


@pytest.mark.parametrize('condition,reason', [('expired', 'expired'), ('cap', 'daily_model_cap')])
def test_expiry_and_model_budget_still_apply(voice_engine, condition, reason):
    e, bridge, _ = voice_engine
    wake = e.store.get_wake(arm(e, bridge, owner=False)['wake']['id'])
    e.clock.advance(301)
    if condition == 'expired':
        e.clock.advance(15*86400)
    else:
        e.policy = replace(e.policy, max_model_wakes_per_day=0)
    e.dispatch(due(e, wake), cache_line='')
    occurrence = e.store.get_occurrence(wake.id, 't:test')
    assert occurrence.error == reason
    assert bridge.calls == e.ctx.calls == []


def test_pulse_off_and_on_work_from_the_voice_conversation(voice_engine):
    e, bridge, state = voice_engine
    wake = e.store.get_wake(arm(e, bridge)['wake']['id'])
    runtime = runtime_for(e)
    # The bridge runs the command in the conversation's session scope; the run id underneath may have rotated.
    state.update(run='run-9', HERMES_SESSION_ID='rotated-id')
    assert not commands.handle('off', runtime).startswith('Error')
    assert not e.store.get_session(KEY).enabled
    e.clock.advance(301)
    e.dispatch(due(e, wake), cache_line='')
    assert e.store.get_occurrence(wake.id, 't:test').error == 'session_off' and bridge.calls == []
    assert e.tool.handle(dict(action='create', note='Check.', trigger={'kind': 'at', 'at': 'in 5m'}))['error'] == 'session_off'
    assert not commands.handle('on', runtime).startswith('Error')
    assert e.store.get_session(KEY).enabled
    assert e.store.list_sessions() == [e.store.get_session(KEY)]


def test_pulse_quiet_holds_voice_wakes(voice_engine):
    e, bridge, _ = voice_engine
    wake = e.store.get_wake(arm(e, bridge, owner=False)['wake']['id'])
    assert not commands.handle('quiet for 2h', runtime_for(e)).startswith('Error')
    e.clock.advance(301)
    e.dispatch(due(e, wake), cache_line='')
    occurrence = e.store.get_occurrence(wake.id, 't:test')
    assert occurrence.state == 'pending' and occurrence.error == 'quiet_hours'
    assert bridge.calls == []


def test_pulse_ok_approves_a_proposed_wake_by_voice(voice_engine):
    e, bridge, _ = voice_engine
    e.policy = replace(e.policy, agent_can_create='ask_first')
    result = arm(e, bridge, owner=False)
    wid = result['wake']['id']
    assert not result['armed'] and e.store.get_wake(wid).status == 'proposed'
    runtime = runtime_for(e)
    assert wid in commands.handle('list', runtime)
    assert not commands.handle('ok ' + wid, runtime).startswith('Error')
    assert e.store.get_wake(wid).status == 'active'
    assert commands.handle('status', runtime).startswith('HeartBeat 2.0: on')
    assert not commands.handle('cancel ' + wid, runtime).startswith('Error')
    assert e.store.get_wake(wid).status == 'cancelled'
    for unsupported in ('warm on', 'owntime on'):
        assert 'not available in voice' in commands.handle(unsupported, runtime)
    assert [w.id for w in e.store.list_wakes()] == [wid]


@pytest.mark.parametrize('change', ['unattached', 'unregistered', 'failing_bridge'])
def test_pulse_command_outside_a_voice_conversation_stays_refused(voice_engine, change):
    e, bridge, state = voice_engine
    wid = arm(e, bridge)['wake']['id']
    state['HERMES_SESSION_KEY'] = 'api-run-key'
    if change == 'unattached': state['HERMES_SESSION_ID'] = 'unknown'
    if change == 'unregistered': e.voice_bridge = VoiceBridgeRegistry().get
    if change == 'failing_bridge': bridge.raises.add('voice_target')
    out = commands.handle('off', runtime_for(e))
    assert 'API-server' in out
    assert e.store.get_session(KEY).enabled and e.store.get_wake(wid).status == 'active'
    assert 'API-server' in commands.handle('cancel ' + wid, runtime_for(e))
    assert e.store.get_wake(wid).status == 'active'


def test_bridge_questions_are_never_asked_inside_a_store_transaction(voice_engine):
    e, bridge, _ = voice_engine
    wid = arm(e, bridge)['wake']['id']
    runtime = runtime_for(e)
    assert e.tool.handle({'action': 'pause', 'id': wid})['ok']
    assert e.tool.handle({'action': 'resume', 'id': wid})['ok']
    assert e.tool.handle({'action': 'update', 'id': wid, 'note': 'Updated reminder.'})['ok']
    commands.handle('status', runtime)
    assert commands.handle('test ' + wid, runtime).startswith('Test ')
    assert not commands.handle('quiet for 1m', runtime).startswith('Error')
    assert not commands.handle('quiet default', runtime).startswith('Error')
    e.clock.advance(301)
    e.tick()
    assert len(bridge.calls) == 1
    names = {name for name, _ in bridge.in_transaction}
    assert names == {'voice_target', 'owner_message', 'wake_ready', 'inject_wake'}
    # Only the injection itself runs under the dispatch transaction, as the gateway injector does.
    assert {name for name, held in bridge.in_transaction if held} == {'inject_wake'}
