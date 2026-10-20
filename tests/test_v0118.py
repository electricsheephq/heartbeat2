"""Inclusive owner-reminder due bounds and truthful owner-exemption receipts."""
from dataclasses import replace
from pathlib import Path

import pytest

from heartbeat2.model import Wake
from test_engine import engine, session, start, KEY
from test_v0113 import owner_turn, finish
from test_v0114 import command, create, receipt

DAY = 86400
EXEMPTION = 'Counts as your own reminder: not muted or capped.'


def reminder(e, command, path, days=365):
    if path == 'command':
        result = command(f'add in {days}d check the domain')
        assert not result.startswith('Error:'), result
        w, = e.store.list_wakes(KEY)
        assert EXEMPTION not in result
        return w
    owner_turn(e)
    result = create(e, {'kind':'at', 'at':f'in {days} days'}, owner_asked=True)
    assert result.get('ok'), result
    w = e.store.get_wake(result['wake']['id'])
    assert w.owner_requested
    return w


@pytest.mark.parametrize('path', ['command', 'agent'])
def test_year_reminder_fires_once_at_due(engine, command, path):
    w = reminder(engine, command, path)
    assert w.next_due_at == w.created_at+365*DAY
    assert w.expires_at == w.next_due_at+14*DAY
    finish(engine)
    engine.clock.advance(365*DAY)
    engine.tick()
    stored = engine.store.get_wake(w.id)
    assert stored.status != 'expired' and stored.status_reason != 'max_lifetime_changed'
    occurrence, = engine.store.list_occurrences(w.id)
    assert occurrence.state == 'claimed' and len(engine.ctx.calls) == 1
    engine.tick()
    assert len(engine.ctx.calls) == 1 and len(engine.store.list_occurrences(w.id)) == 1


@pytest.mark.parametrize('path', ['command', 'agent'])
def test_year_bound_refusal_and_equality(engine, command, path):
    if path == 'command':
        result = command('add in 366d check the domain')
        assert result.startswith('Error:'), result
        detail = result
    else:
        owner_turn(engine)
        result = create(engine, {'kind':'at', 'at':'in 366 days'}, owner_asked=True)
        assert result.get('error') == 'invalid_request', result
        assert 'receipt' not in result
        detail = result['detail']
    assert 'due after the maximum lifetime of 365 days (max_reminder_days setting)' in detail
    assert not engine.store.list_wakes(KEY)
    reminder(engine, command, path)


def test_autonomous_equality_stays_refused_and_reminder_span(engine):
    result = create(engine, {'kind':'at', 'at':'in 14d'})
    assert result.get('error') == 'invalid_request', result
    assert 'max_lifetime_days setting' in result['detail']
    result = create(engine, {'kind':'at', 'at':f'in {14*DAY-1}s'})
    assert result.get('ok'), result
    assert not result['wake']['owner_requested']
    w = Wake(KEY, 'Check.', 'at', creator='user')
    assert engine.policy.max_life_s(w) == 379*DAY
    assert engine.policy.due_bound_s(w) == 365*DAY


def test_raised_lifetime_uses_general_bound_for_all_wakes(engine):
    engine.policy = replace(engine.policy, max_lifetime_days=400)
    for changes in ({'creator':'user'}, {'owner_requested':True}, {}, {'kind':'every'}):
        w = Wake(KEY, 'Check.', 'at')
        w = replace(w, **changes)
        assert engine.policy.max_life_s(w) == 400*DAY
        assert engine.policy.lifetime_setting(w) == 'max_lifetime_days'
        assert engine.policy.due_bound_s(w) == 400*DAY
    owner_turn(engine)
    assert create(engine, {'kind':'at', 'at':'in 399d'}, owner_asked=True).get('ok')
    assert create(engine, {'kind':'at', 'at':'in 400d'}, owner_asked=True).get('error') == 'invalid_request'


@pytest.mark.parametrize('late,fired', [(3*3600, True), (3*DAY, False)])
def test_year_reminder_downtime(engine, command, late, fired):
    w = reminder(engine, command, 'agent')
    finish(engine)
    engine.clock.advance(365*DAY+late)
    engine.tick()
    assert len(engine.ctx.calls) == int(fired)
    occurrence, = engine.store.list_occurrences(w.id)
    assert occurrence.state == ('claimed' if fired else 'missed')
    assert engine.store.get_wake(w.id).status != 'expired'
    if not fired:
        assert 'missed' in engine.store.get_meta('notice:'+KEY)


def test_default_expiry_and_explicit_hard_end_clamp(engine):
    owner_turn(engine)
    short = create(engine, owner_asked=True)
    assert short.get('ok'), short
    assert short['wake']['expires_at'] == short['wake']['created_at']+14*DAY
    result = create(engine, {'kind':'at', 'at':'in 365 days'}, owner_asked=True,
                    until={'expires':'in 500d'})
    assert result.get('ok'), result
    assert result['wake']['expires_at'] == result['wake']['created_at']+379*DAY
    assert 'max_reminder' in result['clamped']['expires']


@pytest.mark.parametrize('action', ['create', 'update', 'resume'])
def test_granted_receipt_after_mutation(engine, action):
    owner_turn(engine)
    result = create(engine, owner_asked=True)
    assert result.get('ok'), result
    if action != 'create':
        wid = result['wake']['id']
        if action == 'resume':
            assert engine.tool.handle({'action':'pause', 'id':wid}, session_id='sid').get('ok')
        result = engine.tool.handle({'action':action, 'id':wid, 'owner_asked':True,
                                     **({'title':'Domain'} if action == 'update' else {})}, session_id='sid')
        assert result.get('ok'), result
    assert result['wake']['owner_requested']
    text = receipt(result)
    assert text.startswith('Fires once at ')
    assert text.split('. ')[1] == EXEMPTION[:-1]


def test_receipt_excludes_ignored_refused_user_proposed_and_paused(engine, command):
    ignored = create(engine, owner_asked=True)
    assert ignored.get('ok') and ignored['owner_asked_ignored'] == 'not_owner_turn'
    assert not ignored['wake']['owner_requested'] and EXEMPTION not in receipt(ignored)
    owner_turn(engine)
    recurring = create(engine, {'kind':'every', 'every':'1h'}, owner_asked=True)
    assert recurring.get('ok') and recurring['owner_asked_ignored'] == 'not_one_shot'
    assert not recurring['wake']['owner_requested'] and EXEMPTION not in receipt(recurring)
    refused = create(engine, {'kind':'at', 'at':'in 366d'}, owner_asked=True)
    assert refused.get('error') == 'invalid_request' and 'receipt' not in refused
    assert EXEMPTION not in command('add in 1h check the domain')
    engine.policy = replace(engine.policy, agent_can_create='ask_first')
    proposed = create(engine, owner_asked=True)
    assert proposed.get('ok') and proposed['wake']['owner_requested']
    assert proposed['wake']['status'] == 'proposed' and EXEMPTION not in receipt(proposed)
    engine.policy = replace(engine.policy, agent_can_create='yes')
    granted = create(engine, owner_asked=True)
    assert granted.get('ok'), granted
    wid = granted['wake']['id']
    assert engine.tool.handle({'action':'pause', 'id':wid}, session_id='sid').get('ok')
    paused = engine.tool.handle({'action':'update', 'id':wid, 'title':'Domain', 'owner_asked':True}, session_id='sid')
    assert paused.get('ok') and paused['wake']['status'] == 'paused'
    assert EXEMPTION not in receipt(paused)
    assert EXEMPTION in receipt(granted)


def test_worst_case_receipt_keeps_exemption(engine):
    owner_turn(engine)
    result = create(engine, owner_asked=True, until={'owner_replies':True})
    assert result.get('ok'), result
    w = engine.store.get_wake(result['wake']['id'])
    engine.policy = replace(engine.policy, quiet_hours='00:00-23:59')
    session(engine, last_wake_turn_at=w.next_due_at-60)
    text = engine.tool.receipt(w, engine.store.get_session(KEY))
    assert 'Wake spacing holds it until ' in text
    assert 'Quiet hours hold it until ' in text
    assert len(text) <= 240 and EXEMPTION in text
    assert text.split('. ')[1] == EXEMPTION[:-1]
    assert 'Wakes are at least ' not in text  # The lower-priority sentence is trimmed.


def test_release_metadata():
    root = Path(__file__).resolve().parents[1]
    assert 'version: "0.2.1"' in (root/'plugin.yaml').read_text()
    changelog = (root/'CHANGELOG.md').read_text()
    assert '## 0.2.1 — 2026-10-20' in changelog
    assert '## 0.2.0 — 2026-10-18' in changelog
    assert '## 0.1.18 — 2026-10-14' in changelog
    assert '## 0.1.17 — 2026-10-14' in changelog


@pytest.mark.parametrize('due', ['in 6h', 'in 30d'])
def test_babysit_owner_reminder_keeps_its_four_hour_bound(engine, due):
    # A babysit wake lives at most 4 h, so a later owner reminder must be refused, never stored to expire unfired.
    owner_turn(engine)
    result = create(engine, {'kind':'at','at':due}, owner_asked=True, babysit=True)
    stored = engine.store.list_wakes(KEY)
    assert not stored or all(w.expires_at > w.next_due_at for w in stored), (result, [(w.next_due_at, w.expires_at) for w in stored])


def test_due_bound_never_exceeds_the_hard_end(engine):
    base = Wake(KEY, 'Owner reminder', 'at', creator='agent', owner_requested=True)
    for wake in (base, replace(base, babysit=True), replace(base, creator='user'), replace(base, owner_requested=False)):
        assert engine.policy.due_bound_s(wake) <= engine.policy.max_life_s(wake), wake


@pytest.mark.parametrize('path', ['command', 'agent'])
def test_quiet_hours_at_the_due_bound_hold_into_the_grace(engine, command, path):
    # The due time 365 days on falls in quiet hours: the reminder is held into its grace, not refused.
    hour = int(engine.clock.now()//3600)%24
    engine.policy = replace(engine.policy, quiet_hours=f'{(hour-1)%24:02d}:00-{(hour+2)%24:02d}:00')
    w = reminder(engine, command, path)
    assert w.next_due_at == w.created_at+365*DAY
    assert w.next_due_at < w.expires_at <= w.created_at+379*DAY
    finish(engine)
    engine.clock.advance(365*DAY)
    engine.tick()
    assert not engine.ctx.calls
    engine.clock.advance(3*3600)
    engine.tick()
    assert len(engine.ctx.calls) == 1
    assert engine.store.get_wake(w.id).status != 'expired'


def test_lowered_reminder_bound_expires_a_reminder_not_yet_due(engine):
    engine.policy = replace(engine.policy, max_reminder_days=40)
    owner_turn(engine)
    late = create(engine, {'kind':'at', 'at':'in 30d'}, owner_asked=True)
    exact = create(engine, {'kind':'at', 'at':'in 20d'}, owner_asked=True)
    assert late.get('ok') and exact.get('ok'), (late, exact)
    finish(engine)
    engine.policy = replace(engine.policy, max_reminder_days=20)
    engine.clock.advance(20*DAY)
    engine.tick()
    stored = engine.store.get_wake(late['wake']['id'])
    assert (stored.status, stored.status_reason) == ('expired', 'max_lifetime_changed')
    assert engine.store.get_wake(exact['wake']['id']).status != 'expired'
    assert len(engine.ctx.calls) == 1
    start(engine); finish(engine)
    engine.clock.advance(10*DAY)
    engine.tick()
    assert len(engine.ctx.calls) == 1 and not engine.store.list_occurrences(late['wake']['id'])


def test_spacing_past_the_hard_end_is_refused(engine):
    # Held past the hard end, the stored expiry would precede the dispatch and the reminder would never fire.
    engine.policy = replace(engine.policy, max_reminder_days=1, max_lifetime_days=1, wake_spacing_minutes=3000)
    owner_turn(engine)
    session(engine, last_wake_turn_at=engine.clock.now())
    result = create(engine, {'kind':'at', 'at':'in 1d'}, owner_asked=True)
    assert result.get('error') == 'invalid_request', result
    assert 'held past the maximum lifetime of 2 days' in result['detail']
    assert not engine.store.list_wakes(KEY)
