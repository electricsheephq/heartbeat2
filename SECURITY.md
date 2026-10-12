# Security and data handling

Disclosure — wakes and watchers run full turns on the conversation's own model,
billed like any message; command probes run allowlisted read-only commands without
an approval prompt; url probes make GET requests you configured; state stays in the
profile's plugin-data; no telemetry.

The argv guard is not a sandbox: allowlisted programs and local configuration
must be trusted and read-only. Model wake turns retain the conversation's tools
and permissions. GET requests include the configured URL, including its query;
URL userinfo, cookies, Authorization headers and ambient proxies are refused or
unused by the probe. Command probes use Hermes' sanitizer on an allowlisted
environment, without provider, bot or GitHub tokens; gh uses its own hosts.yml
or keyring. URL probes always block cloud metadata and block private/local
addresses unless url_allow_private is enabled, checking each hop at connect
time. File probes honor Hermes read blocks at arm/update and every check,
including rotation. Helpers run in the watcher's profile scope. Existing git/gh
credentials and remote configuration can be used by command probes.
gh -R HOST/... and git ls-remote <url> can reach any host the machine can reach.
The plugin adds no telemetry; Hermes, model providers
and configured endpoints retain their own data handling.

These same disclosure and exception paragraphs appear in README and the manifest.

## Boundaries

- The immutable session key comes from the invoking gateway, never from tool
  parameters. `/pulse` reads and changes only that session's items. Unknown IDs
  in another session do not reveal the note or permit mutation.
- v0.1 is intended for messaging-gateway DMs. With default `dm_only=true`,
  groups and unknown chat types are refused. Turning `dm_only=false` permits
  recognized gateway groups; this is an opt-in exception, not tested live here.
  CLI, TUI, Desktop, API server and cron arming are refused.
- Core consent is `plugins.entries.heartbeat2.allow_gateway_injection: true`,
  outside `settings`. Agent tools cannot write settings or specify destinations.
- File probes may read configured local files; relative paths resolve against
  the pinned profile home. Regex matching inspects appended complete lines only,
  with an EOF baseline and truncation/rotation handling. Hermes'
  `agent.file_safety.get_read_block_error` checks the absolute path at arm/update,
  before every open and again after rotation. Import/check failures refuse the
  watcher. The opened file's device/inode must match its pre-open stat.
- Process probes lazily use psutil and compare PID create-time to avoid reuse.
- URL probes allow only HTTP/HTTPS (including redirects), reject URL userinfo,
  disable ambient proxies, use a 10 s total monotonic deadline (remaining socket
  timeout on each read and redirect), and read at most 256 KB plus one
  overflow-detection byte. Hermes' `tools.url_safety` enforces the cloud-metadata
  floor always. Private/local targets are refused unless `url_allow_private=true`
  (default false), independent of Hermes' global private-URL toggle. Initial and
  redirected URLs are checked before connecting; every DNS answer is classified
  at connect time and only a vetted IP is dialed. HTTPS still verifies the
  original hostname. Arm/update checks do no DNS. Missing helpers refuse URL
  watchers; these checks do not provide an OS network isolation boundary.
- Command prefixes match full argv tokens against `probe_commands`. Empty
  allowlists disable them. No shell, no stdin or controlling tty, 30-second total deadline;
  profile home is the default cwd unless a cwd was recorded. The conservative
  guard rejects shell operators, output/code/config/helper/browser flags and
  common mutating verbs, including gh `--jq`/`-q` and `--template`/`-t`.
  Pager variables are set to `cat`; optional git locks
  are disabled. This cannot detect hidden effects in binaries, wrappers, Git
  helpers/hooks, executable lookup or remote services.
  Configure only trusted observers. Git/GH/credential-manager prompts are disabled.
  Capture is bounded while reading to 256 KB plus one overflow-detection byte.
  Timeout or overflow kills the entire process group, including descendants that
  remain in it. Deliberately detached processes remain outside that guarantee.
- Command environments start with only PATH, HOME, USER, LOGNAME, LANG, LC_*,
  TMPDIR, SSH_AUTH_SOCK, XDG_CONFIG_HOME, GH_CONFIG_DIR and GH_HOST. Hermes'
  `tools.environments.local.hermes_subprocess_env(inherit_credentials=False,
  base_env=allowlisted)` sanitizes that base; import/call failures use the same
  allowlisted base. Pager and no-prompt variables are applied last. GH_TOKEN and
  GITHUB_TOKEN are never passed; gh authenticates through its own hosts.yml or
  keyring. No token passthrough setting is offered.
- Pool probes enter the wake's pinned Hermes home scope; the URL worker copies
  that context. Child HOME handling, adapter secrets, URL configuration and file
  blocks therefore use the wake's profile rather than the launch profile.
- The default allowlist still permits `gh -R HOST/...` and `git ls-remote <url>`
  to dial any host the machine can reach. This exception is unchanged in v0.1.1.

- Command JSON must contain exactly `fire`, `message`, `state`, with boolean
  fire, string message and serializable JSON state. Stored wrapper state is
  ≤16 KB; it commits only on a successful fire. Change/JSON nonzero exits and
  I/O/parse/timeout errors are errors, never changes. Success/failure modes
  intentionally interpret the configured exit condition as a signal.
- Fingerprints are hashes, but notes, scratch, event payloads, summaries and
  JSON observer state are stored as plaintext in SQLite WAL under
  `<profile>/plugin-data/heartbeat2/pulse.db`. Hermes filesystem permissions,
  backups and host access govern confidentiality. There is no encryption or
  independent OS permission boundary supplied by this plugin.

## Model and delivery

A wake is a normal model turn in that conversation, billed like one; its size
is your conversation's context size. `/pulse ledger` shows ≈tokens per wake,
and `/pulse status` shows "Today … ≈tokens" to track the running cost.
These are reported token totals, not a monetary bill.

External payloads are fenced as untrusted data and bounded; this reduces prompt
confusion, but cannot guarantee that a full model turn follows every instruction.
A structured `pulse respond` with notify=false yields exactly `[SILENT]`; missing
respond also defaults to silence. Hermes response trailers can break the final
exact-marker match after transformation. Never treat fake-injector acceptance
as proof of delivery. Enabled/settings/source gates, quiet hours, caps, busy
checks, estop, unanswered mute and the no-op governor bound the engine's work.
A deadline reports that nothing happened; it does not assert success.

## Reporting

Report a suspected vulnerability privately to the repository maintainers through
GitHub's private vulnerability reporting when available. If unavailable, request
an alternate private channel without posting exploit details or credentials in
public issues. Include a minimal sanitized reproduction, version and affected
boundary. This unreleased plugin has no promised security response SLA.

A wake turn that fails at the provider shows Hermes's own failure notice; HeartBeat backs off until the provider recovers. Provider-error hooks retain only a bounded class, never the request, response or error message.
