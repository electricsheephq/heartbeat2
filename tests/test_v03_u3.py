"""U3: own time, a chat-local desk, and priced agent-turn budgets."""
from dataclasses import replace
from datetime import datetime
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from heartbeat2 import commands
from heartbeat2.model import Session
from heartbeat2.policy import Policy, is_owner
from test_engine import KEY, arm, end, engine, pending, session, start


NOTE = ('This is your own time. Look at your desk: work on what you own, follow up on something '
        'you noticed, think something through, tidy your notes or memory, or rest. '
        "Message the owner only if it's worth their attention.")
OWN_ID = 'owntime_' + hashlib.sha256(KEY.encode()).hexdigest()[:16]


def own(e, hours=6):
    e.policy = replace(e.policy, own_time_hours=hours)
    e.tick()
    w = e.store.get_wake(OWN_ID)
    assert w is not None
    return w


def desk(e):
    from heartbeat2.desk import render_desk
    return render_desk(e.store, e.store.get_session(KEY), clock=e.clock,
                       timezone=e.policy.resolved_timezone(), policy=e.policy)


def priced(e, w, cost, unpriced=0, key='priced', when=None):
    o = pending(e, w, key)
    assert e.store.claim(w.id, key, epoch=e.epoch, pid=1)
    o = e.store.get_occurrence(w.id, key)
    assert e.store.cas_occurrence(w.id, key, 'claimed', 'started', attempt=o.attempt,
                                  started_at=e.clock.now() if when is None else when)
    assert e.store.cas_occurrence(w.id, key, 'started', 'done', attempt=o.attempt,
                                  cost_microusd=cost, unpriced_calls=unpriced,
                                  summary='  Wrote\n release notes  ', outcome='progress')
    # Durable accounting records usage arrival; these fixtures synthesize that event.
    wall = e.clock.wall
    try:
        if when is not None: e.clock.wall = when
        with e.store.atomic():
            e.store.add_agent_spend(w, o.id, e.policy.resolved_timezone(), cost, unpriced)
    finally:
        e.clock.wall = wall


def test_settings_and_manifest():
    p = Policy.from_settings({'own_time_hours': '6', 'daily_cost_limit_usd': '0.42'})
    assert p.own_time_hours == 6 and p.daily_cost_limit_usd == .42
    schema = yaml.safe_load((Path(__file__).parents[1] / 'plugin.yaml').read_text())['config_schema']
    assert schema['own_time_hours']['default'] == 0
    assert schema['own_time_hours']['type'] == 'int'
    assert schema['own_time_hours']['min'] == 0 and schema['own_time_hours']['max'] == 168
    assert schema['daily_cost_limit_usd']['default'] == 0
    assert schema['daily_cost_limit_usd']['type'] in {'float', 'number'}
    assert schema['daily_cost_limit_usd']['min'] == 0
    assert schema['own_time_hours']['label'] == 'Own time (hours between turns)'
    assert schema['daily_cost_limit_usd']['label'] == "Daily cost limit for the agent's own turns (USD)"


@pytest.mark.parametrize('setting,value', [('own_time_hours', 169), ('own_time_hours', -1),
    ('own_time_hours', 1.5), ('own_time_hours', True), ('daily_cost_limit_usd', -1),
    ('daily_cost_limit_usd', 'nan'), ('daily_cost_limit_usd', 'inf'), ('daily_cost_limit_usd', True)])
def test_invalid_settings(setting, value):
    with pytest.raises((ValueError, TypeError)):
        Policy.from_settings({setting: value})


def test_own_time_arming_settings_and_reactivation(engine):
    e = engine
    w = own(e)
    assert w.id == OWN_ID and w.kind == 'self' and w.creator == 'user'
    assert w.created_in == 'human_turn' and not is_owner(w)
    assert w.note == NOTE and w.title == 'Own time' and w.reason == 'Own time.'
    assert w.trigger_json == {'builtin': True, 'own_time': True, 'hours': 6, 'floor': w.pacing_min_s} and w.expires_at is None
    assert w.base_interval_s == w.eff_interval_s == 21600
    assert w.pacing_min_s == max(e.policy.min_interval_minutes, e.policy.agent_ungated_floor_minutes) * 60
    assert w.pacing_max_s == 43200 and w.next_due_at == e.clock.now() + 21600
    assert len(e.store.list_wakes(KEY)) == 1
    e.policy = replace(e.policy, own_time_hours=12)
    e.tick()
    w = e.store.get_wake(w.id)
    assert w.base_interval_s == w.eff_interval_s == 43200 and w.pacing_max_s == 86400
    for status in ('done', 'orphaned'):
        e.store.cas_wake(w.id, 'active', status=status)
        e.tick()
        assert e.store.get_wake(w.id).status == 'active'
    o = pending(e, e.store.get_wake(w.id))
    e.policy = replace(e.policy, own_time_hours=0)
    e.tick()
    w = e.store.get_wake(w.id)
    assert w.status == 'cancelled' and w.status_reason == 'settings'
    assert e.store.get_occurrence(w.id, o.dedupe_key).state == 'superseded'
    assert not e.ctx.calls


def test_own_time_trigger_protection_pacing_and_children(engine):
    e = engine
    w = own(e)
    e.dispatch(pending(e, w))
    start(e)
    result = e.tool.handle(dict(action='update', id=w.id, trigger={'kind': 'self'}), session_id='sid')
    assert result['error'] == 'builtin_settings_owned'
    e.policy = replace(e.policy, max_rearm_chain=0)
    for _ in range(e.policy.max_children_per_wake_turn):
        result = e.tool.handle(dict(action='create', note='follow up', trigger={'kind': 'self'}), session_id='sid')
        assert result.get('ok'), result
        assert result['wake']['depth'] == 1 and result['wake']['parent_wake_id'] == w.id
    assert e.tool.handle(dict(action='create', note='extra', trigger={'kind': 'self'}), session_id='sid')['error'] == 'rearm_child'
    result = e.tool.handle(dict(action='respond', outcome='progress', notify=False,
                               summary='worked', next_check='2h'), session_id='sid')
    assert result.get('ok'), result
    e.intake.on_session_end(session_id='sid', turn_id='turn')
    assert e.store.get_wake(w.id).next_due_at == e.clock.now() + 7200
    e.tick()  # A poll must preserve agent pacing and governor changes.
    assert e.store.get_wake(w.id).next_due_at == e.clock.now() + 7200


@pytest.mark.parametrize('hold', ['quiet_hours', 'unanswered', 'daily_message_cap'])
def test_own_time_holds_messages_and_model_cap_drops(engine, hold):
    e = engine
    w = own(e)
    if hold == 'quiet_hours': e.policy = replace(e.policy, quiet_hours='00:00-23:59')
    elif hold == 'unanswered': session(e, unanswered_count=e.policy.max_unanswered)
    else: e.ledger(w, delivered=e.policy.max_messages_per_day)
    e.dispatch(pending(e, w))
    o, = e.store.list_occurrences(w.id)
    assert o.state == 'claimed' and o.force_silent and o.held_reason == 'message_held:' + hold
    start(e)
    assert end(e, 'no_change', True, summary='rested') == '[SILENT]'
    assert e.store.get_wake(w.id).noop_streak == 1
    e.ledger(w, model_turns=e.policy.max_model_wakes_per_day)
    e.dispatch(pending(e, e.store.get_wake(w.id), 'model-cap'))
    assert e.store.get_occurrence(w.id, 'model-cap').error == 'daily_model_cap'


@pytest.mark.parametrize('kind', ['own', 'role'])
def test_desk_prompt_placement_and_settled_line(engine, kind):
    e = engine
    w = own(e) if kind == 'own' else arm(e, 'self', role=True, scratch='notes')
    e.dispatch(pending(e, w))
    text = e.ctx.calls[-1][0]
    assert 'Your desk:' in text and 'subject was settled' not in text
    assert text.index('Your desk:') > text.index('Scratch:' if w.scratch else 'Your note:')
    assert text.index('Your desk:') < text.index('Recent outcomes:')
    assert 'Contract: this turn is yours to act on.' in text
    start(e)
    end(e, 'progress', False)
    e.clock.advance(601)
    ordinary = arm(e)
    e.dispatch(pending(e, ordinary))
    text = e.ctx.calls[-1][0]
    assert 'Your desk:' not in text and 'subject was settled' in text


def test_desk_records_counts_local_times_and_bound(engine, monkeypatch):
    e = engine
    e.policy = replace(e.policy, daily_cost_limit_usd=2)
    role = arm(e, 'self', role=True, title='Release notes', next_due_at=e.clock.now() + 60)
    priced(e, role, 420000, 2)
    paused = arm(e, 'every', role=True, status='paused', status_reason='command', title='Paused role')
    other = arm(e, title='Next item', next_due_at=e.clock.now() + 120)
    arm(e, 'file', next_due_at=e.clock.now() + 180)
    e.ledger(role, delivered=2, model_turns=5)
    session(e, last_human_at=e.clock.now() - 11400, unanswered_count=1)
    out = desk(e)
    assert f'- role {role.id} Release notes · self-paced · last: Wrote release notes · next 12:01 · today 1/6' in out
    assert f'- role {paused.id}' in out and 'paused · command' in out
    assert '- Other: 1 wakes, 1 watchers · next due 12:02 (Next item)' in out
    assert 'Owner last wrote 3h 10m ago · unanswered 1 · messages today 2/6 · turns today 5/24' in out
    assert 'cost today $0.42 of $2.00 · 2 unpriced' in out
    assert 'Messages held now:' not in out
    e.store.set_meta('quiet:' + KEY, '{"range":"00:00-23:59"}')
    assert 'Messages held now: quiet hours' in desk(e)
    e.store.set_meta('quiet:' + KEY, '{"range":""}')
    session(e, unanswered_count=e.policy.max_unanswered)
    assert "Messages held now: the owner hasn't replied yet" in desk(e)
    session(e, unanswered_count=0)
    e.ledger(role, delivered=4)
    assert "Messages held now: today's message limit" in desk(e)
    # Old rows are removed first; newest-touched role and all non-role lines survive.
    for n in range(30):
        e.clock.advance(1)
        arm(e, 'self', role=True, title=f'Role {n} ' + 'x' * 70)
    out = desk(e)
    assert len(out) <= 1800 and out.splitlines()[-1].startswith('… ')
    assert out.endswith(' more') and 'Role 29' in out and f'- role {role.id}' not in out
    assert '- Other:' in out and 'Owner last wrote' in out and 'Messages held now:' in out


def test_desk_command_is_chat_local_and_status_cost(engine, monkeypatch):
    e = engine
    e.policy = replace(e.policy, daily_cost_limit_usd=2)
    local = arm(e, 'self', role=True, title='LOCAL')
    priced(e, local, 420000, 1)
    e.store.upsert_session(Session('foreign', 'telegram', 'dm', 'foreign', 'else'))
    e.store.create_wake(replace(local, id='foreign_role', session_key='foreign', title='FOREIGN'))
    monkeypatch.setattr(commands, 'session_env', lambda name: KEY if name == 'HERMES_SESSION_KEY' else '')
    runtime = SimpleNamespace(get_engine=lambda: e)
    assert commands.handle('desk', runtime) == desk(e)
    assert 'LOCAL' in commands.handle('desk', runtime) and 'FOREIGN' not in commands.handle('desk', runtime)
    assert 'desk' in commands.HELP
    assert 'cost today $0.42 of $2.00' in commands.handle('status', runtime)


@pytest.mark.parametrize('kind,changes', [('every', {}), ('self', {'role': True}),
    ('file', {}), ('self', {'trigger_json': {'builtin': True, 'own_time': True}, 'creator': 'user'})])
def test_cost_cap_drops_agent_work_but_exempts_owner(engine, kind, changes):
    e = engine
    e.policy = replace(e.policy, daily_cost_limit_usd=1, own_time_hours=6)
    history = arm(e, 'every')
    priced(e, history, 400000, key='one')
    priced(e, history, 600000, unpriced=3, key='two')
    w = arm(e, kind, **changes)
    e.dispatch(pending(e, w))
    o, = e.store.list_occurrences(w.id)
    assert o.state == 'skipped' and o.error == 'daily_cost_cap'
    for n, (kind, extra) in enumerate([('every', {'creator': 'user'}), ('at', {'owner_requested': True})]):
        owner = arm(e, kind, **extra)
        e.dispatch(pending(e, owner))
        assert len(e.ctx.calls) == n + 1
        start(e)
        end(e, 'progress', False)
        e.clock.advance(601)


def test_cost_uses_local_day_and_excludes_owner_and_unpriced(engine):
    e = engine
    e.policy = replace(e.policy, timezone='Asia/Tokyo', daily_cost_limit_usd=.5)
    e.clock.wall = datetime.fromisoformat('2025-10-12T15:01:00+00:00').timestamp()
    w = arm(e, 'self', role=True)
    priced(e, w, 900000, key='yesterday', when=e.clock.now() - 120)
    priced(e, w, 100000, unpriced=4, key='today')
    for n, extra in enumerate([{'creator': 'user'}, {'owner_requested': True}]):
        owner = arm(e, 'at', **extra)
        priced(e, owner, 1000000, key=f'owner:{n}')
    assert 'cost today $0.10 of $0.50 · 4 unpriced' in desk(e)
    e.dispatch(pending(e, w, 'allowed'))
    assert len(e.ctx.calls) == 1


@pytest.mark.parametrize('switch', ['reset', 'session'])
def test_own_time_survives_new_and_session_switch(engine, switch):
    e = engine
    w = own(e)
    if switch == 'reset':
        e.intake.on_session_reset(old_session_id='sid', new_session_id='new')
    else:
        e.intake._session_switch(w, 'new')
        session(e, current_session_id='new')
    w = e.store.get_wake(w.id)
    assert w.armed_session_id == 'new' and w.status == 'active'
    e.clock.advance(21600)
    e.tick()
    start(e, sid='new')
    assert e.intake.flights['new'].wake_id == w.id and e.ctx.calls[-1][1] == KEY
