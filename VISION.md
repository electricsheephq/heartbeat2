# HeartBeat 2.0 vision

## What it is for

An agent that only acts when someone writes to it is a tool. HeartBeat 2.0 gives a Hermes agent time of its own between
messages: roles it owns and keeps working on, its own time to look over what it is responsible for, and follow-ups it
promised. Most of that work is silent. The agent speaks when it has something worth saying.

## Principles

- **Guardrails limit messages, spend and timing, never initiative.** Caps, quiet hours, daily cost limits and wake spacing
  decide how often the agent may speak and what it may spend. They don't decide whether it may think, check or prepare.
- **The owner stays in charge.** Every wake belongs to one chat and one owner. The owner can see it (`/pulse status`, the desk),
  pause it, cancel it, or stop all of it with `/pulse off` or "stop checking in". Only the owner raises a limit.
- **Truthful status.** A wake that couldn't run says why, and one that is waiting says what it is waiting for. Status
  reports what HeartBeat knows, and says when it can't know (Hermes doesn't yet tell plugins which messages were sent).
- **Nothing is lost silently.** A wake that can't reach its chat is held or ends with a reason the owner can see. It never
  disappears.
- **Thin plugin, real seams.** HeartBeat uses Hermes's own turns, models and tools, and asks Hermes for missing seams
  upstream instead of working around them.

## Where wakes run

| Surface | Status | Notes |
|---|---|---|
| Messaging DMs (Telegram, Discord, Slack, Signal, WhatsApp and the other gateway platforms) | Supported | The default target. |
| Messaging groups and topics | Supported when `dm_only` is off | Own time needs `/pulse owntime on` in each group. |
| Hermes Desktop app sessions | Supported on Hermes 0.23+ | Runs while the app window is open; a wake waits (held) while it is closed. A silent turn's text still shows in the window until Hermes filters it (upstream NousResearch/hermes-agent#156465). Nothing is pushed to a phone. |
| Hermes TUI sessions | Supported on Hermes 0.23+ | Same rules as Desktop. |
| Hermes CLI | Refused | The CLI doesn't show plugin turns. |
| API-server sessions | Refused | Hermes drops plugin turns there today (upstream #156466). Planned: run silent, with an optional notify target. |

## Next

- Run app-session wakes with the window closed, once Hermes can revive a closed local chat for a plugin turn
  (upstream NousResearch/hermes-agent#156894).
- An optional notify fallback (`notify_via`) so a long-held wake reaches the owner through another channel.
- One cache clock per chat: defer keep-warm to Hermes's own cache warmer once it can be read.

## Not goals

- Talking more. Initiative is measured by useful work done between messages, not by message count.
- A second scheduler, approval layer or message transport beside Hermes's own.
