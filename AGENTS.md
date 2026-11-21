# AGENTS.md

Guidance for AI coding agents, and for people, working in this repository.

## What this is

HeartBeat 2.0 is a standalone Hermes Agent plugin for bounded wakes and deterministic watchers. It schedules a turn in the conversation that armed it, using that conversation's model and tools. It delivers into gateway DMs (group chats only when `dm_only` is off) and Desktop/TUI local sessions on Hermes hosts with local plugin injection (0.23+). Retired app sessions HOLD wakes until reattached; CLI, API server and cron arming are refused.

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
Read [the ElectricSheep release and escalation standard](https://github.com/electricsheephq/eng-standards/blob/main/policies/release-and-escalation.md) and record its approved revision; unresolved/unapproved policy does not expand existing grants.
Ship the smallest proven fix as soon as gates pass; preserve non-author review, final-candidate green CI, regression/smoke proof and Plugin Manager guarded installation.
Name storage/migration, routing/permission, shared-hook, compatibility and recovery risks; select decisive tests, and escalate holds on the policy's deadlines rather than waiting for GA.
Require representative authorized canary use, state-safe recovery, an accepted durable watcher and accurate known-issue/update guidance; publication is not user recovery.
Maintainers decide within grants; Eva decides unresolved risk/user-impact/priority trade-offs; silence authorizes only an already-delegated safe default, never a new action or waiver.
Andrew owns new access/credential grants, money, public-as-Andrew and destructive actions; record priority overrides in the existing plan without erasing safety gates.

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
