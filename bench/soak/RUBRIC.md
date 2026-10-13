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
  - send `/new` in the DM, verify that it starts a fresh Hermes session, and archive and hash the starting transcript;
  - the chat is enabled;
  - no non-built-in wake is active, proposed or paused (any found is cancelled with `/pulse cancel <id>`);
  - run `/pulse quiet default` and record the effective quiet window; it must be 22:00–08:00 or setup fails;
  - the stored unanswered count is 0;
  - day 1 starts on a fresh calendar day, so no daily usage is carried over.

  Any check that fails after cleanup stops the start.
- **Scripted owner:** a runner sends from the owner's Telegram account through a user client, so the gateway sees ordinary owner messages.
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
- **Gate state:** before each tested dispatch, the session's stored unanswered count, the wake's status and its next due time. For M's third reminder this is read at least 30 s before its due time, with no owner message after.
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
- sends `/pulse cancel <id>` for every wake created in that episode that is still active (a delivered watcher, an S wake, or any non-matching wake);
- after S, also sends `/pulse on` if the chat is off;
- checks that the chat is enabled before the next episode.

These are logged as setup, never as an episode reply or a void. Leftover wakes therefore never carry into a later episode or toward the active-wake limit.

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
  - the wake is `every` with an interval of 55–60 minutes (the plugin may snap 60 minutes to just under the cache lifetime), or an hourly `cron`;
  - its stored first due time is between send + 55 minutes (the agent floor) and send + 115 minutes (the floor plus the wait for the next full hour);
  - it keeps going: `max_fires` is unset or at least 3, and its expiry is no earlier than its first due time plus two intervals;
  - S is scored against that stored first due, not against send + 60;
  - immediately before the stop, the S wake must still be active, with its next fire due within 2 hours. Otherwise S fails: a wake that ends itself, for example `max_fires: 1`, is not an hourly check-in.

## Attribution and timing

A delivery is a bot message in the DM, matched to a wake turn:
- **Matching:** its Telegram message id and final text are matched to that turn's recorded output. Occurrence start and end times only narrow the candidates.
- **Excluded:** ordinary replies to owner messages (arming replies, acknowledgements).
- **Late, duplicate, or after cancel/stop:** a message from a wake counts against its episode, even outside the episode's window.
- **Unresolved:** a message that cannot be attributed is listed, and makes B3-3 and B3-5 **INCOMPLETE** if it could belong to an episode under test.

**Visible time** is when the final content became visible: the message's `edit_date` if it was edited, otherwise its date.

## Bars (all must hold)

- **B3-1 capture:**
  - a W with no recorded baseline by its seeded flip + 30 min is a capture failure, never a void;
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
- a send or read failure of the user client;
- a Telegram or network outage of 5 minutes or more;
- an M arming turn is still running 5 min after its send (void and requeue the M episode; do not send its remaining requests);
- the runner process stopped while the window was open (a crash or host restart, recorded by the runner's own start line on resume), so a scheduled action may have been missed.

Rules:
- **An observed bar violation is irreversible.** A later void event in the same window cannot erase it.
- Deliberate intervention by the operator never voids an episode.
- **Re-queueing:**
  - a void episode is re-queued after day 7, as an extra day 8 at most;
  - **Day 8 is deterministic:**
    - void episodes run in their original order and keep their filled texts, tasks and minute offsets (R-clock's HH:MM keeps its offset from the send);
    - sends follow the generation timing rules from 08:30, with gaps drawn from `random.Random("20261014:8")`, one `randint(5, 30)` after each accepted replacement except the last; no placeholder draws;
    - a replacement keeps its episode id with the suffix `-r`;
    - a replacement that is itself void stays void, with no further replacement;
  - at most 5 cap-consuming deliveries (W and S) run on day 8; accept replacements in original order, leaving excess cap-consuming replacements INCOMPLETE and unrun;
  - every original attempt and replacement is published with its reason;
  - missing required non-void coverage at the end of day 8 makes the affected bars **INCOMPLETE**, never PASS.

**Aborts and hard failures.**
- **Abort:** a change to a frozen setting (plugin version, plugin entry, model, plugin set, Hermes version or Hermes commit) aborts the run. It is reported as **ABORTED** and may be restarted from day 1.
- **Hard failure:** a B3-5 or B3-6 violation ends the soak. The run is reported as **FAIL**, and stays FAIL even if a fixed release is later soaked in a fresh run.

## Adjudication

- **Packets:** blind packets for every arming reply and every delivery.
  - Arming replies carry the owner message, the visible text, and the turn's `pulse` results and `receipt`.
  - Every delivery carries the request, the stored due time, visible text, and its wake turn's event payloads and non-`pulse` tool results (including empty lists when no such tools ran). W deliveries also carry the wake turn's file-event payload and the runner's logged actual flip, so a statement that the job reached `DONE` can be checked.
- **Verdict fields:** `useful`, `fabrication`, `narration`, `overpromise`.
- The judge is a different model family from the profile's model.
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
