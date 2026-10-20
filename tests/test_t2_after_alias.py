"""Relative one-shot aliases preserve scheduling and proposal semantics."""
from copy import deepcopy
from dataclasses import replace

import pytest

from heartbeat2.tool import DESCRIPTION
from test_engine import engine
from test_v0113 import owner_turn
from test_v0114 import create, receipt


NOT_SET = ('If a result is proposed (armed=false and its receipt names a /pulse ok command), nothing is set yet: '
           'say so first and ask the owner to send that command; never call it set, done or scheduled. ')


@pytest.mark.parametrize('requested', [False, True])
def test_create_after_matches_literal_at_without_mutating_args(engine, requested):
    owner_turn(engine)
    args = {'action':'create', 'note':'Send the file.',
            'trigger':{'kind':'after','after':'10m'}, 'owner_asked':requested}
    original = deepcopy(args)
    result = engine.tool.handle(args, session_id='sid')
    assert args == original
    assert result.get('ok') and result['armed'] is True, result
    wake = engine.store.get_wake(result['wake']['id'])
    literal = create(engine, {'kind':'at','at':'in 10m'}, owner_asked=requested)
    assert literal.get('ok') and literal['armed'] is True, literal
    assert wake.kind == 'at' and wake.status == 'active'
    assert wake.trigger_json == {'kind':'at','at':'in 10m'}
    assert wake.next_due_at == pytest.approx(engine.clock.now()+600)
    assert wake.next_due_at == literal['wake']['next_due_at']
    assert wake.owner_requested is literal['wake']['owner_requested'] is requested


@pytest.mark.parametrize('value', ['in 10m', '+10m', 'IN 10m'])
def test_relative_prefix_is_preserved(engine, value):
    result = create(engine, {'kind':'after','after':value})
    assert result.get('ok') and result['armed'] is True, result
    assert result['wake']['trigger_json'] == {'kind':'at','at':value}
    assert result['wake']['next_due_at'] == pytest.approx(engine.clock.now()+600)


@pytest.mark.parametrize('value,stored', [(' +10m', '+10m'), (' in 10m ', 'in 10m'), (' 10m', 'in 10m')])
def test_alias_strips_whitespace_before_the_prefix_check(engine, value, stored):
    result = create(engine, {'kind':'after','after':value})
    assert result.get('ok') and result['armed'] is True, result
    assert result['wake']['trigger_json'] == {'kind':'at','at':stored}
    assert result['wake']['next_due_at'] == pytest.approx(engine.clock.now()+600)


def test_after_proposal_matches_literal_at(engine):
    engine.policy = replace(engine.policy, agent_can_create='ask_first')
    for trigger in ({'kind':'after','after':'10m'}, {'kind':'at','at':'in 10m'}):
        result = create(engine, trigger)
        assert result.get('ok') and result['armed'] is False, result
        assert result['wake']['status'] == 'proposed'
        assert result['wake']['kind'] == 'at'
        assert result['wake']['trigger_json'] == {'kind':'at','at':'in 10m'}
        assert receipt(result).startswith('Not set yet:')
        assert f'/pulse ok {result["wake"]["id"]}' in receipt(result)


@pytest.mark.parametrize('trigger', [
    {'kind':'after'},
    {'kind':'after','after':''},
    {'kind':'after','after':'   '},
    {'kind':'after','after':None},
    {'kind':'after','after':600},
    {'kind':'after','after':'10m','every':'5m'},
])
@pytest.mark.parametrize('action', ['create', 'update'])
def test_other_after_shapes_still_return_unknown_kind(engine, trigger, action):
    args = {'action':action, 'note':'Send the file.', 'trigger':trigger}
    if action == 'update':
        existing = create(engine)
        assert existing.get('ok'), existing
        args['id'] = existing['wake']['id']
    original = deepcopy(args)
    result = engine.tool.handle(args, session_id='sid')
    assert result.get('error') == 'unknown_kind', result
    assert args == original


def test_owner_silent_after_is_unchanged(engine):
    trigger = {'kind':'owner_silent','after':'90m'}
    result = create(engine, trigger)
    assert result.get('ok'), result
    assert result['wake']['kind'] == 'owner_silent'
    assert result['wake']['trigger_json'] == trigger
    assert receipt(result).startswith('Waits for 90m of owner silence.')


@pytest.mark.parametrize('requested', [False, True])
def test_update_after_reschedules_like_literal_at(engine, requested):
    owner_turn(engine)
    aliased = create(engine)
    literal = create(engine)
    assert aliased.get('ok') and literal.get('ok'), (aliased, literal)
    engine.clock.advance(60)
    args = {'action':'update', 'id':aliased['wake']['id'],
            'trigger':{'kind':'after','after':'5m'}, 'owner_asked':requested}
    original = deepcopy(args)
    result = engine.tool.handle(args, session_id='sid')
    assert args == original
    assert result.get('ok') and result['armed'] is True, result
    control = engine.tool.handle({**args, 'id':literal['wake']['id'],
                                  'trigger':{'kind':'at','at':'in 5m'}}, session_id='sid')
    assert control.get('ok'), control
    wake = engine.store.get_wake(aliased['wake']['id'])
    assert wake.kind == 'at' and wake.trigger_json == {'kind':'at','at':'in 5m'}
    assert wake.next_due_at == pytest.approx(engine.clock.now()+300)
    assert wake.next_due_at == control['wake']['next_due_at']
    assert wake.owner_requested is control['wake']['owner_requested'] is requested


def test_description_leads_with_not_set_instruction_once():
    lead = 'Results include a receipt line; tell the owner what it says, not a promise of your own. '
    assert DESCRIPTION.count(NOT_SET) == 1
    assert lead + NOT_SET in DESCRIPTION


@pytest.mark.parametrize('requested', [False, True])
def test_at_with_after_matches_literal_at(engine, requested):
    # #106: the model's first call in round-6 C-reply rep 1.
    owner_turn(engine)
    args = {'action':'create', 'note':'Water the basil.',
            'trigger':{'kind':'at','after':'20m'}, 'owner_asked':requested}
    original = deepcopy(args)
    result = engine.tool.handle(args, session_id='sid')
    assert args == original
    assert result.get('ok') and result['armed'] is True, result
    literal = create(engine, {'kind':'at','at':'in 20m'}, owner_asked=requested)
    assert literal.get('ok'), literal
    wake = engine.store.get_wake(result['wake']['id'])
    assert wake.kind == 'at' and wake.trigger_json == {'kind':'at','at':'in 20m'}
    assert wake.next_due_at == pytest.approx(engine.clock.now()+1200)
    assert wake.next_due_at == literal['wake']['next_due_at']
    assert wake.owner_requested is literal['wake']['owner_requested'] is requested


@pytest.mark.parametrize('value,stored', [('20m', 'in 20m'), (' +20m', '+20m'), ('in 20m ', 'in 20m')])
def test_at_with_after_keeps_relative_prefix(engine, value, stored):
    result = create(engine, {'kind':'at','after':value})
    assert result.get('ok') and result['armed'] is True, result
    assert result['wake']['trigger_json'] == {'kind':'at','at':stored}


def test_update_at_with_after_reschedules_like_literal_at(engine):
    existing = create(engine)
    assert existing.get('ok'), existing
    engine.clock.advance(60)
    result = engine.tool.handle({'action':'update', 'id':existing['wake']['id'],
                                 'trigger':{'kind':'at','after':'5m'}}, session_id='sid')
    assert result.get('ok'), result
    wake = engine.store.get_wake(existing['wake']['id'])
    assert wake.trigger_json == {'kind':'at','at':'in 5m'}
    assert wake.next_due_at == pytest.approx(engine.clock.now()+300)


@pytest.mark.parametrize('trigger', [{'kind':'at'}, {'kind':'at','after':600}])
def test_at_without_a_time_names_the_accepted_form(engine, trigger):
    result = create(engine, trigger)
    assert result.get('error') == 'invalid_request', result
    assert '{"kind": "at", "at": "in 20m"}' in result['detail']


def test_at_with_both_at_and_after_is_not_rewritten(engine):
    trigger = {'kind':'at','at':'in 5m','after':'20m'}
    result = create(engine, trigger)
    assert result.get('ok'), result
    assert result['wake']['trigger_json'] == trigger
    assert result['wake']['next_due_at'] == pytest.approx(engine.clock.now()+300)
