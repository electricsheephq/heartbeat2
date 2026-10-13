"""Arming feasibility and receipts: real store/tool/command paths, fake time."""
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from heartbeat2 import commands, governor
from heartbeat2.model import Wake
from heartbeat2.tool import DESCRIPTION
from test_engine import engine, session, KEY, arm
from test_v0113 import owner_turn

DAY = 86400


@pytest.fixture
def command(engine, monkeypatch):
    monkeypatch.setattr(commands, 'session_env', lambda n: KEY if n == 'HERMES_SESSION_KEY' else '')
    return lambda text: commands.handle(text, SimpleNamespace(get_engine=lambda: engine))


def create(e, trigger=None, **args):
    return e.tool.handle({'action':'create', 'note':'Check the report.',
                         'trigger':trigger or {'kind':'at','at':'in 1h'}, **args}, session_id='sid')


def receipt(result):
    text = result.get('receipt')
    assert isinstance(text, str) and text and '\n' not in text, result
    assert len(text) <= 240, text
    return text


def local(e, epoch):
    return datetime.fromtimestamp(epoch, e.policy.resolved_timezone()).strftime('%a %d %b %H:%M')


def test_beyond_maximum_tool_and_command_do_not_store(engine, command):
    result = create(engine, {'kind':'at','at':'in 30d'})
    assert result.get('error') == 'invalid_request', result
    assert 'maximum lifetime' in result['detail']
    assert command('add in 30d Check the report.').startswith('Error:')
    assert not engine.store.list_wakes(KEY)


@pytest.mark.parametrize('expires', ['in 5d', 'in 10d'])
def test_explicit_expiry_and_equality_rejected(engine, expires):
    result = create(engine, {'kind':'at','at':'in 10d'}, until={'expires':expires})
    assert result.get('error') == 'invalid_request', result
    assert 'due after expiry' in result['detail']
    assert not engine.store.list_wakes(KEY)


@pytest.mark.parametrize('edit', [
    {'until':{'expires':'in 30m'}},
    {'trigger':{'kind':'at','at':'in 30d'}},
    {'trigger':{'kind':'at','at':'in 2h'},'until':{'expires':'in 1h'}},
])
def test_rejected_update_rolls_back(engine, edit):
    result = create(engine)
    assert result.get('ok'), result
    before = engine.store.get_wake(result['wake']['id'])
    result = engine.tool.handle({'action':'update','id':before.id, **edit}, session_id='sid')
    assert result.get('error') == 'invalid_request', result
    assert engine.store.get_wake(before.id) == before


@pytest.mark.parametrize('fresh', [False, True])
def test_owner_edit_raised_floor_keeps_explicit_expiry(engine, command, fresh):
    command('add every 15m Check the report.')
    w, = engine.store.list_wakes(KEY)
    edit = {'note':'Check another report.', 'until':{'expires':'in 30m'}}
    if fresh: edit['trigger'] = {'kind':'every','every':'15m'}
    result = engine.tool.handle({'action':'update','id':w.id, **edit}, session_id='sid')
    assert result.get('error') == 'invalid_request', result
    assert 'due after expiry' in result['detail']
    assert engine.store.get_wake(w.id) == w


@pytest.mark.parametrize('kind', ['every', 'self'])
def test_implicit_recurring_expiry_stretches_and_survives_clamps(engine, kind):
    result = create(engine, {'kind':kind,'every':'10d'})
    assert result.get('ok'), result
    w = engine.store.get_wake(result['wake']['id'])
    assert w.next_due_at == engine.clock.now()+10*DAY
    assert w.next_due_at < w.expires_at <= w.created_at+14*DAY
    assert w.expires_at == w.next_due_at+3600
    assert f'Next at {local(engine,w.next_due_at)}, then every 10d.' in receipt(result)
    update = engine.tool.handle({'action':'update','id':w.id,'title':'Report'}, session_id='sid')
    assert update.get('ok'), update
    assert engine.store.get_wake(w.id).expires_at == w.expires_at
    engine.tool.handle({'action':'pause','id':w.id}, session_id='sid')
    resumed = engine.tool.handle({'action':'resume','id':w.id}, session_id='sid')
    assert resumed.get('ok'), resumed
    assert engine.store.get_wake(w.id).expires_at == w.expires_at
    engine.clock.advance(10*DAY)
    engine.tick()
    assert len(engine.ctx.calls) == 1
    assert engine.store.list_occurrences(w.id)[0].state == 'claimed'


@pytest.mark.parametrize('kind,trigger', [
    ('cron', {'cron':'0 12 * * *'}), ('every', {'every':'1d'}), ('self', {'every':'1d'})
])
def test_all_time_kinds_compare_final_due(engine, kind, trigger):
    result = create(engine, {'kind':kind, **trigger}, until={'expires':'in 1m'})
    assert result.get('error') == 'invalid_request', result
    assert 'due after expiry' in result['detail']
    assert not engine.store.list_wakes(KEY)


def test_quiet_receipt_and_explicit_expiry(engine):
    engine.policy = replace(engine.policy, quiet_hours='22:00-08:00')
    result = create(engine, {'kind':'at','at':'tomorrow 23:00'})
    assert result.get('ok'), result
    w = engine.store.get_wake(result['wake']['id'])
    assert f'Fires once at {local(engine,w.next_due_at)}.' in receipt(result)
    release = w.next_due_at+9*3600
    assert f'Quiet hours hold it until {local(engine,release)}.' in receipt(result)
    for end in (release-3600, release):
        expiry = datetime.fromtimestamp(end, engine.policy.resolved_timezone()).isoformat()
        rejected = create(engine, {'kind':'at','at':'tomorrow 23:00'}, until={'expires':expiry})
        assert rejected.get('error') == 'invalid_request', rejected
        assert 'due after expiry' in rejected['detail']
    assert len(engine.store.list_wakes(KEY)) == 1


@pytest.mark.parametrize('bypass', ['urgent', 'off', 'range', 'elapsed'])
def test_quiet_override_is_evaluated_at_due(engine, command, bypass):
    engine.policy = replace(engine.policy, quiet_hours='22:00-08:00', quiet_hours_urgent=True)
    if bypass == 'off': command('quiet off')
    if bypass == 'range': command('quiet 09:00-10:00')
    if bypass == 'elapsed': command('quiet for 1h')
    result = create(engine, {'kind':'at','at':'tomorrow 23:00'}, urgent=bypass=='urgent')
    assert result.get('ok'), result
    text = receipt(result)
    assert ('Quiet hours hold' in text) is (bypass == 'elapsed')


def test_timed_quiet_applies_only_before_override_end(engine, command):
    command('quiet for 2h')
    result = create(engine, until={'expires':'in 90m'})
    assert result.get('error') == 'invalid_request', result
    result = create(engine)
    assert result.get('ok'), result
    assert f'Quiet hours hold it until {local(engine,engine.clock.now()+7200)}.' in receipt(result)
    result = create(engine, {'kind':'at','at':'in 3h'}, until={'expires':'in 4h'})
    assert result.get('ok'), result
    assert 'Quiet' not in receipt(result)


def test_quiet_implicit_extension_and_recurring_skip(engine):
    engine.policy = replace(engine.policy, quiet_hours='22:00-08:00')
    result = create(engine, {'kind':'at','at':'tomorrow 23:00'})
    w = engine.store.get_wake(result['wake']['id'])
    engine.store.cas_wake(w.id, w.status, expires_at=w.next_due_at+3600)
    updated = engine.tool.handle({'action':'update','id':w.id,'title':'Report'}, session_id='sid')
    assert updated.get('ok'), updated
    changed = engine.store.get_wake(w.id)
    assert changed.expires_at == w.next_due_at+10*3600
    # Recurring ticks in quiet hours are skipped, not held until the release.
    recurring = create(engine, {'kind':'every','every':'1d at 23:00'},
                       until={'expires':'tomorrow 07:00'})
    assert recurring.get('ok'), recurring
    assert 'Quiet' not in receipt(recurring)


@pytest.mark.parametrize('kind', ['command', 'subagent_done', 'owner_silent'])
@pytest.mark.parametrize('explicit', [False, True])
def test_watcher_deadline_implicit_and_explicit(engine, kind, explicit):
    engine.policy = replace(engine.policy, max_lifetime_days=30)
    fields = {'command':['git','status']} if kind == 'command' else {'after':'1d'} if kind == 'owner_silent' else {}
    result = create(engine, {'kind':kind,'deadline':'in 20d', **fields},
                    until={'expires':'in 7d'} if explicit else {})
    if explicit:
        assert result.get('error') == 'invalid_request', result
        assert 'deadline after expiry' in result['detail']
        assert not engine.store.list_wakes(KEY)
    else:
        assert result.get('ok'), result
        w = engine.store.get_wake(result['wake']['id'])
        assert w.deadline_at < w.expires_at <= w.created_at+30*DAY
        # An implicit stored expiry is allowed to stretch to the deadline.
        engine.store.cas_wake(w.id,w.status,expires_at=engine.clock.now()+7*DAY)
        changed = engine.tool.handle({'action':'update','id':w.id,'title':'Report'}, session_id='sid')
        assert changed.get('ok'), changed
        assert engine.store.get_wake(w.id).expires_at == w.deadline_at+3600
        text = receipt(changed)
        assert f'until {local(engine,w.deadline_at)}' in text


@pytest.mark.parametrize('kind', ['command', 'subagent_done', 'owner_silent'])
def test_deadline_beyond_maximum_rejected(engine, kind):
    fields = {'command':['git','status']} if kind == 'command' else {'after':'1d'} if kind == 'owner_silent' else {}
    result = create(engine, {'kind':kind,'deadline':'in 20d', **fields})
    assert result.get('error') == 'invalid_request', result
    assert 'deadline after' in result['detail']
    assert not engine.store.list_wakes(KEY)


@pytest.mark.parametrize('path', ['tool', 'ok', 'resume', 'timed'])
def test_irreconcilable_resume_does_not_change_wake(engine, command, path):
    w = arm(engine, next_due_at=engine.clock.now()+30*DAY,
            status='proposed' if path == 'ok' else 'paused', status_reason='command')
    engine.store.set_meta('resume_after:'+w.id,str(engine.clock.now()-1))
    if path == 'tool':
        result = engine.tool.handle({'action':'resume','id':w.id}, session_id='sid')
        assert result.get('error') == 'invalid_request', result
        assert 'maximum lifetime' in result['detail']
    elif path == 'timed':
        commands.resume_timed(engine)
        assert engine.store.get_meta('resume_after:'+w.id)
    else:
        assert command(path+' '+w.id).startswith('Error:')
    assert engine.store.get_wake(w.id) == w


@pytest.mark.parametrize('path', ['tool', 'ok', 'resume', 'timed'])
def test_resumed_implicit_expiry_stretches_and_is_stored(engine, command, path):
    w = arm(engine, next_due_at=engine.clock.now()+10*DAY, expires_at=engine.clock.now()+7*DAY,
            status='proposed' if path == 'ok' else 'paused', status_reason='command')
    if path == 'tool':
        result = engine.tool.handle({'action':'resume','id':w.id}, session_id='sid')
        assert result.get('ok'), result
        assert 'Fires once' in receipt(result)
    elif path == 'timed':
        engine.store.set_meta('resume_after:'+w.id,str(engine.clock.now()-1))
        commands.resume_timed(engine)
    else:
        assert 'Fires once' in command(path+' '+w.id)
    changed = engine.store.get_wake(w.id)
    assert changed.status == 'active'
    assert changed.expires_at == changed.next_due_at+3600


def test_resume_checks_final_occurrence_key(engine):
    w = arm(engine,status='paused',next_due_at=engine.clock.now()+14*DAY-.0005)
    engine.store.add_occurrence(w.id,f't:{w.next_due_at:.17g}')
    result = engine.tool.handle({'action':'resume','id':w.id}, session_id='sid')
    assert result.get('error') == 'invalid_request', result
    assert 'maximum lifetime' in result['detail']
    assert engine.store.get_wake(w.id) == w


@pytest.mark.parametrize('answer', ['keep','slow'])
def test_governor_checks_its_final_due_and_rolls_back(engine, command, answer):
    w = arm(engine,'every',status='paused',status_reason='governor:noop',
            base_interval_s=10*DAY,eff_interval_s=10*DAY,expires_at=engine.clock.now()+7*DAY)
    if answer == 'keep':
        assert 'active' in command('keep '+w.id)
        changed = engine.store.get_wake(w.id)
        assert changed.expires_at == changed.next_due_at+3600
    else:
        engine.store.set_meta('card:'+w.id,'noop')
        assert command('slow '+w.id).startswith('Error:')
        assert engine.store.get_wake(w.id) == w
        assert engine.store.get_meta('card:'+w.id) == 'noop'


def test_proposal_and_paused_receipts_do_not_promise_fire(engine):
    engine.policy = replace(engine.policy, agent_can_create='ask_first')
    result = create(engine, until={'owner_replies':True})
    assert result.get('ok'), result
    text = receipt(result)
    assert text.startswith(f'Proposed, not armed: awaiting /pulse ok {result["wake"]["id"]}.')
    assert 'Fires' not in text and 'Next at' not in text and 'min apart' not in text
    engine.policy = replace(engine.policy, agent_can_create='yes')
    w = arm(engine, status='paused')
    result = engine.tool.handle({'action':'update','id':w.id,'title':'Report'}, session_id='sid')
    assert result.get('ok'), result
    assert receipt(result).startswith('Paused.') and 'Fires' not in receipt(result)


@pytest.mark.parametrize('kind', ['at', 'command', 'subagent_done'])
def test_muted_receipts_and_cancel(engine, kind):
    session(engine, unanswered_count=engine.policy.max_unanswered)
    fields = {'at':'in 1h'} if kind == 'at' else {'command':['git','status']} if kind == 'command' else {}
    result = create(engine, {'kind':kind, **fields}, until={'owner_replies':True})
    assert result.get('ok'), result
    text = receipt(result)
    assert ('Muted: it is dropped unless you reply first.' if kind == 'at' else
            'Muted: it waits for your reply.') in text
    assert 'Cancels if you reply.' in text and 'Expires ' in text
    assert 'Wakes are at least 10 min apart, so it can land a few minutes late.' in text
    if kind == 'command': assert 'Checks every 1m' in text
    if kind == 'subagent_done': assert 'Waits for subagent_done events' in text


def test_owner_receipts_and_commands_are_not_muted(engine, command):
    owner_turn(engine)
    session(engine, unanswered_count=engine.policy.max_unanswered)
    result = create(engine, owner_asked=True)
    assert result.get('ok') and result['wake']['owner_requested'], result
    assert 'Muted' not in receipt(result)
    output = command('add in 1h Check the report.')
    assert 'Fires once' in output and 'Muted' not in output
    w = next(w for w in engine.store.list_wakes(KEY) if w.creator == 'user')
    assert 'paused' in command('pause '+w.id)
    assert 'Fires once' in command('resume '+w.id)


def test_id_all_returns_receipts_and_rolls_back_on_reject(engine):
    good = arm(engine,status='paused')
    result = engine.tool.handle({'action':'resume','id':'all'}, session_id='sid')
    assert result.get('ok'), result
    assert all('Fires once' in receipt(item) for item in result['results'])
    engine.tool.handle({'action':'pause','id':'all'}, session_id='sid')
    before = engine.store.get_wake(good.id)
    bad = arm(engine,status='paused',next_due_at=engine.clock.now()+30*DAY)
    result = engine.tool.handle({'action':'resume','id':'all'}, session_id='sid')
    assert result.get('error') == 'invalid_request', result
    assert engine.store.get_wake(good.id) == before and engine.store.get_wake(bad.id) == bad


def test_receipt_timezone_spacing_and_maximum_length(engine):
    engine.policy = replace(engine.policy, timezone='Asia/Tokyo',quiet_hours='22:00-08:00')
    session(engine, unanswered_count=engine.policy.max_unanswered)
    result = create(engine, {'kind':'at','at':'tomorrow 23:00'}, until={'owner_replies':True})
    assert result.get('ok'), result
    text = receipt(result)
    w = engine.store.get_wake(result['wake']['id'])
    assert local(engine,w.next_due_at) in text and 'Muted' in text and 'Quiet' in text
    assert 'min apart' in text and 'late' in text and 'Expires' in text and 'Cancels if you reply.' in text
    engine.policy = replace(engine.policy, wake_spacing_minutes=0, quiet_hours='')
    assert 'min apart' not in receipt(create(engine))


def test_babysit_cap_and_builtin_are_preserved(engine):
    result = create(engine, {'kind':'self','every':'4h'}, babysit=True)
    assert result.get('error') == 'invalid_request', result
    assert 'maximum lifetime' in result['detail']
    builtin = arm(engine,'owner_silent',trigger_json={'builtin':True},expires_at=None)
    prepared = engine.tool.prepare(builtin,{},reschedule=False).wake
    assert prepared.expires_at is None


def test_update_already_due_one_shot_has_receipt(engine):
    result = create(engine, {'kind':'at','at':'in 1m'})
    assert result.get('ok'), result
    w = engine.store.get_wake(result['wake']['id'])
    engine.clock.advance(60)
    engine.tick()
    assert engine.store.get_wake(w.id).next_due_at is None
    updated = engine.tool.handle({'action':'update','id':w.id,'title':'Report'}, session_id='sid')
    assert updated.get('ok'), updated
    assert 'Already due' in receipt(updated)
    assert engine.store.get_wake(w.id).title == 'Report'


def test_docs_and_version():
    root = Path(__file__).resolve().parents[1]
    assert 'Results include a receipt line; tell the owner what it says, not a promise of your own.' in DESCRIPTION
    assert 'version: "0.1.14"' in (root/'plugin.yaml').read_text()
    skill = (root/'skills/heartbeat2-usage/SKILL.md').read_text()
    assert 'receipt' in skill and 'awaiting your OK' in skill
    readme = (root/'README.md').read_text()
    assert 'receipt' in readme
    assert 'A wake that could not fire before it expires is refused; an implicit lifetime stretches to fit, up to the maximum.' in readme
    changelog = (root/'CHANGELOG.md').read_text()
    assert '## 0.1.14 — 2026-10-14' in changelog
    assert '#56' in changelog.split('## 0.1.13')[0]
