# Optional live API voice bridge

HeartBeat can arm and deliver wakes in a connected API-backed voice conversation when
a trusted voice plugin installs a bridge on the **same profile's** plugin manager:
`ctx._manager._heartbeat2_voice_hub`. Without a complete bridge, API sessions retain
the existing `non_gateway` refusal. This is an optional Python integration seam, not
an HTTP API or a setting an agent can enable.

This interface comes from a local hermes-converse integration. The complementary
bridge is not assumed to ship in upstream hermes-converse. Installing HeartBeat alone
does not add it; no import of that plugin is required.

## Transport interface

All four methods must be callable. HeartBeat can call them from worker/poller threads;
the bridge must synchronize connection state and marshal work to its owning event loop.

| Method | Contract |
| --- | --- |
| `voice_target(session_id)` | Return the canonical, nonempty conversation ID for a currently authenticated, connected voice session with a wake handler, otherwise `None`. Resolve compression/run aliases to the same canonical ID; that ID must resolve to itself. |
| `owner_message(conversation_id, text)` | Return true only for an actual owner input currently being processed in that conversation. Do not infer ownership from arbitrary API text or model-provided claims. |
| `wake_ready(conversation_id)` | Return true only when the same device is connected and idle: no owner turn, capture, speech/output backlog, or pending wake. |
| `inject_wake(conversation_id, text)` | Atomically recheck readiness and reserve at most one wake, then schedule the supplied text as a normal turn in that conversation. Return true only if accepted; false or an exception uses HeartBeat's existing injection retry path. |

HeartBeat reads the session-scoped `HERMES_SESSION_PLATFORM` and session ID, resolves
them through the bridge and persists `heartbeat2:voice:<canonical ID>` as the schedule
identity. Run/approval IDs may rotate without changing the destination. Ordinary API,
CLI, Desktop and cron sources cannot select a destination through tool arguments.

## Delivery and lifecycle

The bridge must use the originating profile, conversation history, model and tools.
Wake execution must call the normal Hermes plugin hooks (`pre_llm_call`, usage,
`transform_llm_output`, session end) so verdicts and occurrence accounting settle.
Deliver only final **transformed** output; `[SILENT]` or empty output must never become
speech or a notification. Do not deliver raw token streams or reasoning. Avoid duplicate
speech for voice-owned turns.

The bridge owns authenticated connection attachment, aliasing and teardown. On a
busy/offline device HeartBeat defers within its normal expiry/catch-up bounds; there is
no broadcast or offline outbox. Recheck the connection and handler after crossing a
thread/event-loop boundary. If an accepted turn never starts, the existing acknowledgement
retry limit applies. No schedule may be transferred to a different device or profile.

`plugins.entries.heartbeat2.allow_gateway_injection: true` is still required. HeartBeat's
quiet hours, caps, source policy, owner-evidence checks, expiry and silent verdicts remain
in force. Existing exceptions for owner reminders and silent recurring work are unchanged.
Restart the gateway after installing or updating the plugins.

## Verification

`python -m pytest -q tests/test_voice_bridge.py` exercises the real scheduler/store and
intake hooks against a deterministic transport double, with the normal network-denial
fixture. It covers identity rotation, owner evidence, notify/silent settlement, busy and
offline deferral, consent, budgets, expiry, mutation isolation and injection refusal.
It does not prove an external bridge's device authentication, event-loop races, audio
playback or actual client delivery; those need tests in the voice plugin itself.
