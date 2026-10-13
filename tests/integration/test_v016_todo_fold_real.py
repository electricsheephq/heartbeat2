"""Hermes' real todo snapshot fold at the pinned checkout keeps owner rows counted once (#33)."""
import importlib
import os
from types import SimpleNamespace

import pytest

from heartbeat2.intake import TODO_HEADER, origin_rows
from test_engine import engine
from test_u11c import hermes_origin, humans, steer_row
from test_v016 import api, begin_wake, silent_reply


@pytest.fixture
def hermes():
    try:
        return (importlib.import_module('agent.conversation_compression'),
                importlib.import_module('tools.todo_tool'),
                importlib.import_module('agent.agent_runtime_helpers'))
    except ImportError:
        if os.environ.get('HEARTBEAT2_REQUIRE_HERMES') == '1':
            pytest.fail('Hermes compression is not importable at the required pin')
        pytest.skip('Hermes is not importable in this venv')


def boundary(hermes, rows, task):
    """Run Hermes' own post-compaction fold with a live, non-empty task list."""
    compression, todo, _ = hermes
    store = todo.TodoStore()
    store.write([{'id': '1', 'content': task, 'status': 'in_progress'}])
    compression._fold_todo_snapshot(SimpleNamespace(_todo_store=store, _repair_message_sequence=lambda _: None), rows)
    return rows


# The wake turn's assistant row, so Hermes' repair never merges the wake row itself.
WORKING = {'role': 'assistant', 'content': 'working on it'}


def owner(text, wrapped, parts):
    row = steer_row(text) if wrapped else {'role': 'user', 'content': text}
    if parts:
        row['content'] = [{'type': 'text', 'text': row['content']}]
    return row


def test_header_matches_hermes(hermes):
    assert TODO_HEADER == hermes[1].TODO_INJECTION_HEADER


@pytest.mark.parametrize('tail', ['', '  \n'], ids=['plain', 'trailing-space'])
@pytest.mark.parametrize('parts', [False, True], ids=['text', 'parts'])
@pytest.mark.parametrize('wrapped', [False, True], ids=['origin', 'steer'])
def test_real_fold_keeps_counted_steer_once(hermes, engine, wrapped, parts, tail):
    e = engine
    row, history = begin_wake(e, [], 'signal')
    first = owner(hermes_origin('please check the deploy' + tail, 'signal'), wrapped, parts)
    rows = history + [first]
    api(e, rows)
    expected = [fp for fp, _ in origin_rows(rows)]
    assert humans(e) == 1
    boundary(hermes, rows, 'check the deploy')
    assert TODO_HEADER in str(rows[-1]['content']), 'positive control: Hermes folded the snapshot'
    api(e, rows)
    assert [fp for fp, _ in origin_rows(rows)] == expected
    if not tail or wrapped:
        # A repeat boundary re-strips with rstrip(); a plain row ending in whitespace
        # changes then and may be recounted once, as in v0.1.5.
        boundary(hermes, rows, 'roll back if red')
        api(e, rows)
        assert [fp for fp, _ in origin_rows(rows)] == expected
    # A new steer takes the next snapshot; Hermes strips the old one from the first row.
    second = owner(hermes_origin('and the docs', 'signal'), wrapped, parts)
    rows.append(second)
    boundary(hermes, rows, 'update the docs')
    assert TODO_HEADER not in str(rows[-2]['content']) and TODO_HEADER in str(rows[-1]['content'])
    api(e, rows)
    # On string content that strip uses rstrip(), so a plain row ending in whitespace
    # changes. v0.1.6 recounted it once; the row is now tracked as the same dict, so a
    # rewrite that adds no ORIGIN block is never a new owner message.
    assert humans(e) == 2


@pytest.mark.parametrize('parts', [False, True], ids=['text', 'parts'])
def test_real_fold_on_carried_row_keeps_wake_silent(hermes, engine, parts):
    e = engine
    carried = owner(hermes_origin('earlier request', 'signal'), True, parts)
    row, history = begin_wake(e, [carried], 'signal')
    rows = [row, carried]
    boundary(hermes, rows, 'earlier request')
    assert TODO_HEADER in str(rows[-1]['content'])
    api(e, rows)
    assert humans(e) == 0
    assert silent_reply(e) == '[SILENT]'


@pytest.mark.parametrize('wrapped', [False, True], ids=['origin', 'steer'])
def test_real_repair_merging_an_owner_row_after_a_snapshot_counts(hermes, engine, wrapped):
    # Hermes' pre-call repair joins consecutive user rows; the snapshot sits between them.
    e = engine
    row, history = begin_wake(e, [], 'signal')
    rows = history + [WORKING, owner(hermes_origin('please check the deploy', 'signal'), False, False)]
    api(e, rows)
    boundary(hermes, rows, 'check the deploy')
    rows.append(owner(hermes_origin('and roll back if red', 'signal'), wrapped, False))
    rows, repairs = hermes[2]._merge_consecutive_users(rows)
    assert repairs == 1 and TODO_HEADER in rows[-1]['content'], 'positive control: Hermes merged after the snapshot'
    api(e, rows)
    assert humans(e) == 2


def test_real_repair_after_a_quoted_header_counts(hermes, engine):
    e = engine
    row, history = begin_wake(e, [], 'signal')
    rows = history + [WORKING, {'role': 'user', 'content': 'please explain ' + TODO_HEADER + ' literally'},
                      owner(hermes_origin('new follow-up', 'signal'), False, False)]
    rows, repairs = hermes[2]._merge_consecutive_users(rows)
    assert repairs == 1
    api(e, rows)
    assert humans(e) == 1


def test_real_reload_notice_is_part_of_the_snapshot(hermes, engine):
    # A pruned skill makes Hermes add its reload notice after the task list.
    e = engine
    row, history = begin_wake(e, [], 'signal')
    rows = history + [{'role': 'assistant', 'content': "[SKILL_PRUNED: content lost in compression; reload with skill_view(name='deploy')]"},
                      owner(hermes_origin('please check the deploy', 'signal'), False, False)]
    api(e, rows)
    expected = [fp for fp, _ in origin_rows(rows)]
    boundary(hermes, rows, 'check the deploy')
    assert hermes[0]._PRUNED_SKILL_RELOAD_NOTICE_HEADER in rows[-1]['content'], 'positive control: notice present'
    api(e, rows)
    assert [fp for fp, _ in origin_rows(rows)] == expected
    assert humans(e) == 1


def test_real_repair_merging_bare_text_after_a_snapshot_counts(hermes, engine):
    e = engine
    row, history = begin_wake(e, [], 'signal')
    rows = history + [WORKING, owner(hermes_origin('please check the deploy', 'signal'), False, False)]
    api(e, rows)
    boundary(hermes, rows, 'check the deploy')
    rows.append({'role': 'user', 'content': 'and roll back if red'})
    rows, repairs = hermes[2]._merge_consecutive_users(rows)
    assert repairs == 1 and rows[-1]['content'].endswith('and roll back if red')
    api(e, rows)
    # On gateway messaging platforms every mid-turn owner message is ORIGIN-wrapped
    # (all steer and redirect paths pass their event to _steer_text_with_origin). A bare
    # row merged into a tracked owner row is internal text, like the wake row Hermes
    # merges into an unanswered owner row, and is not counted.
    assert humans(e) == 1
