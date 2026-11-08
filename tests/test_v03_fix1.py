"""FIX1 regressions through the fake gateway; no network or live profile."""
from dataclasses import replace
from datetime import datetime
import hashlib
from types import SimpleNamespace

import pytest

from heartbeat2 import cache, commands, governor
from test_engine import KEY, arm, end, engine, pending, session, start


def usage(e, monkeypatch, dollars=1):
    monkeypatch.setattr(cache, 'cost', lambda *a, **kw: dollars)
    e.intake.post_api_request(session_id='sid', turn_id='turn', usage={'prompt_tokens': 10})


def total(e):
    return e.store.agent_cost_today(KEY, e.policy.resolved_timezone())


def own(e, hours=1):
    e.policy = replace(e.policy, own_time_hours=hours)
    e.own_time()
    return next(w for w in e.store.list_wakes(KEY) if w.trigger_json.get('own_time'))


def command(e, monkeypatch, text):
    monkeypatch.setattr(commands, 'session_env', lambda n: KEY if n == 'HERMES_SESSION_KEY' else '')
    return commands.handle(text, SimpleNamespace(get_engine=lambda: e))


def create(e, **args):
    return e.tool.handle({'action': 'create', 'note': 'Own this work.',
                          'trigger': {'kind': 'self'}, 'role': True, **args}, session_id='sid')


def test_f1_replay_agent_spend_and_owner_exemption(engine, monkeypatch):
    e = engine
    # Exercise the actual recovery-note replay recognizer, including its class marker.
    for creator in ('user', 'agent'):
        w = arm(e, 'every', creator=creator)
        e.dispatch(pending(e, w))
        text = e.ctx.calls[-1][0]
        start(e)
        end(e, 'progress', False)
        e.intake.pre_llm_call(session_id='sid', turn_id='turn', platform='telegram',
            user_message='[System note: The previous turn was interrupted by a gateway restart; the gateway is now back online. Any restart/shutdown command in the history has already run — do NOT re-execute or verify it. Report to the user that the session was restored successfully and ask what they would like to do next. Do NOT re-execute old tool calls — skip any unfinished work from the conversation history.]',
            conversation_history=[{'role': 'user', 'content': text},
                {'role': 'user', 'content': '[System note: The previous turn was interrupted by a gateway restart; the gateway is now back online. Any restart/shutdown command in the history has already run — do NOT re-execute or verify it. Report to the user that the session was restored successfully and ask what they would like to do next. Do NOT re-execute old tool calls — skip any unfinished work from the conversation history.]',
                 'display_kind': 'internal_notification'}])
        assert e.intake.flights['sid'].replay
        usage(e, monkeypatch)
        assert total(e)['cost_microusd'] == (0 if creator == 'user' else 1000000)
        e.intake.on_session_end(session_id='sid', turn_id='turn')
        e.clock.advance(601)
    e.policy = replace(e.policy, daily_cost_limit_usd=1)
    e.dispatch(pending(e, w, 'over'))
    assert e.store.get_occurrence(w.id, 'over').error == 'daily_cost_cap'


def test_f1_dispatch_class_survives_agent_edit(engine, monkeypatch):
    e = engine
    w = arm(e, 'every', creator='user')
    e.dispatch(pending(e, w))
    start(e)
    usage(e, monkeypatch)
    assert total(e)['cost_microusd'] == 0
    end(e, 'progress', False)
    e.clock.advance(601)
    # The agent edits the owner's wake in a human turn (no flight); owner_item still blocks wake turns.
    assert 'sid' not in e.intake.flights
    result = e.tool.handle({'action': 'update', 'id': w.id, 'note': 'Agent now owns this.'}, session_id='sid')
    assert result.get('ok'), result
    assert e.store.get_wake(w.id).agent_edited
    assert total(e)['cost_microusd'] == 0
    e.clock.advance(601)
    e.dispatch(pending(e, e.store.get_wake(w.id), 'agent-turn'))
    start(e)
    usage(e, monkeypatch)
    assert total(e)['cost_microusd'] == 1000000
    assert e.store.get_meta('spend:'+w.id+'/t:test') == 'owner'
    assert e.store.get_meta('spend:'+w.id+'/agent-turn') == 'agent'


def test_f1_gc_cannot_restore_daily_budget(engine, monkeypatch):
    e = engine
    e.policy = replace(e.policy, daily_cost_limit_usd=1)
    w = arm(e, 'every')
    e.dispatch(pending(e, w))
    start(e)
    usage(e, monkeypatch)
    end(e, 'progress', False)
    for n in range(205):
        e.clock.advance(1)
        e.dispatch(pending(e, e.store.get_wake(w.id), f'cap:{n}'))
        assert e.store.get_occurrence(w.id, f'cap:{n}').error == 'daily_cost_cap'
    e.store.set_meta('agent_spend:'+KEY+':2000-01-01', '{"cost_microusd":9,"unpriced_calls":0}')
    e.store.gc(ledger_days=30, timezone=e.policy.resolved_timezone())
    assert total(e)['cost_microusd'] == 1000000
    assert e.store.get_occurrence(w.id, 't:test') is None
    assert e.store.get_meta('spend:'+w.id+'/t:test') is None
    assert e.store.get_meta('agent_spend:'+KEY+':2000-01-01') is None
    e.dispatch(pending(e, e.store.get_wake(w.id), 'after-gc'))
    assert e.store.get_occurrence(w.id, 'after-gc').error == 'daily_cost_cap'


def test_f1_usage_arrival_day_and_legacy_class_fallback(engine, monkeypatch):
    e = engine
    e.policy = replace(e.policy, timezone='Asia/Tokyo')
    e.clock.wall = datetime.fromisoformat('2025-10-12T14:59:00+00:00').timestamp()
    w = arm(e, 'every')
    e.dispatch(pending(e, w))
    start(e)
    # No marker models an occurrence claimed by the older plugin.
    e.store.connection.execute('DELETE FROM meta WHERE key=?', ('spend:'+w.id+'/t:test',))
    e.clock.advance(120)
    usage(e, monkeypatch, .25)
    e.intake.post_api_request(session_id='sid', turn_id='turn', usage={})
    assert total(e) == {'cost_microusd': 250000, 'unpriced_calls': 1}
    assert e.store.get_occurrence(w.id, 't:test').state == 'started'
    assert e.store.get_meta('agent_spend:'+KEY+':2025-10-12') is None
    # A read of a different chat cannot see this counter.
    assert e.store.agent_cost_today('another-chat', e.policy.resolved_timezone())['cost_microusd'] == 0


def test_f2_role_expiry_create_and_old_role_update(engine):
    e = engine
    result = create(e, until={'expires': 'in 30d'})
    assert result.get('ok'), result
    wid = result['wake']['id']
    assert result['wake']['expires_at'] == e.clock.now()+30*86400
    e.clock.advance(15*86400)
    result = e.tool.handle({'action': 'update', 'id': wid, 'until': {'expires': 'in 10d'}}, session_id='sid')
    assert result.get('ok'), result
    assert result['wake']['expires_at'] == e.clock.now()+10*86400
    result = create(e, until={'expires': 'in 400d'})
    assert result['wake']['expires_at'] == e.clock.now()+365*86400
    assert result['clamped']['expires'].endswith('(max_reminder)')
    result = create(e, role=False, trigger={'kind': 'every', 'every': '1h'}, until={'expires': 'in 30d'})
    assert result['wake']['expires_at'] == e.clock.now()+14*86400


@pytest.mark.parametrize('mode', ['floor', 'cache'])
def test_f3_own_time_keeps_firing_over_twelve_hours(engine, monkeypatch, mode):
    e = engine
    if mode == 'floor':
        e.policy = replace(e.policy, agent_ungated_floor_minutes=120)
    else:
        monkeypatch.setattr(cache, 'ttl_for_session', lambda *a: (3600, 'fixture'))
    w = own(e)
    if mode == 'cache':
        result = e.tool.handle({'action': 'update', 'id': w.id, 'note': 'Think about the work.'}, session_id='sid')
        assert result.get('ok'), result
        assert result['wake']['agent_edited']
    fires = []
    for _ in range(12*12+1):
        e.tick()
        if len(e.ctx.calls) > len(fires):
            fires.append(e.clock.now())
            start(e)
            end(e, 'progress', False)
        e.clock.advance(300)
    assert len(fires) >= (5 if mode == 'floor' else 10)
    if mode == 'floor':
        assert all(7200 <= b-a <= 7500 for a, b in zip(fires, fires[1:]))


def test_f3_slow_setting_change_and_legacy_pacing(engine, monkeypatch):
    e = engine
    w = own(e, 6)
    governor.pause(e, w, 'noop')
    assert not command(e, monkeypatch, 'slow '+w.id).startswith('Error:')
    slowed = e.store.get_wake(w.id)
    e.clock.advance(60)
    e.tick()
    assert e.store.get_wake(w.id).eff_interval_s == slowed.eff_interval_s == 43200
    assert e.store.get_wake(w.id).next_due_at == slowed.next_due_at
    changed = own(e, 12)
    assert changed.next_due_at == e.clock.now()+43200
    e.clock.advance(60)
    e.tick()
    assert e.store.get_wake(w.id).next_due_at == changed.next_due_at
    e.store.cas_wake(w.id, 'active', trigger_json={'builtin': True, 'own_time': True})
    upgraded = own(e, 12)
    assert upgraded.trigger_json['hours'] == 12
    e.clock.advance(60)
    e.own_time()
    assert e.store.get_wake(w.id).next_due_at == upgraded.next_due_at


def test_f3_agent_cannot_cancel_pause_and_owner_toggle_rearms(engine, monkeypatch):
    e = engine
    w = own(e)
    for in_flight in (False, True):
        if in_flight:
            e.dispatch(pending(e, w))
            start(e)
        for action in ('cancel', 'pause'):
            result = e.tool.handle({'action': action, 'id': w.id}, session_id='sid')
            assert result == {'error': 'builtin_settings_owned', 'detail': 'Own time is set in Settings (own_time_hours).'}
        if in_flight: end(e, 'progress', False)
    assert not command(e, monkeypatch, 'cancel '+w.id).startswith('Error:')
    e.tick()
    assert e.store.get_wake(w.id).status == 'cancelled'
    own(e, 0)
    assert e.store.get_wake(w.id).status_reason == 'settings'
    assert own(e, 1).status == 'active'


def test_f4_sha256_own_time_and_unchanged_checkin_id(engine):
    e = engine
    assert own(e).id == 'owntime_'+hashlib.sha256(KEY.encode()).hexdigest()[:16]
    e.policy = replace(e.policy, owner_silent_checkin=True)
    e.silence()
    assert e.store.get_wake('checkin_'+hashlib.sha1(KEY.encode()).hexdigest()[:16]) is not None


def test_f5_recurring_receipt_matches_message_holds(engine):
    e = engine
    e.policy = replace(e.policy, quiet_hours='00:00-23:59')
    session(e, unanswered_count=5)
    result = create(e, role=False, trigger={'kind': 'every', 'every': '1h'})
    assert result.get('ok'), result
    receipt = result['receipt']
    assert 'hold its messages' in receipt and 'messages are held; work still runs' in receipt
    assert 'skip its ticks' not in receipt and 'dropped unless you reply' not in receipt
    w = arm(e, 'every', creator='user', next_due_at=e.clock.now()+3600)
    receipt = e.tool.receipt(w, e.store.get_session(KEY))
    assert 'Quiet hours skip its ticks until' in receipt and 'messages are held' not in receipt


def test_f6_governor_cards_only_offer_valid_conversions(engine):
    e = engine
    role = arm(e, 'self', role=True)
    ordinary = arm(e, 'every')
    owntime = own(e)
    for w in (role, ordinary, owntime): governor.pause(e, w, 'noop')
    e.store.set_meta('human_context:'+KEY, '1')
    result = e.intake.pre_llm_call(session_id='sid', turn_id='human', user_message='Hello', platform='telegram')
    lines = result['context'].splitlines()
    for w in (role, owntime):
        line = next(x for x in lines if f'({w.id}) paused' in x)
        assert line.endswith('keep / slow / stop via /pulse.') and '/pulse watch' not in line
    assert any('/pulse watch '+ordinary.id in x for x in lines)


def test_f7_new_role_cannot_escape_creating_budget(engine):
    e = engine
    parent = arm(e, 'self', role=True, budget_turns_per_day=2)
    e.dispatch(pending(e, parent))
    start(e)
    for args in ({'budget': {'turns_per_day': 12}}, {}):
        result = create(e, **args)
        assert result.get('ok'), result
        assert result['wake']['budget_turns_per_day'] == 2
        assert result['clamped']['budget.turns_per_day'].endswith('(wake_turn cannot raise budget)')
    end(e, 'progress', False)
    e.clock.advance(601)
    w = own(e)
    e.dispatch(pending(e, w))
    start(e)
    result = create(e, budget={'turns_per_day': 12})
    assert result['wake']['budget_turns_per_day'] == 6
    end(e, 'progress', False)
    assert create(e, budget={'turns_per_day': 12})['wake']['budget_turns_per_day'] == 12


def test_f8_self_role_spreads_budget_by_default(engine, monkeypatch):
    e = engine
    out = command(e, monkeypatch, 'role self Keep the release notes current')
    assert not out.startswith('Error:'), out
    assert e.store.get_wake(out.split()[0]).eff_interval_s == 14400
    result = create(e, budget={'turns_per_day': 12})
    assert result['wake']['base_interval_s'] == result['wake']['eff_interval_s'] == 7200
    result = create(e, role=False)
    assert result['wake']['eff_interval_s'] == e.policy.agent_ungated_floor_minutes*60


def test_f9_embedded_desk_is_bounded_after_escaping(engine):
    e = engine
    for _ in range(10): arm(e, 'self', role=True, title='&<>'*26)
    w = e.store.list_wakes(KEY)[0]
    e.dispatch(pending(e, w))
    text = e.ctx.calls[-1][0]
    desk = text[text.index('Your desk:'):text.index('\nRecent outcomes:')]
    assert len(desk) <= 1800
    assert desk.splitlines()[-1].startswith('… ') and desk.endswith(' more')
    assert '&amp;' in desk and '&lt;' in desk and '&gt;' in desk


def test_f10_role_cannot_be_babysit(engine):
    e = engine
    assert create(e, babysit=True) == {'error': 'role_not_babysit'}
    result = create(e)
    assert e.tool.handle({'action': 'update', 'id': result['wake']['id'], 'babysit': True}, session_id='sid') == {'error': 'role_not_babysit'}
    result = create(e, role=False, babysit=True, trigger={'kind': 'self'})
    assert result.get('ok'), result
    assert result['wake']['expires_at'] <= e.clock.now()+4*3600
    assert result['wake']['max_fires'] == 24
