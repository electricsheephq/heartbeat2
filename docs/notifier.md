# Closed-window notifier handshake (v1)

The owner sets `notify_via: birdie:assistant` in HeartBeat's plugin settings and restarts Hermes. The flat setting accepts a plugin id matching `[a-z0-9_-]{1,64}` and a target matching `[A-Za-z0-9._-]{1,64}`, separated by one colon. Blank disables it. Invalid settings log once and act as blank. The agent cannot set it.

HeartBeat subscribes to `<plugin>:heartbeat2_notifier` and emits `notifier_wanted` with `{version: 1}` at registration. Hermes namespaces that request as `heartbeat2:notifier_wanted`. Notifiers offer once at registration and again when they receive that request, so either load order works. Hosts without callable `ctx.emit` and `ctx.subscribe` have no notifier.

An offer contains:

```python
{"version": 1, "registered_at": time.monotonic_ns(), "send": send}
```

`version` must be integer 1, `registered_at` a positive integer, and `send` callable. The function's defining module must still be live. The newest stamp wins. On unload, withdraw the same stamp:

```python
{"version": 1, "registered_at": registered_at, "withdrawn": True}
```

A withdrawal retires the current offer if it is not newer. Its stamp remains withdrawn forever, including after forced reloads with surviving subscribers. Older announcements cannot replace newer offers or withdrawals. This registry is separate from the [voice bridge](voice-bridge.md).

`send(target, text)` returns `{ok: bool, message_id, status: "queued", error}` and should never raise; HeartBeat wraps it anyway. `ok: true` means the bus accepted the message, not that a recipient read it. Birdie bounds each network step to two seconds and applies its loop guard (12 messages to one agent per ten minutes). A timeout or Birdie's “can't reach birdie” error shows **may repeat**: the notifier couldn't confirm the send, and the message may already have gone, so a retry can duplicate it. Before the sending profile's first turn Birdie has no token: this shows **notifier not ready**.

HeartBeat uses one daemon worker and at most one in-flight notice per profile. A Desktop/TUI chat gets one successful notice per closed-window episode after its oldest held wake waits ten minutes. Only the count, profile, surface and waiting time leave the chat; no titles, notes, scratch or payloads do. A wake still waits for the window to reopen. Messaging chats get no notices.

The chat's quiet-hours override (or profile quiet hours when there is no override) and daily message cap defer notices; unanswered mute does not. Off or paused chats send none, and paused wakes or occurrences now waiting for another reason do not count. A queued notice counts toward the daily cap, but not unanswered messages; `/pulse ledger` charges it to the first held wake's row. The receipt is a text-free `notify` event. Failed sends retry at five-minute intervals for three attempts, then hourly, stopping 24 hours after the first attempt. An accepted injection or no remaining held wakes ends the chat episode. Status and the desk show connection and episode state only when configured (status also shows invalid settings); retrying and failed notices show the last error clipped to 80 characters, never the notice text. A queued notice is never retried within that episode.

Unload signals the worker without waiting for the store lock and joins it for at most five seconds. A still-running send keeps its store open; it cannot write a result after the stop signal. Its persisted attempt and retry deadline survive a restart, but the message may already have gone, so a later retry can duplicate it.
