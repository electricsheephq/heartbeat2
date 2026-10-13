# Round-1 live wake bench

Pre-register this package at an immutable commit before running a real model.
P1 forwards wake requests to the primary OpenAI-compatible route. P3 injects
a primary 503 and exercises Hermes's fallback through the same proxy with a
second upstream model. P2/P4 and a live adjudicator are outside round 1.

Run with the selected pin's isolated Python environment (never a live profile):
```sh
python -m bench.live.run --pin "$PIN" --path P1 --upstream-url "$URL" \
  --model "$MODEL" --key-env BENCH_KEY --reps 3 --workers 2 --out "$RESULTS"
python -m bench.live.run --pin "$PIN" --path P3 --upstream-url "$URL" \
  --model "$MODEL" --fallback-model "$SECOND_MODEL" --key-env BENCH_KEY --out "$RESULTS"
python -m bench.live.run --pin "$PIN" --path P1 --upstream fake \
  --reps 1 --scenarios L1,L3,L7,L8 --out "$SMOKE_RESULTS"
python -m bench.live.score --results "$RESULTS" --out "$SCORES"
python -m bench.live.adjudicate packets --results "$RESULTS" --out "$PACKETS"
python -m bench.live.adjudicate merge --score "$SCORES/score.json" \
  --verdicts "$VERDICTS" --out "$SCORES"
```

All output directories must be outside the checkout. Use a fresh output directory
for each run; bodies contain synthetic conversations but are not public artifacts.
The key comes only from the named environment variable. Records contain bodies,
usage and timing, never headers. The gateway uses dummy credentials in a temporary
home. model.streaming=false selects JSON transport in each temporary rig.
A second 429 records QUOTA and stops scheduling. One retry covers 429/5xx;
injected P3 faults go directly to Hermes fallback. HTTP timeout is 180 seconds.

Every cell has its own subprocess, PulseRig, home, stand-in and plugin copy.
Seeding and arming are scripted; only wakes, their continuations and L8's stop
turn are forwarded. Relative seconds compress waiting without changing clocks:
L1 is an owner reminder ("call the bank before it closes") on a five-second owner timer; L2 changes a
file probe from green to red; L3 leaves its file unchanged and fires its deadline;
L4 arms before seeding resolution; L5 delivers a scripted prior wake without an owner reply;
L6 has empty facts; L7 inserts a scoped subagent completion inbox event;
L8 forwards the stop after arming and observes real poller ticks; L9 offers lookup.
L8's owner acknowledgement is excluded from wake delivery counts. Cancellation
before the next due wake is successful stop evidence, not a fabricated wake.

The fake upstream is plumbing proof only. Incomplete rounds and missing semantic
judgments remain PENDING; fake costs and verdicts never establish real quality.
The runner retains the harness's temp evidence and produces body-only JSONL,
cell JSON, score JSON/Markdown and blind packets. No model adjudication runs here.

## Round 3: capture and realistic context

Use v0.1.14 or later for real round-3 runs; GLM on the subscription lane is required.
The second family remains **not run** until its non-metered lane and null-usage fix exist.
Metered spend requires owner approval. Capture cells forward one exact owner turn and
its continuations; only N7 is pre-armed. Pulse arguments/results, refusals, receipts,
final wakes and replies are retained. Fake cells are plumbing only and stay PENDING.
A single created wake that does not match in a MUST_ARM cell counts as a false arm for B2-7 (an "unrelated wake") and as a miss for B2-6.
```sh
python -m bench.live.run --round 3 --pin "$PIN" --path P1 --upstream-url "$URL" --model "$MODEL" --key-env BENCH_KEY --out "$RESULTS"
python -m bench.live.run --round 3 --pin "$PIN" --path P1 --upstream-url "$URL" --model "$MODEL" --key-env BENCH_KEY --context-tokens 120000 --out "$RESULTS"
python -m bench.live.score --round 3 --results "$RESULTS" --out "$SCORES"
```
Without an explicit scenario list, the runner defaults to the nine round-2 wake
scenarios; `--round 3` selects all 25 wake and capture scenarios. The default
round-2 scorer ignores capture results in mixed directories.
Both arms must pass; ctx reports as `P1-ctx120000` (or `P3-ctx120000`). The frozen
prefix is `round3.filler_history`, seed **314159**, N **120000**, **553** synthetic
small-talk pairs, sized by `len(prefix)//4` (**120014**). Turns alternate owner
(20–80 words) and assistant (40–160 words), joining 2–6 sentences from forty
distinct fact-free templates per role without identical consecutive turns. The
pair count is chosen to make the serialized prefix closest to N. Prefix serialization is
UTF-8 JSON of the pairs, `ensure_ascii=False, separators=(',', ':')`; SHA-256:
`e526720ce096d3059cdb40856f2b3730565706cd5c160e6e0cc771a158980e61`.
Any other size/hash stays PENDING. Judge packets retain frozen history, measured
owner text and pulse results/receipts. Round-3 verdicts also require `overpromise`;
use a different model family and owner spot-check every adjudicator FAIL.
