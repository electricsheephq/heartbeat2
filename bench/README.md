# Offline regression simulator

Run `.venv/bin/python -m bench.run --baseline --gate --out /path/to/artifacts`.
Select one YAML with `--scenario ID`. There is no network or real model call.
The runtime uses only development dependencies already used by the tests.

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
Latency uses actual injection time and the occurrence's scheduled time;
probe latency starts at event detection. Invalid stale attempts still count
as model turns but cannot satisfy a required fire. Only valid structured
respond results count as verdicts. All HB2 hard bars and declared scenario bars
are gated; baseline bars are reported. A disabled transform is tested to fail
on sentinel leaks. Generated reports include dirty-source content identity.

Default model context cost is 150,000 tokens per started wake turn, an estimate,
not provider metering. No-op backoff scenarios must demonstrate the actual
pause and a single card, rather than merely stopping the simulation early.
