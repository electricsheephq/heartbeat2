# HeartBeat 2.0

**Close an open loop without burning a model turn on every clock tick.**

HeartBeat 2.0 is an unreleased Hermes plugin for people who leave real work open
in messaging conversations: a promised follow-up, a background build, pending
CI, or a draft waiting for a reaction. Arm a time **wake** or an event/probe
**watcher** with a self-contained note and bounds. It returns to the conversation
that armed it, using that session's own model. Silence is a structured decision.
Never arm a wake merely "to stay in touch"; name the open loop.

Disclosure — wakes and watchers run full turns on the conversation's own model,
billed like any message; command probes run allowlisted read-only commands without
an approval prompt; url probes make GET requests you configured; state stays in the
profile's plugin-data; no telemetry.

The argv guard is not a sandbox: allowlisted programs and local configuration
must be trusted and read-only. Model wake turns retain the conversation's tools
and permissions. GET requests include the configured URL, including its query;
URL userinfo, cookies, Authorization headers and ambient proxies are refused or
unused by the probe. Existing git/gh credentials and remote configuration can be
used by command probes. The plugin adds no telemetry; Hermes, model providers
and configured endpoints retain their own data handling.

## How it works

```text
You / agent: named task + trigger + expiry / fire budget
                         |
                SQLite in pinned profile home
                         |
     time / event / file / PID / URL / command observer
                         |
       unchanged? ---- yes ---> no model turn
                         | change / due / deadline
                         v
    enabled -> quiet -> caps -> unanswered -> busy -> spacing
                         |
             durable claim + same-session inject
                         |
              conversation's own model turn
                         |
               pulse respond (structured verdict)
                  /                        \
        notify=false                    notify=true
        [SILENT]                         short reply
                  \                        /
                    ledger + no-op governor
```

Checks are deterministic; qualifying fires still cost a full model turn. An
injector's `True` means scheduled, not delivered. Time is wall-clock durable;
restart/sleep coalesces overdue ticks rather than replaying a burst. A probe/event
can set a deadline: "deadline passed — nothing happened" is a separate warning,
never evidence that the task succeeded.

## Install and compatibility

Requires **Hermes >=0.21.6**, Python >=3.11 on a POSIX host. Copy this repository's
plugin files into `<your Hermes profile>/plugins/heartbeat2/` (keep root
`__init__.py`, `plugin.yaml`, `heartbeat2/` and `skills/`). Enable it in that
profile's `config.yaml`, then restart its messaging gateway:

```yaml
plugins:
  entries:
    heartbeat2:
      enabled: true
      allow_gateway_injection: true
      settings:
        quiet_hours: "22:00-08:00"
        agent_can_create: "yes"
```

`allow_gateway_injection` is a **core key outside settings**. Without it, arming
is refused and `/pulse status` prints the exact YAML needed. No API keys or new
service are required by this plugin. Optional `croniter`/`psutil` are imported
lazily from Hermes; missing dependencies refuse only the affected kind.

v0.1 targets **messaging-gateway DMs only**. CLI, TUI, Desktop, API-server and cron
arming are refused. Default `dm_only=true` refuses groups and unknown chat types;
setting it false permits recognized gateway groups, which are outside v0.1 live
acceptance. `codex_app_server` provider turns drop per-turn context: do not assume
extra hook context survives that provider path. Hermes response trailers appended
after transformation may break exact silence suppression. Real delivery and
provider compatibility require separate end-user-session acceptance.

CI pins Hermes v0.21.6 (`818c13be1dc4fd28987e1e881a9408224afd4535`) and main
(`04eb00cef7d9ac2730ed5fd9039a6f3287eb49ca`). The integration test uses the real
loader and registered hooks/tool/command with a fake gateway; it makes no model
calls and proves no live messaging delivery.

## Settings

These keys live under `plugins.entries.heartbeat2.settings`. User-owned settings
bound the agent; the `pulse` tool cannot write them. Clamps appear in results.

| key | type | default | meaning |
|---|---|---|---|
| enabled | bool | true | Master switch |
| agent_can_create | str choices yes/ask_first/no | yes | ask_first → agent-created items are `proposed` until `/pulse ok <id>` |
| dm_only | bool | true | Refuse group chats; fail closed on unknown chat type |
| timezone | str | "" | Blank → Hermes profile timezone (`hermes_time`) → system |
| quiet_hours | str | "22:00-08:00" | "" = none; crossing midnight allowed; due wakes defer to quiet end, recurring ticks inside are skipped |
| quiet_hours_urgent | bool | false | Let `urgent` wakes through quiet hours |
| max_model_wakes_per_day | int | 24 | Per session, local day |
| max_messages_per_day | int | 6 | Delivered notify=true per session per day |
| max_unanswered | int | 2 | After N delivered unprompted messages with no human reply, the session is muted (all occurrences skipped `unanswered`, except user-created `urgent`) until the owner writes |
| min_interval_minutes | int | 15 | Floor for recurring time wakes |
| agent_ungated_floor_minutes | int | 60 | Floor for agent-created recurring time wakes with no probe gate (babysit excepted) |
| wake_spacing_minutes | int | 10 | Min gap between wake turns in one session |
| probe_floor_seconds | int | 60 | Fastest probe check |
| max_active | int | 10 | Active items per session (global cap 50) |
| max_lifetime_days | int | 14 | Hard expiry; agent recurring items default 7 days unless renewed |
| owner_silent_checkin | bool | false | Built-in check-in after silence (managed profiles may turn it on) |
| checkin_after_hours | int | 24 | Threshold for the built-in check-in; doubles for the next episode if ignored |
| max_rearm_chain | int | 2 | Depth limit for wakes armed inside wake turns (reset by a human message); max 1 child per wake turn |
| noop_slowdown_after | int | 3 | Empty fires before interval doubling |
| noop_backoff_factor | int | 2 | |
| noop_max_backoff_hours | int | 24 | |
| suggest_gate_after | int | 4 | Wake text suggests converting to a probe watcher |
| noop_pause_after | int | 6 | Auto-pause + one keep/slow/stop/watch card |
| ignored_pause_after | int | 3 | Delivered messages with no engagement before pause |
| error_pause_after | int | 5 | |
| engagement_window_hours | int | 6 | A human reply within this window marks a delivered message as engaged |
| catch_up_hours | int | 6 | One-shots later than this are `missed` (reported once), not fired |
| probe_commands | str | "gh pr view, gh pr checks, gh run view, gh run list, git ls-remote, git status, git log" | Comma-separated argv prefixes allowed for `command` probes; "" disables command probes |
| source_silence / source_process / source_file / source_url / source_command / source_events | bool | true | Enable each trigger family |
| presence_line | bool | true | One ≤25-token context line on human turns when something is armed (via `pre_llm_call`) |
| ledger_days | int | 30 | |

## /pulse reference

Commands operate only on the invoking gateway session. They are owner controls,
so `add` creates a user item even when agent creation is disabled. No destination
argument exists. All output is compact plain text.

| Command | Effect |
|---|---|
| `/pulse` or `status` | Poller alive/stalled/not running + reason, consent, local time/quiet state, today's counters, active items, next due |
| `/pulse list` | All this chat's items, including paused/terminal items |
| `/pulse ledger [7d]` | Fires, checks, model wakes, messages, engaged, ≈tokens and simple value verdict; at most retained `ledger_days` |
| `/pulse why <id>` | Note, reason, schedule, current status and governor streaks |
| `/pulse add in 2h <note>` | Single user wake |
| `/pulse add every 1d at 09:00 <note>` | Anchored recurring user wake, subject to bounds |
| `/pulse add when silent 24h <note>` | At most once per human-message silence episode, until expiry/fire cap |
| `/pulse pause <id\|all> [for 3d]` | Pause; optional timer resumes only when activation gates allow |
| `/pulse resume <id\|all>` | Resume paused items with fresh checks/clamps; proposed items need ok |
| `/pulse cancel <id\|all>` | Cancel and supersede live occurrences |
| `/pulse off` / `/pulse on` | Disable/enable this chat, without changing master settings |
| `/pulse quiet <22:00-07:30\|off\|for 3h>` | Durable per-session override; temporary `for` falls back to the configured schedule when elapsed |
| `/pulse ok <id>` | Owner approves a proposed item within current bounds |
| `/pulse keep <id>` | Resume and clear governor streaks |
| `/pulse slow <id>` | Double recurring cadence within bounds and resume |
| `/pulse stop <id>` | Cancel a governor-paused item |
| `/pulse watch <id>` | Pause and request a specific probe/event conversion; does not guess a watcher |

Ledger "checks" is fires plus gated skips from engine accounting, not a raw
transport-request counter; caps/dispatch gates can contribute skips. Tokens are
reported usage, not a forecast or monetary bill. `off` does not erase existing
items, so `on` can expose overdue work to the ordinary catch-up rules. Timed
pause stops mutation/delivery of current occurrences; resuming an overdue one-shot
re-arms it at the current time. Invalid/foreign IDs never expose another chat's note.

## pulse tool reference

The single agent tool `pulse` has actions `create`, `list`, `update`, `pause`,
`resume`, `cancel`, `respond`, `off`.

| Field | Use |
|---|---|
| `title`, `note`, `reason` | Name and explain the task; create requires a self-contained note and trigger.kind |
| `trigger` | `at`, `every` (+ optional anchor `at`), `cron`, `self`, `owner_silent`; `file`, `process`, `url`, `command`; supported events |
| Probe fields | `path`, optional `pattern`; `pid`; `url`, optional `pattern`; `command` argv array, optional `cwd`, `fire_on` change/success/failure/json; `check_every`, optional `deadline` |
| Event fields | selected by `trigger.kind`, optional reaction `emojis`; events: kanban_completed/blocked/worker_exited, subagent_done, api_error, reaction, message_edited |
| `until` | `expires`, positive `max_fires`, `owner_replies` to cancel on the next human reply |
| `pacing` | `min`, `max` bounds for recurring/self scheduling |
| `babysit`, `urgent` | Short human-turn babysit; urgency bypasses quiet only when `quiet_hours_urgent` permits |
| `respond` fields | `outcome`: no_change/progress/done/blocked/needs_attention; boolean `notify`; ≤280-character summary; optional next_check and replacement scratch |

Examples are in the registered [`heartbeat2-usage` skill](skills/heartbeat2-usage/SKILL.md).
Every wake turn should call respond. Last respond wins; notify=false suppresses
chatty output and missing respond defaults to silence. Scratch/next_check commit
only after a successful turn. Do not type sentinels. Agent-created proposals need
owner `/pulse ok`; the agent can switch itself off, but only the owner can turn
it back on. Reacting to an external payload does not authorize protected work.

File probes establish an EOF baseline and match only appended complete lines;
mtime/size/inode changes are stat-polled. Truncation/rotation starts a fresh
stream. URL probes compare body hashes or a regex match on a changed body;
initial non-regex GET establishes a baseline. Process probes capture create-time
at arming and fire on exit/PID reuse. Command change mode fires on its first
successful result; unchanged successful results do not call the model. For
success/failure modes, each qualifying check can fire. JSON observers print
exactly `{"fire":bool,"message":str,"state":JSON}`; stored wrapper state ≤16 KB
commits only on successful fire. Change/JSON nonzero exit and probe I/O errors
are errors, never changes; the error governor can pause the item.

## Guardrails and governor

Quiet hours, local-day model/message caps, unanswered mute, busy-session checks,
one in-flight wake, spacing, flood admission, lifetime/fire limits and estop run
before injection. Defaults include 24 model wakes / 6 delivered messages per day,
2 unanswered messages, a 15-minute recurring floor (60 minutes for ungated agent
recurrence), 60-second probes, 10 active items per session / 50 globally, and a
14-day hard lifetime. Built-in silence check-in is off by default. Re-arm chains
are bounded; a wake carrying untrusted payload cannot create recurring work.

Repeated zero-value runs slow recurring wakes, suggest a watcher, then pause
(default six no-ops). Ignored deliveries and repeated errors can also pause.
One governor card appears on the next human turn; keep/slow/stop/watch are owner
answers. Babysit has a floor of max(5 minutes, owner floor), a 4-hour/24-fire cap,
and ends on notify=true or three consecutive no_change results. Silence and
gates save model calls, not the cost of a qualifying full conversation turn.

Read [SECURITY.md](SECURITY.md) before configuring file, URL or command observers.
Local/private HTTP targets are allowed; URL probes use a 10 s total monotonic
deadline and a streaming 256 KB cap. Command probes have a 30 s total deadline,
no controlling tty, disabled Git/GH/credential-manager prompts, and bounded
streaming capture. Timeout or output overflow kills the process group, including
descendants that stay in that group. Trusted programs/configuration can still
hide side effects or deliberately detach; the argv guard is not a sandbox.

## Choosing a mechanism

This is a workflow comparison, not a measured performance or parity claim.

| Mechanism | Best fit / distinction |
|---|---|
| HeartBeat 2.0 | Named open loops in the existing messaging session; deterministic probes, per-chat bounds, structured silence and governor |
| Hermes core `/heartbeat` | One recurring instruction in the current session; core persistence, idle admission and gateway recovery, with bare silence-marker output |
| Hermes `/loop` | Repeated active-session instructions; choose when working in the interactive loop rather than durable DM watchers |
| Hermes cron | Scheduled jobs with cron's own execution/delivery lifecycle; choose independent scheduled tasks rather than this conversation's open loop |
| OpenClaw heartbeat | Its periodic heartbeat workflow; HeartBeat 2.0 implements distinct persisted claims/gates/verdicts, without claiming live parity or resolution of every upstream bug |
| Claude Code | Coding-session automation within Claude Code; this plugin instead depends on Hermes messaging-gateway session injection |

## Known issues / proof boundary

- A wake is a normal model turn, billed like one at your conversation's context size; `/pulse ledger` ≈tokens per wake and `/pulse status` "Today … ≈tokens" show the running cost.
- Unreleased. CI is defined here; remote matrix results and live DM delivery are
  separate gates. Offline U4 scenarios and the comparison runner are in
  [bench/README.md](bench/README.md); generated results are simulation evidence.
- File polling can miss truncate-and-regrow or replacement changes that happen
  entirely between checks. The reader catches up within a 4 MB per-check budget. Lines over 256 KB are
  skipped through their newline with advancing offsets and a state counter. Regex evaluation
  is not sandboxed or separately time-limited; use simple trusted patterns.
- Fingerprints contain hashes; payload/note/scratch/summary/JSON state can contain
  sensitive plaintext. Do not watch credentials or print them from observers.
- A probe establishes/advances its baseline only when checked; rotation and errors
  follow the rules above. A stopped gateway cannot deliver, and local fixtures do
  not prove delivery receipts. `codex_app_server` per-turn context and Hermes
  post-transform trailers have the limitations noted under compatibility.
- `/pulse watch` needs an explicit `pulse update` specifying the actual observer.
  It pauses safely rather than inventing executable argv or an endpoint.
- Session overrides are persistent plugin metadata, not profile setting writes.
  `quiet off` is an owner override; agent tools cannot make that change.

## Development and credits

```sh
python -m pytest -q
HERMES_DISABLE_LAZY_INSTALLS=1 HERMES_HOME=/tmp/heartbeat2-test-home \
  python -m pytest tests/integration -q
```

Unit tests deny network; HTTP transport is tested through local fake responses,
not external endpoints. Integration skips explicitly if Hermes is not importable.
See the pinned [CI workflow](.github/workflows/ci.yml) for the real-loader matrix.

Built for [NousResearch/Hermes Agent](https://github.com/NousResearch/hermes-agent).
Ideas credited in source: Hermes heartbeat claim/refund/coalescing, cron monitor
fingerprinting, loop response digestion, and gateway estop/busy ordering. Loader
fixture/action-pinning patterns follow `fish-audio-hermes`. OpenClaw issue analysis
informed the design; it is not an OpenClaw component. Apache-2.0; see [LICENSE](LICENSE).

Known issue: a wake turn that fails at the provider shows Hermes's own failure notice; HeartBeat backs off until the provider recovers.
