# AGENTS.md

Guidance for AI coding agents, and for people, working in this repository.

## What this is

HeartBeat 2.0 is a standalone Hermes Agent plugin for bounded wakes and deterministic watchers. It schedules a turn in the conversation that armed it, using that conversation's model and tools. It delivers into gateway DMs (group chats only when `dm_only` is off) and Desktop/TUI local sessions on Hermes hosts with local plugin injection (0.23+). Chats served by an app backend of their own (the Desktop app's standalone `hermes serve`, or the terminal TUI's backend) take one-time and recurring wakes, scheduled and delivered by the one app backend that holds the profile's backend lock. Retired app sessions HOLD wakes until reattached; CLI, API server and cron arming are refused.

## Layout

- `plugin.yaml`: manifest, compatibility and settings schema.
- `__init__.py` and `heartbeat2/registration.py`: plugin loading, hooks and profile runtime.
- `heartbeat2/`: scheduler, policy, commands, probes and SQLite store.
- `skills/heartbeat2-usage/SKILL.md`: bundled usage guidance; `skills/heartbeat2-report-problem/SKILL.md`: the bug-report workflow.
- `tests/`: network-denied unit tests and separate integration/E2E suites.
- `bench/`: deterministic simulation and separately operated live benchmarks.
- `.github/workflows/`: unit matrix, pinned-Hermes loader and gateway E2E CI.
- `SECURITY.md`: consent, session, probe and data-handling boundaries.

## Setup and checks

CI is `.github/workflows/ci.yml` (unit suite and Hermes integration) and `.github/workflows/e2e.yml` (real gateways on loopback).
The unit suite runs locally in about two minutes:

```bash
python -m pip install pytest pyyaml croniter psutil
python -m pytest -q
```

The integration and gateway E2E jobs install pinned Hermes and run `tests/integration` and the E2E harness. Leave those to CI unless
your change touches them.

A pull request merges only when these 12 required checks pass:

- `Unit (ubuntu-latest, Python 3.11)`, `Unit (ubuntu-latest, Python 3.13)`, `Unit (ubuntu-latest, Python 3.14)`
- `Unit (macos-latest, Python 3.11)`, `Unit (macos-latest, Python 3.13)`, `Unit (macos-latest, Python 3.14)`
- `Real Hermes loader (v0.21.6)`, `Real Hermes loader (pinned-main)`
- `Real Telegram gateway (v0.21.6)`, `Real Telegram gateway (v0.21.7)`, `Real Telegram gateway (v0.23.1)`, `Real Telegram gateway (main)`

Renaming one of these jobs, or changing the Hermes versions they pin, changes the check names. Maintainers update the required
checks in the same change.

## Rules

- Keep each change small and focused: one change per PR.
- Don't bump versions, create tags, or add CHANGELOG release headings. Maintainers cut releases.
- No build step or committed bundles: this plugin ships Python source and its manifest.
- Unit tests must not access the network or billed model APIs; preserve the network-denial fixture.
- Keep consent, destination/session isolation, quiet hours, caps and silent-response boundaries intact.
- Diagnose installed deployments with status and logs only; do not fire a wake, send a DM or change consent to reproduce a bug.
- Never commit secrets, tokens, real user data, or machine-specific paths.
- Tests use a temporary `HERMES_HOME`, never a real one.

## Release and escalation
- Lane by changed path, not by plugin (below, "the orchestrating owner" and "the human owner" are this project's owners). The careful lane covers changes to storage or migrations, deletion or compaction, locking, provider routing, streaming or retries, permissions, shared hooks, dependencies or update machinery, a host-wide install (a shared plugin root or processes, including one that can't be isolated), and anything with uncertain recovery. On top of the fast-lane proof floor below, it adds mechanism tests (old-state upgrade, restart or interruption, concurrency, recovery after new writes), a consistent backup, a recovery rehearsed on a copy that keeps post-upgrade data, and a canary on one already-authorized live install, running real operations with stated pass/fail thresholds. For an install that can't be isolated, the orchestrating owner decides the blast radius. Everything else takes the fast lane.
- Fast-lane proof floor for the smallest proven fix: an independent non-author review and green CI at the exact final SHA, targeted regression tests, and a smoke test of install, startup and the affected workflow against the current release. Reaching that proof should take hours, not days. Don't wait for GA, a release date, unrelated work or a benchmark campaign. Once it passes, release on the cadence below. A remaining P0 doesn't block the other fixes if it is contained (verified workaround, no new exposure, known-issue note, named fix owner, next-update date); otherwise the orchestrating owner decides the trade-off. In either lane, before shipping, name a watcher whose wake route has been seen to fire and a ready rollback or disable command, then watch for 72 h or one update cycle. On corruption, disclosure, an unauthorized action, a critical-path failure or a breached threshold: stop, contain, roll back or disable, tell the orchestrating owner and keep the evidence. A published release can't be recalled from auto-updating users; when that matters, ship a fast corrective release with clear user instructions.
- Tests follow the diff: if the plugin has slow or benchmark suites, keep a short map from each one to the code and behavior it measures. A release runs only the suites whose inputs changed and records each skip in the release note ("suite X skipped: nothing it measures changed"). The full suite runs on its own nightly or weekly schedule, off the release path. A regression it finds gets its own fix and never gates an unrelated hotfix.
- Severity and cadence (binding release rules for maintainers; not a support promise to users). P0 is data loss, disclosure or an unauthorized action (even for one user), or the default critical workflow broken with no safe workaround: contain a suspected P0 first and tell the orchestrating owner at once. P1 is a supported workflow blocked or materially degraded for some users, without P0 harm. An owner's priority call goes into your plan and changes the order of work, never the severity. P0 fixes ship immediately; ready P1 fixes go out in at most one bundled release a day; features ride a regular train (weekly by default, set per plugin by the orchestrating owner). Never hold a ready fix past the next daily bundle, and never cut a release that carries nothing a user needs.
- Anti-stall clocks, which never reset. A P0 with no safe path within 2 h of triage goes to the orchestrating owner. For a fix candidate with no decisive test, review or authorization result within 4 h, send the orchestrating owner the blocker, the remaining evidence, the fix owner and the next result time; a longer test needs the orchestrating owner's explicit OK, once, and a missed checkpoint re-escalates. A ready fix held 4 h needs a named blocker; send it to the orchestrating owner with a deadline for their decision. Running out of time never waives proof; it forces a decision.
- Who decides: maintainers make reversible technical choices within the permissions they already have, then report. The orchestrating owner decides cross-plugin priorities, user-impact trade-offs, unresolved high risks, blast radius and longer tests; an escalation carries the evidence, the options, a recommendation, a deadline and a safe fallback. The human owner decides new access or credentials, money, social posts in their name and destructive actions. Code posts (pull requests, issues, comments and reviews): on our own repositories the maintainer's own privacy scan is enough for the privacy check, unless the post quotes a user's message or names a person; on third-party repositories, and for any post that quotes a user's message or names a person, the orchestrating owner's privacy-checked OK comes first, and only that OK can allow a specific name or quote. Privacy means no client or personal data, names, hosts, paths, internal IDs or message text; public issue and pull request numbers are fine. This standard grants no new authority to publish, install or spend: live installs happen only in the guarded install window (backup first, then one restart), run by whoever the orchestrating owner has assigned to it, never on your own. Silence allows only a default that is already delegated and passes the gates above; it never grants new authority or accepts new risk: on silence, hold or contain, and escalate again.

Maintainer agents: the full internal standard is in eng-standards policies/release-and-escalation.md.

## Release closeout (maintainers, after every release)

After the release is published:

1. Close every issue the release fully served, with a comment naming the version. Don't close an umbrella or parent issue
   whose own acceptance isn't met: split what is left into its own issue, or narrow it.
2. Tick the boxes it completed in VISION.md, the roadmap tracker (#139) and issue checklists. Close a milestone once all its
   issues are closed, and move work that is parked or waits on Hermes to the Backlog or Research / blocked milestones.
3. Update every line in the README, VISION.md, #139 and milestone descriptions that names the latest release or what ships
   next.
4. Add the release's row to #139, and post one closeout line as a comment on the release pull request: issues closed, boxes
   ticked, docs updated (or "none").

## Filing issues

- Search first: `gh issue list -R electricsheephq/heartbeat2 --state all --search "<key words>"`.
- Use the bug form headings: What happened, What you expected, Steps to reproduce, Plugin version, Hermes version, Where it happened, Operating system, Diagnostics, Relevant log lines.
- Maintainers filing from the CLI always pass labels: `--label bug,triage-pending` (or `triaged` once triaged), plus `agent-drafted` when an agent wrote it.

## Security

Report vulnerabilities privately: https://github.com/electricsheephq/heartbeat2/security/advisories/new
