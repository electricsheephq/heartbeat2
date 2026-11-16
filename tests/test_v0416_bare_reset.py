"""#198: only a real /new (both ids, different) orphans one-shot wakes.

Hermes fires ``on_session_reset`` with a bare ``session_id`` each time an app or Desktop opens or rebuilds an
existing session (tui_gateway), and with ``new_session_id=None`` when a viewer leaves. Neither rotates the
owner's conversation, so neither may orphan its wakes.
"""
import json

import pytest

from test_engine import KEY, arm, engine, session, start  # noqa: F401  (engine is a fixture)

NOTICE = 'Wakes from the old conversation were orphaned after /new.'

NOT_A_ROTATION = {
    # tui_gateway/session_lifecycle._notify_session_boundary after session_registry._start_session_services
    'app_open_bare_session_id': dict(session_id='sid', platform='desktop'),
    # gateway/session_viewer_close._run_close_lifecycle: the viewer's next id is not known yet
    'viewer_close_no_new_id': dict(session_id=None, reason='new_session', platform='desktop',
                                   old_session_id='sid', new_session_id=None),
    'same_ids': dict(session_id='sid', reason='new_session', platform='telegram',
                     old_session_id='sid', new_session_id='sid'),
    'no_ids': dict(platform='telegram'),
}


def notices(e):
    return json.loads(e.store.get_meta('notice:'+KEY) or '[]')


@pytest.mark.parametrize('with_role', [False, True])
@pytest.mark.parametrize('shape', sorted(NOT_A_ROTATION))
def test_reset_that_is_not_a_rotation_changes_nothing(engine, shape, with_role):
    e = engine
    one_shot = arm(e, next_due_at=e.clock.now()+3600)
    role = arm(e, 'self', role=True, next_due_at=e.clock.now()+3600) if with_role else None
    session(e, last_cache_touch_at=e.clock.now(), last_prompt_tokens=1234, last_route='route')
    e.intake.on_session_reset(**NOT_A_ROTATION[shape])
    w = e.store.get_wake(one_shot.id)
    assert (w.status, w.status_reason, w.armed_session_id) == ('active', '', 'sid')
    if role:
        assert e.store.get_wake(role.id).armed_session_id == 'sid'
    s = e.store.get_session(KEY)
    assert (s.current_session_id, s.last_cache_touch_at, s.last_prompt_tokens, s.last_route) == (
        'sid', e.clock.now(), 1234, 'route')
    assert NOTICE not in notices(e)


def test_real_new_still_orphans_one_shots_and_rebinds_roles(engine):
    e = engine
    one_shot = arm(e, next_due_at=e.clock.now()+3600)
    role = arm(e, 'self', role=True, next_due_at=e.clock.now()+3600)
    # gateway/slash_commands_session._handle_reset_command, the same on every supported Hermes pin
    e.intake.on_session_reset(session_id='sid-new', reason='new_session', platform='telegram',
                              old_session_id='sid', new_session_id='sid-new')
    w = e.store.get_wake(one_shot.id)
    assert (w.status, w.status_reason) == ('orphaned', 'session_reset')
    assert e.store.get_wake(role.id).armed_session_id == 'sid-new'
    assert e.store.get_session(KEY).current_session_id == 'sid-new'
    assert NOTICE in notices(e)


def test_ignored_reset_then_a_real_conversation_change_is_still_caught(engine):
    e = engine
    one_shot = arm(e, next_due_at=e.clock.now()+3600)
    e.intake.on_session_reset(**NOT_A_ROTATION['viewer_close_no_new_id'])
    assert e.store.get_wake(one_shot.id).status == 'active'
    # The owner's next turn arrives in a different conversation that is not a compression continuation.
    start(e, sid='sid-2', message='hello')
    w = e.store.get_wake(one_shot.id)
    assert (w.status, w.status_reason) == ('orphaned', 'session_switched')
