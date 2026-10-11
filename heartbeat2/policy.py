"""Settings-owned bounds. Decisions never mutate settings or perform delivery."""
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, tzinfo
import math
import os
import shlex
from typing import Mapping, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .clock import Clock, SystemClock
from .model import Wake, RECURRING_KINDS, PROBE_KINDS, EVENT_KINDS, KINDS
from .schedule import parse_duration, quiet_end

# Built-in messaging gateways at Hermes 04eb00cef7, gateway/config.py:217.
# Registered plugin adapters can be admitted explicitly by the Hermes adapter.
MESSAGING_PLATFORMS = frozenset({"telegram", "discord", "whatsapp", "whatsapp_cloud",
    "slack", "signal", "mattermost", "matrix", "email", "sms", "dingtalk", "feishu",
    "wecom", "wecom_callback", "weixin", "bluebubbles", "qqbot", "yuanbao", "relay"})
NON_GATEWAY_PLATFORMS = frozenset({"cli", "tui", "desktop", "api_server", "cron", "local"})
INJECTION_CONFIG = "plugins:\n  entries:\n    heartbeat2:\n      allow_gateway_injection: true"


@dataclass(frozen=True)
class Gate:
    allowed: bool
    reason: str = ""
    detail: str = ""
    status: str = "active"


@dataclass(frozen=True)
class ClampedWake:
    wake: Wake
    clamped: dict[str, str]


@dataclass(frozen=True)
class Policy:
    enabled: bool = True
    agent_can_create: str = "yes"
    dm_only: bool = True
    timezone: str = ""
    quiet_hours: str = "22:00-08:00"
    quiet_hours_urgent: bool = False
    max_model_wakes_per_day: int = 24
    max_messages_per_day: int = 6
    max_unanswered: int = 2
    min_interval_minutes: int = 15
    agent_ungated_floor_minutes: int = 60
    wake_spacing_minutes: int = 10
    probe_floor_seconds: int = 60
    max_active: int = 10
    max_lifetime_days: int = 14
    owner_silent_checkin: bool = False
    checkin_after_hours: int = 24
    max_rearm_chain: int = 2
    noop_slowdown_after: int = 3
    noop_backoff_factor: int = 2
    noop_max_backoff_hours: int = 24
    suggest_gate_after: int = 4
    noop_pause_after: int = 6
    ignored_pause_after: int = 3
    error_pause_after: int = 5
    engagement_window_hours: int = 6
    catch_up_hours: int = 6
    probe_commands: str = "gh pr view, gh pr checks, gh run view, gh run list, git ls-remote, git status, git log"
    source_silence: bool = True
    source_process: bool = True
    source_file: bool = True
    source_url: bool = True
    source_command: bool = True
    source_events: bool = True
    presence_line: bool = True
    ledger_days: int = 30
    clock: Clock = field(default_factory=SystemClock, repr=False, compare=False)
    profile_timezone: str = field(default="", repr=False, compare=False)

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any], *, clock: Clock | None = None,
                      profile_timezone: str = "") -> "Policy":
        defaults = cls()
        values = {}
        for f in fields(cls):
            if f.name in {"clock", "profile_timezone"} or f.name not in settings:
                continue
            value, default = settings[f.name], getattr(defaults, f.name)
            if value is None:
                continue
            if isinstance(default, bool):
                if isinstance(value, str):
                    token = value.strip().lower()
                    if token not in {"true", "false", "yes", "no", "on", "off", "1", "0"}:
                        raise ValueError(f"invalid boolean setting: {f.name}")
                    value = token in {"true", "yes", "on", "1"}
                elif value not in (True, False, 0, 1):
                    raise ValueError(f"invalid boolean setting: {f.name}")
                value = bool(value)
            elif isinstance(default, int):
                if isinstance(value, bool):
                    raise ValueError(f"invalid integer setting: {f.name}")
                number = float(value)
                if not math.isfinite(number) or not number.is_integer() or number < 0:
                    raise ValueError(f"invalid nonnegative integer setting: {f.name}")
                value = int(number)
            else:
                # YAML 1.1 turns yes/no/on/off into bools before we see them.
                if isinstance(value, bool):
                    value = ("yes" if value else "no") if f.name == "agent_can_create" else (default if value else "")
                elif not isinstance(value, str):
                    raise ValueError(f"invalid string setting: {f.name}")
                value = value.strip()
            values[f.name] = value
        result = cls(**values, clock=clock if clock is not None else SystemClock(),
                     profile_timezone=profile_timezone)
        if result.agent_can_create not in {"yes", "ask_first", "no"}:
            raise ValueError("agent_can_create must be yes, ask_first, or no")
        for name in ("min_interval_minutes", "agent_ungated_floor_minutes", "probe_floor_seconds",
                     "max_lifetime_days", "checkin_after_hours", "noop_backoff_factor",
                     "noop_max_backoff_hours", "ledger_days"):
            if getattr(result, name) < 1:
                raise ValueError(f"{name} must be positive")
        # Validate configured boundaries without consulting the real wall clock.
        if result.timezone:
            try:
                ZoneInfo(result.timezone)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(f"invalid timezone: {result.timezone}") from exc
        quiet_end(result.clock.now(), result.quiet_hours, result.resolved_timezone())
        return result

    def resolved_timezone(self) -> tzinfo:
        if self.timezone:
            try:
                return ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(f"invalid timezone: {self.timezone}") from exc
        if self.profile_timezone:
            try:
                return ZoneInfo(self.profile_timezone)
            except (ZoneInfoNotFoundError, ValueError):
                pass
        try:
            from hermes_time import get_timezone
            tz = get_timezone()
            if tz is not None:
                return tz
        except Exception:
            pass
        if os.environ.get("TZ"):
            try:
                return ZoneInfo(os.environ["TZ"].lstrip(":"))
            except (ZoneInfoNotFoundError, ValueError):
                pass
        # ZoneInfo.from_file retains system DST rules, unlike a fixed UTC offset.
        try:
            with open("/etc/localtime", "rb") as source:
                return ZoneInfo.from_file(source)
        except (OSError, ValueError):
            return datetime.fromtimestamp(self.clock.now()).astimezone().tzinfo

    def creation_gate(self, *, session_key: str | None, platform: str, chat_type: str,
                      allow_gateway_injection: bool, creator: str = "agent", kind: str = "at",
                      active_count: int = 0, global_active_count: int = 0,
                      created_in: str = "human_turn", depth: int = 0,
                      children_created: int = 0, untrusted_payload: bool = False,
                      registered_gateway: bool = False) -> Gate:
        if not session_key or session_key == "default":
            return Gate(False, "missing_session")
        if not platform or platform in NON_GATEWAY_PLATFORMS or (platform not in MESSAGING_PLATFORMS and not registered_gateway):
            return Gate(False, "non_gateway")
        if self.dm_only and chat_type != "dm":
            return Gate(False, "dm_only")
        if not self.enabled:
            return Gate(False, "disabled")
        if creator == "agent" and self.agent_can_create == "no":
            return Gate(False, "agent_create_disabled")
        if not allow_gateway_injection:
            return Gate(False, "injection_disabled", INJECTION_CONFIG)
        if kind not in KINDS:
            return Gate(False, "unknown_kind")
        family = ("silence" if kind == "owner_silent" else "events" if kind in EVENT_KINDS
                  else kind if kind in PROBE_KINDS else None)
        if family and not getattr(self, f"source_{family}"):
            return Gate(False, "source_disabled")
        if active_count >= self.max_active:
            return Gate(False, "max_active")
        if global_active_count >= 50:
            return Gate(False, "global_max_active")
        if created_in == "wake_turn":
            if depth > self.max_rearm_chain:
                return Gate(False, "rearm_depth")
            if children_created >= 1:
                return Gate(False, "rearm_child")
            if untrusted_payload and kind != "at":
                return Gate(False, "untrusted_rearm")
        return Gate(True, status="proposed" if creator == "agent" and self.agent_can_create == "ask_first" else "active")

    def mutation_gate(self, wake: Wake, *, action: str, session_key: str,
                      created_in: str, new_kind: str | None = None,
                      babysit: bool | None = None, untrusted_payload: bool = False) -> Gate:
        if wake.session_key != session_key:
            return Gate(False, "wrong_session")
        if wake.creator == "user" and created_in != "human_turn":
            return Gate(False, "owner_item")
        if action not in {"update", "resume", "pause", "cancel", "off"}:
            return Gate(False, "unknown_action")
        if action == "pause" and wake.status != "active":
            return Gate(False, "not_pausable")
        if action == "resume" and wake.status != "paused" and wake.status != "proposed":
            return Gate(False, "not_resumable")
        if wake.status in {"done", "cancelled", "orphaned", "expired", "failed"}:
            return Gate(False, "terminal_wake")
        if action in {"pause", "cancel", "off"}:
            return Gate(True)
        if not self.enabled:
            return Gate(False, "disabled")
        if action == "resume" and wake.status == "proposed":
            return Gate(False, "needs_owner_ok")
        if action == "resume" and wake.status_reason.startswith("governor:") and created_in != "human_turn":
            return Gate(False, "needs_owner")
        kind = new_kind if new_kind is not None else wake.kind
        if kind not in KINDS:
            return Gate(False, "unknown_kind")
        family = ("silence" if kind == "owner_silent" else "events" if kind in EVENT_KINDS
                  else kind if kind in PROBE_KINDS else None)
        if family and not getattr(self, f"source_{family}"):
            return Gate(False, "source_disabled")
        if wake.creator == "agent":
            if self.agent_can_create == "no":
                return Gate(False, "agent_create_disabled")
            if self.agent_can_create == "ask_first" and wake.status != "proposed":
                return Gate(False, "needs_owner_ok")
        if created_in == "wake_turn" and untrusted_payload and kind != "at":
            return Gate(False, "untrusted_rearm")
        if (wake.babysit if babysit is None else babysit) and created_in != "human_turn":
            return Gate(False, "babysit_human_turn")
        return Gate(True, status=wake.status)

    def interval_floor(self, wake: Wake, *, probe_gated: bool = False) -> float:
        if wake.babysit:
            return max(300, self.min_interval_minutes * 60)
        floor = self.min_interval_minutes * 60
        if wake.creator == "agent" and not probe_gated:
            floor = max(floor, self.agent_ungated_floor_minutes * 60)
        return floor

    def clamp(self, wake: Wake, *, probe_gated: bool = False) -> ClampedWake:
        if wake.kind not in KINDS:
            raise ValueError("unknown wake kind")
        if wake.babysit and (wake.kind not in {"every", "self"} or wake.created_in != "human_turn"):
            raise ValueError("babysit requires an every/self wake created in a human turn")
        updates, clamped = {}, {}
        def change(attr, value, label, rule):
            old = getattr(wake, attr)
            if old != value:
                updates[attr] = value
                clamped[label] = f"{old} → {value} ({rule})"
        for attr, limit in (("title", 80), ("note", 2000), ("reason", 280), ("scratch", 4000)):
            change(attr, getattr(wake, attr)[:limit], attr, "length")
        floor = self.interval_floor(wake, probe_gated=probe_gated)
        floor_rule = "babysit_floor" if wake.babysit else ("agent_ungated_floor" if wake.creator == "agent" and not probe_gated else "min_interval")
        if wake.kind in RECURRING_KINDS:
            if wake.pacing_min_s is not None:
                change("pacing_min_s", max(parse_duration(wake.pacing_min_s), floor), "pacing.min", floor_rule)
            minimum = updates.get("pacing_min_s", wake.pacing_min_s) or floor
            maximum = None
            if wake.pacing_max_s is not None:
                change("pacing_max_s", max(parse_duration(wake.pacing_max_s), minimum), "pacing.max", "pacing_min")
                maximum = updates.get("pacing_max_s", wake.pacing_max_s)
            label = "cron" if wake.kind == "cron" else "every"
            for attr, attr_label in (("base_interval_s", label), ("eff_interval_s", "effective_interval")):
                old = getattr(wake, attr)
                value = old if old is not None else wake.base_interval_s
                # Cron retains its expression; this interval is the earliest
                # permitted next scheduled time rather than a rewritten cron.
                if value is None and wake.kind in {"cron", "self"}:
                    value = floor
                if value is not None:
                    value = parse_duration(value)
                    bounded = max(value, floor, minimum)
                    rule = "pacing_min" if minimum > floor and value < minimum else floor_rule
                    if maximum is not None and bounded > maximum:
                        bounded, rule = maximum, "pacing_max"
                    change(attr, bounded, attr_label, rule)
        if wake.kind in PROBE_KINDS:
            trigger = dict(wake.trigger_json)
            old = trigger.get("check_every")
            interval = max(parse_duration(old) if old is not None else self.probe_floor_seconds, self.probe_floor_seconds)
            trigger["check_every"] = interval
            updates["trigger_json"] = trigger
            if old != interval:
                clamped["check_every"] = f"{old} → {interval} (probe_floor)"
        now = self.clock.now()
        max_life = min(self.max_lifetime_days * 86400, 4 * 3600) if wake.babysit else self.max_lifetime_days * 86400
        default_life = min(max_life, 7 * 86400) if wake.creator == "agent" and wake.kind in RECURRING_KINDS else max_life
        hard_end = (wake.created_at or now) + max_life
        expires = min(now + default_life, hard_end) if wake.expires_at is None else min(wake.expires_at, hard_end)
        if not math.isfinite(expires) or expires <= now:
            raise ValueError("expiry must be in the future")
        change("expires_at", expires, "expires", "babysit_lifetime" if wake.babysit else "max_lifetime")
        if wake.max_fires is not None and (isinstance(wake.max_fires, bool) or not isinstance(wake.max_fires, int) or wake.max_fires < 1):
            raise ValueError("max_fires must be positive")
        if wake.babysit:
            change("max_fires", min(wake.max_fires or 24, 24), "max_fires", "babysit_max_fires")
        return ClampedWake(replace(wake, **updates), clamped)

    def quiet_until(self, *, urgent: bool = False, now: float | None = None) -> float | None:
        if urgent and self.quiet_hours_urgent:
            return None
        return quiet_end(self.clock.now() if now is None else now, self.quiet_hours,
                         self.resolved_timezone())

    def unanswered_gate(self, wake: Wake, unanswered_count: int) -> str | None:
        if unanswered_count >= self.max_unanswered and not (wake.creator == "user" and wake.urgent):
            return "unanswered"
        return None

    def command_allowed(self, argv: list[str]) -> bool:
        if not self.source_command or not argv or not self.probe_commands:
            return False
        return any(argv[:len(prefix)] == prefix for token in self.probe_commands.split(",")
                   if (prefix := shlex.split(token.strip())))
