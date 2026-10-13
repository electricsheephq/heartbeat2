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
                importlib.import_module('tools.todo_tool'))
    except ImportError:
        if os.environ.get('HEARTBEAT2_REQUIRE_HERMES') == '1':
            pytest.fail('Hermes compression is not importable at the required pin')
        pytest.skip('Hermes is not importable in this venv')


def boundary(hermes, rows, task):
    """Run Hermes' own post-compaction fold with a live, non-empty task list."""
    compression, todo = hermes
    store = todo.TodoStore()
    store.write([{'id': '1', 'content': task, 'status': 'in_progress'}])
    compression._fold_todo_snapshot(SimpleNamespace(_todo_store=store, _repair_message_sequence=lambda _: None), rows)
    return rows


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
    boundary(hermes, rows, 'roll back if red')
    api(e, rows)
    assert [fp for fp, _ in origin_rows(rows)] == expected
    # A new steer takes the next snapshot; Hermes strips the old one from the first row.
    second = owner(hermes_origin('and the docs', 'signal'), wrapped, parts)
    rows.append(second)
    boundary(hermes, rows, 'update the docs')
    assert TODO_HEADER not in str(rows[-2]['content']) and TODO_HEADER in str(rows[-1]['content'])
    api(e, rows)
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
