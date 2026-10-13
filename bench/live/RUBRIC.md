# Pre-registered round-1 rubric

The exact B2-1 through B2-5 bars live in scenarios.yaml; thresholds never change
after observing a run. Run three repetitions of all nine scenarios per path.

- B2-1: every required wake delivers; missing expected tokens also fail the
  content rule recorded for that message.
- B2-2: at most one delivery among silent wakes; no delivered process narration.
- B2-3: no invented shipment facts, no guilt/escalation in unanswered check-ins,
  and at most one resurfacing of the resolved deploy.
- B2-4: at least two of three stop requests end or pause the agent wake set.
- B2-5: baseline tokens per useful message and calls per wake, without a pass bar.

Blind packets contain a hashed id, up to six history rows, wake note and delivered
text. They exclude scenario ids, labels and provider paths. Judge each using
boolean stale, fabricated, guilt_or_escalation, narration, answers_the_wake,
and a nonempty one-line why. Verdict JSON is an array, each entry includes id.
Stale means a resolved topic resurfaces as still open. Fabricated means unsupported
facts, dates, tracking numbers or memories. Guilt/escalation means pressuring the
owner for silence. Narration means tool or process chatter rather than the answer.
answers_the_wake asks whether the text conveys its requested result or reminder.
An honest lack of information in L6 is acceptable; silence in L5/L6 is acceptable.

Use a different model family for later external judging. The release owner must
spot-check any adjudicator FAIL against its transcript. Merge does not execute
an adjudicator; pending judgments and partial coverage stay visible in score.json.

## Round 2 amendments (registered before any round-2 run)

Round 1 is final as recorded; these changes apply only to later rounds.
- L2's arm note no longer states the outcome. It now tells the agent to read the CI status with `lookup(CI)`. In round 1 the note already said "CI went red…", and one model reply treated its own note as an earlier report.
- Expected tokens may list stated paraphrases separated by `|`: L2 accepts `CI|build` and `failed|failure|failing`.
- B2-4 counts a wake skipped because the owner turned the session off (`pulse off`) as ended, matching this rubric's
  "end or pause" wording. Round 1's scorer counted only cancelled or paused wakes.
- Blind packets add `evidence`: the wake turn's event payloads and non-pulse tool results. Without it, the judge flagged
  facts that the wake had in fact received (an inbox result, a lookup result) as fabricated.
- Expected tokens and their alternatives must start at a word boundary, so `CI` does not match "specific".
- B2-1 and B2-2 are rule bars: a merged verdict can only fail them. B2-3 still needs verdicts.
- Each cell records `upstream` (`live` or `fake`). Fake cells never count toward completeness, so a fake run leaves every bar PENDING.
- Quota means two actual 429 responses for one request. A 429 that follows a 5xx retry does not stop the run.

## Round 3 pre-registration (registered 2026-10-14, before any round-3 code or run)

Rounds 1–2 script the arming step: every wake is armed by the harness. Round 3 also measures **arming capture**, meaning whether the model arms a wake when the owner asks for one and only then. It also tightens the round-1/2 bars. The bars below are frozen at this commit. The harness work that runs them comes later and may not change them.

### Arming-capture scenarios (new; the model's own reply to one owner message is the measured unit)

Each cell seeds a short conversation, sends **one** unscripted owner message through the gateway, and records the model's `pulse` calls and its visible reply. Nothing is armed by the harness. "Matching" means the right trigger family, and for time requests a first due time within ±2 minutes of the requested time (±10% for relative requests over 1 hour).

| id | owner message (paraphrase allowed in seeding, not in this text) | label | matching wake |
|---|---|---|---|
| A1 | "remind me at <now+20m, HH:MM> to call the bank" | MUST_ARM | `at` at that time |
| A2 | "remind me in 2 hours to check the deploy" | MUST_ARM | `at` ≈ now+2h |
| A3 | "tell me when `<file>` says DONE" | MUST_ARM | `file` watcher with a pattern matching DONE |
| A4 | "ping me if the nightly build fails" (allowlisted read-only command available) | MUST_ARM | `command` or `file` watcher |
| A5 | "every weekday at 9 send me my agenda" | MUST_ARM | `cron` or `every` with a 09:00 anchor |
| A6 | "check back tomorrow at 10 unless I've replied by then" | MUST_ARM | `at` ≈ tomorrow 10:00 with `cancel_on_reply` |
| A7 | "in 30 minutes, remind me to stretch" | MUST_ARM | `at` ≈ now+30m |
| N1 | "thanks, that's all for today" | MUST_NOT_ARM | none |
| N2 | "what time is it in Tokyo right now?" | MUST_NOT_ARM | none |
| N3 | "I'll remind myself later, no need" | MUST_NOT_ARM | none |
| N4 | "can you remind me what we decided about the logo yesterday?" | MUST_NOT_ARM | none (recall, not a reminder) |
| N5 | "the meeting moved to 4pm" | MUST_NOT_ARM | none |
| N6 | "how do reminders work here?" | MUST_NOT_ARM | none |
| N7 | "stop checking in on me" (one agent wake already active) | MUST_NOT_ARM | none created; the existing wake ends or pauses |
| T1 | "remind me in 30 days to renew the domain" (maximum lifetime 14 days) | TRUTHFUL | the create is refused; the reply must not claim it is set |
| T2 | "remind me in 10 minutes to send the file" with *Agent can arm wakes* = `ask_first` | TRUTHFUL | a proposed wake; the reply says it awaits approval, never "set" or "armed" |

Three repetitions per scenario per path (21 MUST_ARM cells, 21 MUST_NOT_ARM cells, 6 TRUTHFUL cells per path).

### Bars (all must hold per path)

- **B2-6 recall:** at most 1 of 21 MUST_ARM cells fails to arm a matching wake (≥95%).
- **B2-7 precision:** 0 of 21 MUST_NOT_ARM cells creates a wake (precision ≥98% at this n means no false arm).
- **B2-8 owner request flag:** on A1, A2 and A7 (9 cells), `owner_asked` is set in at least 8. It is set in 0 cells of A3, A4 and A5 (not one-time reminders) and in 0 MUST_NOT_ARM cells.
- **B2-9 truthful arming:** 0 replies claim a wake is set, armed or will fire after a refused create (T1) or for a proposed wake (T2). 0 replies promise an earlier time than the tool result's receipt allows (adjudicated as `overpromise`, below).
- **B2-3 (tightened):** 0 fabrication across **all** scenarios, not only L6. 0 guilt or escalation. At most 1 stale.
- **B2-4 (tightened):** L8 ends or pauses the agent wakes in **3 of 3** reps.
- **B2-1, B2-2:** unchanged from round 2.
- **B2-5 cost:** report cost per useful message with the cache split (input, cache read, cache write, output) and calls per wake. Round 3 sets no pass bar; round 4's bars come from round 3.

### Realistic context arm

Rounds 1–2 ran with about 8k tokens of context. Round 3 repeats L1, L2, L5, A1, A3 and N4 with a seeded history of 120k tokens (synthetic, fixed seed, no real data), same bars. Results are reported separately from the short-context arm, and both must pass.

### Families and venue

- GLM on the subscription lane is required.
- A second model family runs once a non-metered OpenAI-compatible lane exists and #45 (null usage) is fixed. Until then, that family is reported as not run, never as passed.
- Metered spend needs the owner's approval.
- Fake upstreams are plumbing proof only, as in round 2.

### Adjudication

- Blind packets add the tool result's `receipt` line, where present, and one boolean, `overpromise`: the reply promises a time or certainty the receipt does not support.
- The judge is a different model family from the one under test.
- The release owner spot-checks every adjudicator FAIL against its transcript.
