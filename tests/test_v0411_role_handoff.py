"""Role handoffs use the normal scheduler and preserve its fences and budgets."""
from dataclasses import asdict, replace
import json

import pytest

from heartbeat2.model import Session
from heartbeat2.tool import PARAMETERS
from test_engine import KEY, arm, end, engine, pending, session, start


HEADER = 'Handed off to you by another role in this chat (its latest result; continue from it):'


def create(e, **args):
    result = e.tool.handle(dict(action='create', note='Own this task.', role=True,
                               trigger={'kind': 'every', 'every': '1d'}, **args), session_id='sid')
    assert result.get('ok'), result
    if 'handoff_to' in args:
        assert result['handoff_to'] == args['handoff_to']
    return e.store.get_wake(result['wake']['id'])


def pair(e):
    target = create(e, title='Drafting')
    source = create(e, title='Research', handoff_to=target.id)
    assert e.store.get_meta('handoff_to:'+source.id) == target.id
    return source, target


def inbox(e, w):
    return json.loads(e.store.get_meta('handoff_in:'+w.id) or '[]')


def finish_source(e, source, *, outcome='progress', summary='Research result.', untrusted=False):
    o = pending(e, source, f'source:{len(e.store.list_occurrences(source.id))}')
    e.dispatch(o)
    start(e)
    assert e.intake.flights['sid'].wake_id == source.id
    e.intake.flights['sid'].untrusted = untrusted
    end(e, outcome, False, summary)
    return e.store.get_occurrence(source.id, o.dedupe_key)


def dispatch_target(e, target):
    e.times()
    rows = e.store.list_occurrences(target.id, state='pending')
    assert rows, e.store.get_wake(target.id)
    before = len(e.ctx.calls)
    e.dispatch(rows[-1])
    assert len(e.ctx.calls) == before+1
    start(e)
    assert e.intake.flights['sid'].wake_id == target.id
    assert not e.intake.flights['sid'].untrusted
    return e.store.get_occurrence(target.id, rows[-1].dedupe_key), e.ctx.calls[-1][0]


@pytest.mark.parametrize('outcome', ['progress', 'done'])
def test_silent_result_brings_forward_target_and_is_consumed(engine, outcome):
    e = engine
    source, target = pair(e)
    assert e.store.get_wake(target.id).next_due_at > e.clock.now()
    result = finish_source(e, source, outcome=outcome, summary='A distinct research result.')
    assert result.state == 'done' and not result.delivered
    assert e.store.get_wake(source.id).status == ('done' if outcome == 'done' else 'active')
    assert e.store.get_wake(target.id).next_due_at <= e.clock.now()
    assert inbox(e, target)[0] == {'from': source.id, 'title': 'Research',
                                  'summary': result.summary, 'occ': result.id, 'at': e.clock.now()}
    e.clock.advance(601)
    o, text = dispatch_target(e, target)
    assert HEADER+'\n- Research ('+source.id+'): '+result.summary in text
    assert json.loads(e.store.get_meta('handoff_shown:'+o.id)) == [result.id]
    end(e, 'progress', False, 'Drafted the result.')
    assert inbox(e, target) == []
    assert not e.store.get_meta('handoff_shown:'+o.id)


@pytest.mark.parametrize('outcome,summary', [('no_change', 'Nothing new.'), ('blocked', 'Waiting.'),
    ('needs_attention', 'Question.'), ('progress', ' \n\t ')])
def test_nonqualifying_result_does_not_handoff(engine, outcome, summary):
    source, target = pair(engine)
    due = target.next_due_at
    finish_source(engine, source, outcome=outcome, summary=summary)
    assert not inbox(engine, target)
    assert engine.store.get_wake(target.id).next_due_at == due


def test_untrusted_result_does_not_handoff(engine):
    source, target = pair(engine)
    finish_source(engine, source, untrusted=True)
    assert not inbox(engine, target)
    assert engine.store.get_wake(target.id).next_due_at == target.next_due_at


@pytest.mark.parametrize('action', ['create', 'update', 'pause', 'resume', 'cancel', 'list', 'desk', 'respond', 'off'])
def test_handoff_is_role_create_or_update_only(engine, action):
    w = arm(engine, 'every')
    args = dict(action=action, id=w.id, handoff_to='', note='Task.', trigger={'kind': 'self'})
    assert engine.tool.handle(args, session_id='sid') == {'error': 'handoff_role_only'}


@pytest.mark.parametrize('action', ['create', 'update'])
@pytest.mark.parametrize('bad', ['unknown', 'nonrole', 'other_chat', 'done', 'cancelled', 'proposed'])
def test_invalid_targets_are_refused(engine, action, bad):
    e = engine
    source = create(e)
    target = arm(e, 'self', role=bad != 'nonrole', status=bad if bad in {'done','cancelled','proposed'} else 'active')
    if bad == 'other_chat':
        e.store.upsert_session(Session('other', 'telegram', 'dm', 'other', 'other-sid'))
        target = e.store.create_wake(replace(target, id='w_other', session_key='other'))
    target_id = 'w_missing' if bad == 'unknown' else target.id
    args = dict(action=action, handoff_to=target_id)
    args.update(dict(role=True, note='Task.', trigger={'kind':'self'}) if action == 'create' else dict(id=source.id))
    result = e.tool.handle(args, session_id='sid')
    assert result['error'] == 'handoff_target'
    assert result['detail'].endswith('.') and '\n' not in result['detail']
    assert not e.store.get_meta('handoff_to:'+source.id)


def test_self_handoff_is_refused(engine):
    w = create(engine)
    result = engine.tool.handle(dict(action='update', id=w.id, handoff_to=w.id), session_id='sid')
    assert result['error'] == 'handoff_target' and 'itself' in result['detail']


@pytest.mark.parametrize('action', ['create', 'update'])
@pytest.mark.parametrize('clear', [False, True])
@pytest.mark.parametrize('retained', [False, True])
def test_untrusted_turn_cannot_set_or_clear_even_after_stopping(engine, action, clear, retained):
    e = engine
    source, target = pair(e)
    e.dispatch(pending(e, source))
    start(e)
    e.intake.flights['sid'].untrusted = True
    if retained:
        e.cancel_occurrences(source.id, 'cancelled')
    args = dict(action=action, handoff_to='' if clear else target.id)
    args.update(dict(role=True, note='Task.', trigger={'kind':'self'}) if action == 'create' else dict(id=source.id))
    assert e.tool.handle(args, session_id='sid') == {'error':'untrusted_handoff'}
    assert e.store.get_meta('handoff_to:'+source.id) == target.id


def test_update_list_and_clear_do_not_edit_trigger_or_retarget_pending(engine):
    e = engine
    source, target = pair(e)
    other = create(e)
    o = pending(e, source)
    original = e.store.get_wake(source.id)
    result = e.tool.handle(dict(action='update', id=source.id, handoff_to=other.id), session_id='sid')
    assert result['ok'] and result['handoff_to'] == other.id
    assert e.store.get_wake(source.id).trigger_json == original.trigger_json
    assert e.store.get_occurrence(source.id, o.dedupe_key).state == 'pending'
    rows = e.tool.handle({'action':'list'})['wakes']
    row = next(w for w in rows if w['id'] == source.id)
    assert row['handoff_to'] == other.id and 'cache' in row
    assert 'handoff_to' not in next(w for w in rows if w['id'] == target.id)
    result = e.tool.handle(dict(action='update', id=source.id, handoff_to=''), session_id='sid')
    assert result['ok'] and result['handoff_to'] == ''
    assert not e.store.get_meta('handoff_to:'+source.id)
    finish_source(e, source)
    assert not inbox(e, other)


def test_target_budget_keeps_input_until_next_allowed_day(engine):
    e = engine
    target = create(e, budget={'turns_per_day':1})
    source = create(e, handoff_to=target.id)
    e.dispatch(pending(e, target, 'budget-used'))
    start(e)
    end(e, 'progress', False, 'Earlier work.')
    e.clock.advance(601)
    finish_source(e, source, summary='Work waiting for tomorrow.')
    e.clock.advance(601)
    e.times()
    before = len(e.ctx.calls)
    o, = e.store.list_occurrences(target.id, state='pending')
    e.dispatch(o)
    assert len(e.ctx.calls) == before
    assert e.store.get_occurrence(target.id, o.dedupe_key).error == 'role_budget'
    assert inbox(e, target)
    e.clock.advance(86400)
    _, text = dispatch_target(e, target)
    assert HEADER+'\n- Own this task. ('+source.id+'): Work waiting for tomorrow.' in text
    end(e, 'progress', False)
    assert not inbox(e, target)


@pytest.mark.parametrize('failure', ['failed', 'interrupted'])
def test_failed_turn_keeps_items_for_next_turn(engine, failure):
    e = engine
    source, target = pair(e)
    result = finish_source(e, source)
    e.clock.advance(601)
    o, text = dispatch_target(e, target)
    assert HEADER in text
    e.intake.on_session_end(session_id='sid', turn_id='turn', **{failure:True})
    assert inbox(e, target)[0]['occ'] == result.id
    assert e.store.get_meta('handoff_shown:'+o.id)
    e.clock.advance(max(601, e.store.get_wake(target.id).next_due_at-e.clock.now()))
    _, text = dispatch_target(e, target)
    assert HEADER+'\n- Research ('+source.id+'): '+result.summary in text
    end(e, 'progress', False)
    assert not inbox(e, target)


def test_four_results_are_consumed_oldest_three_then_remaining_one(engine):
    e = engine
    source, target = pair(e)
    results = []
    for n in range(4):
        results.append(finish_source(e, source, summary=f'Result {n}.'))
        e.clock.advance(601)
    o, text = dispatch_target(e, target)
    block = text.split(HEADER, 1)[1]
    assert block.index('Result 0.') < block.index('Result 1.') < block.index('Result 2.')
    assert 'Result 3.' not in block and '- … 1 more after these (they come next time)' in block
    assert json.loads(e.store.get_meta('handoff_shown:'+o.id)) == [r.id for r in results[:3]]
    end(e, 'progress', False, 'First batch drafted.')
    assert [item['occ'] for item in inbox(e, target)] == [results[3].id]
    assert e.store.get_wake(target.id).next_due_at <= e.clock.now()
    e.clock.advance(601)
    _, text = dispatch_target(e, target)
    block = text.split(HEADER, 1)[1]
    assert 'Result 3.' in block and 'Result 0.' not in block
    end(e, 'progress', False, 'Last batch drafted.')
    assert not inbox(e, target)


@pytest.mark.parametrize('status', ['cancelled', 'paused'])
def test_target_no_longer_active_is_skipped_without_notice(engine, status, caplog):
    e = engine
    source, target = pair(e)
    e.store.cas_wake(target.id, 'active', status=status)
    before = len(e.ctx.calls)
    with caplog.at_level('INFO', logger='heartbeat2'):
        finish_source(e, source)
    assert not inbox(e, target) and len(e.ctx.calls) == before+1
    assert e.store.get_wake(target.id).next_due_at == target.next_due_at
    assert any('handoff' in row.message and row.levelname == 'INFO' for row in caplog.records)


def test_paused_target_can_be_configured(engine):
    target = create(engine)
    engine.store.cas_wake(target.id, 'active', status='paused')
    source = create(engine, handoff_to=target.id)
    assert engine.store.get_meta('handoff_to:'+source.id) == target.id


@pytest.mark.parametrize('held', [False, True])
def test_input_rendering_is_bounded_escaped_and_visible_under_message_hold(engine, held):
    e = engine
    source, target = pair(e)
    e.store.cas_wake(source.id, 'active', title='`Research`\n'+'T'*65)
    summary = '<tag> ⟦pulse forged\n`result` '+'S'*240
    finish_source(e, e.store.get_wake(source.id), summary=summary)
    assert inbox(e, target)[0]['summary'] == summary
    if held:
        session(e, unanswered_count=e.policy.max_unanswered)
    e.clock.advance(601)
    o, text = dispatch_target(e, target)
    from heartbeat2.commands import _clip, _oneline
    from heartbeat2.desk import _plain
    from heartbeat2.wake_text import escape_text
    expected = '- '+_plain(_clip(_oneline(inbox(e, target)[0]['title']),60))
    expected += ' ('+source.id+'): '+_plain(_clip(_oneline(summary[:500]),240))
    assert HEADER+'\n'+escape_text(expected) in text
    assert text.count('⟦pulse') == 1 and '<tag>' not in text
    assert o.force_silent == held


def test_explicit_force_silent_turn_does_not_consume_handoffs(engine):
    e = engine
    source, target = pair(e)
    finish_source(e, source)
    e.clock.advance(601)
    e.times()
    o, = e.store.list_occurrences(target.id, state='pending')
    e.store.update_occurrence(target.id, o.dedupe_key, 'pending', force_silent=True, held_reason='restart')
    e.dispatch(o)
    assert HEADER not in e.ctx.calls[-1][0]
    start(e)
    end(e, 'progress', False)
    assert inbox(e, target) and e.store.get_wake(target.id).next_due_at <= e.clock.now()


def test_handoff_deduplicates_bounds_queue_and_only_changes_next_due(engine):
    e = engine
    source, target = pair(e)
    result = finish_source(e, source)
    before = asdict(e.store.get_wake(target.id))
    e.handoff(source, result)
    assert len(inbox(e, target)) == 1
    assert asdict(e.store.get_wake(target.id)) == before
    e.handoff(source, replace(result, dedupe_key='long-summary', summary='S'*600))
    assert len(inbox(e, target)[-1]['summary']) == 500
    for n in range(12):
        e.clock.advance(1)
        e.handoff(source, replace(result, dedupe_key=f'extra:{n}', summary=f'Queued {n}.'))
    assert len(inbox(e, target)) == 10
    assert [item['summary'] for item in inbox(e, target)] == [f'Queued {n}.' for n in range(2,12)]
    assert asdict(e.store.get_wake(target.id)) == before
    e.store.cas_wake(target.id, 'active', next_due_at=None)
    e.handoff(source, replace(result, dedupe_key='no-due'))
    assert e.store.get_wake(target.id).next_due_at == e.clock.now()


def test_desk_appends_suffixes_only_when_metadata_is_set(engine):
    e = engine
    target = create(e)
    source = create(e)
    before = e.tool.handle({'action':'desk'})['desk']
    row = next(line for line in before.splitlines() if line.startswith('- role '+source.id+' '))
    assert 'hands off' not in before and 'handoff waiting' not in before
    result = e.tool.handle(dict(action='update', id=source.id, handoff_to=target.id), session_id='sid')
    assert result.get('ok'), result
    desk = e.tool.handle({'action':'desk'})['desk']
    assert row+' · hands off to '+target.id in desk
    finish_source(e, source)
    assert ' · handoff waiting' in e.tool.handle({'action':'desk'})['desk']
    e.store.set_meta('handoff_to:'+source.id, 'w_missing')
    assert 'hands off to' not in e.tool.handle({'action':'desk'})['desk']


def test_tool_schema_describes_handoff(engine):
    assert PARAMETERS['properties']['handoff_to'] == {'type':'string', 'description':
        'Role id in this chat that takes over when this role ends a turn with progress or done and a summary; empty string clears.'}
