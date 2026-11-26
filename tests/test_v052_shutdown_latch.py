"""A backend exit leaves its active wake for restart recovery."""
import ast
from concurrent.futures import ThreadPoolExecutor
from importlib.util import find_spec
import os
from pathlib import Path

import pytest

from heartbeat2 import serving
from test_backend_wakes import CHAT, hosts, notice_sends, process_table
from test_v052_notices import flush_notices
from test_watchdog_retry import NOTICE, reminder


@pytest.fixture(autouse=True)
def reset_shutdown_latch(monkeypatch):
    # Also isolate the baseline run before the latch exists.
    monkeypatch.setattr(serving, '_EXITING', False, raising=False)


def stop_from(engine, filename, caller):
    # A synthetic module gives the frame the same code filename as Hermes.
    namespace = {'hook': engine.runtime_status.__self__.hook, 'chat': CHAT}
    exec(compile(f'def {caller}():\n'
                 '    hook("agent_loop_stopped", session_key=chat, platform="tui",\n'
                 '         reason="user_stop", invalidation_reason="session_interrupt")\n',
                 filename, 'exec'), namespace)
    namespace[caller]()


def restart(engine, wake_id, process_table):
    # Simulate a prior backend's abandoned claim and run startup reconciliation.
    engine.ctx._manager.has_tui_message_injector = True
    if serving._EXITING:
        serving._reset_exiting_for_tests()
    pid = os.getpid() + 1
    epoch = f'{pid}:{process_table.pop(pid)}:previous-backend'
    engine.store.connection.execute('UPDATE occurrences SET pid=?,epoch=? WHERE wake_id=?',
                                    (pid, epoch, wake_id))
    engine.process_start = True
    engine.reconcile()


@pytest.mark.parametrize('owner,urgent,source,surface', [
    pytest.param(True, False, 'desktop', 'Desktop', id='owner-desktop'),
    pytest.param(False, True, 'tui', 'TUI', id='urgent-tui'),
])
@pytest.mark.parametrize('api_calls', [0, 1], ids=['before-model', 'after-model'])
@pytest.mark.parametrize('first_end', ['interrupted', 'failed'])
def test_shutdown_mid_wake_notifies_after_restart(reminder, process_table, owner, urgent, source, surface,
                                                 api_calls, first_end):
    engine, wid, sends = reminder(owner=owner, urgent=urgent, source=source)
    if api_calls:
        engine.intake.post_api_request(session_id=CHAT, turn_id='wake', usage={'input_tokens': 1})
    occurrence, = engine.store.list_occurrences(wid)
    assert (occurrence.state, occurrence.api_calls) == ('started', api_calls)

    # Desktop may clear the injector before dispatching its synchronous stop hook.
    engine.ctx._manager.has_tui_message_injector = source == 'tui'
    stop_from(engine, '/fixture/tui_gateway/session_reaper.py', '_stop_turns_before_exit')
    assert engine.store.get_occurrence(wid, occurrence.dedupe_key).state == 'started'
    assert serving._EXITING

    # Finalization can run off the shutdown stack. Exercise each shape with a live flight first.
    endings = {
        'interrupted': dict(turn_id='wake', interrupted=True, turn_exit_reason='interrupted_by_user'),
        'failed': dict(failed=True),
    }
    end_hook = engine.runtime_status.__self__.hook
    second_end = 'failed' if first_end == 'interrupted' else 'interrupted'
    with ThreadPoolExecutor(max_workers=1) as worker:
        for end in (first_end, second_end):
            worker.submit(end_hook, 'on_session_end', session_id=CHAT, **endings[end]).result(timeout=2)
            assert engine.store.get_occurrence(wid, occurrence.dedupe_key).state == 'started'
    flush_notices(engine)
    assert engine.store.get_wake(wid).status == 'active'
    assert sends == []

    restart(engine, wid, process_table)
    settled = engine.store.get_occurrence(wid, occurrence.dedupe_key)
    assert (settled.state, settled.error) == ('interrupted', 'restart')
    wake = engine.store.get_wake(wid)
    assert (wake.status, wake.status_reason) == ('failed', 'interrupted:restart')
    flush_notices(engine)
    flush_notices(engine)
    expected = (f"Reminder may not have finished; say 'again' in its {surface} chat to run it again"
                if api_calls else NOTICE.format(surface=surface))
    assert sends == [('owner', expected)]


@pytest.mark.parametrize('source', ['desktop', 'tui'])
@pytest.mark.parametrize('first_hook', ['stop', 'finalizer'])
def test_real_stop_stays_silent(reminder, process_table, source, first_hook):
    engine, wid, sends = reminder(source=source)
    stop = dict(session_key=CHAT, platform='tui', reason='user_stop', invalidation_reason='session_interrupt')
    finalizer = dict(session_id=CHAT, turn_id='wake', interrupted=True, turn_exit_reason='interrupted_by_user')
    hook = engine.runtime_status.__self__.hook
    if first_hook == 'stop':
        hook('agent_loop_stopped', **stop)
        hook('on_session_end', **finalizer)
    else:
        hook('on_session_end', **finalizer)
        hook('agent_loop_stopped', **stop)

    occurrence, = engine.store.list_occurrences(wid)
    expected = ('app_invalidation', 'failed') if first_hook == 'stop' else ('owner_interrupted', 'cancelled')
    assert (occurrence.state, occurrence.error, engine.store.get_wake(wid).status) == ('interrupted', *expected)
    flush_notices(engine)
    assert sends == []
    restart(engine, wid, process_table)
    flush_notices(engine)
    assert engine.store.get_occurrence(wid, occurrence.dedupe_key).error == expected[0]
    assert sends == []
    assert not serving._EXITING


@pytest.mark.parametrize('filename,caller', [
    ('/fixture/other/session_reaper.py', '_stop_turns_before_exit'),
    ('/fixture/tui_gateway/methods_session_interrupt.py', 'session_interrupt'),
])
def test_latch_ignores_lookalike_frames(reminder, filename, caller):
    engine, wid, _ = reminder()
    stop_from(engine, filename, caller)
    assert not serving._EXITING
    occurrence, = engine.store.list_occurrences(wid)
    assert (occurrence.state, occurrence.error) == ('interrupted', 'app_invalidation')


def test_completed_turn_during_shutdown_finishes(reminder):
    engine, wid, sends = reminder()
    stop_from(engine, r'C:\fixture\tui_gateway\session_reaper.py', '_stop_turns_before_exit')
    engine.runtime_status.__self__.hook('on_session_end', session_id=CHAT, turn_id='wake', completed=True)
    occurrence, = engine.store.list_occurrences(wid)
    assert occurrence.state == engine.store.get_wake(wid).status == 'done'
    assert serving._EXITING
    flush_notices(engine)
    assert sends == []


def test_hermes_shutdown_callers_remain_synchronous():
    pins = Path(os.environ.get('HEARTBEAT2_HERMES_PINS', Path.home() / 'repos' / 'hermes-pins'))
    if pins.is_dir():
        roots = [pins / pin for pin in ('v0.21.6', 'v0.21.7', 'v0.23.0', 'v0.23.2', 'main-04eb00c')]
    else:
        spec = find_spec('tui_gateway')
        if spec is None or spec.origin is None:
            pytest.skip('Hermes shutdown source unavailable')
        roots = [Path(spec.origin).parent.parent]

    for root in roots:
        for filename, name, callee in (
            ('session_reaper.py', '_stop_turns_before_exit', '_interrupt_session_turn'),
            ('session_reaper.py', '_handle_exit_flush_signal', '_stop_turns_before_exit'),
            ('server.py', '_shutdown_sessions', '_stop_turns_before_exit'),
        ):
            path = root / 'tui_gateway' / filename
            tree = ast.parse(path.read_text(), filename=str(path))
            function = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name), None)
            assert function is not None, (path, name)
            assert any(isinstance(n, ast.Name) and n.id == callee for n in ast.walk(function)), (path, name, callee)

        lifecycle = root / 'tui_gateway' / 'session_lifecycle.py'
        tree = ast.parse(lifecycle.read_text(), filename=str(lifecycle))
        interrupt = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_interrupt_session_turn')
        assert any(isinstance(n, ast.Call) and n.args and isinstance(n.args[0], ast.Constant)
                   and n.args[0].value == 'agent_loop_stopped' for n in ast.walk(interrupt)), lifecycle

        dispatch = root / 'hermes_cli' / 'plugins_dispatch.py'
        tree = ast.parse(dispatch.read_text(), filename=str(dispatch))
        timeout_sets = {'_HOOK_TIMEOUT_BOUNDED_HOOKS', '_HOOK_TIMEOUT_FAIL_CLOSED_HOOKS'}
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in timeout_sets:
                assert 'agent_loop_stopped' not in ast.literal_eval(node.value), dispatch
                timeout_sets.remove(node.target.id)
        assert not timeout_sets, (dispatch, timeout_sets)
