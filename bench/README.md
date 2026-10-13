# Offline regression simulator

Run `.venv/bin/python -m bench.run --baseline --gate --out /path/to/artifacts`.
Select one YAML with `--scenario ID`. There is no network or real model call.
The runtime uses only development dependencies already used by the tests.
The gate fails when the engine arm takes 30 s or more. `HEARTBEAT2_BENCH_RUNTIME_BUDGET_S`
overrides that budget; CI sets 120 s because shared runners are several times slower.

Claim class: deterministic simulation on this suite (advisory). These results
prove the named source/harness/scenarios, not live Hermes delivery, providers,
remote CI, installations, or current OpenClaw behavior.

The comparison is a **naive heartbeat-1.0 emulator built from documented failure
reports; not current OpenClaw (which retired HEARTBEAT.md in 2026.8.1)**. Its
numbers are never presented as measurements of OpenClaw. Fixed interval and
context assumptions appear beside each scenario; this is a failure-regression
comparison, not a speed or token quality benchmark of two deployed products.

The shared benchmark base sets `quiet_hours=""` and `timezone="UTC"`; all other
bounds start at Policy defaults. YAML settings and per-profile settings override
that base. This intentionally tests failure handling with quiet hours disabled
except in scenarios that exercise quiet deferral; it is not a fresh-install
configuration comparison.

Each YAML declares provenance, settings overrides, tool/command arm actions,
timeline, model policies, duration, expectations and a HEARTBEAT checklist for
the baseline. Times and required windows are seconds from the fixed epoch.
Policies call the real pulse respond handler; raw output passes through the
real transform and post-turn hooks. The gateway only suppresses exactly
`[SILENT]`. Model turns, outgoing deliveries, attempts, human events, cards,
command answers and the final SQLite snapshot are retained in the JSON.

Clock jumps visit the same `tick()` path; pending work stays on 15-second ticks.
Explicit suspend events skip all intervening ticks. Restarts retain the same
SQLite file; two-poller scenarios use distinct connections and the real flock.
Profiles use distinct homes even when session keys are identical.

The common metric evaluator computes duplicate, destination, marker, missed
window, burst, silence-episode, no-op, estimated-token and latency metrics.
Invalid stale attempts still count
as model turns but cannot satisfy a required fire. Only valid structured
respond results count as verdicts. All HB2 hard bars and declared scenario bars
are gated; baseline bars are reported. A disabled transform is tested to fail
on sentinel leaks. Generated reports include dirty-source content identity.

The wake token estimate uses `context_tokens=150000` input tokens per call,
`calls_per_turn=2`, `output_tokens_per_call=150`, and `cache_ttl_s=300` by default.
Both arms use the same scenario keys and defaults. The first call reads the
cache when the gap since the previous model turn in that profile/session is
at most the TTL; otherwise it writes the cache. Later calls always read it.
Owner model turns warm the cache but their cost is outside these wake totals.
The first model turn in a session has no previous cache touch and writes it.
`est_tokens` remains total input (calls × context), equal to `est_input_tokens`;
`est_cache_read_tokens` + `est_cache_write_tokens` partition that input.
`est_output_tokens` is separate. These are estimates, not provider metering.

B1 now has 34 scenarios, including caps, mute classes, production quiet defaults,
DM admission, cron, command changes, cache snap, ignored/error ladders, lifetime
feasibility and the #58 final-state change-watcher limit. `url-trigger` is skipped:
the simulator has no loopback URL stub, and B1 adds no network. The command
fixture is an allowlisted read-only status-file reader. New observation and
stored-wake bars are hard gates alongside delivery counts and required windows.
No-op, ignored and error ladders must pause and expose exactly one owner card.
