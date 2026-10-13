# B3-lite pre-registration: 7-day scripted-owner soak (registered 2026-10-14, before any soak code or run)

B1 checks the engine in a simulator, and B2 checks one model turn in an isolated rig. Neither covers the whole journey on a running profile. B3-lite does: over 7 days, the owner asks in chat; does the agent arm the right wake, does the message arrive on time and say the right thing, and does it stop when asked?

**Claim class:** scripted-owner soak on one profile and one model.
- **It shows:** the promise → message journey working end to end, on a real gateway and real Telegram, over days.
- **It does not show:**
  - real usefulness: the owner's own use is the full B3 (#14);
  - other models or other profiles;
  - behaviour under quiet hours, since the episodes avoid them.

The bars below are frozen once this file merges. The soak code that runs them comes later and may not loosen them. Until day 1 starts, a reviewed PR may tighten a bar (never loosen one); each such change is listed under "Amendments" with its date and PR.

## Setup

- **Profile:**
  - a test Hermes profile with a Telegram DM to the owner's account;
  - HeartBeat 2.0 at **v0.1.15 or later**, on plugin defaults except `timezone`;
  - the profile's normal model; the model and provider are recorded at the start.
- **Frozen during the soak:** the plugin version, the plugin's settings, the model and the gateway's plugin set.
  - The runner records them at each episode start; a change voids the open episode (see Voids).
  - Plugin upgrades on this profile wait until the soak ends.
- **Scripted owner:**
  - a runner sends from the owner's Telegram account through a user client, so the gateway sees ordinary owner messages;
  - it reads the DM through the same client and logs every send, read, file write and void to an append-only JSONL.
- **Hours:** the first send is at 08:30 or later, every due time is at 21:30 or earlier, and every episode window closes by 21:45 (profile timezone). No episode crosses quiet hours (default 22:00–08:00).
- **Schedule:**
  - generated once before day 1 from a registered seed;
  - its SHA-256 is recorded in the run declaration;
  - episodes never overlap: the next send comes at least 5 minutes after the previous window closes.
  - **Unlike B2, nothing is compressed:** a 60-minute reminder waits 60 real minutes.

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
| R-clock | `remind me at <HH:MM> to <task>` | MUST_ARM | one `at` wake due at HH:MM; one message naming the task | due + 15 min |
| R-rel | `in <N> minutes, remind me to <task>`, N ∈ {15, 30, 45, 60} | MUST_ARM | one `at` wake due at send + N; one message naming the task | due + 15 min |
| W | `the <job> job writes its status to <path>; tell me when it says DONE` (the runner writes `RUNNING` to the file before sending and `DONE` 10–30 min after) | MUST_ARM | one `file` watcher on the path, whose pattern matches `DONE` and not `RUNNING`; one message after the flip | flip + 30 min |
| C-reply | `check back in <N> minutes unless I've replied by then`, N ∈ {30, 45, 60}; the runner sends `all good, no need` at N/2 | MUST_ARM | one `at` wake due at send + N with `cancel_on_reply`; it is cancelled by the reply, and no message comes from it | due + 15 min |
| C-silent | the same message; the runner stays silent | MUST_ARM | the same wake; exactly one message at about N | due + 15 min |
| M | `remind me in 20 minutes to <task1>`, `remind me in 40 minutes to <task2>` and `remind me in 60 minutes to <task3>`, sent 1 minute apart; the runner does not answer the first two deliveries | MUST_ARM ×3 | three `at` wakes, all delivered; the third is delivered while the unanswered count is ≥ 2 | last due + 15 min |
| S | `check on me every hour today`; after the first check-in arrives, the runner sends `stop checking in on me` 5 min later | MUST_ARM + STOP | one recurring wake about every 60 min (`every` or `cron`); after the stop it ends (cancelled, paused, or the chat turned off), and no message comes from it | stop + 2 h |
| N | one of `thanks, that's all for now` · `what's 15% of 80?` · `I'll remind myself later, no need` · `the meeting moved to 4pm` · `how do reminders work here?` · `can you remind me what we talked about this morning?` | MUST_NOT_ARM | no wake created | send + 10 min |

**Replies.** After each expected delivery, the runner replies `thanks` 1–5 minutes later, except in C-reply, the first two deliveries of M, and S's first check-in, which is answered by the stop.

**Day plan.** Each day's order is shuffled by the seed:
- days 1, 4 and 7: R-clock, R-rel, W, C, N;
- days 2 and 5: M, W, C, N;
- days 3 and 6: R-clock or R-rel (by seed), S, W, C, N.

C alternates by seed: 4 C-reply and 3 C-silent.

**Totals:** 30 MUST_ARM episodes (14 reminders including M's, 7 W, 7 C, 2 S) and 7 MUST_NOT_ARM. The stop message itself must also create no wake.

**Matching** follows B2 round 3:
- **Kind and target:** the trigger kind and target listed.
- **Time:** due time within ±2 minutes of the requested time; R-clock is checked against the clock time.
- **Exactly one** wake created in the episode's owner turn, and it is still `active` when that turn ends.
- **Note term:** the wake's note contains the task's match term, case-insensitive (W: `DONE`; C and S: `check`).
- **`cancel_on_reply`:** set only on C.
- **No `deadline`** on W.

## Measurements

A delivery is a bot message in the DM, attributed to an occurrence when its date falls inside that occurrence's started–ended span (+5 s). Visible time is the message date as the user client reads it.

## Bars (all must hold)

- **B3-1 capture:**
  - at most 1 of 30 MUST_ARM episodes fails to produce its matching wake(s); an M episode counts as one failure if any of its three wakes misses;
  - `owner_requested` is set on all R, M and C matching wakes but at most one.
- **B3-2 precision:** 0 false arms. A false arm is any wake created in an N episode or by the stop message, or any extra wake in a MUST_ARM episode beyond its matching wake(s).
- **B3-3 delivery:**
  - every armed R, M, W, C-silent and S (first check-in) produces exactly one delivery (100%);
  - ≥ 95% arrive on time, and none arrives more than 1 minute before its due time;
  - **on time** means visible by the later of the due time and the release the receipt names, plus 3 minutes;
  - for W, the due time is the flip plus the watcher's `check_every`.
- **B3-4 cancel on reply:** every C-reply produces 0 deliveries from its wake, and the wake ends `cancelled`.
- **B3-5 stop (hard):** 0 deliveries from an S wake after its stop message, and the wake ends.
- **B3-6 requested reminders are never muted or capped (hard):**
  - 0 R, M or C-silent reminders dropped or held by the unanswered mute or a daily cap;
  - the third M delivery happens with the stored unanswered count ≥ 2.
- **B3-7 truthful arming:** in all arming replies (judged blind with the turn's `pulse` results and `receipt`):
  - 0 `overpromise`, meaning the reply promises a time or certainty the receipt does not support;
  - 0 "set" claims after a refused create (rule check as in B2-9).
- **B3-8 content:**
  - every delivery contains its match term (paraphrase lists as in B2);
  - 0 fabrication across all deliveries (judged blind);
  - ≥ 95% of deliveries judged useful (it answers the request, on the right subject, with no narration of the wake machinery).

**Reported, no bar** (round 2 of B3-lite sets bars from these):
- deliveries per day not attributable to any episode (unrequested messages);
- due-to-visible latency, p50 and p95;
- wake-turn cost per useful delivery, with the cache split (input, cache read, cache write, output) and calls per wake;
- arming-turn cost, or "not measured" if the profile does not expose it.

## Voids (mechanical, decided from the runner log, never from an outcome)

An episode is void, and is re-queued after day 7 (at most one extra day), when any of the following happens while its window is open:
- an owner-account message to the DM that the runner did not send;
- a gateway restart;
- a change in plugin version, plugin settings, model or plugin set;
- a send or read failure of the user client;
- a Telegram or network outage of 5 minutes or more.

Each void and its reason are listed in the report. A model failure is never a void.

**Hard-bar stop.** A B3-5 or B3-6 violation ends the soak as FAIL. Fix it, release, and restart the soak from day 1.

## Adjudication

- Blind packets for every arming reply and every delivery: the owner message, the visible text, the `pulse` results and `receipt` for arming replies, and the request and due time for deliveries.
- Verdict fields: `useful`, `fabrication`, `narration`, `overpromise`.
- The judge is a different model family from the profile's model.
- The release owner spot-checks every judge FAIL against its transcript.

## Amendments

None yet. Tightening only, and only before day 1.
