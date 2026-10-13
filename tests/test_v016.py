"""Hermes' todo snapshot fold must not make a counted owner row new again (#33)."""
import hashlib

import pytest

from heartbeat2.intake import ORIGIN, origin_rows
from test_engine import arm, engine, session
from test_u11c import hermes_origin, humans, steer_row


# tools/todo_tool.py TODO_INJECTION_HEADER at the three supported Hermes pins.
TODO = '[Your active task list was preserved across context compression]'
SNAPSHOT = TODO + '\n- [ ] check the deploy (in_progress)'
# conversation_compression._PRUNED_SKILL_RELOAD_NOTICE_HEADER rides after the snapshot.
RELOAD = '\n\n[Skills pruned during compression — reload before acting on these tasks]\n- deploy-skill'


def strip(content):
    """conversation_compression._strip_stale_todo_snapshot (identical at all three pins)."""
    if isinstance(content, str):
        idx = content.find(TODO)
        return content if idx == -1 else content[:idx].rstrip()
    cleaned = []
    for part in content:
        text = str(part.get('text') or '') if isinstance(part, dict) and part.get('type') == 'text' else ''
        idx = text.find(TODO) if text else -1
        if idx == -1:
            cleaned.append(part)
        elif stripped := text[:idx].rstrip():
            cleaned.append({**part, 'text': stripped})
    return cleaned


def fold(row, snapshot=SNAPSHOT):
    """conversation_compression._fold_todo_snapshot merging into a real user tail."""
    content = strip(row['content'])
    if isinstance(content, list):
        content = [*content, {'type': 'text', 'text': snapshot}]
    else:
        content = content + '\n\n' + snapshot if content else snapshot
    return {**row, 'content': content}


def owner_row(text, wrapped, parts):
    row = steer_row(text) if wrapped else {'role': 'user', 'content': text}
    if parts:
        row['content'] = [{'type': 'text', 'text': row['content']}]
    return row


def api(e, history):
    e.intake.pre_api_request(session_id='sid', turn_id='wake', conversation_history=history)


def begin_wake(e, history, platform):
    session(e, platform=platform)
    arm(e, 'every', platform=platform)
    e.tick()
    row = {'role': 'user', 'content': e.ctx.calls[-1][0], 'display_kind': 'internal_notification'}
    history = history + [row]
    e.intake.pre_llm_call(session_id='sid', turn_id='wake', user_message=row['content'],
                          conversation_history=history, platform=platform)
    return row, history


def silent_reply(e):
    assert e.tool.handle({'action': 'respond', 'outcome': 'no_change', 'notify': False}, session_id='sid')['ok']
    return e.intake.transform_llm_output(session_id='sid', turn_id='wake', response_text='Chatty wake text')


CASES = pytest.mark.parametrize('wrapped,parts,tail,reload', [
    (True, False, '', ''), (False, False, '', ''), (True, True, '', ''), (False, True, '', ''),
    (True, False, '\n', ''), (True, False, '  \n', RELOAD), (False, True, '\n', RELOAD)],
    ids=['steer', 'origin', 'steer-parts', 'origin-parts', 'trailing-newline', 'trailing-space-reload',
         'parts-trailing-reload'])


@CASES
@pytest.mark.parametrize('platform,mid', [('signal', None), ('simplex', None), ('telegram', '701')])
def test_todo_fold_on_counted_steer_is_not_a_new_message(engine, platform, mid, wrapped, parts, tail, reload):
    e = engine
    row, history = begin_wake(e, [], platform)
    owner = owner_row(hermes_origin('please also check the deploy' + tail, platform, mid), wrapped, parts)
    api(e, history + [owner])
    assert humans(e) == 1
    api(e, [row, fold(owner, SNAPSHOT + reload)])
    api(e, [row, fold(fold(owner), SNAPSHOT + reload)])
    assert humans(e) == 1


@CASES
def test_todo_fold_on_carried_owner_row_keeps_the_wake_silent(engine, wrapped, parts, tail, reload):
    # The owner row predates the wake, so a fold mid-wake is not an owner reply.
    e = engine
    owner = owner_row(hermes_origin('earlier request' + tail, 'signal'), wrapped, parts)
    row, history = begin_wake(e, [owner], 'signal')
    api(e, [fold(owner, SNAPSHOT + reload), row])
    assert humans(e) == 0
    assert not e.intake.flights['sid'].owner_replied
    assert silent_reply(e) == '[SILENT]'


@pytest.mark.parametrize('parts', [False, True])
def test_restripped_stale_snapshot_is_not_a_new_message(engine, parts):
    # A later boundary strips an older snapshot from a carried row (and rstrips it)
    # while the new snapshot lands elsewhere.
    e = engine
    owner = fold(owner_row(hermes_origin('earlier request  \n', 'signal'), True, parts))
    row, history = begin_wake(e, [owner], 'signal')
    api(e, [{**owner, 'content': strip(owner['content'])}, row,
            {'role': 'user', 'content': SNAPSHOT, '_todo_snapshot_synthetic': True}])
    assert humans(e) == 0
    assert silent_reply(e) == '[SILENT]'


@pytest.mark.parametrize('platform,mid', [('signal', None), ('telegram', '702')])
def test_new_owner_steers_still_count_after_a_fold(engine, platform, mid):
    # Positive control: stripping the snapshot never hides a genuinely new message.
    e = engine
    row, history = begin_wake(e, [], platform)
    first = steer_row(hermes_origin('first steer', platform, mid))
    api(e, history + [first])
    second = steer_row(hermes_origin('second steer', platform, mid and '703'))
    api(e, [row, fold(first), second])
    third = steer_row(hermes_origin('third steer', platform, mid and '704'))
    api(e, [row, first, second, fold(third)])
    assert humans(e) == 3
    assert e.intake.flights['sid'].owner_replied


def test_snapshot_text_is_never_owner_text():
    # A task list quoting the origin header is scaffolding, not an owner message.
    quoted = hermes_origin('reply to the deploy thread', 'signal')
    assert not list(origin_rows([{'role': 'user', 'content': TODO + '\n- [ ] ' + quoted,
                                  '_todo_snapshot_synthetic': True}]))
    assert not list(origin_rows([{'role': 'user', 'content': 'plain owner text\n\n' + TODO + '\n- ' + quoted}]))


@pytest.mark.parametrize('prefix', ['', 'preamble\n', '  '])
def test_fingerprint_unchanged_without_snapshot_or_trailing_space(prefix):
    text = prefix + hermes_origin('steady text', 'signal')
    for content in (text, [{'type': 'text', 'text': text}]):
        (fp, seen), = origin_rows([{'role': 'user', 'content': content}])
        assert seen == text and fp == hashlib.sha256(text.encode()).hexdigest()[:16]
    assert ORIGIN in text
