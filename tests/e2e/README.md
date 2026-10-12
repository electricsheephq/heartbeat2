# Real-gateway acceptance

These opt-in tests run the shipping Hermes gateway and Telegram adapter against
Hermes's loopback Bot API stand-in and scripted OpenAI-compatible model. No
Telegram account, real token, model spend, or live profile is used. The default
unit run skips all 15 cells (E2 has two display variants).

Use a separate read-only checkout for each exact Hermes commit:

| Label | Commit |
| --- | --- |
| v0.21.6 | `818c13be1dc4fd28987e1e881a9408224afd4535` |
| v0.21.7 | `71d34bad1045610252eda02ee79549436896e784` |
| main | `04eb00cef7d9ac2730ed5fd9039a6f3287eb49ca` |

From the plugin worktree, with `HERMES_PIN` pointing to that checkout:

```sh
export HERMES_DISABLE_LAZY_INSTALLS=1 PYTHONDONTWRITEBYTECODE=1
uv venv --python 3.14 .e2e-venvs/v0.21.6
uv pip install --python .e2e-venvs/v0.21.6/bin/python \
  -e "$HERMES_PIN[messaging]" pytest pytest-xdist pytest-timeout pyyaml croniter psutil
TMPDIR=/tmp HEARTBEAT2_E2E_HERMES="$HERMES_PIN" \
  HEARTBEAT2_E2E_RESULTS=/tmp/heartbeat2-results \
  .e2e-venvs/v0.21.6/bin/python -m pytest tests/e2e -q -n 4 \
  -p no:cacheprovider --junitxml=/tmp/heartbeat2-e2e.xml
```

Repeat with the matching label and venv for the other pins. Python 3.14 is
intentional: these pins declare most core dependencies only for Python >=3.14.
Their own E2E workflow installs the `messaging` extra. Do not use a live Hermes
checkout, its venv, the `hermes` executable, or Hermes.app.

Each scenario owns a gateway, temporary HOME/HERMES_HOME, Telegram stand-in,
model server, and plugin database. Use a short TMPDIR on macOS so the gateway's
Unix sockets fit. At most four workers run in parallel, with a 240-second
scenario deadline; the CI job has a 30-minute deadline. Serial pytest works too.

The fixture copies only the plugin's production files into the temporary home.
It enables gateway injection, disables quiet hours, lowers supported scheduling
floors, disables streaming, and restricts tools to `heartbeat2`. Tool Search is
explicitly off so the scripted direct `pulse` calls are offered and executed.
E2's default Telegram display keeps tool progress/reasoning off; its second cell
sets `display.platforms.telegram.tool_progress: new` and `show_reasoning: true`.
The gateway still uses its real streaming provider client.

Helpers are imported from the selected checkout, never vendored. The gateway's
restart glue requires fresh polling and completion of the startup restore drain,
because the upstream helper's connected predicate remains true across restarts.
The gateway's
`sitecustomize` and pytest fixture deny non-loopback DNS/connect/sendto traffic.
No clock shim, DB write, fake gateway, or production plugin patch is used. E6
installs a separate test-only hook barrier before heartbeat2's `pre_llm_call`.
Busy input is sent by `Script` at provider request arrival, and held there until
the adapter acknowledges it or logs queue admission (queue text has no visible
ack). Queue debounce is explicitly zero. E9 uses real compaction and an earlier owner ORIGIN
row carried through history.

Assertions use the stand-in's current visible messages, read-only `pulse.db`
queries, and recorded provider request bodies. `/pulse status` is a same-chat
delivery barrier and does not count as an owner reply.
The status barrier uses at most three sends to cover the upstream adapter-close
window (#121393); arming and owner/model input are never retried.
Negative replay/loop claims have a bounded poller-tick observation window;
they are not soak claims.

Every rig retains `gateway.log`, its temporary home (including `pulse.db`),
`visible.json`, `requests.json`, `aux.json`, and `pulse-state.json` in TMPDIR.
Failures print the evidence directory. Setting `HEARTBEAT2_E2E_RESULTS` adds
per-worker JSONL receipts with outcome, phase duration, and evidence path. CI
uploads temporary rigs, receipts, XML, and the full test log on failure.

Reproduced product defects use `xfail(strict=True)` limited to a named finding
exception, so unrelated boot, provider, and assertion failures remain failures.
An XPASS requires revisiting the finding. Pin-specific inability to exercise a
scenario must have an exact skip reason; skips do not prove that path.

E11 records the provider's visible failure copy without failing on it. Its strict
xfail covers the separate observed settlement gap: after an HTTP 500, backoff is
recorded and the gateway is idle, but the occurrence stays started and heartbeat2
keeps the session busy pending the `HERMES_AGENT_TIMEOUT` fallback (30 minutes by
default; this suite does not wait for it). E14 proves that one wake API error
yields exactly one coalesced API-error watcher occurrence, held by the real
provider backoff. A self-loop across backoff periods is not covered here.
E10's separate strict xfail records native startup auto-resume replaying the old
wake marker even though heartbeat2 has recovered the occurrence as interrupted.
Both xfails require the named signature; unrelated failures still fail the run.

Passing this suite establishes **local-e2e-proof against these pins**. It does
not prove real Telegram server/client rendering, notification delivery, live
model quality/cost, long-running reliability, or customer readiness.
