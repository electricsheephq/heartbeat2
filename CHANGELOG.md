# Changelog

## 0.1.5 — 2026-10-13

- Eight numeric settings have upper bounds (ledger history 100 years, wake lifetime 10 years, interval floors and slowed intervals one year, watcher floor one week, slow-down multiplier 100). A larger value is reported by `/pulse status` and ignored, instead of crashing every tick.
- The E2E `/stop` cell names the upstream Telegram duplicate-`/stop` wedge, NousResearch/hermes-agent#137545.

## 0.1.4 — 2026-10-13

- At start a rejected setting falls back to its default; on a live change it keeps its last good value, and your other changes still apply. `/pulse status` and pulse tool results name the ignored setting.
- Explain the error threshold, source switches and ledger retention in the Settings form.
- Hold pending watcher matches through a governor pause and release them after keep, slow or resume.
- Preserve busy flags for turns started in this process when restart recovery runs.
- Use the tool's cache labels on owner commands and governor cards; when-silent check-ins show TTL and last touch without warm/cold claims.
- Count a crash-resumed wake turn's tokens and estimated cost on the wake and the daily ledger. It still never counts as a fire or delivery.
- Keep the provider's full base URL in cache routes (without userinfo, query or fragment), so custom endpoints that declare prompt caching get the TTL Hermes uses. Host-only routes stored by earlier versions still resolve.
- Correct command-watcher documentation: the allowlisted environment does not pass proxy or custom-CA variables yet.

## 0.1.3 — 2026-10-13

- Silence crash-resumed wake replays, including a replay that arrives before restart recovery runs, and reject stale responses. Each replay counts as a model turn toward the daily cap, never as a fire or delivery. Only an admitted owner message (not a bot or unauthorized sender) marks a resumed turn as the owner's.
- Revoke owner-wake privileges when agent tools edit lifetime limits (expiry, max fires, owner-reply cancellation) or babysit.
- Correct empty-usage accounting (known output tokens still count), call-route pricing TTL, transactional learned routes, owner first-wake cache age, and failed probe CAS handling.
- Provider-exhaustion settlement (#27) remains blocked upstream; its strict expected failure is unchanged.

## 0.1.2 — 2026-10-13

- Agent watchers hold and merge under unanswered mute; all watchers hold at daily caps until capacity frees. Wakes you armed bypass mute, and your unedited one-shot reminders bypass daily caps. Agent edits revoke that exemption; the built-in check-in's trigger is Settings-owned. Held expiry and the last ten occurrences are visible in `/pulse why`.
- Count probe checks separately from gate skips, missing verdicts, model turns (refunding only zero-call provider failures), and cache reads/writes with price estimates.
- Cache-aware agent timing and governor backoff preserve warm intervals; the default agent timer floor is 55 minutes. Owner timing stays explicit.

**Upgrade:** restart the gateway right after updating, before opening other Hermes sessions on that profile. Downgrading to an earlier version needs the profile's `plugin-data/heartbeat2` folder removed; this discards stored wakes and history. v0.1.0 and v0.1.1 cannot read the migrated database.

## 0.1.1 — 2026-10-13

- Command watchers sanitize an allowlisted environment with Hermes' helper,
  never pass GH_TOKEN/GITHUB_TOKEN, and refuse gh output-shaping flags
  (`--jq`/`-q`, `--template`/`-t`, including attached and bundled spellings).
- URL watchers reuse Hermes URL safety: cloud metadata is always blocked;
  private/local addresses require `url_allow_private` (default false). Every
  hop checks every resolved address at connect time and dials a vetted IP.
- File watchers honor Hermes read blocks at arm/update, before each open and
  after rotation, and reject device/inode changes between stat and open.
- Pool probes and URL workers retain the watcher's profile home scope.

**Upgrade:** existing watchers using `--jq`/`--template`, a private URL or a
blocked path will error and then pause after 5 errors (the default). Re-arm them
with an allowed command/path, or enable `url_allow_private` for private URLs.
Cloud metadata, including the whole IPv4 link-local range 169.254.0.0/16,
remains blocked with that setting enabled.

gh watchers that authenticated only through `GH_TOKEN`/`GITHUB_TOKEN` in the
gateway environment stop authenticating: run `gh auth login` (hosts.yml or
keyring) for the gateway's user before upgrading. Command watchers also no longer
inherit proxy, custom-CA or D-Bus variables (`HTTPS_PROXY`, `SSL_CERT_FILE`,
`DBUS_SESSION_BUS_ADDRESS`). The command watcher's allowlisted environment
does not pass proxy or custom-CA variables yet; gh has no config workaround for them.

## 0.1.0 — 2026-10-12

First release. Requires Hermes ≥ 0.21.6. Delivers to messaging-gateway DMs.

**Wakes and watchers, in the same chat, on the same model**
- You arm them with `/pulse`; the agent arms them with its `pulse` tool. Kinds:
  - time: `at`, `every`, `cron`, `self`;
  - silence: `when silent` and the built-in check-in, which is off by default;
  - watchers: `file`, `process`, `url`, read-only `command`;
  - Hermes events: kanban, subagent, API error, reactions and edits.
- A watcher records its first check as a starting point and calls the model only when something changes. Any watcher can carry a deadline.
- The agent answers each wake with `pulse respond {outcome, notify, summary}`. `notify=false` keeps the final reply out of the chat, and the model never types a sentinel.

**Reliability**
- The schedule and every fire are durable SQLite claims, using compare-and-set, an attempt marker and refunds.
- One poller runs per profile, guarded by a file lock. Restart reconciliation collapses missed fires into one.
- A turn that started but never finished is recorded as interrupted and never re-run.
- Wakes follow compaction. `/new`, or a switch to another conversation, stops the old conversation's wakes and tells you once.
- Provider outages back off every wake (5 min → 15 min → 1 h → 4 h) until a model call succeeds. A retryable error that Hermes recovers from does not cost the wake.

**Guardrails you own** (all of them are settings in Settings ▸ Plugins)
- Quiet hours, with per-chat overrides and `quiet default`.
- Daily caps on wake turns and on messages.
- A mute after unanswered messages. It covers agent wakes and check-ins; your own wakes still fire.
- Repeat floors, wake spacing, lifetimes, active-item caps, re-arm depth and DM-only delivery.
- Each of your messages counts once as a reply, including one you send while the agent is busy. Hermes' machine turns (background notices, `/loop`, `/goal` continuations, core heartbeat ticks, replays) never count. If you redirect or steer into a running wake turn, that turn is not silenced.
- A no-op governor slows down a wake that keeps finding nothing, suggests a watcher, then pauses it and asks you once.

**Security**
- Command watchers run allowlisted argv prefixes with no shell and no terminal. Output, config, editor, browser and mutating flags and verbs are refused, and the process group is killed after 30 s.
- URL watchers refuse credentials in the URL and send no cookies, auth headers or proxy settings. They have a 10 s hard deadline and a 256 KB cap.
- Watcher regexes run in a separate process with a 5 s deadline, so a pathological pattern cannot freeze the gateway.

**Proof**
- 714 unit tests on Ubuntu and macOS (Python 3.11, 3.13, 3.14), and a real-loader integration test against Hermes v0.21.6 and a pinned main commit.
- A deterministic 23-scenario benchmark of heartbeat-1.0 failure classes, with a mutant test that keeps the benchmark gate honest.
- Live proof on one Telegram DM (GLM-5.3) covered:
  - a delivered follow-up;
  - a silent verdict;
  - surviving a gateway restart;
  - a quiet-hours hold;
  - the no-op governor pausing a wake;
  - an agent-armed file watcher.

**Known issues**: see the README. In brief:
- A wake that fails at the provider shows Hermes's own failure notice (NousResearch/hermes-agent#136749).
- Some platforms show tool progress on silent wakes (#136825).
- Memory providers see wake text as a user turn (#130622).
