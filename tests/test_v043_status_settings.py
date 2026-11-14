"""Status and desk show the settings that shape what happens: item headroom (#171), the time zone and where it comes from (#172),
and the quiet-hours range before it first holds a wake (#173)."""
from dataclasses import replace

from heartbeat2 import commands
from heartbeat2.desk import render_desk
from heartbeat2.policy import Policy
from test_engine import engine, arm, KEY  # noqa: F401
from test_commands import runtime  # noqa: F401


def _status(runtime):
    return commands.handle('status', runtime)


def test_status_counts_items_the_way_the_limits_do(runtime):
    e = runtime.get_engine()
    arm(e, 'at', next_due_at=e.clock.now() + 600)
    arm(e, 'every', status='proposed')
    arm(e, 'self', trigger_json={'builtin': 'own_time', 'own_time': True})  # a built-in: never counted
    arm(e, 'at', status='paused', next_due_at=e.clock.now() + 600)  # paused: not counted either
    e.store.create_wake(replace(e.store.get_wake(arm(e, 'at').id), session_key='agent:telegram:dm:other', id='w_other'))
    text = _status(runtime)
    assert f'Limits: 3 of {e.policy.max_active} items in this chat, 4 of {e.policy.max_active_profile} in this profile' in text
    assert "check-in, own time and keep-warm don't count" in text and 'max_active' in text


def test_a_full_chat_refusal_says_how_many_and_who_can_raise_it(engine):
    e = engine
    e.policy = replace(e.policy, max_active=2)
    gate = e.policy.cap_gate(2, 2)
    assert not gate.allowed and '(2 in use, reminders included' in gate.detail
    assert 'Only the owner can raise the limit, with the max_active setting.' in gate.detail
    gate = e.policy.cap_gate(0, e.policy.max_active_profile)
    assert f'({e.policy.max_active_profile} in use' in gate.detail and 'max_active_profile setting' in gate.detail


def test_status_names_the_zone_and_its_source(runtime):
    e = runtime.get_engine()
    e.policy = replace(e.policy, timezone='Europe/Paris')
    assert 'zone Europe/Paris (plugin setting)' in _status(runtime)
    e.policy = replace(e.policy, timezone='', profile_timezone='Asia/Tokyo')
    text = _status(runtime)
    assert 'zone Asia/Tokyo (Hermes)' in text and 'pins' not in text


def test_a_plugin_pin_that_differs_from_hermes_is_flagged(runtime):
    e = runtime.get_engine()
    e.policy = replace(e.policy, timezone='Asia/Seoul', profile_timezone='Asia/Bangkok')
    text = _status(runtime)
    assert "the plugin's `timezone` setting pins Asia/Seoul, while Hermes uses Asia/Bangkok" in text
    assert 'clear plugins.entries.heartbeat2.settings.timezone' in text
    e.policy = replace(e.policy, profile_timezone='Asia/Seoul')  # an intentional pin that agrees: no notice
    assert 'pins' not in _status(runtime)


def test_a_pin_without_a_hermes_zone_is_not_flagged():
    # With no Hermes zone, a plugin pin is the only setting; the server's own zone is not a rival to warn about.
    assert Policy(timezone='Asia/Seoul').timezone_pin_notice() == ''


def test_status_shows_the_default_quiet_window_before_it_holds_anything(runtime):
    e = runtime.get_engine()
    e.policy = replace(e.policy, quiet_hours=Policy().quiet_hours)  # 22:00-08:00; the fake clock is 12:00 UTC
    assert 'quiet: 22:00-08:00 (default), off now' in _status(runtime)
    e.clock.advance(11 * 3600)  # 23:00
    assert 'quiet: 22:00-08:00 (default), on until' in _status(runtime)
    e.policy = replace(e.policy, quiet_hours='23:30-07:00')
    assert 'quiet: 23:30-07:00 (settings), off now' in _status(runtime)
    e.policy = replace(e.policy, quiet_hours='')
    assert 'quiet: off in settings, off now' in _status(runtime)


def test_the_desk_shows_headroom_and_the_quiet_window(engine):
    e = engine
    e.policy = replace(e.policy, quiet_hours=Policy().quiet_hours, timezone='Asia/Tokyo')
    arm(e, 'at', next_due_at=e.clock.now() + 600)
    session = e.store.get_session(KEY)
    desk = render_desk(e, session, clock=e.clock, timezone=e.policy.resolved_timezone(), policy=e.policy)
    assert f'- Items 1/{e.policy.max_active} in this chat, 1/{e.policy.max_active_profile} in the profile' in desk
    assert 'quiet hours 22:00-08:00 (default, Asia/Tokyo)' in desk


def test_the_usage_skill_teaches_the_quiet_default_and_the_owner_commands():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / 'skills/heartbeat2-usage/SKILL.md').read_text()
    assert 'by default 22:00-08:00' in text
    assert all(c in text for c in ('/pulse quiet off', '/pulse quiet default', 'Focus'))
