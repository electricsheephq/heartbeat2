"""Pure wall-clock scheduling. Callers supply now; no timers or ambient clock.

Missed ticks collapse to one, following Hermes heartbeat.py's coalescing idea.
DST boundaries use the first valid minute after a gap; quiet-end folds use the
later instant so the repeated hour cannot let a quiet wake through early.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
import math
import re
from zoneinfo import ZoneInfo

from .model import Wake, agent_authored

_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(s(?:econds?|ecs?)?|m(?:inutes?|ins?)?|h(?:ours?|rs?)?|d(?:ays?)?|w(?:eeks?|ks?)?)", re.I)


def parse_duration(value: str | float) -> float:
    error = f"invalid duration '{str(value)[:60]}': use a number and unit such as 10m, 2 hours or 1h30m"
    if isinstance(value, bool):
        raise ValueError(error)
    if isinstance(value, (int, float)):
        try: result = float(value)
        except OverflowError: raise ValueError(error) from None
    elif isinstance(value, str):
        text = re.sub(r"^(?:in\s+|\+)", "", value.strip(), flags=re.I)
        result, pos = 0.0, 0
        for match in _DURATION.finditer(text):
            gap = text[pos:match.start()]
            if (not pos and gap.strip()) or (pos and not re.fullmatch(r"\s*(?:,\s*)?(?:and\s*)?", gap, re.I)):
                raise ValueError(error)
            result += float(match[1]) * {"s": 1, "m": 60, "h": 3600,
                                        "d": 86400, "w": 604800}[match[2][0].lower()]
            pos = match.end()
        if not pos or text[pos:].strip():
            raise ValueError(error)
    else:
        raise ValueError(error)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(error)
    return result


def zone(timezone: str | tzinfo) -> tzinfo:
    # Policy resolves a blank timezone before calling scheduling functions.
    return ZoneInfo(timezone) if isinstance(timezone, str) else timezone


def _hm(value: str) -> tuple[int, int]:
    if not re.fullmatch(r"\d{2}:\d{2}", value):
        raise ValueError("expected HH:MM")
    hour, minute = map(int, value.split(":"))
    if hour > 23 or minute > 59:
        raise ValueError("invalid HH:MM")
    return hour, minute


def _local_instant(naive: datetime, tz: tzinfo, *, later: bool = False) -> float:
    # Round-trip checks distinguish real local times from ZoneInfo's imaginary
    # spring-gap values. No clock reads or I/O are involved.
    for _ in range(24 * 60 + 1):
        candidates = []
        for fold in (0, 1):
            aware = naive.replace(tzinfo=tz, fold=fold)
            stamp = aware.timestamp()
            if datetime.fromtimestamp(stamp, tz).replace(tzinfo=None) == naive:
                candidates.append(stamp)
        if candidates:
            return max(candidates) if later else min(candidates)
        naive += timedelta(minutes=1)
    raise ValueError("no valid local time within one day")


def parse_at(value: str, *, now: float, timezone: str | tzinfo) -> float:
    try: return _parse_at(value, now=now, timezone=timezone)
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith('invalid duration '): raise
        raise ValueError(f"invalid time '{str(value)[:60]}': use HH:MM, tomorrow HH:MM, in 10m, or an ISO date-time") from None


def _parse_at(value: str, *, now: float, timezone: str | tzinfo) -> float:
    tz = zone(timezone)
    text = value.strip()
    if text.startswith("+") or text.lower().startswith("in "):
        return now + parse_duration(text)
    clock = re.fullmatch(r"(?:today\s+)?(\d{1,2}:\d{2})", text, re.I)
    if clock or text.lower().startswith("tomorrow "):
        hour, minute = _hm(clock[1].zfill(5) if clock else text.split(maxsplit=1)[1])
        date = datetime.fromtimestamp(now, tz).date() + timedelta(days=0 if clock else 1)
        local = datetime(date.year, date.month, date.day, hour, minute)
        if clock:  # today's first fold, its repeated fold, else tomorrow
            return next(t for t in (_local_instant(local, tz), _local_instant(local, tz, later=True),
                                    _local_instant(local + timedelta(days=1), tz)) if t > now)
    else:
        local = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if local.tzinfo is not None:
            return local.timestamp()
    return _local_instant(local, tz)


def next_every(now: float, interval_s: float, *, jitter_unit: float = 0,
               anchor: str | None = None, timezone: str | tzinfo = "UTC", cache_ttl=None, minimum_s=0) -> float:
    interval_s = parse_duration(interval_s)
    if not math.isfinite(jitter_unit) or not -1 <= jitter_unit <= 1:
        raise ValueError("jitter_unit must be between -1 and 1")
    if anchor is None:
        if cache_ttl and interval_s <= 1.25*cache_ttl:
            edge=cache_ttl-max(30,.05*cache_ttl)
            if interval_s>edge and edge>=minimum_s: interval_s=edge
            return now+max(minimum_s,interval_s*(1-.1*abs(jitter_unit)))
        return now + interval_s * (1 + 0.1 * jitter_unit)
    hour, minute = _hm(anchor)
    tz = zone(timezone)
    local = datetime.fromtimestamp(now, tz)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0,
                              tzinfo=None)
    stamp = _local_instant(candidate, tz)
    if stamp > now:
        return stamp
    # Whole-day anchors retain local wall time over DST; shorter intervals are
    # elapsed time from today's anchor. Polling later reanchors at now (§9).
    if interval_s >= 86400 and interval_s % 86400 == 0:
        return _local_instant(candidate + timedelta(days=interval_s / 86400), tz)
    return stamp + (math.floor((now - stamp) / interval_s) + 1) * interval_s


def next_cron(expression: str, *, now: float, timezone: str | tzinfo,
              minimum_interval_s: float = 0) -> float:
    try:
        from croniter import croniter
    except ImportError as exc:
        raise ValueError("cron wakes require Hermes-provided croniter") from exc
    try:
        if not math.isfinite(minimum_interval_s) or minimum_interval_s < 0:
            raise ValueError("invalid cron minimum interval")
        # Search from just before the permitted boundary, including a cron tick
        # exactly on it, without looping over thousands of skipped minute ticks.
        earliest = now if not minimum_interval_s else now + minimum_interval_s - 0.000001
        return croniter(expression, datetime.fromtimestamp(earliest, zone(timezone))).get_next(datetime).timestamp()
    except (ValueError, KeyError, OverflowError) as exc:
        raise ValueError("invalid cron expression") from exc


def quiet_end(now: float, quiet_hours: str, timezone: str | tzinfo) -> float | None:
    if not quiet_hours:
        return None
    try:
        start, end = quiet_hours.split("-")
        start_hm, end_hm = _hm(start), _hm(end)
    except (ValueError, AttributeError) as exc:
        raise ValueError("quiet_hours must be HH:MM-HH:MM") from exc
    if start_hm == end_hm:
        return None
    tz = zone(timezone)
    date = datetime.fromtimestamp(now, tz).date()
    for offset in (-1, 0):
        begin_day = date + timedelta(days=offset)
        end_day = begin_day + timedelta(days=int(start_hm > end_hm))
        begin = _local_instant(datetime.combine(begin_day, datetime.min.time()).replace(
            hour=start_hm[0], minute=start_hm[1]), tz)
        finish = _local_instant(datetime.combine(end_day, datetime.min.time()).replace(
            hour=end_hm[0], minute=end_hm[1]), tz, later=True)
        if begin <= now < finish:
            return finish
    return None


def local_day(now: float, timezone: str | tzinfo) -> str:
    return datetime.fromtimestamp(now, zone(timezone)).date().isoformat()


@dataclass(frozen=True)
class DueTime:
    state: str
    dedupe_key: str
    scheduled_for: float
    next_due_at: float | None


def time_due(wake: Wake, *, now: float, catch_up_s: float,
             timezone: str | tzinfo = "UTC", jitter_unit: float = 0, cache_ttl=None, minimum_s=0) -> DueTime | None:
    due = wake.next_due_at
    if wake.status != "active" or wake.kind not in {"at", "every", "self", "cron"} or due is None or due > now:
        return None
    key = f"t:{due:.17g}"
    if wake.kind == "at":
        return DueTime("missed" if now - due > catch_up_s else "pending", key, due, None)
    if wake.kind == "cron":
        next_due = next_cron(wake.trigger_json["cron"], now=now, timezone=timezone,
                             minimum_interval_s=wake.eff_interval_s or 0)
    elif wake.kind == "every" and wake.trigger_json.get("at"):
        next_due = next_every(now, wake.eff_interval_s, anchor=wake.trigger_json["at"], timezone=timezone)
    else:
        next_due = next_every(now, wake.eff_interval_s, jitter_unit=jitter_unit,
            cache_ttl=cache_ttl if agent_authored(wake) else None,minimum_s=minimum_s)
    return DueTime("pending", key, due, next_due)
