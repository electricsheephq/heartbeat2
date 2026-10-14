"""Creation validation, first checks, and bounded owner one-time reminders."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from heartbeat2.model import Wake
from heartbeat2.policy import Policy
from heartbeat2.tool import PARAMETERS, DESCRIPTION
from test_engine import engine, session, KEY, arm
from test_v0113 import owner_turn, finish
from test_v0114 import command, create, local, receipt

DAY = 86400
MODES = {'change', 'success', 'failure', 'json'}


def test_fire_on_schema():
    assert set(PARAMETERS['properties']['trigger']['properties']['fire_on']['enum']) == MODES


@pytest.mark.parametrize('mode', ['nonzero', '', 'FAILURE', None, 3, []])
@pytest.mark.parametrize('action', ['create', 'update'])
def test_invalid_fire_on_refused_without_mutation(engine, mode, action):
    trigger = {'kind':'command', 'command':['git','status'], 'fire_on':mode}
    before = arm(engine, 'command', trigger_json={'command':['git','status']}) if action == 'update' else None
    result = create(engine, trigger) if action == 'create' else engine.tool.handle(
        {'action':'update', 'id':before.id, 'trigger':trigger}, session_id='sid')
    assert result.get('error') == 'invalid_request', result
    assert result['detail'] == (f"invalid fire_on '{mode}': use failure (alert when the command exits nonzero), "
                                'success, change, or json')
    assert engine.store.get_wake(before.id) == before if before else not engine.store.list_wakes(KEY)


@pytest.mark.parametrize('mode', sorted(MODES)+[None])
def test_valid_command_modes_and_default(engine, mode):
    trigger = {'kind':'command', 'command':['git','status']}
    if mode is not None: trigger['fire_on'] = mode
    assert create(engine, trigger).get('ok')


def test_stray_fire_on_ignored(engine):
    assert create(engine, {'kind':'at', 'at':'in 1h', 'fire_on':'nonzero'}).get('ok')


def test_startup_sweep_all_statuses_one_notice_per_chat_idempotent(engine, monkeypatch):
    bad = [arm(engine, 'command', status=status, trigger_json={'command':['git','status'], 'fire_on':'nonzero'})
           for status in ('active', 'paused', 'proposed')]
    other = engine.store.create_wake(Wake('other', 'Check.', 'command', trigger_json={'fire_on':None}))
    good = [arm(engine, 'command', trigger_json={'fire_on':mode}) for mode in sorted(MODES)]
    good += [arm(engine, 'command'), arm(engine, trigger_json={'fire_on':'nonzero'}),
             arm(engine, 'command', status='done', trigger_json={'fire_on':'nonzero'})]
    for w in bad:
        for state in ('pending', 'claimed', 'started'):
            engine.store.add_occurrence(w.id, state)
            if state != 'pending':
                assert engine.store.claim(w.id, state, epoch=engine.epoch, pid=1)
            if state == 'started': assert engine.store.start(w.id, state, attempt=1)
    # Restart handling owns interruption; isolate the sweep's preserve-started rule.
    monkeypatch.setattr(engine, 'interrupt', lambda *args: None)
    engine.reconcile()
    for w in bad+[other]:
        stored = engine.store.get_wake(w.id)
        assert (stored.status, stored.status_reason) == ('failed', 'invalid_fire_on')
    for w in bad:
        assert [(o.dedupe_key, o.state) for o in engine.store.list_occurrences(w.id)] == [
            ('pending', 'superseded'), ('claimed', 'superseded'), ('started', 'started')]
    assert all(engine.store.get_wake(w.id) == w for w in good)
    notices = engine.store.get_meta('notice:'+KEY)
    assert len(json.loads(notices)) == len(json.loads(engine.store.get_meta('notice:other'))) == 1
    assert 'fire_on' in notices and 'stopped' in notices
    engine.process_start = True
    engine.reconcile()
    assert engine.store.get_meta('notice:'+KEY) == notices


@pytest.mark.parametrize('delay', [0, 1, 60, 3600])
@pytest.mark.parametrize('kind', ['command', 'file', 'url', 'process'])
def test_probe_receipt_first_check(engine, delay, kind):
    w = arm(engine, kind, next_due_at=engine.clock.now()+delay,
            trigger_json={'check_every':300}, deadline_at=engine.clock.now()+7200)
    result = {'receipt':engine.tool.receipt(w, engine.store.get_session(KEY))}
    lead = 'Checks now' if delay <= 0 else f'First check {local(engine,w.next_due_at)}'
    assert receipt(result).startswith(lead+', then every 5m;')


@pytest.mark.parametrize('trigger,detail', [
    ({'kind':'command','command':['git','status'],'cwd':'missing'}, 'command cwd must be a directory'),
    ({'kind':'command','command':['git','status'],'cwd':3}, 'command cwd must be a directory'),
    ({'kind':'command','command':['rm','file']}, 'command probe is not allowlisted'),
    ({'kind':'file','path':'.'}, 'file probe requires a regular file'),
    ({'kind':'file','path':'missing','pattern':'['}, 'invalid pattern'),
    ({'kind':'url','url':'file:///report'}, 'URL requires http or https'),
    ({'kind':'process','pid':0}, 'process requires a positive pid'),
])
def test_static_probe_rejections_at_create(engine, trigger, detail):
    result = create(engine, trigger)
    assert result.get('error') == 'invalid_request', result
    assert detail in result['detail']
    assert not engine.store.list_wakes(KEY)


def test_missing_file_and_relative_command_directory_supported(engine):
    assert create(engine, {'kind':'file','path':'future.log'}).get('ok')
    assert create(engine, {'kind':'command','command':['git','status'],'cwd':'.'}).get('ok')


def test_owner_requested_thirty_days_receipt_and_poll(engine):
    owner_turn(engine)
    result = create(engine, {'kind':'at','at':'in 30d'}, owner_asked=True)
    assert result.get('ok'), result
    w = engine.store.get_wake(result['wake']['id'])
    assert w.owner_requested and w.next_due_at == w.created_at+30*DAY
    assert w.expires_at == w.next_due_at+14*DAY  # A short reminder's grace, not the 365-day bound.
    assert f'Fires once at {local(engine,w.next_due_at)}.' in receipt(result)
    assert f'Expires {local(engine,w.expires_at)}.' in receipt(result)
    finish(engine)
    engine.clock.advance(14*DAY)
    engine.times()
    assert engine.store.get_wake(w.id).status == 'active'
    engine.clock.advance(16*DAY)
    engine.tick()
    assert len(engine.ctx.calls) == 1
    assert engine.store.list_occurrences(w.id)[0].state == 'claimed'


def test_agent_thirty_days_still_refused(engine):
    result = create(engine, {'kind':'at','at':'in 30d'})
    assert result.get('error') == 'invalid_request', result
    assert 'maximum lifetime of 14 days' in result['detail'] and 'max_lifetime_days' in result['detail']
    assert not engine.store.list_wakes(KEY)


def test_command_thirty_days_accepted(engine, command):
    result = command('add in 30d Renew the domain.')
    assert not result.startswith('Error:'), result
    w, = engine.store.list_wakes(KEY)
    assert w.creator == 'user' and w.next_due_at == w.created_at+30*DAY
    assert w.expires_at == w.next_due_at+14*DAY
    assert engine.tool.receipt(w, engine.store.get_session(KEY)) in result


@pytest.mark.parametrize('value', [0, -1, 3651, True, 1.5, float('inf'), 'bad'])
def test_reminder_setting_invalid(value):
    with pytest.raises((ValueError, TypeError), match='max_reminder_days|could not convert'):
        Policy.from_settings({'max_reminder_days':value})


@pytest.mark.parametrize('value', [1, 365, 3650, '30'])
def test_reminder_setting_bounds(value):
    assert Policy.from_settings({'max_reminder_days':value}).max_reminder_days == int(value)


@pytest.mark.parametrize('changes,days', [
    ({'creator':'user'}, 379), ({'owner_requested':True}, 379), ({}, 14),
    ({'owner_requested':True,'parent_wake_id':'parent'}, 14),
    ({'creator':'user','agent_edited':True}, 14),
    ({'owner_requested':True,'babysit':True}, 1/6),
    ({'owner_requested':True,'kind':'every'}, 14),
])
def test_lifetime_helper_owner_one_shots_only(engine, changes, days):
    w = Wake(KEY, 'Check.', 'at', **{k:v for k,v in changes.items() if k != 'kind'})
    w = replace(w, kind=changes.get('kind','at'))
    assert engine.policy.max_life_s(w) == days*DAY


def test_reminder_bound_never_below_general_lifetime(engine):
    engine.policy = replace(engine.policy, max_lifetime_days=400)
    w = Wake(KEY, 'Check.', 'at', creator='user')
    assert engine.policy.max_life_s(w) == 400*DAY and engine.policy.lifetime_setting(w) == 'max_lifetime_days'
    owner_turn(engine)
    result = create(engine, {'kind':'at','at':'in 380d'}, owner_asked=True)
    assert result.get('ok'), result
    w = engine.store.get_wake(result['wake']['id'])
    engine.clock.advance(370*DAY)
    engine.times()
    assert engine.store.get_wake(w.id).status == 'active'


def test_owner_expiry_clamp_label_and_lowered_setting(engine):
    engine.policy = Policy.from_settings({'max_reminder_days':40}, clock=engine.clock)
    engine.policy = replace(engine.policy, quiet_hours='', timezone='UTC')
    owner_turn(engine)
    result = create(engine, {'kind':'at','at':'in 30d'}, owner_asked=True, until={'expires':'in 60d'})
    assert result.get('ok'), result
    w = engine.store.get_wake(result['wake']['id'])
    assert w.expires_at == w.created_at+54*DAY and 'max_reminder' in result['clamped']['expires']
    engine.policy = replace(engine.policy, max_reminder_days=20)
    engine.clock.advance(34*DAY)
    engine.times()
    assert engine.store.get_wake(w.id).status_reason == 'max_lifetime_changed'


@pytest.mark.parametrize('path,action', [('requested','update'), ('requested','resume'), ('command','update')])
def test_long_reminder_edit_revocation_refused_atomically(engine, command, path, action):
    if path == 'requested':
        owner_turn(engine)
        result = create(engine, {'kind':'at','at':'in 30d'}, owner_asked=True)
        assert result.get('ok'), result
    else: assert not command('add in 30d Renew the domain.').startswith('Error:')
    w, = engine.store.list_wakes(KEY)
    if action == 'resume':
        assert engine.tool.handle({'action':'pause','id':w.id}, session_id='sid').get('ok')
    before = engine.store.get_wake(w.id)
    result = engine.tool.handle({'action':action,'id':w.id, **({'note':'Different task.'} if action == 'update' else {})}, session_id='sid')
    assert result.get('error') == 'invalid_request', result
    assert 'max_lifetime_days' in result['detail']
    assert engine.store.get_wake(w.id) == before


def test_short_reminder_keeps_default_expiry_through_revocation(engine, command):
    assert not command('add in 2h Check the deploy.').startswith('Error:')
    w, = engine.store.list_wakes(KEY)
    assert w.expires_at == w.created_at+14*DAY
    owner_turn(engine)
    result = create(engine, owner_asked=True)
    assert result.get('ok'), result
    assert result['wake']['expires_at'] == result['wake']['created_at']+14*DAY
    assert 'max_lifetime' in result['clamped']['expires']
    result = engine.tool.handle({'action':'update','id':result['wake']['id'],'note':'Autonomous follow-up.'}, session_id='sid')
    assert result.get('ok'), result
    assert not result['wake']['owner_requested']
    assert result['wake']['expires_at'] == result['wake']['created_at']+14*DAY


@pytest.mark.parametrize('late,fired', [(3*3600, True), (3*DAY, False)])
def test_long_reminder_downtime_catches_up_or_reports_missed(engine, late, fired):
    owner_turn(engine)
    result = create(engine, {'kind':'at','at':'in 30d'}, owner_asked=True)
    assert result.get('ok'), result
    w = engine.store.get_wake(result['wake']['id'])
    finish(engine)
    engine.clock.advance(w.next_due_at-engine.clock.now()+late)  # The gateway was down across the due time.
    engine.tick()
    assert len(engine.ctx.calls) == int(fired)
    if not fired:  # Past catch_up_hours: reported as missed, never silently expired.
        assert engine.store.list_occurrences(w.id)[0].state == 'missed'
        assert 'missed' in engine.store.get_meta('notice:'+KEY)


def test_long_reminder_grace_bounded_by_hard_end(engine):
    owner_turn(engine)
    result = create(engine, {'kind':'at','at':'in 360d'}, owner_asked=True)
    assert result.get('ok'), result
    assert result['wake']['expires_at'] == result['wake']['next_due_at']+14*DAY


def test_owner_refusal_names_reminder_setting(engine):
    owner_turn(engine)
    result = create(engine, {'kind':'at','at':'in 366d'}, owner_asked=True)
    assert result.get('error') == 'invalid_request', result
    assert 'maximum lifetime of 365 days' in result['detail'] and 'max_reminder_days' in result['detail']
    assert not engine.store.list_wakes(KEY)


def test_no_chaining_and_release_metadata():
    import yaml
    root = Path(__file__).resolve().parents[1]
    for text in (DESCRIPTION, (root/'skills/heartbeat2-usage/SKILL.md').read_text()):
        assert 'When a create is refused' in text
        assert 'never arm a stand-in' in text.lower() and 're-arms' in text
    assert '## 0.1.17 — 2026-10-14' in (root/'CHANGELOG.md').read_text()
    assert '`max_reminder_days`' in (root/'README.md').read_text()
    setting = yaml.safe_load((root/'plugin.yaml').read_text())['config_schema']['max_reminder_days']
    assert setting['type'] == 'int' and setting['default'] == 365


@pytest.mark.parametrize('owner_asked,until,error', [
    (True, {'expires':'in 31d'}, None),
    (True, {}, 'set until.expires to extend it'),
    (False, {'expires':'in 31d'}, 'max_lifetime_days setting'),
])
def test_owner_retime_of_agent_reminder_gets_reminder_bound(engine, owner_asked, until, error):
    result = create(engine)
    assert result.get('ok'), result
    w = engine.store.get_wake(result['wake']['id'])
    assert not w.owner_requested
    owner_turn(engine)
    result = engine.tool.handle({'action':'update','id':w.id,'trigger':{'kind':'at','at':'in 30d'},
                                 'owner_asked':owner_asked,'until':until}, session_id='sid')
    stored = engine.store.get_wake(w.id)
    if error:
        assert result.get('error') == 'invalid_request' and error in result['detail'], result
        assert stored == w
    else:
        assert result.get('ok'), result
        assert stored.owner_requested and stored.next_due_at == engine.clock.now()+30*DAY


def test_unknown_home_in_command_cwd_is_a_request_error(engine):
    result = create(engine, {'kind':'command','command':['git','status'],'cwd':'~no-such-user-hb2/repo'})
    assert result.get('error') == 'invalid_request', result
    assert result['detail'] == 'command cwd must be a directory'
    assert not engine.store.list_wakes(KEY)
