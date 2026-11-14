"""Settings-owned bounds. Decisions never mutate settings or perform delivery."""
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, tzinfo
import math
import os
import shlex
from typing import Mapping, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .clock import Clock, SystemClock
from .model import Wake, RECURRING_KINDS, PROBE_KINDS, EVENT_KINDS, KINDS, agent_authored
from .schedule import parse_duration, quiet_end


def is_owner(wake):
    return wake.creator == 'user' and not wake.role and not wake.trigger_json.get('builtin') and not wake.agent_edited


def owner_requested(wake):
    return (bool(wake.owner_requested) and wake.creator == 'agent' and wake.kind == 'at'
            and wake.parent_wake_id is None and not wake.babysit and not wake.trigger_json.get('builtin'))


def owner_class(wake):
    return is_owner(wake) or owner_requested(wake)


def is_watcher(wake):
    return wake.kind in PROBE_KINDS | EVENT_KINDS


def holds_messages(wake):
    return not is_owner(wake) and wake.kind in RECURRING_KINDS and not is_watcher(wake)


def one_shot(wake):
    return wake.kind == 'at'


def is_keep_warm(wake):
    """The owner's per-chat cache keep-warm ping (#163): a settings-like built-in, never agent work."""
    trigger = wake.trigger_json
    return isinstance(trigger, dict) and bool(trigger.get('builtin') and trigger.get('keep_warm'))

# Built-in messaging gateways at Hermes 04eb00cef7, gateway/config.py:217.
# Registered plugin adapters can be admitted explicitly by the Hermes adapter.
MESSAGING_PLATFORMS = frozenset({"telegram", "discord", "whatsapp", "whatsapp_cloud",
    "slack", "signal", "mattermost", "matrix", "email", "sms", "dingtalk", "feishu",
    "wecom", "wecom_callback", "weixin", "bluebubbles", "qqbot", "yuanbao", "relay"})
NON_GATEWAY_PLATFORMS = frozenset({"cli", "tui", "desktop", "api_server", "cron", "local"})
INJECTION_CONFIG = "plugins:\n  entries:\n    heartbeat2:\n      allow_gateway_injection: true"


def deliverable_platform(platform) -> bool:
    return platform in MESSAGING_PLATFORMS


def cap_counts(store, session_key, exclude_id=None) -> tuple[int, int]:
    """Settings-owned built-ins never consume user wake slots."""
    wakes = [w for w in store.list_wakes() if w.id != exclude_id
             and w.status in {'active', 'proposed'} and not (isinstance(w.trigger_json, dict) and w.trigger_json.get('builtin'))]
    return sum(w.session_key == session_key for w in wakes), len(wakes)


def unknown_kind(kind):
    return Gate(False, "unknown_kind", f"unknown trigger kind '{kind}'; valid kinds: {', '.join(sorted(KINDS))}. "
                'A one-time reminder is {"kind": "at", "at": "in 10m"}.')


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
    daily_cost_limit_usd: float = 0.0
    own_time_hours: int = 0
    role_turns_per_day_default: int = 6
    role_turns_per_day_max: int = 12
    max_children_per_wake_turn: int = 3
    max_messages_per_day: int = 6
    max_unanswered: int = 2
    min_interval_minutes: int = 15
    agent_ungated_floor_minutes: int = 55
    wake_spacing_minutes: int = 10
    probe_floor_seconds: int = 60
    max_active: int = 50
    max_active_profile: int = 200
    max_lifetime_days: int = 14
    max_reminder_days: int = 365
    owner_silent_checkin: bool = False
    keep_warm_idle_hours: int = 12
    keep_warm_min_tokens: int = 20000
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
    url_allow_private: bool = False
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
            elif isinstance(default, float):
                if isinstance(value, bool):
                    raise ValueError(f'invalid number setting: {f.name}')
                value = float(value)
                if not math.isfinite(value) or value < 0:
                    raise ValueError(f'invalid nonnegative number setting: {f.name}')
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
        role_max = max(1, min(result.role_turns_per_day_max, result.max_model_wakes_per_day))
        result = replace(result, role_turns_per_day_max=role_max,
                         role_turns_per_day_default=max(1, min(result.role_turns_per_day_default, role_max)),
                         max_children_per_wake_turn=max(1, min(result.max_children_per_wake_turn, 50)))
        for name in ("min_interval_minutes", "agent_ungated_floor_minutes", "probe_floor_seconds",
                     "max_lifetime_days", "max_reminder_days", "checkin_after_hours", "noop_backoff_factor",
                     "noop_max_backoff_hours", "ledger_days", "keep_warm_idle_hours"):
            if getattr(result, name) < 1:
                raise ValueError(f"{name} must be positive")
        # Readable upper bounds, each far inside the value where date, float
        # or SQLite arithmetic downstream would overflow (with 10% jitter).
        for name, maximum in {
            "ledger_days": 36500,
            "min_interval_minutes": 525600,
            "agent_ungated_floor_minutes": 525600,
            "noop_backoff_factor": 100,
            "noop_max_backoff_hours": 8760,
            "probe_floor_seconds": 604800,
            "max_lifetime_days": 3650,
            "max_reminder_days": 3650,
            "checkin_after_hours": 8760,
            "own_time_hours": 168,
            "max_active": 1000,
            "max_active_profile": 10000,
            "keep_warm_idle_hours": 168,
            "keep_warm_min_tokens": 10000000,
        }.items():
            if getattr(result, name) > maximum:
                raise ValueError(f"{name} exceeds maximum {maximum:g}")
        # Validate configured boundaries without consulting the real wall clock.
        if result.timezone:
            try:
                ZoneInfo(result.timezone)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(f"invalid timezone: {result.timezone}") from exc
        quiet_end(result.clock.now(), result.quiet_hours, result.resolved_timezone())
        return result

    def timezone_source(self) -> tuple[str, str]:
        """The effective zone's name and where it comes from, for status and desk (#172)."""
        if self.timezone:
            return self.timezone, "plugin setting"
        return self.inherited_timezone()

    def inherited_timezone(self) -> tuple[str, str]:
        """The zone the plugin follows when its own `timezone` setting is blank: Hermes', then TZ, then the system's."""
        if self.profile_timezone:
            try:
                ZoneInfo(self.profile_timezone)
                return self.profile_timezone, "Hermes"
            except (ZoneInfoNotFoundError, ValueError):
                pass
        try:
            from hermes_time import get_timezone
            tz = get_timezone()
            if tz is not None:
                return getattr(tz, "key", None) or str(tz), "Hermes"
        except Exception:
            pass
        if os.environ.get("TZ"):
            try:
                return ZoneInfo(os.environ["TZ"].lstrip(":")).key, "TZ"
            except (ZoneInfoNotFoundError, ValueError):
                pass
        tz = self.resolved_timezone()
        return getattr(tz, "key", None) or datetime.fromtimestamp(self.clock.now(), tz).tzname() or "local", "system"

    def timezone_pin_notice(self) -> str:
        """A plugin `timezone` that differs from the zone Hermes sets: shown so a stale pin isn't mistaken for Hermes' zone (#172)."""
        if not self.timezone:
            return ""
        inherited, source = self.inherited_timezone()
        if source != "Hermes" or inherited == self.timezone:
            return ""
        return (f"Time zone: the plugin's `timezone` setting pins {self.timezone}, while Hermes uses {inherited}. "
                "To follow Hermes, clear plugins.entries.heartbeat2.settings.timezone.")

    def quiet_hours_source(self) -> str:
        """Where the profile's quiet hours come from: the built-in default or the owner's settings (#173)."""
        return "default" if self.quiet_hours == type(self).__dataclass_fields__["quiet_hours"].default else "settings"

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
                      registered_gateway: bool = False, parent_role: bool = False) -> Gate:
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
            return unknown_kind(kind)
        family = ("silence" if kind == "owner_silent" else "events" if kind in EVENT_KINDS
                  else kind if kind in PROBE_KINDS else None)
        if family and not getattr(self, f"source_{family}"):
            return Gate(False, "source_disabled")
        cap = self.cap_gate(active_count, global_active_count)
        if not cap.allowed:
            return cap
        if created_in == "wake_turn":
            if not parent_role and depth > self.max_rearm_chain:
                return Gate(False, "rearm_depth")
            if children_created >= (self.max_children_per_wake_turn if parent_role else 1):
                return Gate(False, "rearm_child")
            if untrusted_payload and kind != "at":
                return Gate(False, "untrusted_rearm")
        return Gate(True, status="proposed" if creator == "agent" and self.agent_can_create == "ask_first" else "active")

    def cap_gate(self, active_count, global_active_count) -> Gate:
        if active_count >= self.max_active:
            return Gate(False, "max_active", f"This chat has reached its limit of {self.max_active} active or proposed wakes "
                        f"({active_count} in use, reminders included; the check-in, own time and keep-warm don't count). "
                        "Cancel one with /pulse cancel <id> or pulse cancel. Only the owner can raise the limit, with the max_active setting.")
        if global_active_count >= self.max_active_profile:
            return Gate(False, "global_max_active", f"This profile has reached its limit of {self.max_active_profile} active or proposed wakes "
                        f"across its chats ({global_active_count} in use, reminders included; check-ins, own time and keep-warm don't count). "
                        "Cancel some with /pulse cancel <id> in their chats. Only the owner can raise the limit, with the max_active_profile setting.")
        return Gate(True)

    def mutation_gate(self, wake: Wake, *, action: str, session_key: str,
                      created_in: str, new_kind: str | None = None,
                      babysit: bool | None = None, untrusted_payload: bool = False) -> Gate:
        if wake.session_key != session_key:
            return Gate(False, "wrong_session")
        if wake.trigger_json.get('own_time') and action in {'pause','cancel','off'}:
            return Gate(False, 'builtin_settings_owned', 'Own time is set in Settings (own_time_hours).')
        if is_keep_warm(wake):
            return Gate(False, 'builtin_settings_owned', 'Keep-warm is the owner\'s: they turn it on and off with /pulse warm on|off.')
        if wake.role and babysit:
            return Gate(False, 'role_not_babysit')
        if wake.creator == "user" and not wake.role and created_in != "human_turn":
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
        if action == "resume" and wake.status_reason == "command" and created_in != "human_turn":
            # The owner paused it with /pulse pause: only the owner, or the agent in the owner's turn, resumes it.
            return Gate(False, "needs_owner", "The owner paused this; they can resume it with /pulse resume, or ask you to.")
        kind = new_kind if new_kind is not None else wake.kind
        if wake.role and kind not in RECURRING_KINDS:
            return Gate(False, 'role_needs_recurring')
        if kind not in KINDS:
            return unknown_kind(kind)
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
        if agent_authored(wake) and not probe_gated:
            floor = max(floor, self.agent_ungated_floor_minutes * 60)
        return floor

    def lifetime_setting(self, wake):
        # An owner reminder is never bounded below the general lifetime, so raising either keeps it valid.
        reminder = owner_class(wake) and one_shot(wake) and self.max_reminder_days >= self.max_lifetime_days
        return 'max_reminder_days' if reminder else 'max_lifetime_days'

    def max_life_s(self, wake):
        setting = self.lifetime_setting(wake)
        days = getattr(self, setting)
        if setting == 'max_reminder_days':
            days += self.max_lifetime_days
        seconds = days * 86400
        return min(seconds, 4 * 3600) if wake.babysit else seconds

    def due_bound_s(self, wake):
        # Never later than the hard end: a babysit wake keeps its 4-hour bound.
        if self.lifetime_setting(wake) == 'max_reminder_days' and not wake.babysit:
            return self.max_reminder_days * 86400
        return self.max_life_s(wake)

    def clamp(self, wake: Wake, *, probe_gated: bool = False, cache_ttl=None) -> ClampedWake:
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
        if wake.budget_turns_per_day is not None:
            value = wake.budget_turns_per_day
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError('budget.turns_per_day must be an integer')
            change('budget_turns_per_day', max(1, min(value, self.role_turns_per_day_max)),
                   'budget.turns_per_day', 'role_turns_per_day_max')
        floor = self.interval_floor(wake, probe_gated=probe_gated)
        floor_rule = "babysit_floor" if wake.babysit else ("agent_ungated_floor" if agent_authored(wake) and not probe_gated else "min_interval")
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
                    value = max(floor, 86400 // self.role_budget(wake)) if wake.role and wake.kind == 'self' else floor
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
        if not ((wake.role or wake.trigger_json.get('builtin')) and wake.expires_at is None):
            max_life = self.max_life_s(wake)
            # The implicit expiry stays max_lifetime_days for every wake; only a long owner
            # reminder outlives it, via the hard end and Tool.feasible's due-based grace.
            base_life = min(self.max_lifetime_days * 86400, 4 * 3600) if wake.babysit else self.max_lifetime_days * 86400
            default_life = min(base_life, 7 * 86400) if wake.creator == "agent" and wake.kind in RECURRING_KINDS else min(base_life, max_life)
            hard_end = now + self.max_reminder_days * 86400 if wake.role else (wake.created_at or now) + max_life
            expires = min(now + default_life, hard_end) if wake.expires_at is None else min(wake.expires_at, hard_end)
            if not math.isfinite(expires) or expires <= now:
                raise ValueError("expiry must be in the future")
            rule = 'max_reminder' if wake.role else ("max_lifetime" if wake.expires_at is None and now + default_life < hard_end else self.lifetime_setting(wake).removesuffix('_days'))
            change("expires_at", expires, "expires", "babysit_lifetime" if wake.babysit else rule)
        if wake.max_fires is not None and (isinstance(wake.max_fires, bool) or not isinstance(wake.max_fires, int) or wake.max_fires < 1):
            raise ValueError("max_fires must be positive")
        if wake.babysit:
            change("max_fires", min(wake.max_fires or 24, 24), "max_fires", "babysit_max_fires")
        from .cache import interval
        for attr in ('base_interval_s','eff_interval_s'):
            value=updates.get(attr,getattr(wake,attr))
            if value is not None:
                snapped=interval(replace(wake,**updates),value,cache_ttl,floor)
                if snapped!=value: change(attr,snapped,attr,'prompt_cache')
        return ClampedWake(replace(wake, **updates), clamped)

    def quiet_until(self, *, urgent: bool = False, now: float | None = None) -> float | None:
        if urgent and self.quiet_hours_urgent:
            return None
        return quiet_end(self.clock.now() if now is None else now, self.quiet_hours,
                         self.resolved_timezone())

    def role_budget(self, wake):
        return max(1, min(wake.budget_turns_per_day if wake.budget_turns_per_day is not None
                          else self.role_turns_per_day_default, self.role_turns_per_day_max))

    def unanswered_gate(self, wake: Wake, unanswered_count: int) -> str | None:
        if unanswered_count >= self.max_unanswered and not owner_class(wake):
            return "held_unanswered" if is_watcher(wake) else "unanswered"
        return None

    def command_allowed(self, argv: list[str]) -> bool:
        from .probes import command_allowed
        return self.source_command and command_allowed(argv, self.probe_commands)
