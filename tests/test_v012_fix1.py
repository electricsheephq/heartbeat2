"""Fix-round regressions: fake gateway, synthetic usage, no network/model."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from heartbeat2 import cache, commands, governor
from heartbeat2.schedule import local_day
from test_engine import engine, arm, pending, session, start, end, admit, KEY
from test_v012 import hermes_helpers


@pytest.mark.parametrize('edit', [
    {'trigger': {'kind': 'at', 'at': 'in 1m'}},
    {'note': 'Agent chosen text'},
    {'pacing': {'min': '2h'}},
])
def test_R1_agent_owner_edit_revokes_owner_privilege(engine, edit):
    engine.policy = replace(engine.policy, max_model_wakes_per_day=0, max_messages_per_day=0)
    w = arm(engine, 'every', creator='user', trigger_json={'kind': 'every', 'every': '1d'})
    result = engine.tool.handle({'action': 'update', 'id': w.id, **edit}, session_id='sid')
    assert result['ok']
    changed = engine.store.get_wake(w.id)
    assert getattr(changed, 'agent_edited', False) is True
    engine.clock.advance(61)
    engine.dispatch(pending(engine, changed, 'e:due'))
    assert not engine.ctx.calls
    assert engine.store.list_occurrences(w.id)[0].error == 'daily_model_cap'


def test_R1_builtin_repro_is_settings_owned_and_capped(engine):
    engine.policy = replace(engine.policy, owner_silent_checkin=True,
                            max_model_wakes_per_day=0, max_messages_per_day=0)
    engine.tick()
    w, = engine.store.list_wakes(KEY)
    result = engine.tool.handle({'action': 'update', 'id': w.id,
                                'trigger': {'kind': 'at', 'at': 'in 1m'}, 'note': 'New text'}, session_id='sid')
    assert result.get('error') == 'builtin_settings_owned'
    assert 'Settings' in result['detail']
    assert engine.store.get_wake(w.id).trigger_json['builtin'] is True
    engine.clock.advance(25*3600)
    engine.tick()
    assert not engine.ctx.calls


def test_R1_builtin_flag_survives_note_pause_resume(engine):
    engine.policy = replace(engine.policy, owner_silent_checkin=True)
    engine.tick()
    w, = engine.store.list_wakes(KEY)
    for action in ('update', 'pause', 'resume'):
        result = engine.tool.handle({'action': action, 'id': w.id,
                                     **({'note': 'Still a check-in'} if action == 'update' else {})})
        assert result['ok'] and result['wake']['trigger_json']['builtin'] is True
    assert result['wake'].get('agent_edited') is True


def test_R1_owner_command_edit_does_not_mark_agent_edited(engine, monkeypatch):
    monkeypatch.setattr(commands, 'session_env', lambda n: KEY if n == 'HERMES_SESSION_KEY' else '')
    runtime = SimpleNamespace(get_engine=lambda: engine)
    commands.handle('add every 1h My reminder', runtime)
    w, = engine.store.list_wakes(KEY)
    governor.pause(engine, w, 'noop')
    assert 'New cadence' in commands.handle('slow ' + w.id, runtime)
    assert getattr(engine.store.get_wake(w.id), 'agent_edited', None) is False


@pytest.mark.parametrize('kind', ['url', 'subagent_done'])
def test_final_rule_owner_watcher_bypasses_mute_but_holds_caps(engine, kind):
    w = arm(engine, kind, creator='user')
    session(engine, unanswered_count=2)
    assert engine.policy.unanswered_gate(w, 2) is None
    engine.policy = replace(engine.policy, max_messages_per_day=0)
    assert engine.gate(w, pending(engine, w, 'e:match'), engine.store.get_session(KEY)) == ('held_daily_cap', True, None)


def test_R2_url_held_match_survives_five_errors_pause_resume(engine):
    session(engine, unanswered_count=2)
    w = arm(engine, 'url', trigger_json={'check_every': 60}, probe_state_json={'fingerprint': 'v1'})
    engine.probe_runner = lambda _: {'fire': True, 'identity': 'v2', 'state': {'fingerprint': 'v2'}, 'payload': {'message': 'Changed'}}
    engine.tick()
    o, = engine.store.list_occurrences(w.id)
    assert o.state == 'pending' and o.error == 'held_unanswered'
    engine.probe_runner = lambda _: {'fire': False, 'error': 'URLError'}
    for _ in range(5):
        engine.clock.advance(61)
        engine.tick()
    assert engine.store.get_wake(w.id).status == 'paused'
    held = engine.store.get_occurrence(w.id, o.dedupe_key)
    assert held.state == 'pending' and held.error == 'held_paused'
    assert not engine.store.get_meta('not_before:' + o.id)
    admit(engine, text='Keep checking')
    assert governor.owner_answer(engine, engine.store.get_wake(w.id), 'keep')['ok']
    engine.probe_runner = lambda _: {'fire': False, 'state': {'fingerprint': 'v2'}}
    engine.tick()
    assert len(engine.ctx.calls) == 1
    start(engine)
    end(engine)
    engine.clock.advance(61)
    engine.tick()
    assert len(engine.ctx.calls) == 1


def test_R2_explicit_pause_preserves_held_match(engine):
    session(engine, unanswered_count=2)
    w = arm(engine, 'subagent_done')
    o = pending(engine, w, 'e:match')
    engine.dispatch(o)
    assert engine.tool.handle({'action': 'pause', 'id': w.id})['ok']
    engine.tick()
    assert engine.store.get_occurrence(w.id, o.dedupe_key).error == 'held_paused'


def test_R3_cap_reservation_releases_next_tick_before_midnight(engine):
    engine.policy = replace(engine.policy, max_messages_per_day=6, wake_spacing_minutes=0)
    timer = arm(engine)
    engine.ledger(timer, delivered=5)
    engine.tick()
    start(engine)
    watcher = arm(engine, 'subagent_done')
    o = pending(engine, watcher, 'e:match')
    engine.tick()
    held = engine.store.get_occurrence(watcher.id, o.dedupe_key)
    assert held.state == 'pending' and held.error == 'held_daily_cap'
    # Simulate an old persisted deadline so upgrade also releases promptly.
    engine.store.set_meta('not_before:' + o.id, str(engine.clock.now() + 12*3600))
    end(engine, notify=False)
    engine.clock.advance(1)
    engine.tick()
    assert len(engine.ctx.calls) == 2
    assert engine.store.get_occurrence(watcher.id, o.dedupe_key).state == 'claimed'
    assert not engine.store.get_meta('not_before:' + o.id)


def test_R4_codex_normal_end_without_usage_counts_and_caps(engine):
    engine.policy = replace(engine.policy, max_model_wakes_per_day=1, wake_spacing_minutes=0)
    first = arm(engine)
    engine.tick()
    start(engine)
    assert engine.tool.handle({'action': 'respond', 'outcome': 'progress', 'notify': True}, session_id='sid')['ok']
    engine.intake.on_session_end(session_id='sid', turn_id='turn', completed=True)
    assert engine.store.list_occurrences(first.id)[0].api_calls == 0
    assert engine.daily(KEY)['model_turns'] == 1
    second = arm(engine)
    engine.clock.advance(1)
    engine.tick()
    assert len(engine.ctx.calls) == 1
    assert engine.store.list_occurrences(second.id)[0].error == 'daily_model_cap'


@pytest.mark.parametrize('cause', ['failed', 'no_session_end', 'restart'])
def test_R4_missing_hooks_without_provider_evidence_not_refunded(engine, cause):
    w = arm(engine)
    engine.tick()
    start(engine)
    o, = engine.store.list_occurrences(w.id)
    assert engine.daily(KEY)['model_turns'] == 1
    engine.interrupt(o, cause)
    assert engine.daily(KEY)['model_turns'] == 1
    assert engine.store.get_wake(w.id).model_turns == 1


def test_R5_record_tolerates_future_additive_columns(engine):
    w = arm(engine)
    o = pending(engine, w)
    for table in ('wakes', 'occurrences', 'sessions'):
        engine.store.connection.execute(f'ALTER TABLE {table} ADD COLUMN future_field TEXT')
    assert engine.store.get_wake(w.id).note == w.note
    assert engine.store.get_occurrence(w.id, o.dedupe_key).state == 'pending'
    assert engine.store.get_session(KEY).session_key == KEY


def test_N1_why_migrated_timestamp_is_before_v012(engine, monkeypatch):
    w = arm(engine)
    pending(engine, w)
    engine.store.connection.execute('UPDATE occurrences SET created_at=0, matched_at=NULL')
    monkeypatch.setattr(commands, 'session_env', lambda n: KEY if n == 'HERMES_SESSION_KEY' else '')
    output = commands.handle('why ' + w.id, SimpleNamespace(get_engine=lambda: engine))
    assert 'before v0.1.2' in output and '1970' not in output


def test_N2_released_text_uses_latest_hold_reason(engine):
    session(engine, unanswered_count=2)
    w = arm(engine, 'subagent_done')
    o = pending(engine, w, 'e:match')
    engine.dispatch(o)
    session(engine, unanswered_count=0)
    engine.policy = replace(engine.policy, max_messages_per_day=0)
    engine.dispatch(engine.store.get_occurrence(w.id, o.dedupe_key))
    assert engine.store.get_occurrence(w.id, o.dedupe_key).held_reason == 'held_daily_cap'
    engine.policy = replace(engine.policy, max_messages_per_day=6)
    engine.clock.advance(86400)
    engine.dispatch(engine.store.get_occurrence(w.id, o.dedupe_key))
    text = engine.ctx.calls[-1][0]
    assert 'daily cap' in text and "you hadn't replied" not in text


def test_N3_released_started_expiry_keeps_normal_reason(engine):
    assert engine.acquire()  # Admit the process before creating the running turn.
    session(engine, unanswered_count=2)
    w = arm(engine, 'subagent_done', expires_at=engine.clock.now()+10)
    o = pending(engine, w, 'e:match')
    engine.dispatch(o)
    session(engine, unanswered_count=0)
    engine.dispatch(engine.store.get_occurrence(w.id, o.dedupe_key))
    start(engine)
    engine.clock.advance(11)
    engine.tick()
    result = engine.store.get_occurrence(w.id, o.dedupe_key)
    assert result.state == 'superseded' and result.error == 'expired'


@pytest.mark.parametrize('floor,cold', [(3420, False), (3421, True), (3600, True)])
def test_N4_cache_line_floor_at_edge_says_wakes_cold(engine, hermes_helpers, monkeypatch, floor, cold):
    # A floor exactly at the snap edge (T-m) still snaps warm, matching cache.interval.
    monkeypatch.setattr(cache, 'cost', lambda *a, **k: None)
    cache.touch(engine.store, engine.store.get_session(KEY), started_at=engine.clock.now(),
                route='anthropic|claude-opus-5.5|api.anthropic.com', prompt=100, read=100, api_mode='anthropic_messages')
    w = arm(engine, 'every', pacing_min_s=floor)
    text = engine.cache_line(w, seconds=3600)
    if cold:
        assert 'floor ≥ TTL: wakes cold' in text and 'next warm' not in text
    else:
        assert 'next warm ≤57m' in text and 'wakes cold' not in text


@pytest.mark.parametrize('reason,label', [('held_unanswered', "you hadn't replied"), ('held_daily_cap', 'daily cap'),
                                          ('held_paused', 'wake was paused')])
def test_released_hold_text_names_its_reason(engine, reason, label):
    from heartbeat2.wake_text import render_wake
    w = arm(engine, 'subagent_done')
    o = pending(engine, w, 'e:x')
    engine.store.update_occurrence(w.id, o.dedupe_key, 'pending', held_since=engine.clock.now(), held_reason=reason, held_matches=1)
    o = engine.store.get_occurrence(w.id, o.dedupe_key)
    text = render_wake(w, o, engine.store.get_session(KEY), clock=engine.clock, timezone='UTC', recent_outcomes=[],
                       messages_today=0, max_messages=6)
    assert f'({label})' in text


def test_N5_pricing_outside_transactions_intake_dispatch_and_pause(engine, hermes_helpers, monkeypatch):
    calls = []
    def cost(*a, **k):
        calls.append(engine.store.connection.in_transaction)
        return .1
    monkeypatch.setattr(cache, 'cost', cost)
    cache.touch(engine.store, engine.store.get_session(KEY), started_at=engine.clock.now(),
                route='anthropic|claude-opus-5.5|api.anthropic.com', prompt=100, read=100, api_mode='anthropic_messages')
    w = arm(engine)
    engine.tick()
    start(engine)
    engine.intake.post_api_request(session_id='sid', turn_id='turn', usage={'prompt_tokens':100},
                                  provider='anthropic', model='claude-opus-5.5', base_url='https://api.anthropic.com')
    with engine.store.atomic():
        governor.pause(engine, engine.store.get_wake(w.id), 'noop')
    assert calls and not any(calls)
    assert engine.store.list_occurrences(w.id)[0].cost_microusd == 100000


def test_N6_usage_none_counts_only_api_and_unpriced_preserves_prefix(engine, monkeypatch):
    calls = []
    monkeypatch.setattr(cache, 'cost', lambda *a, **k: calls.append(True) or 0)
    session(engine, last_prompt_tokens=1234)
    w = arm(engine)
    engine.tick()
    start(engine)
    engine.intake.post_api_request(session_id='sid', turn_id='turn', usage=None,
                                  started_at=engine.clock.now(), provider='openai', model='gpt-6.1-sol')
    o, = engine.store.list_occurrences(w.id)
    assert (o.api_calls, o.unpriced_calls, o.warm_wakes, o.cold_wakes, o.cost_microusd) == (1, 1, 0, 0, 0)
    assert engine.store.get_session(KEY).last_prompt_tokens == 1234
    assert not calls


@pytest.mark.parametrize('kind', ['at', 'subagent_done', 'every'])
def test_N7_tool_cache_fields_match_due_or_unknown_watcher(engine, hermes_helpers, monkeypatch, kind):
    monkeypatch.setattr(cache, 'cost', lambda *a, **k: None)
    cache.touch(engine.store, engine.store.get_session(KEY), started_at=engine.clock.now(),
                route='anthropic|claude-opus-5.5|api.anthropic.com', prompt=100, read=100, api_mode='anthropic_messages')
    trigger = {'kind':kind, **({'at':'in 3h'} if kind == 'at' else {'every':'1h'} if kind == 'every' else {})}
    created = engine.tool.handle({'action':'create', 'note':'Task', 'trigger':trigger})
    updated = engine.tool.handle({'action':'update', 'id':created['wake']['id'], 'note':'Task update'})
    listed = engine.tool.handle({'action':'list'})['wakes'][0]
    for text in (created['cache'], updated['cache'], listed['cache']):
        if kind == 'at': assert '180.0m cold' in text
        elif kind == 'every': assert '57.0m warm' in text
        else: assert '1h' in text and 'last touch' in text and 'warm' not in text and 'cold' not in text


def test_N8_docs_upgrade_cost_routes_and_final_mute():
    root = Path(__file__).resolve().parents[1]
    security = (root/'SECURITY.md').read_text()
    assert '≈tokens' not in security and 'warm/cold wake counts and estimated cost' in security
    for path in ('README.md', 'skills/heartbeat2-usage/SKILL.md'):
        text = (root/path).read_text()
        assert 'OpenAI API routes' in text and 'Codex OAuth' in text and 'unknown until learned' in text
    assert 'Wakes you armed always fire; agent watchers wait for your reply; agent reminders are dropped.' in (root/'plugin.yaml').read_text()
    changelog = (root/'CHANGELOG.md').read_text()
    assert 'Upgrade' in changelog and 'restart the gateway' in changelog and 'plugin-data' in changelog


def test_N7_unknown_watcher_cache_is_only_ttl_and_last_touch(engine):
    session(engine, last_route='openai-codex|gpt-6.1-sol|chatgpt.com', last_cache_touch_at=engine.clock.now())
    w = arm(engine, 'subagent_done')
    engine.store.set_meta('cache_routes', '{"openai-codex|gpt-6.1-sol|chatgpt.com":{"max_warm_gap":100}}')
    text = engine.tool.handle({'action':'list'})['wakes'][0]['cache']
    assert 'TTL unknown' in text and 'last touch' in text
    assert 'warm' not in text and 'cold' not in text
