"""Live API transport contract, exercised without another plugin or model calls."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from heartbeat2.model import Session, Wake
from heartbeat2.poller import Poller
from heartbeat2.policy import Policy
from heartbeat2.store import Store
from test_engine import GatewayCtx


class VoiceBridge:
    """Trusted transport seam; connection lifecycle belongs to the voice plugin."""
    def __init__(self):
        self.connected = True
        self.busy = False
        self.accept = True
        self.owner_text = None
        self.calls = []

    def voice_target(self, sid):
        return 'voice-root' if self.connected and sid in {'voice-root', 'rotated-id'} else None

    def owner_message(self, conversation_id, text):
        return conversation_id == 'voice-root' and text == self.owner_text

    def wake_ready(self, conversation_id):
        return self.voice_target(conversation_id) is not None and not self.busy

    def inject_wake(self, conversation_id, text):
        if not self.accept or not self.wake_ready(conversation_id):
            return False
        self.calls.append((conversation_id, text))
        return True


@pytest.fixture
def voice_engine(tmp_path, clock, monkeypatch):
    from heartbeat2 import tool
    ctx = GatewayCtx(tmp_path)
    bridge = VoiceBridge()
    ctx._manager._heartbeat2_voice_hub = bridge
    state = {'HERMES_SESSION_PLATFORM': 'api_server', 'HERMES_SESSION_ID': 'voice-root'}
    monkeypatch.setattr(tool, 'session_env', lambda name: state.get(name, ''))
    store = Store(tmp_path / 'pulse.db', clock)
    engine = Poller(ctx, store, Policy(clock=clock, quiet_hours='', timezone='UTC'), clock,
                    session_key_fn=lambda: state.get('run', 'run-1'), paused=lambda: False,
                    compression_tip=lambda sid: sid, jitter=lambda: 0)
    yield engine, bridge, state
    engine.close()
    store.close()


def arm(engine, bridge, *, owner=True):
    text = 'Remind me in five minutes.'
    bridge.owner_text = text if owner else None
    engine.intake.pre_llm_call(session_id='voice-root', turn_id='owner',
                              user_message=text, platform='api_server')
    result = engine.tool.handle(dict(action='create', note='The timer is ready.',
                                trigger={'kind': 'at', 'at': 'in 5m'}, owner_asked=True),
                                session_id='voice-root')
    engine.intake.on_session_end(session_id='voice-root', turn_id='owner', completed=True)
    return result


def due(engine, wake):
    engine.store.add_occurrence(wake.id, 't:test')
    return engine.store.get_occurrence(wake.id, 't:test')


def test_stable_identity_and_trusted_owner_evidence(voice_engine):
    e, bridge, state = voice_engine
    first = e.tool.identity()
    state.update(run='run-2', HERMES_SESSION_ID='rotated-id')
    assert e.tool.identity().session_key == first.session_key == 'heartbeat2:voice:voice-root'
    assert first.platform == 'api_server' and first.chat_type == 'dm'
    result = arm(e, bridge)
    assert result['armed'] and result['wake']['owner_requested']
    assert e.store.get_session(first.session_key).last_human_at == e.clock.now()


def test_api_text_alone_does_not_grant_owner_privileges(voice_engine):
    e, bridge, _ = voice_engine
    result = arm(e, bridge, owner=False)
    assert result['armed'] and not result['wake']['owner_requested']
    assert result['owner_asked_ignored'] == 'not_owner_turn'


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


@pytest.mark.parametrize('change', ['unattached', 'other_profile', 'incomplete_bridge', 'cli'])
def test_ordinary_api_and_other_sources_remain_refused(voice_engine, change):
    e, bridge, state = voice_engine
    if change == 'unattached': state['HERMES_SESSION_ID'] = 'unknown'
    if change == 'other_profile': del e.ctx._manager._heartbeat2_voice_hub
    if change == 'incomplete_bridge': e.ctx._manager._heartbeat2_voice_hub = SimpleNamespace()
    if change == 'cli': state['HERMES_SESSION_PLATFORM'] = 'cli'
    result = e.tool.handle(dict(action='create', note='Check.', trigger={'kind': 'at', 'at': 'in 5m'}))
    assert result['error'] == 'non_gateway'
    assert not bridge.calls


def test_consent_still_required_at_creation(voice_engine):
    e, bridge, _ = voice_engine
    e.ctx.allowed = False
    assert arm(e, bridge)['error'] == 'injection_disabled'


def test_transport_refusal_uses_existing_retry_path(voice_engine):
    e, bridge, _ = voice_engine
    wake = e.store.get_wake(arm(e, bridge)['wake']['id'])
    bridge.accept = False
    e.clock.advance(301)
    e.dispatch(due(e, wake), cache_line='')
    occurrence = e.store.get_occurrence(wake.id, 't:test')
    assert occurrence.state == 'pending' and occurrence.error == 'inject_failed'
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
