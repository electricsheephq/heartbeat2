# B3-lite pre-registration: 7-day scripted-owner soak (registered 2026-10-14, before any soak code or run)

B1 checks the engine in a simulator, and B2 checks one model turn in an isolated rig. Neither covers the whole journey on a running profile. B3-lite does: over 7 days, the owner asks in chat; does the agent arm the right wake, does the message arrive on time and say the right thing, and does it stop when asked?

**Claim class:** scripted-owner soak on one profile and one model.
- **It shows:** the promise → message journey working end to end, on a real gateway and real Telegram, over days.
- **It does not show:**
  - real usefulness: the owner's own use is the full B3 (#14);
  - other models or other profiles;
  - quiet hours, which the episodes avoid;
  - the daily-cap exemption: the schedule stays under the default caps, and B1's cap scenarios cover it. It is reported as **not tested**.

The bars below are frozen once this file merges. The soak code that runs them comes later and may not loosen them. Until day 1 starts, a reviewed PR may tighten a bar (never loosen one); each such change is listed under "Amendments" with its date and PR.

## Setup

- **Profile:**
  - a test Hermes profile with a Telegram DM to the owner's account;
  - HeartBeat 2.0 at **v0.1.15 or later**, with `plugins.entries.heartbeat2.allow_gateway_injection: true` (required for any wake to be armed) and plugin settings at their defaults except `timezone`;
  - the profile's normal model; the model and provider are recorded at the start.
- **Frozen during the soak:** the plugin version, the plugin's entry (settings and `allow_gateway_injection`), the model, the gateway's plugin set, and the Hermes version and commit. A change aborts the run (see Voids).
- **Baseline before day 1** (logged as setup):
  - before any send, verify Tokyo timezone, Hermes and pulse sessions, the current calendar day, gateway injection, Hermes version/commit, and no claimed/started occurrence;
  - refuse pre-existing active, proposed or paused non-built-in wakes before any send; the release owner clears them with the owner;
  - a non-built-in start-day ledger row stops soak setup; record built-in usage and require built-in deliveries plus day-1 expected deliveries ≤ `max_messages_per_day` − 1;
  - send `/new`, verify its confirmation prompt, send `/approve` once, verify the fresh DM session from Hermes state, and archive and hash the old session transcript; never send `always`;
  - the chat is enabled;
  - no non-built-in wake is active, proposed or paused; cancellation is only a safety net for wakes appearing during setup;
  - run `/pulse quiet default` and record the effective quiet window; it must be 22:00–08:00 or setup fails;
  - the stored unanswered count is 0;
  - day 1 starts on a fresh calendar day; built-in usage on that day is recorded and budgeted, not carried-over usage.

  Any check that fails after cleanup stops the start.
- **Smoke:** not a soak day; record delivered-today usage and the cap without rejecting usage. Its owner one-shot asks `remind me in 5 minutes to check the smoke receipt`, closes at due + 15 min, and reports unexercised bars as `NOT_EXERCISED`.
- **Scripted owner:** a runner sends from the owner's Telegram account through a user client, so the gateway sees ordinary owner messages.
- **Timezone:** both the registered schedule and the plugin's resolved timezone must be Asia/Tokyo; setup fails closed otherwise.
- **Hours:** the first send is at 08:30 or later, every due time is at 21:30 or earlier, and every episode window closes by 21:45 (profile timezone). No episode crosses quiet hours (default 22:00–08:00).
- **Schedule:** deterministic from the registered seed **20261014**.
  - The generator is code committed to this repository and reviewed before day 1. The run declaration records the generator's commit, the seed, the first day's date and the schedule's SHA-256. The scorer regenerates the schedule and refuses a mismatch.
  - **Draw order:**
    - Day `d` (1–7) draws from Python's `random.Random(f"20261014:{d}")`. Global choices draw from `random.Random("20261014:0")`, in this order:
      - the 4 C-reply days, `sample(range(1, 8), 4)`;
      - the R kind for days 3 and 6, `choice` per day;
      - the task order, one `shuffle` of the 14 tasks, each used exactly once by the 14 reminder requests in schedule order;
      - the N texts: one `shuffle` of the 6 texts, used by days 1–6 in order, then one `choice` among them for day 7.
    - The exact per-day RNG call sequence is:
      1. Create `random.Random(f"20261014:{d}")`; `shuffle` only the non-S list in Day plan order, then append S on days 3 and 6.
      2. Visit that resulting episode order. Draw its placeholders first: R-clock `randint(20, 90)` minutes and round the resulting HH:MM **up** to the next 5-minute mark (an exact mark stays); R-rel `choice([15, 30, 45, 60])`; W `randint(10, 30)`; C `choice([30, 45, 60])`. M, N and S make no per-day placeholder draws. Tasks and N texts consume their precomputed global order.
      3. After that episode's placeholders, draw its following gap with `randint(5, 30)`, unless it is the last episode (no draw). Then visit the next episode.
      4. If layout does not fit, reuse the filled episodes with all gaps at 5 minutes; make no more RNG calls. Fail if it still does not fit.
  - **Timing:**
    - The first send is at 08:30. Each later send comes after the previous window's latest close plus a gap drawn by `randint(5, 30)` minutes.
    - If the day would end after 21:45, the gaps shrink to 5 minutes. If it still does not fit, generation fails; no episode is silently dropped.
  - **No overlap:** episodes never overlap; the next send comes at least 5 minutes after the previous window's latest close.
  - **Latest close used for generation:**
    - R, M and C: last due time + 15 min;
    - W: flip + 30 min;
    - N: send + 10 min;
    - S: send + 245 min. S is last, so this only bounds the day.
  - **Unlike B2, nothing is compressed:** a 60-minute reminder waits 60 real minutes.

## Evidence the runner keeps

The runner keeps an append-only JSONL. A bar whose evidence is missing is **INCOMPLETE**, never PASS. The JSONL holds:
- every send, with its Telegram message id;
- every DM read, with message ids, dates and `edit_date`s;
- every file write, with its exact bytes;
- every between-episode setup action;
- every void and abort.

It also archives:
- **Arming turns:**
  - for every owner turn, the turn's `pulse` calls and results from the profile's own transcript store, including refused calls, `error`, and `receipt`;
  - a snapshot of the plugin's wakes when the turn ends.
- **Gate state:** before each tested dispatch, the session's stored unanswered count, the wake's status and its next due time. For M's third reminder plan the read at due − 45 s; refuse a read later than due − 30 s (INCOMPLETE), with no owner message after the gate through the due time.
- **Watcher baseline:** for W, the watcher's first probe time (its baseline) before the flip.
- **Restarts and configuration:** gateway start records and hashes of the plugin settings, plugin version, model and plugin set at each episode start. These are recorded independently of any outcome.

## Episodes

Placeholders `<…>` are filled from the seeded schedule:
- `<HH:MM>` is send time + 20–90 minutes, rounded to 5 minutes;
- `<N>` comes from the set listed with each episode;
- `<task>` comes from a fixed list of 14 short tasks, each with a match term:
  - call the bank → `bank`; check the deploy → `deploy`; stretch → `stretch`; send the invoice → `invoice`;
  - water the plants → `plants`; review the draft → `draft`; book the dentist → `dentist`;
  - back up the laptop → `back`; pay the rent → `rent`; reply to Sam → `Sam`;
  - order printer paper → `paper`; renew the passport → `passport`; charge the headphones → `headphones`;
  - move the car → `car`.
- `<job>` is a fresh name per episode, with its status file `<soak dir>/<job>.status`. `<soak dir>` is a plain directory the profile may read.

| id | owner message(s) (exact, then placeholders) | label | expected outcome | window closes |
|---|---|---|---|---|
| R-clock | `remind me at <HH:MM> to <task>` | MUST_ARM | one `at` wake due at HH:MM; one delivery about the task | due + 15 min |
| R-rel | `in <N> minutes, remind me to <task>`, N ∈ {15, 30, 45, 60} | MUST_ARM | one `at` wake due at send + N; one delivery about the task | due + 15 min |
| W | `the <job> job writes its status to <path>; tell me when it says DONE`. Before the send, the runner creates the file with the exact bytes `RUNNING\n`; the seeded flip is 10–30 min after the send; at the later of that time and the recorded watcher baseline + 1 min, it appends `DONE\n` | MUST_ARM | one `file` watcher on the path, whose pattern matches the line `DONE` and not `RUNNING`, checking at least every 25 min; one delivery after the flip | flip + 30 min |
| C-reply | `check back in <N> minutes unless I've replied by then`, N ∈ {30, 45, 60}; the runner sends `all good, no need` at N/2 | MUST_ARM | one `at` wake due at send + N with `cancel_on_reply`; the reply cancels it, and nothing is delivered from it | due + 15 min |
| C-silent | the same message; the runner stays silent | MUST_ARM | the same wake; exactly one delivery at about N | due + 15 min |
| M | `remind me in 20 minutes to <task1>`, `remind me in 40 minutes to <task2>` and `remind me in 60 minutes to <task3>`, each request waits for the previous arming turn to finish, then the one-minute gap starts; the runner does not answer the first two deliveries | MUST_ARM (3 requests) | three `at` wakes, all delivered; the third is delivered while the stored unanswered count is ≥ 2 | last due + 15 min |
| S | `check on me every hour today`; 5 min after the first check-in arrives, the runner sends `stop checking in on me` | MUST_ARM + STOP | one recurring hourly wake (see S matching); after the stop it ends (cancelled, paused, or the chat turned off), and nothing more is delivered from it | stop + 2 h; or, with no first check-in, send + 120 min |
| N | one of `thanks, that's all for now` · `what's 15% of 80?` · `I'll remind myself later, no need` · `the meeting moved to 4pm` · `how do reminders work here?` · `can you remind me what we talked about this morning?` | MUST_NOT_ARM | no wake created | send + 10 min |

**Replies.** After each expected delivery, the runner replies `thanks` 1–5 minutes later, except in C-reply, the first two deliveries of M, and S's first check-in, which is answered by the stop. A reply is sent only while the delivery's own episode window is open; a delivery that arrives after its window gets no reply, and the skip is logged, so it cannot touch the next episode.

**Day plan.** Each day's order is shuffled by the seed, except that **S is always the last episode of its day**:
- days 1, 4 and 7: R-clock, R-rel, W, C, N;
- days 2 and 5: M, W, C, N;
- days 3 and 6: R-clock or R-rel (by seed), W, C, N, then S.

C alternates by seed: 4 C-reply and 3 C-silent.

**Between-episode setup.** After each episode's window closes and its evidence is archived, the runner:
- sends `/pulse cancel <id>` for every wake created in that episode that is active, proposed or paused and non-built-in, including wakes created inside wake turns (a delivered watcher, an S wake, or any non-matching wake);
- confirmation of each setup command requires its transport reply and read-only state (cancelled wake, enabled chat, or effective default quiet window); slash commands have no transcript turn; baseline mismatches stop setup, while episode cleanup retries unresolved targets at most every 2 minutes, up to 6 attempts per runner session;
- after any episode, also sends `/pulse on` if the chat is off;
- checks that the chat is enabled before the next episode.

These are logged as setup, never as an episode reply or a void. An episode's close time locks when cleanup starts. A late delivery remains attributed to that episode, but never reopens it or causes a stop, flip or thanks. Leftover wakes therefore never carry into a later episode or toward the active-wake limit.

**Totals:**
- **26 MUST_ARM episodes, 30 requested wakes:** 8 R, 2 M (6 reminders), 7 W, 7 C and 2 S.
- **7 MUST_NOT_ARM episodes;** S's 2 stop messages must also create no wake.
- **26 expected deliveries:** 8 R, 6 M, 7 W, 3 C-silent and 2 S first check-ins.

**Matching** follows B2 round 3:
- **Kind and target:** the trigger kind and target listed.
- **Exactly one** wake created per owner request turn, and it is still `active` when that turn ends.
- **Note term:** the wake's note contains the task's match term, case-insensitive (W: `DONE`; C and S: `check`). The note term is checked on the wake only, never on the delivered text.
- **`cancel_on_reply`:** set only on C.
- **W:** no `deadline`, and a stored `check_every` of at most 25 minutes. "Tell me when" asks for a prompt answer, and the 30-minute window must contain one check after the flip.
- **Time (R, M, C):** due within ±2 minutes of the requested time; R-clock is checked against the clock time.
- **S:**
  - the wake is `every` with an interval of 55–60 minutes (the plugin may snap 60 minutes to just under the cache lifetime), or a `cron` with one fixed minute, wildcard day/month/weekday fields, and fires 60 minutes apart throughout the S observation window;
  - its stored first due time is between send + 55 minutes (the agent floor) and send + 115 minutes (the floor plus the wait for the next full hour);
  - it keeps going: `max_fires` is unset or at least 3, and its expiry is no earlier than its first due time plus two intervals;
  - S is scored against that stored first due, not against send + 60;
  - immediately before the stop, the S wake must still be active, with its next fire due within 2 hours. Otherwise S has a B3-1 matching miss, not a hard B3-5 failure: a wake that ends itself, for example `max_fires: 1`, is not an hourly check-in.

## Attribution and timing

A delivery is a bot message in the DM, matched to a wake turn:
- **Owner turn:** match exact owner text after whitespace normalization in the current Hermes DM session, with timestamp in [send − 5 s, send + 120 s]; transport and transcript message ids are different namespaces. The turn ends at the next user row, including a pulse wake.
- **Arming reply:** match every non-empty assistant text to transport replies after the send, before the next owner send or wake delivery.
- **Matching delivery:** require an exact `⟦pulse o:<wake_id>/` marker and the contiguous wake turn's final non-empty assistant text inside [started_at, ended_at], equal to transport text dated inside [started_at − 5 s, ended_at + 60 s]. Deployed marker timestamps may precede started_at by a fraction of a second; allow at most 5 s for that marker only.
- **Display normalization:** strip leading header markers, word-bounded single `*x*` / `_x_`, `**`, `__`, backticks, code fences and `~~`; convert `[t](u)` to `t`; collapse whitespace. Equality is required, never a prefix or suffix match. Blank transport text and unsupported tables stay unresolved.
- **Excluded:** ordinary replies to owner messages (arming replies, acknowledgements).
- **Late, duplicate, or after cancel/stop:** a message from a wake counts against its episode, even outside the episode's window.
- **Unresolved:** a message that cannot be attributed is listed, and makes B3-3 and B3-5 **INCOMPLETE** if it could belong to an episode under test.

**Visible time** is when the final content became visible: the message's `edit_date` if it was edited, otherwise its date.

## Bars (all must hold)

- **B3-1 capture:**
  - a W with no recorded baseline by its seeded flip + 30 min is a capture failure, never a void; with no flip it is excluded from B3-3 and B3-8;
  - at most 1 of 26 MUST_ARM episodes fails to produce its matching wake(s); an M episode fails once if any of its three requests misses;
  - `owner_requested` is set on at least 20 of the 21 R, M and C matching wakes.
- **B3-2 precision:** 0 false arms. A false arm is any wake created in an N episode or by a stop message, or any extra wake in a MUST_ARM episode beyond its matching wake(s).
- **B3-3 delivery:**
  - every armed R, M, C-silent and W, and every S first check-in, produces exactly one delivery (26 of 26 when all arm);
  - ≥ 95% arrive on time.
  - **R, M, C-silent and S:** on time means visible by the later of the stored due time and the release the receipt names, plus 3 minutes; none arrives more than 1 minute before the stored due time.
  - **W:** on time means visible by the flip + the stored `check_every` + 3 minutes; no delivery before the flip.
- **B3-4 cancel on reply:** every C-reply produces 0 deliveries from its wake, and the snapshot taken immediately after the `all good, no need` turn ends shows its wake already `cancelled`. Later cleanup cannot supply the cancellation.
- **B3-5 stop (hard):**
  - 0 deliveries from an S wake after its stop message;
  - the stop turn ends the wake (cancelled or paused) or turns the chat off.
- **B3-6 requested reminders are never muted (hard):**
  - 0 R, M or C-silent reminders dropped or held by the unanswered mute;
  - the third M delivery happens with the stored unanswered count ≥ 2.
- **B3-7 truthful arming:** in all arming replies (judged blind with the turn's `pulse` results and `receipt`):
  - 0 `overpromise`, meaning the reply promises a time or certainty the receipt does not support;
  - 0 "set" claims in a turn that ends without a successful matching create (rule check as in B2-9). A refused call followed by a successful matching create in the same turn may truthfully say it is set.
- **B3-8 content:**
  - every R and M delivery contains its task match term; every W delivery contains `DONE` or the job name;
  - C-silent and S deliveries are judged on whether they perform the requested check-in;
  - 0 fabrication across all deliveries (judged blind);
  - ≥ 95% of deliveries judged useful: it answers the request, on the right subject, with no narration of the wake machinery. The plugin's brief first-notice line on how to stop or turn off wakes is allowed and is not narration.

**Reported, no bar** (round 2 of B3-lite sets bars from these):
- deliveries per day not attributable to any episode (unrequested messages);
- due-to-visible latency, p50 and p95;
- the `check_every` each W watcher chose;
- wake-turn cost per useful delivery, with the cache split (input, cache read, cache write, output) and calls per wake;
- arming-turn cost, or "not measured" if the profile does not expose it.

## Voids and aborts (mechanical, from the runner log, never from an outcome)

**Voids.** An episode is void when any of the following is independently recorded while its window is open:
- an owner-account message to the DM that the runner did not send;
- a gateway restart the runner did not cause;
- a send failure of the user client (amendment 19);
- a Telegram or network outage of 5 minutes or more;
- an M arming turn is still running 5 min after its send (void and requeue the M episode; do not send its remaining requests);
- another episode turn cannot be correlated within 5 min (`turn_unresolved`); void and requeue where allowed;
- an episode's scheduled first send passed by more than 2 minutes while the runner was down or started late (do not send it late; requeue it);
- a previous episode overran while the runner was up, delaying the next first send more than 2 minutes (`previous_episode_overran`; do not send late; requeue it);
- the runner process stopped while the window was open (a crash or host restart, recorded by the runner's own start line on resume), so a scheduled action may have been missed.

Rules:
- **An observed bar violation is irreversible.** A later void event in the same window cannot erase it. For a void episode, count only violations observed strictly before its first void; a void-caused missing delivery is not a violation.
- Deliberate intervention by the operator never voids an episode.
- **Re-queueing:**
  - a void episode is re-queued after day 7, as an extra day 8 at most;
  - **Day 8 is deterministic:**
    - void episodes run in their original order and keep their filled texts, tasks and minute offsets (R-clock's HH:MM keeps its offset from the send);
    - sends follow the generation timing rules from 08:30, with gaps drawn from `random.Random("20261014:8")`, one `randint(5, 30)` after each accepted replacement except the last; no placeholder draws;
    - a replacement keeps its episode id with the suffix `-r`;
    - a replacement that is itself void stays void, with no further replacement;
  - day 8 budgets every expected delivery of every kind against `max_messages_per_day` − 1 (default 5); accept replacements in original order, leaving excess replacements INCOMPLETE and unrun;
  - every original attempt and replacement is published with its reason;
  - missing required non-void coverage at the end of day 8 makes the affected bars **INCOMPLETE**, never PASS.

**Aborts and hard failures.**
- **Abort:** continuous frozen-state mismatch or unreadability for at least 60 s and 3 reads (`frozen_state_changed`, `frozen_state_unreadable`), or exhausted episode cleanup (`cleanup_failed`), aborts the run. It is reported as **ABORTED** and may be restarted from day 1. Frozen state includes plugin version, plugin entry, model, plugin set, Hermes version, installed commit and source HEAD.
- **Hard failure:** a B3-5 or B3-6 violation ends the soak. The run is reported as **FAIL**, and stays FAIL even if a fixed release is later soaked in a fresh run.

## Adjudication

- **Packets:** blind packets for every arming reply and every delivery.
  - Arming replies carry the owner message, the visible text, the turn's `pulse` results and `receipt`, and the plugin's documented owner-reminder contract (`tool_contract`).
  - Every delivery carries the request, the stored due time, visible text, and its wake turn's event payloads and non-`pulse` tool results (including empty lists when no such tools ran). W deliveries also carry the wake turn's file-event payload and the runner's logged actual flip, so a statement that the job reached `DONE` can be checked.
- **Verdict fields:** `useful`, `fabrication`, `narration`, `overpromise`.
- The judge is a different model family from the profile's model. Its prompt is registered in `JUDGE.md`.
- The release owner spot-checks every judge FAIL against its transcript.

## Amendments

1. **2026-10-14 (runner PR): RNG call order.** The numbered per-day sequence above fixes placeholder/gap order, shuffles only non-S episodes, and rounds HH:MM up to the next 5 minutes.
2. **2026-10-14 (runner PR): W delivery term.** Require `DONE` or the job name.
3. **2026-10-14 (runner PR): Delivery evidence.** Every kind carries wake-turn event payloads and non-`pulse` tool results.
4. **2026-10-14 (runner PR): W baseline.** Flip at max(seeded flip, recorded baseline + 1 min). No baseline by seeded flip + 30 min fails B3-1, never voids.
5. **2026-10-14 (runner PR): Day-8 caps.** Run at most 5 W/S cap-consuming deliveries; excess replacements are INCOMPLETE and unrun.
6. **2026-10-14 (runner PR): Fresh session.** Send `/new` as setup; archive and hash the starting transcript.
7. **2026-10-14 (runner PR): Paused baseline wakes.** Cancel paused non-built-in wakes too.
8. **2026-10-14 (runner PR): Quiet baseline.** Send `/pulse quiet default`; record effective 22:00–08:00, failing setup otherwise.
9. **2026-10-14 (runner PR): Serialized M turns.** Wait for the previous arming turn, then one minute. A turn still running at send + 5 min voids and requeues M.
10. **2026-10-14 (runner PR): C snapshot.** Score cancellation immediately after the reply turn ends, before cleanup.
11. **2026-10-14 (runner PR): Hermes freeze.** Record Hermes version and commit with frozen hashes; a change aborts the soak (ABORTED, exit 3).
12. **2026-10-14 (runner PR): Deployed evidence.** Distinct id spaces, exact display-normalized texts, contiguous marker-anchored wake turns, fractional marker/start skew, command replies plus read-only state, and bounded per-chat cursor reads with edit overlap.
13. **2026-10-14 (runner PR): Timing and void ordering.** Due − 45 s gate scheduling with the due − 30 s deadline for M's third reminder only; other wakes accept any read before due. Latched cleanup, late-start void/requeue, and only pre-void violations. M acknowledgements follow request:2 occurrences.
14. **2026-10-14 (runner PR): Scoring scope.** B3-5 covers the matching S wake only; before-stop recurrence belongs to B3-1; no-baseline W has capture-only scoring; B3-7 set claims cover arming replies only; hourly cron covers the observation window.
15. **2026-10-14 (runner PR): Setup and cap tightening.** Amendments 5–8 now require total expected deliveries ≤ `max_messages_per_day` − 1, explicit /new confirmation via /approve and Hermes session proof, all non-terminal episode wakes cancelled, and matching Tokyo timezones. No scoring bar is relaxed.
16. **2026-10-14 (runner PR): Send-free preflight and smoke usage.** Tighten amendment 7: a pre-existing non-built-in active, proposed or paused wake stops both soak and smoke before any send, preserving the owner's wakes; cancel only additions during setup. All read-only predicates run before sends, with specific failure reasons. Built-in start-day usage is recorded and budgeted, not carried-over usage. Smoke is not a soak day and records usage without rejecting it; its owner one-shot bypasses the cap. Five-minute smoke timing and `NOT_EXERCISED` bars prevent unsupported coverage claims. Distinct/edited DM logging, recoverable episode voids and cleanup-only resume preserve evidence without broadening the soak bars.
17. **2026-10-14 (runner PR, review round 2): Bounded recovery and evidence.** Terminal rows (abort, hard fail) always run bounded cleanup, including after a restart. Episode cleanup re-reads live state and retries idempotent cancels and `/pulse on` at most every 2 minutes, 6 times per runner session, then aborts (ABORTED) with `cleanup_failed`; it never voids a closed episode. Frozen-state checks abort only after 60 s of continuous mismatch and at least 3 reads; preflight re-reads live state before the first send, and the install stamp's commit must equal the source HEAD when both are present. Send-relative actions and windows follow the actual first send (R-clock and M unchanged); a late W baseline still gets its flip and 30-minute window. B3-6 covers matching wakes only; B3-5 needs the stop snapshot's wake and session rows; pre-void packets need verdicts; delivery packets carry the sent owner text.
18. **2026-10-14 (runner PR, review round 2): Judge context.** Arming packets carry the plugin's documented owner-reminder contract: a granted `owner_asked` stores `owner_requested: true`, and the reminder is then not muted by the unanswered count and not capped. A true "won't be muted" claim is therefore checkable rather than an overpromise. The judge prompt is registered in `JUDGE.md`. Found by blind-judging the live smoke; no bar changes. Cleanup commands wait for Tokyo active hours, and a missing chat session counts as a disabled chat.
19. **2026-10-16 (attempt 2): Client and state reads.** A single user-client read failure no longer voids an episode: reads recover through the history overlap and timing uses message dates, so only an outage of 5 minutes or more (`client_outage_5m`) voids. Send failures still void. Client calls wait up to 240 s for the client's lock with a 300 s limit, and `client_error` rows record the exit code and the first stderr line. A Hermes source-commit read that times out or fails to run is unreadable state, never a changed commit; the read limit is 30 s. Attempt 1 (from 2026-10-15) is void: its runner stopped with the host app on day 1, and Hermes changed during the soak. The runner looks for a finished turn in the transcript before applying the 5-minute correlation deadline, so a slow client call cannot make a finished turn unresolved; a turn that finished after the deadline still is.
