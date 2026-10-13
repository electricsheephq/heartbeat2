"""Hermes' todo snapshot fold must not make a counted owner row new again (#33)."""
import hashlib

import pytest

from heartbeat2.intake import ORIGIN, origin_rows
from test_engine import arm, engine, session
from test_u11c import hermes_origin, humans, steer_row


# tools/todo_tool.py TODO_INJECTION_HEADER at the three supported Hermes pins.
TODO = '[Your active task list was preserved across context compression]'
# TodoStore.format_for_injection renders one line per task (children indented).
SNAPSHOT = TODO + '\n- [>] 1. check the deploy (in_progress)\n  - [ ] 1a. read the logs (pending)'
# conversation_compression._pruned_skill_reload_notice rides after the snapshot.
RELOAD = ('\n\n[Skills pruned during compression — reload before acting on these tasks]\n'
          'The task list above crossed the compression boundary verbatim, but the skill instructions that '
          'governed it were pruned. Before executing any preserved task that depends on these skills, reload '
          "them first: skill_view(name='deploy'). After reloading, re-check that each pending task is still "
          'justified — findings recorded before the boundary may have invalidated it.')


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
# Signal is the id-less case; Telegram's message ids are the control.
@pytest.mark.parametrize('platform,mid', [('signal', None), ('telegram', '701')])
def test_todo_fold_on_counted_steer_is_not_a_new_message(engine, platform, mid, wrapped, parts, tail, reload):
    e = engine
    row, history = begin_wake(e, [], platform)
    owner = owner_row(hermes_origin('please also check the deploy' + tail, platform, mid), wrapped, parts)
    api(e, history + [owner])
    assert humans(e) == 1
    api(e, [row, fold(owner, SNAPSHOT + reload)])
    if not tail:
        # Hermes re-strips a stale snapshot with rstrip(), so a row ending in
        # whitespace changes; that row may be recounted once, as in v0.1.5.
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
    owner = fold(owner_row(hermes_origin('earlier request', 'signal'), True, parts))
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
    # A one-line task quoting the origin header is scaffolding, not an owner message.
    task = '\n- [ ] 1. answer the ' + ORIGIN + ' question (pending)'
    assert not list(origin_rows([{'role': 'user', 'content': TODO + task, '_todo_snapshot_synthetic': True}]))
    assert not list(origin_rows([{'role': 'user', 'content': 'plain owner text\n\n' + TODO + task}]))


def test_task_spanning_lines_keeps_the_row_as_is(engine):
    # Not provably Hermes' rendering, so nothing is removed: a fold may recount (as in
    # v0.1.5), but a quoted owner block inside a task is never mistaken for removable text.
    e = engine
    row, history = begin_wake(e, [], 'signal')
    owner = {'role': 'user', 'content': hermes_origin('please check the deploy', 'signal')}
    api(e, history + [owner])
    task = TODO + '\n- [ ] 1. quoted block\n\n' + hermes_origin('old reply', 'signal') + ' (pending)'
    api(e, [row, fold(owner, task)])
    assert humans(e) == 2


@pytest.mark.parametrize('suffix', ['', '  ', '\n', ' quoting ' + TODO + ' inline', '\n' + TODO + ' on its own line'])
@pytest.mark.parametrize('prefix', ['', 'preamble\n', '  '])
def test_fingerprint_unchanged_without_a_snapshot(prefix, suffix):
    # Only a real snapshot block is removed; every other row is fingerprinted byte for byte.
    text = prefix + hermes_origin('steady text' + suffix, 'signal')
    for content in (text, [{'type': 'text', 'text': text}]):
        (fp, seen), = origin_rows([{'role': 'user', 'content': content}])
        assert seen == text and fp == hashlib.sha256(text.encode()).hexdigest()[:16]
    assert ORIGIN in text


# agent/context_compressor.py _merge_summary_into_tail_row, delimiter form, at the three pins.
PRIOR = '[PRIOR CONTEXT — for reference only; not a new message]'
DELIMITER = '[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]'
END = '--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---'


@pytest.mark.parametrize('metadata', [False, True])
def test_carrier_holding_a_snapshot_stays_skipped(metadata):
    # A tail that kept an earlier snapshot is merged with the next summary, so the
    # snapshot sits before the delimiter and END marker. The marker still wins.
    tail = fold(steer_row(hermes_origin('earlier request', 'signal')))
    carrier = {'role': 'user', 'content': PRIOR + '\n' + tail['content'] + '\n\n' + DELIMITER +
               '\n\n[CONTEXT COMPACTION — REFERENCE ONLY]\nsummary\n\n' + END}
    if metadata:
        carrier['_compressed_summary'] = True
    assert not list(origin_rows([carrier]))


def merge_users(first, second):
    """agent_runtime_helpers._merge_consecutive_users: plain-text user rows joined by a blank line."""
    return {**first, 'content': first['content'] + '\n\n' + second['content']}


@pytest.mark.parametrize('wrapped', [False, True], ids=['origin', 'steer'])
def test_owner_row_merged_after_a_snapshot_still_counts(engine, wrapped):
    # Hermes' pre-call repair can append a new owner row after a folded snapshot.
    e = engine
    row, history = begin_wake(e, [], 'signal')
    first = {'role': 'user', 'content': hermes_origin('please check the deploy', 'signal')}
    api(e, history + [first])
    folded = fold(first, SNAPSHOT + RELOAD)
    api(e, [row, folded])
    second = owner_row(hermes_origin('and roll back if red', 'signal'), wrapped, False)
    api(e, [row, merge_users(folded, second)])
    assert humans(e) == 2
    assert e.intake.flights['sid'].owner_replied


@pytest.mark.parametrize('first,second', [(' ' + TODO + ' one', ' ' + TODO + ' two'), (' ', '\n'), ('', '  ')],
                         ids=['quoted-header', 'whitespace', 'trailing-space'])
def test_new_message_that_differs_only_after_a_quote_or_in_space_counts(engine, first, second):
    # The earlier message has left history (summarised), so only distinct fingerprints count it.
    e = engine
    row, history = begin_wake(e, [], 'signal')
    api(e, history + [{'role': 'user', 'content': hermes_origin('shared prefix' + first, 'signal')}])
    carrier = {'role': 'user', 'content': 'summary --- END OF CONTEXT SUMMARY', '_compressed_summary': True}
    api(e, [carrier, row, {'role': 'user', 'content': hermes_origin('shared prefix' + second, 'signal')}])
    assert humans(e) == 2


@pytest.mark.parametrize('second', ['and roll back if red', 'owner preamble\n' + hermes_origin('and roll back', 'signal')],
                         ids=['bare', 'preamble-then-origin'])
def test_any_row_merged_after_a_snapshot_keeps_the_text(engine, second):
    # Text after a snapshot is not Hermes' rendering; v0.1.5 counted the changed row, and so do we.
    e = engine
    row, history = begin_wake(e, [], 'signal')
    first = {'role': 'user', 'content': hermes_origin('please check the deploy', 'signal')}
    api(e, history + [first])
    folded = fold(first)
    api(e, [row, folded])
    api(e, [row, merge_users(folded, {'role': 'user', 'content': second})])
    assert humans(e) == 2
