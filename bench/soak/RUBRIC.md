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
  - HeartBeat 2.0 at **v0.1.15 or later**, on plugin defaults except `timezone`;
  - the profile's normal model; the model and provider are recorded at the start.
- **Frozen during the soak:** the plugin version, the plugin's settings, the model and the gateway's plugin set. A change aborts the run (see Voids).
- **Scripted owner:** a runner sends from the owner's Telegram account through a user client, so the gateway sees ordinary owner messages.
- **Hours:** the first send is at 08:30 or later, every due time is at 21:30 or earlier, and every episode window closes by 21:45 (profile timezone). No episode crosses quiet hours (default 22:00–08:00).
- **Schedule:** deterministic from the registered seed **20261014**.
  - The generator is code committed to this repository and reviewed before day 1. The run declaration records the generator's commit, the seed, the first day's date and the schedule's SHA-256. The scorer regenerates the schedule and refuses a mismatch.
  - **Draw order:**
    - Day `d` (1–7) draws from Python's `random.Random(f"20261014:{d}")`. Global choices draw from `random.Random("20261014:0")`, in this order:
      - the 4 C-reply days, `sample(range(1, 8), 4)`;
      - the R kind for days 3 and 6, `choice` per day;
      - the task order, one `shuffle` of the 14 tasks, each used exactly once by the 14 reminder requests in schedule order.
    - Each day shuffles its episode list with `shuffle`, keeping S last, then draws its placeholders in episode order (`choice` from each listed set; HH:MM offsets and flip delays by `randint` over the listed minute ranges, rounded where stated).
  - **Timing:**
    - The first send is at 08:30. Each later send comes after the previous window's latest close plus a gap drawn by `randint(5, 30)` minutes.
    - If the day would end after 21:45, the gaps shrink to 5 minutes. If it still does not fit, generation fails; no episode is silently dropped.
  - **No overlap:** episodes never overlap; the next send comes at least 5 minutes after the previous window's latest close.
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
| W | `the <job> job writes its status to <path>; tell me when it says DONE`. Before the send, the runner creates the file with the exact bytes `RUNNING\n`; at the seeded flip, 10–30 min after the send, it appends `DONE\n` | MUST_ARM | one `file` watcher on the path, whose pattern matches the line `DONE` and not `RUNNING`; one delivery after the flip | flip + max(30 min, `check_every` + 5 min) |
| C-reply | `check back in <N> minutes unless I've replied by then`, N ∈ {30, 45, 60}; the runner sends `all good, no need` at N/2 | MUST_ARM | one `at` wake due at send + N with `cancel_on_reply`; the reply cancels it, and nothing is delivered from it | due + 15 min |
| C-silent | the same message; the runner stays silent | MUST_ARM | the same wake; exactly one delivery at about N | due + 15 min |
| M | `remind me in 20 minutes to <task1>`, `remind me in 40 minutes to <task2>` and `remind me in 60 minutes to <task3>`, sent 1 minute apart; the runner does not answer the first two deliveries | MUST_ARM (3 requests) | three `at` wakes, all delivered; the third is delivered while the stored unanswered count is ≥ 2 | last due + 15 min |
| S | `check on me every hour today`; 5 min after the first check-in arrives, the runner sends `stop checking in on me` | MUST_ARM + STOP | one recurring hourly wake (see S matching); after the stop it ends (cancelled, paused, or the chat turned off), and nothing more is delivered from it | stop + 2 h; or, with no first check-in, send + 120 min |
| N | one of `thanks, that's all for now` · `what's 15% of 80?` · `I'll remind myself later, no need` · `the meeting moved to 4pm` · `how do reminders work here?` · `can you remind me what we talked about this morning?` | MUST_NOT_ARM | no wake created | send + 10 min |

**Replies.** After each expected delivery, the runner replies `thanks` 1–5 minutes later, except in C-reply, the first two deliveries of M, and S's first check-in, which is answered by the stop.

**Day plan.** Each day's order is shuffled by the seed, except that **S is always the last episode of its day**:
- days 1, 4 and 7: R-clock, R-rel, W, C, N;
- days 2 and 5: M, W, C, N;
- days 3 and 6: R-clock or R-rel (by seed), W, C, N, then S.

C alternates by seed: 4 C-reply and 3 C-silent.

**Between-episode setup.** After S's window closes and its evidence is archived, the runner:
- sends `/pulse cancel <id>` for any S wake still active;
- sends `/pulse on` if the chat is off;
- checks that the chat is enabled before the next day's first episode.

These are logged as setup, never as an episode reply or a void.

**Totals:**
- **26 MUST_ARM episodes, 30 requested wakes:** 8 R, 2 M (6 reminders), 7 W, 7 C and 2 S.
- **7 MUST_NOT_ARM episodes;** S's 2 stop messages must also create no wake.
- **26 expected deliveries:** 8 R, 6 M, 7 W, 3 C-silent and 2 S first check-ins.

**Matching** follows B2 round 3:
- **Kind and target:** the trigger kind and target listed.
- **Exactly one** wake created per owner request turn, and it is still `active` when that turn ends.
- **Note term:** the wake's note contains the task's match term, case-insensitive (W: `DONE`; C and S: `check`). The note term is checked on the wake only, never on the delivered text.
- **`cancel_on_reply`:** set only on C.
- **No `deadline`** on W.
- **Time (R, M, C):** due within ±2 minutes of the requested time; R-clock is checked against the clock time.
- **S:**
  - the wake is `every` with an interval of 55–60 minutes (the plugin may snap 60 minutes to just under the cache lifetime), or an hourly `cron`;
  - its stored first due time is at or before send + 115 minutes (the agent floor of 55 minutes plus the wait for the next full hour);
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
  - at most 1 of 26 MUST_ARM episodes fails to produce its matching wake(s); an M episode fails once if any of its three requests misses;
  - `owner_requested` is set on all 21 R, M and C matching wakes but at most one.
- **B3-2 precision:** 0 false arms. A false arm is any wake created in an N episode or by a stop message, or any extra wake in a MUST_ARM episode beyond its matching wake(s).
- **B3-3 delivery:**
  - every armed R, M, C-silent and W, and every S first check-in, produces exactly one delivery (26 of 26 when all arm);
  - ≥ 95% arrive on time.
  - **R, M, C-silent and S:** on time means visible by the later of the stored due time and the release the receipt names, plus 3 minutes; none arrives more than 1 minute before the stored due time.
  - **W:** on time means visible by the flip + the stored `check_every` + 3 minutes; no delivery before the flip.
- **B3-4 cancel on reply:** every C-reply produces 0 deliveries from its wake, and the wake ends `cancelled`.
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
  - every R, M and W delivery contains its task match term;
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
- a Telegram or network outage of 5 minutes or more.

Rules:
- **An observed bar violation is irreversible.** A later void event in the same window cannot erase it.
- Deliberate intervention by the operator never voids an episode.
- **Re-queueing:**
  - a void episode is re-queued after day 7, as an extra day 8 at most;
  - every original attempt and replacement is published with its reason;
  - missing required non-void coverage at the end of day 8 makes the affected bars **INCOMPLETE**, never PASS.

**Aborts.** The run is aborted, reported as ABORTED, and restarted from day 1 when:
- a frozen setting changes (plugin version, plugin settings, model or plugin set);
- or a hard bar fails: a B3-5 or B3-6 violation ends the soak as **FAIL**; fix it, release, and restart from day 1.

## Adjudication

- **Packets:** blind packets for every arming reply and every delivery.
  - Arming replies carry the owner message, the visible text, and the turn's `pulse` results and `receipt`.
  - Deliveries carry the request, the stored due time and the visible text. W deliveries also carry the wake turn's file-event payload and the runner's logged flip, so a statement that the job reached `DONE` can be checked.
- **Verdict fields:** `useful`, `fabrication`, `narration`, `overpromise`.
- The judge is a different model family from the profile's model.
- The release owner spot-checks every judge FAIL against its transcript.

## Amendments

None yet. Tightening only, and only before day 1.
