"""Shared immutable records; JSON fields contain decoded JSON values."""
from dataclasses import dataclass, field
from typing import Any
import base64
import secrets

TIME_KINDS = frozenset({"at", "every", "cron", "self"})
RECURRING_KINDS = frozenset({"every", "cron", "self"})
PROBE_KINDS = frozenset({"file", "process", "url", "command"})
EVENT_KINDS = frozenset({"kanban_completed", "kanban_blocked", "kanban_worker_exited",
                         "subagent_done", "api_error", "reaction", "message_edited"})
KINDS = TIME_KINDS | PROBE_KINDS | EVENT_KINDS | {"owner_silent"}
WAKE_STATUSES = frozenset({"active", "paused", "proposed", "done", "cancelled",
                           "expired", "failed", "orphaned"})
TERMINAL_OCCURRENCE_STATES = frozenset({"done", "skipped", "missed", "dead",
                                      "interrupted", "orphaned", "superseded"})


def agent_authored(wake) -> bool:
    """The agent chose this wake's timing: it created it, or it edited an owner wake."""
    return wake.creator == "agent" or wake.agent_edited


def new_wake_id() -> str:
    return "w_" + base64.b32encode(secrets.token_bytes(5)).decode().lower()


@dataclass(frozen=True)
class Wake:
    session_key: str
    note: str
    kind: str
    id: str = field(default_factory=new_wake_id)
    platform: str = ""
    chat_type: str = ""
    chat_id: str = ""
    armed_session_id: str = ""
    creator: str = "agent"
    created_in: str = "human_turn"
    parent_wake_id: str | None = None
    depth: int = 0
    title: str = ""
    reason: str = ""
    scratch: str = ""
    trigger_json: dict[str, Any] = field(default_factory=dict)
    probe_state_json: Any = None
    deadline_at: float | None = None
    base_interval_s: float | None = None
    eff_interval_s: float | None = None
    pacing_min_s: float | None = None
    pacing_max_s: float | None = None
    babysit: bool = False
    urgent: bool = False
    cancel_on_reply: bool = False
    max_fires: int | None = None
    expires_at: float | None = None
    status: str = "active"
    status_reason: str = ""
    next_due_at: float | None = None
    last_fired_at: float | None = None
    fires: int = 0
    model_turns: int = 0
    gated_skips: int = 0
    checks: int = 0
    no_verdict: int = 0
    agent_edited: bool = False
    noop_streak: int = 0
    ignored_streak: int = 0
    error_streak: int = 0
    delivered: int = 0
    engaged: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    warm_wakes: int = 0
    cold_wakes: int = 0
    cost_microusd: int = 0
    unpriced_calls: int = 0
    created_at: float = 0
    updated_at: float = 0
    version: int = 0


@dataclass(frozen=True)
class Occurrence:
    wake_id: str
    dedupe_key: str
    state: str = "pending"
    attempt: int = 0
    epoch: str | None = None
    pid: int | None = None
    claimed_at: float | None = None
    started_at: float | None = None
    ended_at: float | None = None
    payload_json: list[Any] = field(default_factory=list)
    outcome: str | None = None
    notify: bool = False
    summary: str = ""
    summary_hash: str = ""
    next_check_s: float | None = None
    force_silent: bool = False
    delivered: bool = False
    tokens_in: int = 0
    tokens_out: int = 0
    error: str = ""
    created_at: float = 0
    matched_at: float | None = None
    held_since: float | None = None
    held_reason: str = ""
    held_matches: int = 0
    api_calls: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    warm_wakes: int = 0
    cold_wakes: int = 0
    cost_microusd: int = 0
    unpriced_calls: int = 0

    @property
    def id(self) -> str:
        return f"{self.wake_id}/{self.dedupe_key}"


@dataclass(frozen=True)
class Session:
    session_key: str
    platform: str = ""
    chat_type: str = ""
    chat_id: str = ""
    current_session_id: str = ""
    last_human_at: float | None = None
    last_activity_at: float | None = None
    episode_id: str = ""
    unanswered_count: int = 0
    busy_since: float | None = None
    busy_epoch: str | None = None
    enabled: bool = True
    paused_until: float | None = None
    first_notice_sent: bool = False
    last_wake_turn_at: float | None = None
    limits_fingerprint: str = ""
    last_cache_touch_at: float | None = None
    last_route: str = ""
    last_prompt_tokens: int = 0
    last_cache_hit_ratio: float = 0
    last_cache_api_mode: str = ""
