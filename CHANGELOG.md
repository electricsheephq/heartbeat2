# Changelog

## 0.1.15 — 2026-10-14

- **Unsupported event filters are refused (#57).** Select the event with `trigger.kind`; `trigger.event` is no longer advertised or accepted. At startup, existing active, paused or proposed watchers using it are stopped with `invalid_trigger_event`, pending occurrences are cancelled, and one notice per chat asks you to set the watcher up again. Stored task and board filters keep working.
- **First-fire feasibility counts both holds (#68).** Owner-silence waits now account for quiet hours, and feasibility includes the chat's active wake-spacing window before quiet hours. A wake whose first possible fire is at or after expiry is refused; receipts report the holds.
- The bundled skill tells the agent to set `owner_asked: true` when your current message asks for a one-time reminder, never for its own follow-ups, and to repeat the receipt.

## 0.1.14 — 2026-10-14

- **The agent can tell you when a wake will really arrive (#56).** Arming, updating or resuming a wake now returns a one-line `receipt`, at most 240 characters. It names the owner-silence duration and describes a watcher deadline as a one-time "nothing happened" fallback. It covers when the wake fires; whether quiet hours will hold it and until when; whether the unanswered mute will hold or drop it; the spacing between wakes; when it expires; and whether a reply cancels it. Long receipts end at a sentence boundary where possible, with an ellipsis. The bundled skill tells the agent to repeat the receipt instead of promising its own timing. A proposed wake reads "awaiting /pulse ok", never "armed". `/pulse add`, `/pulse ok` and `/pulse resume` print the same line. Before, an agent could say "fires in 3 minutes" for a reminder that quiet hours would hold until 08:00.
- **A wake that could never fire is refused (#56).** A reminder due after its own expiry was accepted, then expired silently. For example, "in 30 days" against the 14-day maximum. Owner-silence waits must fit before expiry, and a probe's next check must fit when it has no deadline. Quiet hours count at the real dispatch time for one-time wakes and watcher deadlines; a timed quiet override chains into the default quiet hours if they are still active when it ends. Only probe and event watchers accept `deadline`. A one-shot in the past outside the catch-up window is refused. On create, a default lifetime stretches to fit, up to your maximum; the clamp reports the stored expiry. Existing wakes keep their expiry, and an edit past it is refused with a hint to set `until.expires` only when an extension can help; the maximum-lifetime reason takes priority otherwise. A timed resume that cannot fit ends the paused wake with one notice.
- Resuming a paused wake keeps its stored deadline instead of re-reading a relative one ("in 2h") from the moment of resume, the same rule v0.1.11 applied to updates.

## 0.1.13 — 2026-10-14

- **A reminder you ask for in chat fires like your own (#55).** When you said "remind me at 4" in chat, the agent armed the reminder as its own follow-up, so after two unanswered messages, or at a daily cap, it was dropped. Only `/pulse add` reminders were safe. Now a one-time reminder the agent arms because you asked in your current message (it marks the call `owner_asked`) bypasses the unanswered mute and both daily caps. HeartBeat 2.0 checks that the turn really carries your message; a reminder the agent sets on its own, or arms from a wake turn, keeps the agent's limits. A later agent edit or resume without a new request from you removes the exemption. Quiet hours, spacing and expiry still apply.
- The wake text's counter now reads "wake messages today" instead of "unprompted messages today", and the matching setting is labelled "Wake messages per day".

## 0.1.12 — 2026-10-13

- A one-time wake that comes due while you have turned HeartBeat 2.0 off for the chat (`/pulse off`, or the agent's `pulse off` when you ask it to stop) is listed as cancelled (`session_off`), not as failed. It still never runs; recurring wakes stay armed and resume after `/pulse on` (#41).

## 0.1.11 — 2026-10-13

- **Wake replies lead with the result.** When a wake is set to notify you, the agent is now told to lead with what it found, to not describe what it checked or looked up, and that `notify` must be set to true or false. In a live-model bench, wake replies with nothing to report that opened by describing the check went from 3 of 3 to 1 of 3 (read by hand; 0 of 3 by the bench's pattern). Silent wakes and delivered results were unchanged.
- **Moving a wake's deadline or marking it urgent counts as an agent edit (#43).** Before, an agent could change either on a wake you armed, and the wake kept its exemption from the unanswered mute. A `/pulse watch` conversion that sets a deadline still stays yours. Title and reason changes stay cosmetic.
- **An update that doesn't change a wake's trigger keeps its deadline.** Before, any later update, even a title change, re-read a relative deadline such as "in 1h" and quietly pushed it out.

## 0.1.10 — 2026-10-13

- Owner messages are recognised by Hermes' own row id (`message_uid`) instead of by their text (#16). Before, when Hermes rewrote history inside a turn (compaction, merged consecutive messages, task-list snapshots), heartbeat2 could count an old owner message again. That reset the unanswered mute, cancelled owner-reply wakes and suppressed the silence check-in. It could also miss a new message that repeated an earlier one word for word. A row that Hermes has not stored yet (the current message, a fresh steer) keeps the text match until Hermes assigns its id. Platforms without platform message ids, such as Signal and SimpleX, benefit most.

## 0.1.9 — 2026-10-13

- A watcher you ask the agent to set up with `/pulse watch` stays yours. When the agent turns your paused wake into a watcher (a new trigger or schedule only), the unanswered mute no longer holds it. Before, the conversion counted as an agent edit, so your watcher waited for a reply you had no reason to send.
- When the agent edits a wake you armed (its trigger, timing or note), the wake gets the agent's limits: the 55-minute floor for timers without a watcher, and the cache-edge snap. The floor also moves the occurrence that was already scheduled at your faster pace; a wake anchored to a time of day moves to its first anchor slot past the floor. Before, an edited wake kept your 15-minute floor while already losing your mute exemption.

## 0.1.8 — 2026-10-13

- After a restart, a busy flag left by the previous gateway process is always cleared, even when the system clock moved backwards. Before, a flag that looked newer than the restart could block wakes into that chat for up to 5 hours, and a clock step back could clear the busy flag of a turn this process was still running. Busy flags now record which process set them, and a plugin runtime that unloads gives up its flags (#22).

## 0.1.7 — 2026-10-13

- The cache line in `/pulse add` and pulse tool results shows minutes rounded to 0.1 (`9.8m`, `2m`) instead of raw floats such as `9.811217749118805m` (#36).

## 0.1.6 — 2026-10-13

- On platforms without message ids (Signal, SimpleX), an owner message is no longer counted a second time when Hermes appends its task list to it after a context compaction. The second count marked a silent wake as answered, so its text was shown (#33).

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
  - an agent-armed file watcher matching its line (its delivery was held by the unanswered mute, as designed; delivery of an agent-armed watcher was proven live on v0.1.12).

**Known issues**: see the README. In brief:
- A wake that fails at the provider shows Hermes's own failure notice (NousResearch/hermes-agent#136749).
- Some platforms show tool progress on silent wakes (#136825).
- Memory providers see wake text as a user turn (#130622).
