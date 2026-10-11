# Security and data handling

Disclosure — wakes and watchers run full turns on the conversation's own model,
billed like any message; command probes run allowlisted read-only commands without
an approval prompt; url probes make GET requests you configured; state stays in the
profile's plugin-data; no telemetry.

The argv guard is not a sandbox: allowlisted programs and local configuration
must be trusted and read-only. Model wake turns retain the conversation's tools
and permissions. GET requests include the configured URL, including its query;
URL userinfo, cookies, Authorization headers and ambient proxies are refused or
unused by the probe. Existing git/gh credentials and remote configuration can be
used by command probes. The plugin adds no telemetry; Hermes, model providers
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
  with an EOF baseline and truncation/rotation handling. Avoid sensitive files.
- Process probes lazily use psutil and compare PID create-time to avoid reuse.
- URL probes allow only HTTP/HTTPS (including redirects), reject URL userinfo,
  disable ambient proxies, use a 10 s total monotonic deadline (remaining socket
  timeout on each read and redirect), and read at most 256 KB plus one
  overflow-detection byte. Local/private HTTP endpoints are allowed; this is
  not an SSRF isolation boundary.
- Command prefixes match full argv tokens against `probe_commands`. Empty
  allowlists disable them. No shell, no stdin or controlling tty, 30-second total deadline;
  profile home is the default cwd unless a cwd was recorded. The conservative
  guard rejects shell operators, output/code/config/helper/browser flags and
  common mutating verbs. Pager variables are set to `cat`; optional git locks
  are disabled. This cannot detect hidden effects in binaries, wrappers, Git
  helpers/hooks, executable lookup, inherited environment or remote services.
  Configure only trusted observers. Git/GH/credential-manager prompts are disabled.
  Capture is bounded while reading to 256 KB plus one overflow-detection byte.
  Timeout or overflow kills the entire process group, including descendants that
  remain in it. Deliberately detached processes remain outside that guarantee.

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
