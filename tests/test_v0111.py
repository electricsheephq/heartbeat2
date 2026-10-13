"""v0.1.11 (#43): deadline and urgency changes by the agent count as agent edits."""
from heartbeat2 import commands
from heartbeat2.policy import is_owner
from test_engine import engine  # noqa: F401
from test_o12 import runtime, owner_add, update  # noqa: F401

EVENT = {'kind': 'subagent_done', 'deadline': 'in 1h'}


def test_watch_conversion_with_a_deadline_stays_the_owners(engine, runtime):
    wake = owner_add(engine, runtime)
    assert not commands.handle('watch ' + wake.id, runtime).startswith('Error:')
    _, changed = update(engine, wake, trigger=EVENT)
    assert not changed.agent_edited and is_owner(changed) and changed.deadline_at is not None


def test_agent_moving_a_deadline_marks_the_wake_agent_edited(engine, runtime):
    wake = owner_add(engine, runtime)
    assert not commands.handle('watch ' + wake.id, runtime).startswith('Error:')
    _, converted = update(engine, wake, trigger=EVENT)
    engine.clock.advance(600)
    _, moved = update(engine, converted, trigger=EVENT)  # same relative text, later deadline
    assert moved.deadline_at == converted.deadline_at + 600
    assert moved.agent_edited and not is_owner(moved)


def test_urgent_change_marks_the_wake_agent_edited(engine, runtime):
    wake = owner_add(engine, runtime)
    _, changed = update(engine, wake, urgent=True)
    assert changed.urgent and changed.agent_edited and not is_owner(changed)


def test_title_only_change_keeps_the_wake_the_owners(engine, runtime):
    wake = owner_add(engine, runtime)
    _, changed = update(engine, wake, title='Agent title')
    assert changed.title == 'Agent title' and not changed.agent_edited and is_owner(changed)


import pytest  # noqa: E402


@pytest.mark.parametrize('args', [{'title': 'Cosmetic'}, {'reason': 'Cosmetic'}, {'urgent': False}, {}],
                         ids=['title', 'reason', 'same-urgent', 'empty'])
def test_update_without_a_trigger_keeps_a_relative_deadline_and_owner_status(engine, runtime, args):
    wake = owner_add(engine, runtime)
    assert not commands.handle('watch ' + wake.id, runtime).startswith('Error:')
    _, converted = update(engine, wake, trigger=EVENT)
    assert engine.tool.handle({'action': 'resume', 'id': wake.id}, session_id='sid')['ok']
    resumed = engine.store.get_wake(wake.id)
    engine.clock.advance(600)
    _, after = update(engine, resumed, **args)
    assert after.deadline_at == resumed.deadline_at  # the stored relative text is not re-read
    assert not after.agent_edited and is_owner(after)
