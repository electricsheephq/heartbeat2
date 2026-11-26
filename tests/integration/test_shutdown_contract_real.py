"""The backend-shutdown latch (heartbeat2/serving.py) keys on Hermes's own exit functions by name.

This runs the same source contract as tests/test_v052_shutdown_latch.py against the Hermes on PYTHONPATH, so CI checks
it at every pinned Hermes. A renamed or rethreaded exit path would otherwise turn every backend shutdown back into a
silent Stop without failing any test.
"""
import ast
from importlib.util import find_spec
import os
from pathlib import Path

import pytest


def _hermes_root():
    spec = find_spec('tui_gateway')
    if spec is None or spec.origin is None:
        if os.environ.get('HEARTBEAT2_REQUIRE_HERMES') == '1':
            pytest.fail('HEARTBEAT2_REQUIRE_HERMES=1 but tui_gateway is not importable')
        pytest.skip('Hermes source unavailable')
    return Path(spec.origin).parent.parent


def _function(path, name):
    tree = ast.parse(path.read_text(), filename=str(path))
    function = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name), None)
    assert function is not None, (path, name)
    return function


_HANDOFFS = {'Thread', 'Timer', 'submit', 'run_in_executor', 'start_new_thread', 'call_soon_threadsafe', 'to_thread'}


def _runs_on_this_thread(function, callee):
    """True if `function` reaches `callee` and never hands it to a thread or executor (the latch reads this stack)."""
    def handed_off(call):
        name = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, 'attr', '')
        values = [*call.args, *(k.value for k in call.keywords)]
        return name in _HANDOFFS and any(isinstance(n, ast.Name) and n.id == callee for v in values for n in ast.walk(v))
    nodes = list(ast.walk(function))
    return (any(isinstance(n, ast.Name) and n.id == callee for n in nodes)
            and not any(isinstance(n, ast.Call) and handed_off(n) for n in nodes))


def test_exit_path_reaches_agent_loop_stopped_on_one_thread():
    root = _hermes_root()
    for filename, name, callee in (
        ('session_reaper.py', '_stop_turns_before_exit', '_interrupt_session_turn'),
        ('session_reaper.py', '_handle_exit_flush_signal', '_stop_turns_before_exit'),
        ('server.py', '_shutdown_sessions', '_stop_turns_before_exit'),
    ):
        path = root / 'tui_gateway' / filename
        function = _function(path, name)
        assert _runs_on_this_thread(function, callee), (path, name, callee)

    lifecycle = root / 'tui_gateway' / 'session_lifecycle.py'
    interrupt = _function(lifecycle, '_interrupt_session_turn')
    assert any(isinstance(n, ast.Call) and n.args and isinstance(n.args[0], ast.Constant)
               and n.args[0].value == 'agent_loop_stopped' for n in ast.walk(interrupt)), lifecycle


def test_agent_loop_stopped_runs_without_a_hook_timeout_thread():
    dispatch = _hermes_root() / 'hermes_cli' / 'plugins_dispatch.py'
    tree = ast.parse(dispatch.read_text(), filename=str(dispatch))
    timeout_sets = {'_HOOK_TIMEOUT_BOUNDED_HOOKS', '_HOOK_TIMEOUT_FAIL_CLOSED_HOOKS'}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in timeout_sets:
            assert 'agent_loop_stopped' not in ast.literal_eval(node.value), dispatch
            timeout_sets.remove(node.target.id)
    assert not timeout_sets, (dispatch, timeout_sets)
