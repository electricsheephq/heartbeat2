"""Chat-requested one-shots keep owner semantics only with current-turn intent."""
from dataclasses import asdict, replace
import sqlite3
from types import SimpleNamespace

import pytest

from heartbeat2 import commands, policy
from heartbeat2.model import Wake, agent_authored
from heartbeat2.store import Store, _SCHEMA, _encode
from heartbeat2.tool import PARAMETERS, DESCRIPTION
from test_engine import engine, session, KEY, admit, arm, pending, start


def owner_turn(e, turn='owner'):
    admit(e, mid=turn, text='Remind me to send the invoice.')
    e.intake.pre_llm_call(session_id='sid', turn_id=turn,
                         user_message='Remind me to send the invoice.', platform='telegram')


def call(e, args, sid='sid'):
    result = e.tool.handle(args, session_id=sid)
    # On the old implementation, exercise its existing path for assertion-based
    # red proof instead of stopping all behavioral cases at unknown_parameter.
    if result.get('error') == 'unknown_parameter' and 'owner_asked' in args:
        result = e.tool.handle({k:v for k,v in args.items() if k != 'owner_asked'}, session_id=sid)
    assert result.get('ok'), result
    return result


def create(e, *, requested=True, kind='at', **args):
    trigger = {'kind':kind, **({'at':'in 1m'} if kind == 'at' else
                              {'every':'1h'} if kind == 'every' else {})}
    result = call(e, {'action':'create', 'note':'Send the invoice.', 'trigger':trigger,
                     **({'owner_asked':True} if requested else {}), **args})
    return e.store.get_wake(result['wake']['id']), result


def finish(e):
    e.intake.on_session_end(session_id='sid', turn_id=e.intake.turns.get('sid',''), completed=True)


def muted_dispatch(e, w):
    finish(e)
    session(e, unanswered_count=e.policy.max_unanswered)
    e.clock.advance(61)
    e.tick()
    return e.store.list_occurrences(w.id)[0]


def flag(w):
    return getattr(w, 'owner_requested', False)


def test_requested_reminder_survives_mute(engine):
    owner_turn(engine)
    w, result = create(engine)
    assert flag(w) is True and result['wake']['owner_requested'] is True
    assert w.creator == 'agent' and agent_authored(w) and not policy.is_owner(w)
    assert muted_dispatch(engine, w).state == 'claimed'
    assert len(engine.ctx.calls) == 1
    assert engine.daily(KEY)['model_turns'] == 1


@pytest.mark.parametrize('cap', ['daily_model_cap', 'daily_message_cap'])
def test_requested_reminder_bypasses_daily_caps(engine, cap):
    owner_turn(engine)
    w, _ = create(engine)
    finish(engine)
    setting = 'max_model_wakes_per_day' if cap == 'daily_model_cap' else 'max_messages_per_day'
    engine.policy = replace(engine.policy, **{setting:0})
    engine.clock.advance(61)
    engine.tick()
    o, = engine.store.list_occurrences(w.id)
    assert o.state == 'claimed', o.error
    assert len(engine.ctx.calls) == 1 and flag(engine.store.get_wake(w.id)) is True


@pytest.mark.parametrize('control', ['undeclared', 'internal', 'empty', 'every', 'watcher', 'stale'])
def test_negative_controls(engine, control):
    if control == 'internal':
        engine.intake.pre_llm_call(session_id='sid', turn_id='internal',
                                 user_message='Internal event', platform='telegram')
    else:
        owner_turn(engine, '' if control == 'empty' else 'owner')
        if control == 'stale':
            engine.intake.pre_llm_call(session_id='sid', turn_id='later',
                                     user_message='Internal event', platform='telegram')
    kind = 'every' if control == 'every' else 'subagent_done' if control == 'watcher' else 'at'
    w, result = create(engine, requested=control != 'undeclared', kind=kind)
    assert flag(w) is False
    if control != 'undeclared':
        assert result.get('owner_asked_ignored') == ('not_one_shot' if kind != 'at' else 'not_owner_turn')
    else:
        assert 'owner_asked_ignored' not in result
    finish(engine)
    session(engine, unanswered_count=engine.policy.max_unanswered)
    o = pending(engine, w)
    expected = 'held_unanswered' if kind == 'subagent_done' else 'unanswered'
    assert engine.gate(w, o, engine.store.get_session(KEY))[0] == expected
    session(engine, unanswered_count=0)
    for setting, expected in [('max_model_wakes_per_day','daily_model_cap'),
                              ('max_messages_per_day','daily_message_cap')]:
        old = engine.policy
        engine.policy = replace(old, **{setting:0})
        assert engine.gate(w, o, engine.store.get_session(KEY))[0] == ('held_daily_cap' if kind == 'subagent_done' else expected)
        engine.policy = old


def test_wake_turn_never_grants(engine):
    parent = arm(engine)
    engine.tick()
    start(engine)
    w, result = create(engine)
    assert result.get('owner_asked_ignored') == 'wake_turn'
    assert w.parent_wake_id == parent.id and flag(w) is False
    assert engine.policy.unanswered_gate(w, engine.policy.max_unanswered) == 'unanswered'


@pytest.mark.parametrize('edit', [
    {'trigger':{'kind':'at','at':'in 2m'}}, {'note':'Updated task.'}, {'urgent':True},
    {'until':{'expires':'in 1h'}}, {'until':{'max_fires':2}}, {'until':{'owner_replies':True}},
])
def test_autonomous_edit_revokes_and_mutes(engine, edit):
    owner_turn(engine)
    w, _ = create(engine)
    assert flag(w) is True
    result = call(engine, {'action':'update', 'id':w.id, **edit})
    changed = engine.store.get_wake(w.id)
    assert result['wake']['owner_requested'] is False and flag(changed) is False
    assert engine.policy.unanswered_gate(changed, engine.policy.max_unanswered) == 'unanswered'


@pytest.mark.parametrize('declared,evidence', [(True,True), (True,False), (False,True)])
def test_retime_requires_declaration_and_current_owner_turn(engine, declared, evidence):
    owner_turn(engine)
    w, _ = create(engine)
    if not evidence:
        engine.intake.pre_llm_call(session_id='sid', turn_id='internal', platform='telegram')
    call(engine, {'action':'update','id':w.id,'trigger':{'kind':'at','at':'in 2m'},
                  **({'owner_asked':True} if declared else {})})
    assert flag(engine.store.get_wake(w.id)) is (declared and evidence)


@pytest.mark.parametrize('edit', [{'title':'New title'}, {'reason':'New reason'}, {}])
def test_cosmetic_update_preserves(engine, edit):
    owner_turn(engine)
    w, _ = create(engine)
    call(engine, {'action':'update','id':w.id, **edit})
    assert flag(engine.store.get_wake(w.id)) is True


@pytest.mark.parametrize('wid', ['single', 'all'])
@pytest.mark.parametrize('declared', [False, True])
def test_pause_preserves_agent_resume_requires_declaration(engine, wid, declared):
    owner_turn(engine)
    w, _ = create(engine)
    target = w.id if wid == 'single' else 'all'
    call(engine, {'action':'pause','id':target})
    assert flag(engine.store.get_wake(w.id)) is True
    call(engine, {'action':'resume','id':target, **({'owner_asked':True} if declared else {})})
    assert flag(engine.store.get_wake(w.id)) is declared


def test_owner_can_retime_earlier_agent_reminder(engine):
    w, _ = create(engine, requested=False)
    owner_turn(engine)
    call(engine, {'action':'update','id':w.id,'trigger':{'kind':'at','at':'in 2m'},'owner_asked':True})
    assert flag(engine.store.get_wake(w.id)) is True


@pytest.fixture
def command(engine, monkeypatch):
    monkeypatch.setattr(commands, 'session_env', lambda n: KEY if n == 'HERMES_SESSION_KEY' else '')
    return lambda text: commands.handle(text, SimpleNamespace(get_engine=lambda: engine))


def test_ask_first_and_owner_command_resume_preserve(engine, command):
    engine.policy = replace(engine.policy, agent_can_create='ask_first')
    owner_turn(engine)
    w, _ = create(engine)
    assert w.status == 'proposed' and flag(w) is True
    finish(engine)
    engine.clock.advance(61)
    engine.tick()
    assert engine.ctx.calls == [] and engine.store.list_occurrences(w.id) == []
    assert 'active' in command('ok '+w.id)
    assert flag(engine.store.get_wake(w.id)) is True
    assert 'paused' in command('pause '+w.id)
    assert 'active' in command('resume '+w.id)
    assert flag(engine.store.get_wake(w.id)) is True
    session(engine, unanswered_count=engine.policy.max_unanswered)
    engine.tick()
    assert len(engine.ctx.calls) == 1


def test_builtin_and_slash_owner_unchanged(engine, command):
    command('add in 1m Send the invoice.')
    w, = engine.store.list_wakes(KEY)
    assert w.creator == 'user' and not flag(w) and policy.is_owner(w)
    builtin = arm(engine, 'owner_silent', trigger_json={'builtin':True})
    assert engine.policy.unanswered_gate(builtin, engine.policy.max_unanswered) == 'unanswered'
    assert engine.policy.unanswered_gate(w, engine.policy.max_unanswered) is None
    call(engine, {'action':'update','id':w.id,'title':'Invoice','owner_asked':True})
    assert not flag(engine.store.get_wake(w.id)) and not engine.store.get_wake(w.id).agent_edited
    session(engine, unanswered_count=engine.policy.max_unanswered)
    engine.policy = replace(engine.policy, max_model_wakes_per_day=0, max_messages_per_day=0)
    engine.clock.advance(61)
    engine.tick()
    assert len(engine.ctx.calls) == 1
    assert 'owner_asked' in PARAMETERS['properties']


def test_old_schema_migration_never_backfills_intent(tmp_path, clock):
    path = tmp_path/'old.db'
    old = _SCHEMA.replace(' owner_requested INTEGER NOT NULL DEFAULT 0,', '')
    data = asdict(Wake(KEY, 'Old reminder.', 'at', created_in='human_turn'))
    data.pop('owner_requested', None)
    with sqlite3.connect(path) as conn:
        conn.executescript(old)
        columns = {r[1] for r in conn.execute('PRAGMA table_info(wakes)')}
        data = {k:v for k,v in data.items() if k in columns}
        conn.execute(f"INSERT INTO wakes ({','.join(data)}) VALUES ({','.join('?' for _ in data)})",
                     [_encode(k,v) for k,v in data.items()])
    with Store(path, clock) as store:
        w = store.get_wake(data['id'])
        assert store.connection.execute('SELECT owner_requested FROM wakes').fetchone()[0] == 0
        assert flag(w) is False and store.get_meta('schema_version') == '1'
        assert policy.Policy().unanswered_gate(w, 2) == 'unanswered'


def test_requested_text_and_command_rendering(engine, command):
    from heartbeat2.wake_text import render_wake
    owner_turn(engine)
    w, _ = create(engine)
    text = render_wake(w, pending(engine, w), engine.store.get_session(KEY), clock=engine.clock)
    assert "armed by you at the owner's request" in text
    assert 'wake messages today:' in text
    assert 'Why: you asked in chat' in command('why '+w.id)
    assert ' · you asked' in command('list') and ' · you asked' in command('status')
    assert 'owner_asked=true' in DESCRIPTION


def test_requested_reminder_still_holds_for_quiet(engine, command):
    owner_turn(engine)
    w, _ = create(engine)
    assert flag(w) is True
    command('quiet for 3m')
    o = muted_dispatch(engine, w)
    assert o.state == 'pending' and o.error == 'quiet_hours' and not engine.ctx.calls


@pytest.mark.parametrize('value', [1, 0, 'true', None, [], {}])
def test_owner_asked_requires_real_bool(engine, value):
    result = engine.tool.handle({'action':'create','note':'Task','trigger':{'kind':'at','at':'in 1m'},
                                 'owner_asked':value}, session_id='sid')
    assert result.get('error') == 'invalid_request'


@pytest.mark.parametrize('change', [{'creator':'user'}, {'kind':'every'}, {'parent_wake_id':'parent'},
                                  {'babysit':True}, {'trigger_json':{'builtin':True}}])
def test_stored_flag_is_defensive(change):
    check = getattr(policy, 'owner_requested', None)
    assert callable(check)
    w = SimpleNamespace(**{**asdict(Wake(KEY, 'Task', 'at')), 'owner_requested':True, **change})
    assert check(w) is False
