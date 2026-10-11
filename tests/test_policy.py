from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from heartbeat2.model import Wake
from heartbeat2.policy import Policy


def make_wake(**changes):
    return replace(Wake("gateway:telegram:dm:test", "Check the work.", "every",
                        base_interval_s=120), **changes)


def test_defaults_are_frozen_and_settings_untouched(clock):
    settings = {"agent_can_create": True, "quiet_hours": False,
                "probe_commands": False, "enabled": "off"}
    before = settings.copy()
    p = Policy.from_settings(settings, clock=clock)
    assert p.agent_can_create == "yes" and p.quiet_hours == ""
    assert p.probe_commands == "" and p.enabled is False
    assert settings == before
    with pytest.raises(FrozenInstanceError):
        p.enabled = True
    defaults = Policy.from_settings({}, clock=clock)
    assert (defaults.max_model_wakes_per_day, defaults.max_messages_per_day,
            defaults.min_interval_minutes, defaults.agent_ungated_floor_minutes,
            defaults.noop_pause_after, defaults.ledger_days) == (24, 6, 15, 60, 6, 30)


@pytest.mark.parametrize("key", ["agent_can_create", "timezone", "quiet_hours", "probe_commands"])
def test_yaml_boolean_strings(key, clock):
    assert isinstance(getattr(Policy.from_settings({key: False}, clock=clock), key), str)


@pytest.mark.parametrize("platform", ["cli", "tui", "desktop", "api_server", "cron", "local", "", "made_up"])
def test_I14_non_gateway_refused(platform, clock):
    p = Policy.from_settings({}, clock=clock)
    result = p.creation_gate(session_key="s", platform=platform, chat_type="dm",
                             allow_gateway_injection=True)
    assert not result.allowed and result.reason == "non_gateway"


@pytest.mark.parametrize("changes,reason", [
    ({"session_key": "default"}, "missing_session"),
    ({"session_key": ""}, "missing_session"),
    ({"chat_type": "group"}, "dm_only"),
    ({"chat_type": ""}, "dm_only"),
    ({"allow_gateway_injection": False}, "injection_disabled"),
    ({"active_count": 10}, "max_active"),
    ({"global_active_count": 50}, "global_max_active"),
    ({"created_in": "wake_turn", "depth": 3}, "rearm_depth"),
    ({"created_in": "wake_turn", "children_created": 1}, "rearm_child"),
    ({"untrusted_payload": True, "created_in": "wake_turn", "kind": "file"}, "untrusted_rearm"),
    ({"untrusted_payload": True, "created_in": "wake_turn", "kind": "every"}, "untrusted_rearm"),
])
def test_I11_I15_I18_I20_creation_gate(changes, reason, clock):
    kwargs = dict(session_key="s", platform="telegram", chat_type="dm",
                  allow_gateway_injection=True)
    kwargs.update(changes)
    verdict = Policy.from_settings({}, clock=clock).creation_gate(**kwargs)
    assert not verdict.allowed and verdict.reason == reason
    if reason == "injection_disabled":
        assert "plugins:\n  entries:\n    heartbeat2:\n      allow_gateway_injection: true" in verdict.detail


@pytest.mark.parametrize("settings,allowed,reason,status", [
    ({}, True, "", "active"), ({"enabled": False}, False, "disabled", "active"),
    ({"agent_can_create": False}, False, "agent_create_disabled", "active"),
    ({"agent_can_create": "ask_first"}, True, "", "proposed"),
    ({"source_file": False}, False, "source_disabled", "active"),
])
def test_creation_switches(settings, allowed, reason, status, clock):
    v = Policy.from_settings(settings, clock=clock).creation_gate(
        session_key="s", platform="telegram", chat_type="dm",
        allow_gateway_injection=True, kind="file")
    assert (v.allowed, v.reason, v.status) == (allowed, reason, status)


def test_deescalation_and_item_ownership(clock):
    p = Policy.from_settings({"enabled": False, "agent_can_create": "no"}, clock=clock)
    agent = make_wake()
    assert p.mutation_gate(agent, action="cancel", session_key=agent.session_key,
                           created_in="wake_turn").allowed
    assert not p.mutation_gate(agent, action="pause", session_key="other",
                               created_in="human_turn").allowed
    owner = replace(agent, creator="user")
    assert not p.mutation_gate(owner, action="cancel", session_key=owner.session_key,
                               created_in="wake_turn").allowed
    assert p.mutation_gate(owner, action="cancel", session_key=owner.session_key,
                           created_in="human_turn").allowed


@pytest.mark.parametrize("wake,interval,expiry,max_fires", [
    (make_wake(), 3600, 7 * 86400, None),
    (make_wake(creator="user"), 900, 14 * 86400, None),
    (make_wake(babysit=True, expires_at=10**12, max_fires=100), 300, 4 * 3600, 24),
    (make_wake(expires_at=10**12), 3600, 14 * 86400, None),
])
def test_I18_interval_lifetime_babysit_clamps(wake, interval, expiry, max_fires, clock):
    result = Policy.from_settings({}, clock=clock).clamp(wake)
    assert result.wake.base_interval_s == interval
    assert result.wake.eff_interval_s == interval
    assert result.wake.expires_at == clock.now() + expiry
    assert result.wake.max_fires == max_fires
    assert "every" in result.clamped and "expires" in result.clamped


def test_all_length_pacing_probe_and_babysit_clamps(clock):
    p = Policy.from_settings({}, clock=clock)
    result = p.clamp(make_wake(title="x" * 81, note="n" * 2001, reason="r" * 281,
                              scratch="s" * 4001, pacing_min_s=1, pacing_max_s=2))
    assert [len(getattr(result.wake, k)) for k in ("title", "note", "reason", "scratch")] == [80, 2000, 280, 4000]
    assert result.wake.pacing_min_s == result.wake.pacing_max_s == 3600
    assert p.clamp(make_wake(), probe_gated=True).wake.base_interval_s == 900
    assert {"title", "note", "reason", "scratch", "pacing.min", "pacing.max"} <= result.clamped.keys()
    probe = p.clamp(make_wake(kind="file", trigger_json={"check_every": 1}))
    assert probe.wake.trigger_json["check_every"] == 60 and "check_every" in probe.clamped
    with pytest.raises(ValueError, match="babysit"):
        p.clamp(make_wake(babysit=True, created_in="wake_turn"))
    with pytest.raises(ValueError, match="babysit"):
        p.clamp(make_wake(babysit=True, kind="cron"))


def test_unanswered_urgent_exception(clock):
    p = Policy.from_settings({}, clock=clock)
    assert p.unanswered_gate(make_wake(), 2) == "unanswered"
    assert p.unanswered_gate(make_wake(urgent=True), 2) == "unanswered"
    assert p.unanswered_gate(make_wake(urgent=True, creator="user"), 2) is None


def test_command_allowlist_is_argv_prefix(clock):
    p = Policy.from_settings({}, clock=clock)
    assert p.command_allowed(["gh", "pr", "view", "123"])
    assert not p.command_allowed(["gh", "pr", "viewer"])
    assert not p.command_allowed(["sh", "-c", "gh pr view"])
    assert not Policy.from_settings({"probe_commands": False}, clock=clock).command_allowed(["gh", "pr", "view"])


@pytest.mark.parametrize("stamp,is_quiet", [("2025-10-12T21:59:00", False),
    ("2025-10-12T22:00:00", True), ("2025-10-13T07:59:00", True),
    ("2025-10-13T08:00:00", False)])
def test_quiet_crossing_midnight(stamp, is_quiet, clock):
    clock.wall = datetime.fromisoformat(stamp).replace(tzinfo=ZoneInfo("UTC")).timestamp()
    p = Policy.from_settings({"timezone": "UTC"}, clock=clock)
    assert (p.quiet_until(urgent=False) is not None) == is_quiet
    assert p.quiet_until(urgent=True) == p.quiet_until(urgent=False)
    bypass = Policy.from_settings({"timezone": "UTC", "quiet_hours_urgent": True}, clock=clock)
    assert bypass.quiet_until(urgent=True) is None


def test_policy_DST_quiet_end(clock):
    clock.wall = datetime.fromisoformat("2025-03-09T01:00:00-05:00").timestamp()
    p = Policy.from_settings({"timezone": "America/New_York", "quiet_hours": "22:00-02:30"}, clock=clock)
    assert datetime.fromtimestamp(p.quiet_until(), ZoneInfo("America/New_York")).isoformat() == "2025-03-09T03:00:00-04:00"


def test_pacing_min_max_and_cron_floor_are_reported(clock):
    p = Policy.from_settings({}, clock=clock)
    result = p.clamp(make_wake(base_interval_s=4000, pacing_min_s=7200, pacing_max_s=8000))
    assert result.wake.base_interval_s == result.wake.eff_interval_s == 7200
    assert "every" in result.clamped and "pacing_min" in result.clamped["every"]
    result = p.clamp(make_wake(base_interval_s=10000, pacing_min_s=3600, pacing_max_s=7200))
    assert result.wake.base_interval_s == result.wake.eff_interval_s == 7200
    assert "pacing_max" in result.clamped["every"]
    cron = p.clamp(make_wake(kind="cron", base_interval_s=None, trigger_json={"cron": "* * * * *"}))
    assert cron.wake.base_interval_s == cron.wake.eff_interval_s == 3600
    assert "cron" in cron.clamped
