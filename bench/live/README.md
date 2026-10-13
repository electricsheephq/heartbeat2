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
L1 represents the 17:00 reminder with a five-second owner timer; L2 changes a
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
