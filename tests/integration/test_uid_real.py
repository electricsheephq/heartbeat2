"""Owner identity through a real Hermes loop and SessionDB, with a mocked provider."""
import copy
import importlib
import json
import os
from contextlib import ExitStack, closing
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import pytest

from test_engine import KEY, engine, session
from test_u11c import hermes_origin, humans


@pytest.fixture
def hermes():
    if os.environ.get('HEARTBEAT2_REQUIRE_HERMES') != '1':
        pytest.skip('set HEARTBEAT2_REQUIRE_HERMES for pinned real-loop acceptance')
    try:
        return NS(agent=importlib.import_module('run_agent').AIAgent,
                  db=importlib.import_module('hermes_state').SessionDB,
                  compression=importlib.import_module('agent.conversation_compression'),
                  compressor=importlib.import_module('agent.context_compressor').ContextCompressor,
                  micro=importlib.import_module('agent.micro_compaction').MicroCompactionMixin,
                  repair=importlib.import_module('agent.agent_runtime_helpers')._merge_consecutive_users,
                  steer=importlib.import_module('agent.prompt_builder').steer_user_row,
                  stamp=importlib.import_module('agent.message_metadata').stamp_message_uid,
                  todo=importlib.import_module('tools.todo_tool'))
    except ImportError:
        pytest.fail('Hermes is not importable at the required pin')


def response(content='', calls=None):
    msg = NS(content=content, tool_calls=calls, reasoning=None, reasoning_content=None,
             reasoning_details=None, refusal=None, audio=None, function_call=None)
    return NS(choices=[NS(message=msg, finish_reason='tool_calls' if calls else 'stop')],
              model='test/model', usage=None)


def make_agent(h, db, sid):
    tooldef = [{'type': 'function', 'function': {'name': 'web_search', 'description': 'synthetic',
                                               'parameters': {'type': 'object', 'properties': {}}}}]
    with patch('model_tools.get_tool_definitions', return_value=tooldef), \
         patch('model_tools.check_toolset_requirements', return_value={}), \
         patch('agent.process_bootstrap.OpenAI'):
        a = h.agent(api_key='synthetic-test-key', base_url='https://provider.invalid/v1',
                    quiet_mode=True, skip_context_files=True, skip_memory=True,
                    session_db=db, session_id=sid)
    a.client = MagicMock()
    a._cached_system_prompt = 'Synthetic test.'
    a._use_prompt_caching = False
    a.compression_enabled = False
    a.save_trajectories = False
    return a


CASES = [(f'U{n}', 'signal') for n in range(1, 10)] + \
        [(f'U10-U{n}', 'telegram') for n in range(1, 4)] + [('U11', 'signal'), ('U12', 'signal')]


@pytest.mark.parametrize('case,platform', CASES, ids=[c for c, _ in CASES])
def test_uid_real_loop(hermes, engine, tmp_path, case, platform):
    h, e = hermes, engine
    session(e, platform=platform)
    scenario = int(case.split('-')[-1][1:])
    expected = {1: 2, 2: 2, 3: 0, 4: 0, 5: 1, 6: 1, 7: 2, 8: 0, 9: 0, 11: 2, 12: 0}[scenario]
    trace, state = [], {'api': 0, 'turn': 'turn', 'calls': 0}
    def origin(body='same genuine reply', mid='new'):
        return hermes_origin(body, platform, mid if platform == 'telegram' else None)
    def steer(body='same genuine reply', mid='new'):
        return h.steer(origin(body, mid))
    def tool_call(n):
        return NS(id=f'call_{n}', type='function', function=NS(name='web_search', arguments='{}'))
    with ExitStack() as stack:
        db = stack.enter_context(closing(h.db(db_path=tmp_path / 'hermes.db')))
        sid = '20261013_000000_uidtest'
        db.create_session(sid, platform, model='test/model')
        a = make_agent(h, db, sid)
        old = []
        if scenario in {3, 4, 5, 8, 9, 12}:
            for n in range(2 if scenario == 3 else 1):
                db.append_message(session_id=sid, role='user', content=origin(f'old-{n}', f'old-{n}'))
                db.append_message(session_id=sid, role='assistant', content='earlier answer')
            if scenario == 12:
                db._conn.execute('UPDATE messages SET message_uid=NULL WHERE session_id=?', (sid,))
            old = db.get_messages_as_conversation(sid, repair_alternation=False)
            if scenario == 12:
                old = [r for r in old if r['role'] == 'user']
                assert old[0].get('message_uid') is None
        def hook(name, **kw):
            history = kw.get('conversation_history') or []
            if name == 'pre_llm_call':
                e.intake.pre_llm_call(session_id='sid', turn_id=state['turn'], platform=platform,
                                      user_message=kw.get('user_message', ''), conversation_history=history)
            elif name == 'pre_api_request':
                state['api'] += 1
                n = state['api']
                if scenario in {1, 2} and n == 3:
                    first = next(r for r in history if r.get('display_kind') == 'steer')
                    assert first.get('message_uid'), 'first steer flushed before summary'
                    cc = object.__new__(h.compressor)
                    cc._summary_has_user_turn = True
                    cc._merge_summary_into_tail_row(first, 'synthetic summary', 'user', False)
                    assert first.get('_compressed_summary')
                    history.remove(first)
                    body = 'same genuine reply'
                    if scenario == 2:
                        body += '\n\n' + h.todo.TODO_INJECTION_HEADER + '\n- [ ] pasted task (pending)'
                    history.append(steer(body, 'second'))
                elif scenario == 3 and n == 1:
                    owner_rows = [r for r in history if 'Gateway message origin' in str(r.get('content'))]
                    assert len(owner_rows) == 2 and all(r.get('message_uid') for r in owner_rows)
                    merged = h.micro._merge_adjacent_user_turns(NS(), owner_rows)
                    assert len(merged) == 1 and merged[0].get('_absorbed_message_uids')
                    history[:] = merged + [r for r in history if r not in owner_rows]
                elif scenario == 4 and n == 1:
                    tail = next(r for r in history if 'Gateway message origin' in str(r.get('content')))
                    store = h.todo.TodoStore()
                    store.write([{'id': '1', 'content': 'ordinary task', 'status': 'pending'}])
                    h.compression._fold_todo_snapshot(NS(_todo_store=store,
                                                        _repair_message_sequence=lambda _: None), [tail])
                    assert h.todo.TODO_INJECTION_HEADER in tail['content']
                elif scenario == 5:
                    if n == 1:
                        state['fresh'] = steer('new unflushed reply')
                        history.append(state['fresh'])
                    elif n == 2:
                        predecessor = next(r for r in history if r.get('message_uid') and
                                           'Gateway message origin' in str(r.get('content')))
                        fresh = state['fresh']
                        # Use a genuinely unflushed synthetic row at this helper seam.
                        assert not fresh.get('message_uid')
                        merged, repairs = h.repair([predecessor, fresh])
                        assert repairs == 1 and not predecessor.get('_absorbed_message_uids')
                        history[:] = merged + [r for r in history if r is not predecessor and r is not fresh]
                elif scenario == 12 and n == 1:
                    survivor = next(r for r in history if 'old-0' in str(r.get('content')))
                    assert not survivor.get('message_uid') and survivor.get('_absorbed_message_uids')
                e.intake.pre_api_request(session_id='sid', turn_id=state['turn'], conversation_history=history)
            if name in {'pre_llm_call', 'pre_api_request', 'post_llm_call'}:
                trace.append({'hook': name, 'api': state['api'], 'humans': humans(e),
                              'rows': [{'uid': bool(r.get('message_uid')),
                                        'absorbed': len(r.get('_absorbed_message_uids') or []),
                                        'origins': str(r.get('content')).count('Gateway message origin')}
                                       for r in history if r.get('role') == 'user']})
            return []
        for target, replacement in [
            ('hermes_cli.plugins.discover_plugins', lambda *a, **k: None),
            ('hermes_cli.lifecycle.has_hook', lambda n: n in {'pre_llm_call', 'pre_api_request', 'post_llm_call'}),
            ('hermes_cli.lifecycle.invoke_hook', hook),
            ('model_tools.handle_function_call', lambda *a, **k: 'synthetic result'),
            ('agent.turn_context._maybe_title_session_at_turn_start', lambda *a, **k: None)]:
            stack.enter_context(patch(target, side_effect=replacement))
        def create(*args, **kwargs):
            state['calls'] += 1
            n = state['calls']
            if scenario in {1, 2} and n == 1:
                body = 'same genuine reply'
                if scenario == 2:
                    body += '\n\n' + h.todo.TODO_INJECTION_HEADER + '\n- [ ] pasted task (pending)'
                a.steer(origin(body))
            elif scenario == 7 and n == 1:
                a.steer(origin(mid='second'))
            elif scenario == 11 and n in {1, 2}:
                a.steer(origin(mid=str(n)))
            rounds = 3 if scenario in {1, 2, 11} else 1 if scenario in {5, 7, 12} else 0
            # U5 mutates only the shallow hook history; avoid flushing its test-only row.
            if scenario == 5 and n == 1:
                hook('pre_api_request', conversation_history=state['history'])
                return response('done')
            return response('', [tool_call(n)]) if n <= rounds else response('done')
        # Capture the real shallow hook history for U5's pre-flush merge seam.
        original_hook = hook
        def capture(name, **kw):
            if name == 'pre_api_request':
                state['history'] = kw.get('conversation_history')
            return original_hook(name, **kw)
        stack.enter_context(patch('hermes_cli.lifecycle.invoke_hook', side_effect=capture))
        a.client.chat.completions.create.side_effect = create
        message = origin(mid='first') if scenario in {6, 7} else 'synthetic internal wake'
        result = a.run_conversation(message, conversation_history=old,
                                    persist_user_display_kind=None if scenario in {6, 7} else 'internal_notification')
        assert result.get('completed'), result
        if scenario == 9:
            state.update(api=0, turn='replay', calls=0)
            restored = copy.deepcopy(db.get_messages_as_conversation(sid, repair_alternation=True))
            result = a.run_conversation('synthetic internal wake', conversation_history=restored,
                                        persist_user_display_kind='internal_notification')
            assert result.get('completed'), result
        assert any(t['hook'] == 'pre_llm_call' for t in trace)
        assert any(t['hook'] == 'pre_api_request' for t in trace)
        assert humans(e) == expected, (case, expected, humans(e), trace)
