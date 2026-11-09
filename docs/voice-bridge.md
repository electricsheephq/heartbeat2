# Profile-bound voice bridge registration (v1)

A registered bridge lets HeartBeat arm and deliver wakes in a connected API-backed
voice conversation. Without a registered bridge, or when the bridge does not vouch
for the session, API-server sessions keep their `non_gateway` refusal; CLI and cron
arming are refused as before. HeartBeat reaches the bridge only through
`heartbeat2.voice_bridge.voice_bridge_for(engine)` (also `Runtime.voice_bridge()`),
which returns an immutable view of the four callables, or `None`. Lookup never
raises and never emits events.

## Owner configuration and boundary

Set `plugins.entries.heartbeat2.settings.voice_bridge` to the manifest plugin id
of the trusted bridge. The **Voice bridge plugin** setting is a string, blank by
default (no bridge). Whitespace is trimmed; ids must match
`^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}(/[A-Za-z0-9][A-Za-z0-9_.-]{0,63})?$`.
One optional category is allowed, for example `cat/bridge`. Invalid values act as blank and produce a
settings warning. The value is read at registration; changing it takes effect at
the next gateway restart. Status shows `voice bridge: <id> (connected)` or
`voice bridge: <id> (not connected)` only when configured. Connected means a
transport is registered, not that a device is ready.

This setting records owner intent, not a security boundary: the agent's own tools
can write profile configuration. The bus forces the emitter's plugin namespace,
and each profile has its own manager. Only the configured emitter on that same
manager can supply this registration. Trust that plugin's code and transport.
Contexts without the event bus fall back to no bridge.

## Handshake

When creating a runtime with a nonblank bridge id, heartbeat2 subscribes to
`<voice_bridge>:heartbeat2_voice_bridge`, then emits `voice_bridge_wanted` with
`{"version": 1}`. It emits under its own plugin id: normally
`heartbeat2:voice_bridge_wanted`, or `cat/heartbeat2:voice_bridge_wanted` when
installed under that category. It emits no
other handshake events, including on lookup misses: the bus has a pending budget.

The bridge's `register(ctx)` takes one `time.monotonic_ns()` stamp and creates
four **plain functions or closures**. Subscribe to
`<heartbeat2's id>:voice_bridge_wanted` and answer each event by emitting
`heartbeat2_voice_bridge` with the payload below; also emit it once at register
time. Both load orders then work. The bus delivers asynchronously on a worker
thread using `callback(**copy.deepcopy(payload))`. Plain functions retain their
identity; bound methods copy their owning object and can fail (for example on a
lock), causing skipped delivery. Do not send bound methods. A bridge MUST stop
answering wanted events and never announce again once its `on_unload` ran. Set
an unloaded flag first in that callback, before emitting the withdrawal, and
check it on every announcement path.

```python
def register(ctx):
    import time
    unloaded = False
    registered_at = time.monotonic_ns()
    payload = {
        "version": 1,
        "registered_at": registered_at,
        "voice_target": voice_target,
        "owner_message": owner_message,
        "wake_ready": wake_ready,
        "inject_wake": inject_wake,
    }

    def announce(**_):
        if not unloaded:
            ctx.emit("heartbeat2_voice_bridge", payload)

    def on_unload():
        nonlocal unloaded
        unloaded = True
        ctx.emit("heartbeat2_voice_bridge", {
            "version": 1, "registered_at": registered_at, "withdrawn": True,
        })

    # Use "cat/heartbeat2:voice_bridge_wanted" for that heartbeat2 installation.
    ctx.subscribe("heartbeat2:voice_bridge_wanted", announce)
    ctx.on_unload(on_unload)
    announce()
```

Version must be exactly integer `1` (not bool), and the stamp a positive integer
(not bool). All four fields must be callable unless `withdrawn is True`.
Malformed payloads are ignored with one warning per distinct reason. Newest
stamp wins; equal stamps replace the view. Withdrawals permanently retire that
stamp for the runtime. A withdrawal at or above the current stamp also clears the current
registration and raises the stamp floor, so an older announcement stays out even if this
runtime never saw the withdrawn copy's announcement. Older announcements cannot
replace newer ones, even after withdrawal. Forced reloads leave old subscriptions
alive in Hermes, so the stamp, unload flag and withdrawal are required. HeartBeat
also ignores an announcement if any transport Python function's globals no
longer belong to the live module in `sys.modules` (including a missing module).
Lookup clears such a registration too. Callable objects without function module
metadata are not checked. Transport functions must be defined in an imported module (one in
`sys.modules`); a function built by `exec` into a bare dict, or in a module that was never imported, is treated as not live. Closed runtimes ignore callbacks. Registry state is
protected by a lock; liveness reads and transport calls never run under it.

A bridge that ignores the unload rule can stay connected until the gateway restarts:
when disabled, its old module may remain in `sys.modules`, so the liveness check
cannot detect it. In all cases, disabling a bridge takes full effect at the next gateway restart.

## Transport interface

All four methods must be callable. HeartBeat can call them from worker/poller threads;
the bridge must synchronize connection state and marshal work to its owning event loop.

| Method | Contract |
| --- | --- |
| `voice_target(session_id)` | Return the canonical, nonempty conversation ID for a currently authenticated, connected voice session with a wake handler, otherwise `None`. Resolve compression/run aliases to the same canonical ID; that ID must resolve to itself. |
| `owner_message(conversation_id, text)` | Return true only for an actual owner input currently being processed in that conversation. Do not infer ownership from arbitrary API text or model-provided claims. |
| `wake_ready(conversation_id)` | Return true only when the same device is connected and idle: no owner turn, capture, speech/output backlog, or pending wake. |
| `inject_wake(conversation_id, text)` | Atomically recheck readiness and reserve at most one wake, then schedule the supplied text as a normal turn in that conversation. Return `True` only if accepted. Return `False` when the device is busy or gone and nothing was scheduled: HeartBeat then waits and tries again, like `wake_ready` returning false. Raise only when the transport itself failed: that uses HeartBeat's injection retry path, and five failures end the wake. |

HeartBeat reads the session-scoped `HERMES_SESSION_PLATFORM` (`api_server`) and
session ID, resolves them through `voice_target` and persists
`heartbeat2:voice:<canonical ID>` as the schedule identity. Run/approval IDs may
rotate without changing the destination: any ID the bridge resolves to the same
canonical ID is the same conversation, with or without a Hermes compression
lineage between them, and armed wakes stay with it. Ordinary API, CLI, Desktop and
cron sources cannot select a destination through tool arguments.

HeartBeat contains bridge errors. A `voice_target`, `owner_message` or `wake_ready`
that raises reads as no session, no owner credit and not ready; the turn's own
bookkeeping carries on. HeartBeat asks `voice_target`, `owner_message` and
`wake_ready` outside its store lock, so a slow answer delays only the call that
asked. `inject_wake` is called while the scheduler holds that lock, exactly as the
gateway injector is: it must schedule the turn and return, never wait for it. Only
a literal `True` from `owner_message` grants owner credit.

## Owner controls by voice

`/pulse` works in a voice conversation the same way the tool does: when the
command handler runs in a session the bridge resolves, it acts on that
conversation's `heartbeat2:voice:<canonical ID>` identity. To give the owner
`/pulse off`, `/pulse on`, `/pulse quiet ...`, `/pulse ok <id>`, `/pulse list` and
the rest, the bridge runs Hermes's registered `pulse` plugin command (for example
through `hermes_cli.plugins.get_plugin_command_handler("pulse")`) with the
conversation's session context bound (`HERMES_SESSION_PLATFORM=api_server` and the
session ID), and speaks or shows the returned text. It may pass through a typed or
transcribed `/pulse ...`, or map its own phrases or buttons ("stop checking in")
to a command. The bridge MUST run a command only for a genuine owner input on the
authenticated device, never for model output or wake text: a command is the
owner's consent. In a session the bridge does not resolve, `/pulse` keeps the
API-server refusal and changes nothing.

Keep-warm, own time and the built-in check-in are not offered in voice
conversations; `/pulse warm` and `/pulse owntime` say so there.

## Delivery and lifecycle

The bridge must use the originating
profile, conversation history, model and tools. Wake execution must call the
normal Hermes plugin hooks (`pre_llm_call`, usage, `transform_llm_output`, session
end) so verdicts and occurrence accounting settle. Deliver only final
**transformed** output; `[SILENT]` or empty output must never become speech or a
notification. Do not deliver raw token streams or reasoning. Avoid duplicate
speech for voice-owned turns.

The bridge owns authenticated connection attachment, aliasing and teardown. On a
busy/offline device HeartBeat defers within its normal expiry/catch-up bounds;
there is no broadcast or offline outbox. That covers `wake_ready` returning false,
`inject_wake` returning false (retried after 30 seconds, never counted as a failed
injection) and a bridge that is withdrawn or disconnected after arming
(`voice_disconnected`). Recheck the connection and handler after
crossing a thread/event-loop boundary. If an accepted turn never starts, the
existing acknowledgement retry limit applies. No schedule may be transferred to
a different device or profile.

`plugins.entries.heartbeat2.allow_gateway_injection: true` is still required.
Quiet hours, caps, source policy, owner-evidence checks, expiry and silent
verdicts remain in force. Existing exceptions for owner reminders and silent
recurring work are unchanged. Restart the gateway after installing or updating
the plugins. Installing HeartBeat alone supplies no external bridge or audio
delivery; no import of a voice plugin is required.

## Verification

`tests/test_v05_voice_bridge.py` covers the registry and deterministic event bus.
`tests/integration/test_voice_bridge_real.py` covers Hermes PluginManager load
orders, profile isolation and forced reload. `tests/test_voice_bridge.py` runs the
real scheduler, store, intake hooks and `/pulse` commands against a device double
registered through the registry, under the network-denial fixture: identity and ID
rotation, owner evidence, a raising bridge, notify/silent settlement, busy, offline
and refused-injection deferral, consent, budgets, expiry, voice commands and the
`non_gateway` fallback. `tests/integration/test_voice_wake_real.py` loads a small
reference bridge written to this page through the real Hermes loader and event bus,
then arms, holds, delivers and settles one wake and runs `/pulse off` and `/pulse on`
through Hermes's hook, tool and command registries. None of these proves a voice
plugin's device authentication, event-loop races, audio playback or actual client
delivery, and none drives a real Hermes API-server model turn; those need tests in
the voice plugin.

For closed-window notices through another plugin, see the separate [notifier handshake](notifier.md).
