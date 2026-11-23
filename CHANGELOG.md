# Changelog

## 0.4.33 — 2026-11-23

- **The keep-warm notice comes only with a message you send.** In 0.4.32 a reaction could let the next turn the agent started on its own pick up the "keep-warm can't ping" line, and role turns saw it on their desk. Now the agent gets it only on a message you send, and the desk no longer lists it; `/pulse warm`, `/pulse status` and `/pulse list` still say why no ping is due. After a model switch the new model's route gets its own two hours, and a line about the old route that you hadn't seen yet is dropped. (#234)
- **Keep-warm counts on the hour only after a Claude route stays warm past 15 minutes**, not 5. A warm gap under 15 minutes can be timing skew on a 5-minute cache (a retry refreshes it mid-gap), and pinging on the hour there would pay for cold pings. The reported case, a 22-minute warm gap after a model switch, still counts as the hour. (#234)

## 0.4.32 — 2026-11-23

- **Keep-warm keeps working after a model switch on Claude.** A new model is a new cache route, and keep-warm took the longest gap it had seen that route's cache stay warm as the cache's lifetime. After a switch, a 22-minute warm gap read as a 22-minute cache, below the 30 minutes keep-warm needs, so it stopped pinging and two idle gaps paid full cache rewrites. Claude caches last 5 minutes or 1 hour, so a Claude route seen warm past 5 minutes now counts as the hour; two cold pings in a row still stop it if that's ever wrong. A 5-minute cache still gets no pings. Other models' routes are unchanged. (#234)
- **A keep-warm that can't ping tells you.** When keep-warm has been unable to ping for two hours because the cache lifetime is unknown or short, caching is off, or two pings in a row found the cache cold, the agent tells you once on your next message (not while HeartBeat is off in the chat), and `/pulse desk` shows the reason from the start, while it lasts. A new reason tells you again after its own two hours. Your rest or a small context is not a reason in itself, but resting time counts toward the two hours. On Claude a cache under 5 minutes now reads "5m or less". (#234)

## 0.4.31 — 2026-11-21

- **Your reminders are no longer dropped silently after downtime.** If HeartBeat was off or the gateway was down when one of your own one-time reminders came due, a reminder more than `catch_up_hours` (6 by default) late was marked missed. The only trace was a line the agent saw on your next message, which didn't say which reminder it was. Your reminders, from `/pulse add` or asked for in chat, now get their turn after downtime, even when their expiry passed during it. More than 15 minutes and up to 24 hours late (or `catch_up_hours`, if longer), the reminder fires and says it is late. Later than that, the turn tells you in one line which reminder was missed and when it was due. Past the late window the turn only reports the miss and doesn't do the reminder's work. Downtime is time the gateway was down, HeartBeat was switched off or Hermes' emergency stop was on, including after the reminder was queued; time held by quiet hours doesn't count. The agent's own one-time wakes still end as missed after `catch_up_hours`. Their notice now names the wake and its due time, and `/pulse list` says it was too late after downtime. (#230)

## 0.4.30 — 2026-11-20

- **One cold miss no longer turns keep-warm off for good on Claude routes.** Keep-warm stopped for a route as soon as the cache once went cold after a shorter gap than it had stayed warm, and nothing turned it back on. On Claude routes that miss is a changed prompt (a compaction or a rebuilt system prompt), because their caches last 5 minutes or 1 hour, so it no longer counts, and the learned lifetime is capped at the hour. Two pings in a row whose first model call finds the cache cold stop pings on that route instead, until a reply on that route reuses the cache after more than 15 minutes or you run `/pulse warm on` again, so a cache that really got shorter costs at most two cold pings per chat and route. Routes that report their cache lifetime, and routes to other models, keep their old rules. (#228)
- **Status says why keep-warm has no ping due.** `/pulse status`, `/pulse list` and the agent's list used to say only "no ping due now". They now give the same reason `/pulse warm` does. (#228)

## 0.4.29 — 2026-11-19

- **File watchers no longer depend on `os.O_NONBLOCK`.** A file watcher opens the watched file with only the open flags the platform defines, and in binary mode where the platform has one, so a Python build without `os.O_NONBLOCK` no longer fails every check. Linux and macOS open files exactly as before. Native Windows is still not supported (the README lists macOS and Linux). Thanks to @zparallel5. (#115)

## 0.4.28 — 2026-11-17

- **A paused watcher's result speaks once the holds clear.** When quiet hours, the unanswered mute or the daily message cap silenced a held watcher result, its delivery failed, and you then paused and resumed the wake, the result still ran silently even with no message hold left. It now runs normally, unless a message hold still applies. (#217)
- **`/pulse status` next due skips sleeping work.** In a chat that is asleep, "next due" no longer shows the agent's wakes that won't run until you write. (#217)

## 0.4.27 — 2026-11-17

- **The agent can rest in a chat you've left.** A new setting, **Sleep the agent's wakes after days without your message** (`sleep_after_days`, off by default), stops the agent's own work in a chat once you haven't written there for that many days. Its one-time reminders are dropped, its recurring work, roles and own time skip their turns, and its watchers keep checking and hold one result for when you're back (a watcher that expires first drops it). Your own wakes, one-time reminders and watchers you asked for in chat, and the keep-warm, still run; recurring reminders the agent arms for you sleep too. Any message from you wakes the chat; a `/pulse` command doesn't. `/pulse status` says when a chat is asleep, and `/pulse list` marks the sleeping work. Unlike the unanswered mute, which runs held turns silently, sleeping work uses no model turns. (#62)
- **A silenced result speaks once the holds clear.** When quiet hours, the unanswered mute or the daily message cap silenced a watcher's turn, its delivery failed, and it then waited on another hold, it used to run silently later even after every hold had cleared. It now runs normally, unless a message hold still applies. A wake paused in between still ran it silently in this release; fixed in 0.4.28 (#217).
- **Watcher secrets, small follow-ups.** SECURITY.md now says a `probe_command_env` setting refused when it first loads stays blank, so nothing is granted. A code comment explains why granted names skip Hermes' environment sanitizer. (#108)

## 0.4.26 — 2026-11-17

- **A watcher command can use your API key, if you say so.** Command watchers run with a small, cleaned environment that carries no provider, bot or GitHub tokens, so a watcher script that needs your own API key (a flight-status check, say) couldn't work. A new setting, **Secrets for watcher commands** (`probe_command_env`), names the variables one allowed command gets: for example `{python} /srv/trip/check.py = FLIGHT_API_KEY`. Their values come from Hermes' own environment, such as your `.env`, and reach only that command. Only you can set it; the agent can't, and values are never logged or shown. Name the program by a bare command name or an absolute path, and a `{python}` script by absolute path. Names such as PATH, HOME, LD_*, PYTHON* and HERMES_* are refused (the full list is in SECURITY.md); name only the variables the command needs. The command's output still reaches your agent, and your agent chooses the arguments after the allowed command and the folder it runs in, so grant a secret only to a command that doesn't print it, can't be pointed at another host, and doesn't read config or code from that folder (git and npm do). With the setting blank, nothing changes. (#108)

## 0.4.25 — 2026-11-17

- **A watcher you ask for reaches you even when you haven't replied.** After two of the agent's messages go unanswered, HeartBeat holds its watchers' alerts until you reply. A watcher you asked for yourself, such as "tell me if my flight is delayed", was held too, so the alert could arrive after the flight. Now, when you ask for a watcher in your own message, the agent marks it as yours (`owner_asked`), and its alerts are no longer held by that mute. Quiet hours, the daily message and model-turn caps and the spending limit still apply to it, as to the agent's other work. An alert that quiet hours or the daily message cap held is passed on once that hold ends, even if you still haven't replied. Other held results keep waiting for your reply. The arm receipt says "You asked for this watcher: not muted; daily caps still apply." If the agent later edits it without you asking again, it goes back to the usual mute, and if the watcher is cancelled, a result it left waits for your reply. (#108)

## 0.4.24 — 2026-11-17

- **`/pulse test` gets competing wakes right.** When two wakes in one chat both had a turn waiting, the preview could say the other wake "takes the turn first" when it would not. That happens when the other wake's earlier turn goes unacknowledged three times: HeartBeat drops that turn, and if the drop pauses the wake, its next turn waits or is skipped. The previewed wake is then sent after all, and the preview now says so. It also no longer says the report-back wake HeartBeat arms to deliver held watcher reports will pause after a drop, because HeartBeat never pauses it. The preview still sends nothing and changes nothing. (#167)

## 0.4.23 — 2026-11-17

- **Python watchers survive Hermes updating its bundled Python.** A command watcher can name `{python}` as its program, and HeartBeat runs it with the Python running Hermes at each check. Allowlist the observer as `{python} /path/to/check.py` in `probe_commands` instead of the interpreter's full path, which changes when Hermes moves to a new bundled Python and then breaks the watcher until both are re-pinned. The script must be the next word in the entry: an entry with nothing or an option such as `-I` after `{python}` allows nothing. (#108)
- **A held keep-warm ping shows when it started waiting.** A keep-warm ping armed in an app chat before 0.4.17 that waits on a closed app window retries every minute, and `/pulse status` showed it as held since the last retry. It now keeps the time it started waiting. Notices are unchanged. (#209)

## 0.4.22 — 2026-11-17

- **Installing an update while Hermes is running no longer breaks wakes.** An install replaces HeartBeat's files on disk, but Hermes keeps running the code it already loaded until it restarts. HeartBeat loaded some of its files only when a wake first needed them, so a wake could pull a newer file into older running code, fail with an import error on every try, and end up paused. HeartBeat now loads all of its files when Hermes starts it, so the running version keeps working until the restart. `/pulse status` now says when a newer version is installed than the one running: "restart Hermes to finish the upgrade." The fix applies to upgrades from this version on, so restart Hermes after installing this one. (#207)

## 0.4.21 — 2026-11-17

- **Keep-warm now works on Claude routes where it never fired.** HeartBeat learns a route's cache lifetime from the gaps between calls. One cache miss a few seconds after the previous call came from a prompt change, not from the cache expiring, but it was still recorded as the route going cold after seconds. A route that had stayed warm across an hour then never got a known lifetime, and keep-warm never planned a ping. On Claude models, whose caches last at least 5 minutes, such misses no longer count, and routes that already recorded one recover by themselves. Other routes may evict early, so every miss still counts there. If a keep-warm ping lands on a cold cache, the lifetime becomes unknown and the pings stop. When keep-warm can't tell the lifetime, `/pulse warm` now says why. (#203)

## 0.4.20 — 2026-11-17

- **A wake turn can arm a ping-back after its first follow-up.** A wake turn that isn't a role could arm only one wake or watcher, so a turn that had already armed a check couldn't also arm its own ping-back, for example to check in after a restart. It may now arm two, and never more than `max_children_per_wake_turn`. Role and own-time turns keep that setting's limit (3 by default). Chain depth (`max_rearm_chain`), the active-wake and daily caps, and the rule that a watcher's payload may only arm one-time wakes are unchanged. (#200)

## 0.4.19 — 2026-11-17

- **A paused wake no longer brings a second closed-window notice.** With `notify_via` set, pausing or cancelling the last waiting wake while HeartBeat was sending the notice ended the wait before the send was recorded. Resuming that wake, or arming another, while the app window was still closed then sent a second notice for the same wait. The wait now lasts until the send's result is recorded. (#197)

## 0.4.18 — 2026-11-17

- **A wake waiting on a closed app window can tell you through another plugin.** A new owner setting, `notify_via` (blank by default), names a plugin and a target, for example `birdie:assistant`. That plugin offers HeartBeat a way to send a message over Hermes's plugin event bus: [docs/notifier.md](docs/notifier.md) has the handshake. When a Desktop or TUI chat has had a wake waiting for 10 minutes because its app window is closed, HeartBeat sends one short notice: how many wakes are waiting, since when, and in which profile and app. No wake titles, notes or payloads are sent. Quiet hours and the chat's daily message cap delay the notice, and it counts as one message toward that cap. The unanswered mute doesn't delay it, because the mute can only clear once you write in that chat. If sending fails, HeartBeat tries again after 5 and 10 minutes, then hourly for up to a day. `/pulse status` and the desk show whether the notifier is connected and what happened to the notice: "notifier not ready" when the other plugin can't send yet, and "may repeat" when it couldn't confirm the send (the message may already have gone out). One notice goes out per wait. The wait ends when a wake runs in the reopened chat or you write there, and a day after its notice at the latest, so a window closed for days gets at most one notice a day. (#197)

## 0.4.17 — 2026-11-17

- **Wakes, roles and own time now run in Hermes Desktop and TUI chats** on Hermes 0.23 or later, with the same budgets, quiet hours, message caps, roles and armed wakes as a one-to-one DM. HeartBeat reads the real app surface from Hermes, and only admits app chats when the running Hermes can deliver plugin turns to them. While the app window is closed, a wake waits instead of failing: it retries every minute, and `/pulse status` and the desk show it as held since it started waiting. In an app window, a wake turn that isn't notifying, or that runs while messages are held, is asked to reply with exactly `[SILENT]`, because Hermes still shows a silenced turn's text there (no push is sent); a held turn's report is kept from its `pulse respond` summary, and it never carries the how-to-stop line. Own time in an app chat runs only where you turn it on with `/pulse owntime on` (each new app conversation is a separate chat, so nothing turns on by default), and the built-in check-in isn't created there. `/pulse` commands work in app chats once you have sent a message in them. The CLI and API-server sessions stay refused, now with messages that say why. New VISION.md states what HeartBeat is for and where wakes run. (#197)

## 0.4.16 — 2026-11-16

- **Opening a chat in the app no longer cancels its reminders.** Hermes tells plugins a session was reset each time the desktop or mobile app opens or reconnects to an existing chat, even though the conversation didn't change. HeartBeat treated that as `/new`: it orphaned every one-time reminder in the chat and posted "Wakes from the old conversation were orphaned after /new." HeartBeat now acts only on a real reset, one that names both the old and the new conversation and where they differ, as `/new` does. If a conversation changes without a reset, the next turn in it still retires the old reminders. Reminders already orphaned by this bug need to be set again. (#198)

## 0.4.15 — 2026-11-15

- **The "how to stop" line now appears once, not on every check-in.** The wake text used to ask the agent to end with "say \"stop checking in\" or use /pulse off" on the first wake in each chat and on every built-in check-in. A new owner setting, `stop_hint`, decides when: `once` (the default) offers it on the first notifying wake this profile delivers, then never again; `first_per_chat` offers it once in each chat; `always` keeps the old rule; `off` never offers it. In `once` and `first_per_chat`, own-time and message-held turns never carry it, and own-time or report-back deliveries don't use it up. Report-back turns never carry it in any mode. A turn that stays silent or is never delivered doesn't use it up. Profiles and chats that already sent a wake message count as having shown it on upgrade, so upgrading doesn't bring it back. `/pulse off` and "stop checking in" keep working whatever the setting. (#195)

## 0.4.14 — 2026-11-15

- **Voice plugins can register as HeartBeat's voice bridge.** A new owner setting, `voice_bridge` (blank by default), names the voice plugin HeartBeat trusts. That plugin registers its four transport functions through Hermes's plugin event bus: [docs/voice-bridge.md](docs/voice-bridge.md) has the handshake. Hermes stamps every event with the sending plugin's id, so only the named plugin can register, and only for its own profile. Newest-wins and withdrawal rules mean a plugin reload can't bring back a stale copy, and `/pulse status` shows whether the bridge is connected. This release ships the registration only: no wake is delivered by voice yet. With the setting blank, nothing changes. (#162)

## 0.4.13 — 2026-11-15

- **A turn claimed before a trigger change no longer fires on the new trigger.** Changing a wake to or from a watcher, or changing what a check watches, already dropped the turns it had queued. A turn that was already handed to the agent but never started was left alone, and if it was handed back (no acknowledgement, a restart, or a failed hand-off) it fired once on the new trigger, with the next matching event added to it. Now it is retired the same way as the queued turns, so the new trigger's first turn comes from its own first match. (#191)

## 0.4.12 — 2026-11-15

- **A role you run with `/pulse run` can hand off.** `/pulse run` queues the turn with a fixed line from HeartBeat ("The owner asked to run this now"), and any turn with such a line counted as one that read outside text. So a role run that way couldn't hand its result to its follow-on role, set a handoff, or arm recurring work, though the same role on its own schedule could. That line alone no longer counts; a turn carrying anything else, such as a watcher event added after the wake became a watcher, still does. Watcher events, check output, a watcher's deadline notice and the report-back stay fenced as before (#190).

## 0.4.11 — 2026-11-15

- **Roles hand off work to each other.** A role can name a follow-on role in the same chat with `handoff_to`. When the role ends a turn with progress or done and a summary, the follow-on role is brought forward and its next turn shows that summary, so "when research makes progress, drafting continues" needs no extra wake. The follow-on turn still keeps every limit: its own daily turn budget, the daily turn and cost caps, wake spacing and message holds. It shows up to three handoffs at a time and the rest on later turns. A turn that read outside text can't set or pass a handoff. The desk shows which role hands off to which, and when a handoff is waiting (#138).

## 0.4.10 — 2026-11-15

- **Old per-turn records are cleaned up.** The hourly cleanup removed old wake history but left behind small per-turn markers in the plugin's database, so it grew a little with every wake. Those markers now go with the history they belong to. Markers that guard against replaying the owner's messages, and per-wake, per-chat and per-day records, stay. So do a wake turn's child counts, which a resumed turn can still need (#54).

## 0.4.9 — 2026-11-15

- **The report-back never gets an idle-pause card.** The idle limits counted the report-back's silent answers like any wake's, so after six of them the owner's next turn showed a "paused (noop)" card asking to keep, slow or stop it. The card was about a built-in turn that had already finished and that the agent can't change. The report-back is HeartBeat's own one-shot, and its silent answers already settle what it saw, so the idle, ignored and value limits leave it alone. Its error limit doesn't pause it either, since a paused report-back was never set up again: when one keeps failing to start, it is retried with a backoff that grows to hourly, and its reports still reach the owner's next message (#185).

## 0.4.8 — 2026-11-14

- **Agent watchers work while messages are held.** Recurring work and roles already ran silently under quiet hours, the unanswered mute and the daily message cap; a watcher the agent armed waited instead, so a CI failure at 02:00 was first looked at after quiet hours. Now an agent watcher's turn runs when its match arrives, silently and within the turn and spend limits, and its result is held. If that turn leaves a report, one report-back turn per chat is set up, which passes on what still matters once messages can be sent: after quiet hours end, or on the next local day under the daily message or model-turn cap. Under the unanswered mute, or once the daily spend limit is reached, the report-back is skipped and the report waits for the owner's next message. Watchers the owner armed wait as before. A turn carrying an untrusted payload still can't arm new recurring work, and neither can a later turn shown a report such a turn wrote (#137).

## 0.4.7 — 2026-11-14

- **Changing a watcher's filter rechecks what it has queued.** After `pulse update` changes a watcher's reaction emojis or message, or its kanban task or board, events queued for its next turn that no longer match are dropped. A turn left with none ends silently, and the watcher's deadline notice still comes if nothing else matches. Changing a watcher's kind, or what a check watches (path, URL, command or pattern), ends a turn queued from the old trigger, including a time tick queued before a wake became a watcher; the same result from the new target then counts as new. Edits to the note, deadline or check interval leave queued turns alone, and a turn already running is never touched (#149).

## 0.4.6 — 2026-11-14

- **A delegate's calls keep the wake turn's limits.** When a wake turn starts a delegate, the delegate runs under its own session. Its `pulse` calls already counted as part of the wake turn while that turn was running. They now still do after the owner sends `/new`, switches session, or pauses or cancels the wake mid-turn, and after the delegate's own context is compressed. Before, they counted as the owner's own turn, so wake-turn limits such as the budget clamp didn't apply. Once the wake turn ends, a delegate still running falls back to the earlier rules (#120).

## 0.4.5 — 2026-11-14

- **Cron and `every … at` wakes follow a time-zone change.** After the profile's time zone changes, each recurring wake with a local time moves to that local time in the new zone on the next check, rather than firing once at the old zone's time. After a move west it waits for the local time; after a move east it moves earlier, and when that local time has already passed today without a fire it fires once now. A wake fires once for each local time; a time already on the new zone's schedule never moves. One-time reminders keep their instant, and plain intervals keep their spacing. A failed read of the Hermes time zone keeps the zone in use rather than switching to the system's. `pulse update` is no longer needed after changing the zone (#170).

## 0.4.4 — 2026-11-14

- **Own time in groups and topics runs only where you turn it on.** Each topic is a separate conversation with its own full context, so own time on by default in every topic multiplied the cost by the number of topics: in one group with 16 topics it ran in 9 of them within 12 hours and sent one message. Direct chats keep own time by default. In a group or topic, send `/pulse owntime on` where you want it. The same goes for a Matrix direct room, where Hermes can't tell one person from a few, and for a chat on a platform not yet checked; a Slack group DM can't be turned on yet (#169). `/pulse owntime off` turns it off anywhere. After upgrading, own time already armed in a group or topic you haven't turned on stops, and comes back when you turn it on there. If you sent `/pulse owntime on` in a topic with 0.4.2, send it again (#177).

## 0.4.3 — 2026-11-14

- **Status and the desk show the settings that shape what happens.** `/pulse status` now shows how many items the chat and the profile have against their limits (#171), and refusals at a limit say how many are in use and that only the owner can raise it. It names the time zone in use and where it comes from: the plugin setting, Hermes, TZ or the system. When the plugin's own `timezone` setting pins a different zone from Hermes', status says so and how to clear it (#172). It also shows the quiet-hours range and whether it is the default (22:00-08:00) or set in settings, before quiet hours first hold anything (#173). The agent's desk shows the same item headroom and quiet window, and the usage skill teaches the default.

## 0.4.2 — 2026-11-14

- **Own time runs in groups and topics again, and you choose where.** 0.4.1 limited own time to direct chats, which took it away from owners who run their agent in a private group with topics. Own time now follows `dm_only` again: with `dm_only` off, it runs in groups and topics too, under the same message caps, quiet hours and spend limits. `/pulse owntime off` turns it off in one chat or topic, and `/pulse owntime on` turns it back on. Each topic counts as its own chat. `/pulse status` shows whether own time is on in the chat. Own-time turns that 0.4.1 cancelled come back on their own (#175).

## 0.4.1 — 2026-11-14

- **Own time runs in direct chats only.** With `dm_only` off, own time used to arm in every chat where you had written, group chats included, so it multiplied per chat and could speak to everyone in a group. It now arms only in direct chats. Own-time turns already set up in group chats are cancelled, and nothing else changes.

## 0.4.0 — 2026-11-13

- **Your reply keeps the agent's own pace.** When you reply to a wake's message within the reply window, a recurring wake no longer drops back to its shortest interval. If it had slowed down for finding nothing, it returns to the next check the agent asked for, counted from your message, and never later than it was already due. Otherwise its schedule stays exactly as the agent's last turn set it. A no-op or ignored pause you haven't been shown yet is lifted by your reply; one you've already seen still waits for `/pulse keep`, `slow` or `stop`. On a slowed cron wake, your reply gives back the cron times it was skipping (#161).
- **A wake turn stays a wake turn after you pause it.** If you pause or cancel a wake while its turn is running, everything the agent creates in the rest of that turn still follows the wake-turn rules: the role budget, the child limit, and no owner credit for `owner_asked` (#144).
- **`/pulse test` matches what would happen in rare cases.** The preview tells a delivery retry apart from a hold left by quiet hours, and shows when either ends. It shows an unacknowledged turn as dropped and previews the one that would actually be sent. It says when another wake in the chat would take the turn first, and when a wake is waiting to retry after an error (#151).

## 0.3.15 — 2026-11-13

- **Keep a long chat's cache warm: `/pulse warm on`.** On a long conversation, the first message after the prompt cache expires rewrites the whole prompt at the cache-write price. Keep-warm sends a tiny silent turn just before the cache would expire, so your next message reads the cache instead. It's per chat, and only you turn it on or off. It pings only where a ping can pay off: on caches that last 30 minutes or more, while the prompt is at least `keep_warm_min_tokens`, and while you've written in the last `keep_warm_idle_hours` (12 by default). It never pings a cache that has already gone cold. Any real turn moves the next ping out. Pings are silent and don't count as wake turns, messages, active items or agent spend; `/pulse status` and `/pulse ledger` show them on their own row (#163).
- **Higher limits on active wakes.** A chat can hold 50 active or proposed wakes and watchers (was 10), and a profile 200 (was a fixed 50, now the `max_active_profile` setting). Daily turn, message and spend limits are unchanged. Watchers now run in batches of at most 16 per check, so many slow watchers can't hold up timed wakes.

## 0.3.14 — 2026-11-11

- **Writing during a wake counts as a reply on Hermes 0.23.x.** If you write once a wake turn has started, that wake's message no longer counts toward the unanswered mute, isn't later treated as ignored, and can't tip the wake into a no-change pause. Hermes 0.23.x queues your message behind the turn instead of folding it in, so before this fix the mute could start one message early. The wake keeps the next check the agent asked for. The Signal/SimpleX exception under Known issues still applies (#157).
- **Gateway tests run on Hermes 0.23.1 in CI.** The end-to-end matrix now includes Hermes v0.23.1 beside v0.21.6, v0.21.7 and main (#155).

## 0.3.13 — 2026-11-11

- **Fully silent wakes need one Hermes setting.** A wake that decides to stay silent keeps its final reply out of the chat, but Hermes posts interim text (what the model writes alongside a tool call) by default. The README now says so, with the fix: `display.interim_assistant_messages: false`. The wake text now tells the model why it must not write alongside tool calls.
- **Cleaner check-ins and desk.** A check-in's wake header (which the model reads) now says `check-in` instead of `watcher` and drops the empty title slot, so it no longer shows a doubled separator. The desk says `1 wake` and `1 watcher` instead of `1 wakes` (#151).
- **`/pulse why` remembers why something was held.** After a held occurrence is released and fires, its row still shows why it was held, for example `was held: waiting for your reply` (#151).
- **Shorter `/pulse list` rows.** Long watcher filter values (a task id, message id or emoji list) are shortened in the list; `/pulse why` still shows them in full (#151).
- **`/pulse test` matches what would be sent.** The preview includes the same "(held by …)" note a dispatched turn carries, and a wake you made for owner silence no longer says "Would run now: yes" before you've been quiet long enough (#151).
- **Gateway tests on current Hermes.** The end-to-end suite now passes against Hermes 0.23.x in local runs (a 0.23.x CI pin comes next) and adds two scenarios: a role taking its own turn, and arming plus a silent verdict when Hermes's default tool search reaches `pulse` through its `tool_call` bridge (#155). One 0.23.x difference found along the way is tracked in #157.

## 0.3.12 — 2026-11-10

- **The demo plays on the README.** The film now plays inline under the demo link, so you can watch it without leaving GitHub.
- **Says up front that waiting costs nothing.** The README's opening now states that no model runs until there's work: plain code checks every wake and watcher, and the agent gets a turn only when something is due or a watcher's condition is met, with `/pulse` for what's next and `/pulse why <id>` for an item's recent triggers, states and reasons. Documentation only; no code changes.

## 0.3.11 — 2026-11-10

- **A README that starts with the point.** The first screen now has the one-line pitch, the demo, the install line and a three-step first result (a reminder five minutes out) ahead of the slower role path. Stale lines are fixed: Desktop and TUI wakes wait on Hermes core, group and topic delivery has passed a live forum-topic test, and the roadmap points at the current milestones. The silence notes now include Hermes's model-fallback notice, and there is a troubleshooting list plus links to releases, changelog, contributing and license. Documentation only; no code changes.

## 0.3.10 — 2026-11-10

- **Check-ins stay in messaging chats.** On CLI, TUI, Desktop and API sessions, the built-in check-in and own time could pile up (one per launch) until no new wake could be created. They're now made only in messaging chats, leftover ones are cancelled automatically, and built-ins no longer count toward the wake limits. A refused create now says how to free a slot (#147).

## 0.3.9 — 2026-11-10

- **Try a wake now.** The owner can queue one turn of a recurring wake or role with `/pulse run <id>`, under every usual check (it counts as one of its fires), or preview any wake with `/pulse test <id>` without sending anything or calling a model. Watchers, one-time reminders, the quiet check-in and own time can be previewed; they still fire on their own (#50, #61).
- **One cache line.** Owner additions and governor cards show the tool's same cache and pricing label, and a cold wake's cost appears once (#143).
- **See the filters before approving.** Watcher proposals, receipts, list rows and why views name their task, board, message and emoji filters (#149).

## 0.3.8 — 2026-11-10

- **Rest until the work is ready.** The agent can pause one wake with `pause_for: "12h"` and resume it on its own. An owner pause replaces that timer, and owner controls and governor pauses keep priority (#134).
- **Watch one task, board or message.** Kanban watchers accept `task_id` and `board` filters, reaction watchers accept a `message_id`, and receipts name the filter (#135).
- **Priced spend, checked before a turn.** The daily cost setting now explains that it stops new agent turns once today's priced spend reaches the limit, cannot cap a turn already running, and does not count unpriced calls. Status and desk label those calls as not counted (#136).
- **Your pause holds.** A wake you paused with `/pulse pause` can be resumed by the agent only during your own turn in the chat, never from one of its wake turns, including the turn you paused. Kanban filters must not be empty.
- **Tidier history.** Pruning keeps exactly the held results that can still come back to you, and no longer keeps held turns that ended with nothing to report (#146).

## 0.3.7 — 2026-11-10

- **Held results come back.** When your limits hold the agent's messages, HeartBeat keeps the summaries it wanted to send. On your next message, or its next role or own-time turn that can send, the agent gets a short list and passes on what still matters. It shows up to five at a time, oldest first; the rest come back next time. A result counts as passed on only once your reply finishes or the agent's message is sent, so a failed, interrupted or restarted turn leaves it waiting. It starts with this version: results held under earlier versions don't come back. There is no extra message or turn, and your limits stay the same.
- **Finished roles stay on the desk.** A role's final result stays visible until you've written in the chat, for up to 24 hours.
- **Role ceilings above a lowered profile maximum.** When a budget you set is above `role_turns_per_day_max`, the clamp note now names the maximum actually applied. A lowering you ask the agent for records your number, not the capped one (#141).

## 0.3.6 — 2026-11-09

- **The agent can read its desk.** The `pulse` tool has a new read-only `desk` action. The agent can check its roles, what's due, whether messages are held, and the turns, messages and cost left today at any point in a turn, not only at the start of a role or own-time turn. It returns the same view as `/pulse desk` and changes nothing.
- **A role budget you set is a ceiling.** After `/pulse budget <id> 3/day`, the agent can lower that role's budget and raise it back up to 3, but never past it. Only your own `/pulse budget` command raises it: if you ask the agent for more turns, it tells you the command to send. When you ask the agent to lower a budget, it marks the request `owner_asked=true` and the lower number becomes your ceiling. Before, the agent could raise a budget you set to the profile maximum from any turn that wasn't a wake turn. Roles you haven't set a budget for keep today's rules.
- **Pacing requests a cron wake can't follow are reported.** A cron wake follows its schedule, so `next_check` in `respond` never moved it. The tool now says the value was ignored and how to change the schedule, instead of accepting it silently.
- **Idle hints that work for roles.** When a role keeps finding nothing, the hint no longer suggests turning it into a watcher, which roles can't become. It suggests a slower rhythm, a lower budget, a separate watcher or finishing. Own time gets its own hint (push the next check out or rest), since how often it comes is the owner's setting. For a cron wake it suggests changing the schedule rather than `next_check`. The usage skill's advice matches.
- **README: a first-hour walkthrough and the full command and settings tables.** The command table now lists `/pulse role`, `/pulse desk` and `/pulse budget`, and the settings table lists own time, the daily cost limit, the role budgets and follow-ups per role turn.

## 0.3.5 — 2026-11-09

- **README: roles and the desk.** "See it work" now continues with two screenshots from a real run: the agent's receipt when it takes on three roles (rhythm, limits, a one-turn-a-day budget and the facts it still needs), and `/pulse desk` showing those roles with today's budgets and messages held for quiet hours. No code changes.

## 0.3.4 — 2026-11-09

- **A message still being written counts as in progress, not sent.** While a wake turn is running, and its message could still reach you, it holds one of the day's message slots (a turn whose messages are held, or that was silenced, holds none). The desk and `/pulse status` used to count that slot as a message already sent. They now read, for example, `messages today 2/6 (+1 in progress)`. The desk inside the running turn's own wake text no longer tells the agent its messages are held when that turn is the one using the day's last message. Message caps are unchanged.
- **Summaries in plain words.** The wake contract and the usage skill now ask for the turn's summary as a plain sentence, since the owner sees role summaries on the desk.

## 0.3.3 — 2026-11-09

- **Cron rhythms keep their asterisks on the desk.** A cron role's rhythm is now shown as inline code, for example cron `0 10 * * 1,4`, so chat markdown such as Telegram's no longer reads `* *` as bold markers and drops them.

## 0.3.2 — 2026-11-09

- **`/pulse status` agrees with the desk about a resting role.** A role that has used its turns for the day now reads `resting until tomorrow` in `/pulse status` too, instead of a next time it won't take, and it no longer sets the status header's `next due`.
- **README: `/pulse` mid-turn.** Known issues now note that Hermes currently drops a plugin's slash command sent while the agent is running a turn ([NousResearch/hermes-agent#131051](https://github.com/NousResearch/hermes-agent/issues/131051)). If `/pulse` gets no reply, wait a minute and send the same command again, with its arguments.

## 0.3.1 — 2026-11-08

- **The desk shows each role's rhythm.** A role row now reads `every 2m`, `every 1d at 09:00`, `cron …` or `self-paced` instead of just its kind, and a role that has used its turns for the day says `resting until tomorrow` instead of a next time it won't take.
- **A refused command watcher says what would work.** The refusal lists the allowed command prefixes, and when the agent used `git -C <repo>`, it suggests setting the watcher's `cwd` to the repository instead (the allowlist matches the command's first words). The usage skill says the same.

## 0.3.0 — 2026-11-08

HeartBeat 2.0 now gives your agent time of its own between your messages, not just follow-ups. See `specs/v0.3-AGENCY.md`.

- **Roles.** `role: true` on a recurring create (or `/pulse role every 1d at 09:00 <charter>` / `/pulse role self <charter>`) makes a standing responsibility the agent owns. It has no expiry, survives `/new`, keeps its notebook between turns, can arm up to 3 follow-ups per turn, and has its own turns-per-day budget (default 6; at most `role_turns_per_day_max`, default 12; `/pulse budget <id> <n>/day`). In its own turns the agent can lower a role's budget but not raise it, and a role it starts there gets no more than the role it came from (or the default). A `self` role with no rhythm of its own spreads its budget across the day.
- **Own time.** New setting `own_time_hours` (default 0, off) gives the agent a regular turn of its own to work on its roles, follow things up, reflect or rest. It is yours to control: the agent can pace it but not cancel or pause it, and turning the setting off and on re-arms it.
- **The desk.** Role and own-time turns open with a short view of the agent's roles, what's due, when you last wrote, and the budget left. `/pulse desk` shows you the same view.
- **Quiet work counts.** A silent turn that ends `progress` with a summary is real work: it no longer slows a wake down or pauses it. Fires that end `no_change` or `blocked`, or a silent `progress` with no summary, count as idle.
- **Your absence holds messages, not work.** For the agent's own recurring work, roles and own time, quiet hours, the unanswered mute and the daily message cap now hold back messages while the turn still runs silently. The daily wake-turn cap still stops turns, and so does the new optional daily cost limit (`daily_cost_limit_usd`) for turns the agent started; it keeps a running total per day, so replays and history cleanup can't reset it. Your own reminders behave as before.
- **A new wake contract.** The wake prompt tells the agent the turn is its own to act on, explains each outcome, and drops "avoid writing files when staying silent". The usage skill, the tool, skill and manifest descriptions, the README and SECURITY.md are rewritten around roles, own time and budgets. `/pulse watch` refuses roles and own time, which stay recurring.

Upgrade: reinstall and restart the gateway. The daily cost limit counts spend from the upgrade onward. The two new wake columns are added in place to the existing plugin-data database.

## 0.2.4 — 2026-11-08

- `/pulse list` shows every active, proposed and paused item plus the 10 most recently created finished ones, and says how many older finished items it hid. `/pulse list all` shows everything, as before. A chat used for a few weeks no longer buries its live items under old ones.
- `/pulse list`, `/pulse status` and the ledger keep each item on one line: a note or title with line breaks is shown with them collapsed to spaces.
- A finished item no longer shows a "next" time, so a cancelled reminder no longer looks as if it will still fire.
- A paused item names what resumes it: `keep` or `resume` for a pause the governor proposed, the resume time for `/pulse pause <id> for 3d`, and `resume` otherwise. Before, every paused row offered `keep`, which only answers a governor card.
- `/pulse pause all`, `resume all` and `cancel all`, and the `pulse` tool with `id: all`, skip items that have already finished instead of listing each one as "skipped (item is terminal)". Which items change is the same as before.
- The README's "See it work" screenshots are markdown images, so the plugin's page in the Hermes catalog shows them too.
- Benchmarks: live runs no longer fail on a provider that returns `usage: null`, null token counts or a malformed response body (#45), and round-6 judge packets include the wake payload a wake reply answered (#107).

## 0.2.3 — 2026-11-08

- `/pulse status`, `/pulse list` and the ledger now shorten a long note at the last space or line break that fits, and mark the cut with "…". Before, they cut mid-word (for example "…helpful to offer. Sile"). Titles that `/pulse add` stores from a long note are shortened the same way. A single word longer than the limit is still cut inside the word.
- The README has a "See it work" section: three screenshots from one real Telegram run, in which the agent watches a test log, writes first when two tests fail, then restores the old code and reruns the suite.

## 0.2.2 — 2026-11-03

- Only the gateway that serves a profile now opens that profile's schedule, holds its poller lock or delivers its wakes. A Hermes gateway can load every profile's plugins when it starts, and it gives its message injector to all of them. Before, a gateway that did not serve a profile could tick that profile's schedule and send a due wake through itself; Hermes accepted the wake and then dropped it, so it was retried and could be given up after three tries. HeartBeat now asks the gateway with Hermes's own routing rule; a multiplex host must also have admitted the profile (#113).
- A gateway that stops serving a profile mid-run releases the lock; that profile's due wakes wait for its own gateway, and a wake claimed just before is given back unsent. A wake turn already running there still finishes its bookkeeping. When the gateway serves the profile again, it recovers turns left by a process that held the lock in between (#113).
- `/pulse status` names the reason when the gateway does not serve the profile, or when HeartBeat cannot tell (#113).

## 0.2.1 — 2026-10-20

- A one-time reminder written as `{"kind": "at", "after": "20m"}` is now accepted as `in 20m`, the same alias as #104. Before, it failed with "at requires a time", and the model could fall back to a recurring wake that the 55-minute agent floor delayed (#106).
- An `at` trigger with no time now gives the accepted form in its error: `{"kind": "at", "at": "in 20m"}` (#106).

## 0.2.0 — 2026-10-18

- A one-time reminder written as `{"kind": "after", "after": "10m"}` is now accepted as `in 10m`, instead of failing and being retried (#104).
- The pulse tool description tells the model that a proposed result (`armed: false`, with a receipt naming `/pulse ok`) is not set yet, and to ask the owner for that command (#104).
- Group and topic wakes are recognised behind the gateway's sender prefix using a per-profile keyed tag; in group and topic chats a marker without a valid tag never counts as a wake; DM wake text stays unchanged (#98).
- A proposed wake (`agent_can_create: ask_first`) now reads as not set. Its tool result carries `armed: false`, and its receipt starts "Not set yet: it is armed only after the owner sends /pulse ok <id>." Every create, update, pause and resume result carries `armed` (#91).
- The wake text adds one contract line: if the chat shows the note's subject was settled after the wake was armed, the turn ends silently with `outcome="done"` and `notify=false` (#92).
- A bundled `heartbeat2-report-problem` skill drafts a bug report from status and logs, without firing wakes or sending DMs. It files to this repository with `gh` only after the user approves the draft.

## 0.1.18 — 2026-10-14

- Owner-requested one-time reminders, including `/pulse add`, can now be due exactly at `max_reminder_days` (365 days by default). Long reminders keep `max_lifetime_days` of grace after their due time, so they fire on time or follow the catch-up rule after downtime; quiet hours or wake spacing at the due time hold them into that grace instead of refusing them. Lowering `max_reminder_days` still expires a stored reminder not yet due at the new bound. Reminders created before 0.1.18 keep the expiry their receipt stated. Other wakes still must be due before their lifetime bound (#82).
- Granted owner-requested agent reminders say "Counts as your own reminder: not muted or capped." in their receipts, including after update or resume. The sentence survives long-receipt trimming; user-created, ignored, proposed and paused reminders do not show it (#85).
- Mutant checks run their control only over the scenarios they compare, giving slow CI runners more headroom while keeping the 60-second subprocess guard (#83).

## 0.1.17 — 2026-10-14

- Command watchers validate `fire_on` at create and update, with an enum and an actionable refusal (#79). At startup, stored active, paused or proposed watchers with an invalid mode stop with `invalid_fire_on`; queued occurrences are cancelled while started turns are preserved, with one notice per chat. Invalid command working directories and existing non-regular file targets are also refused at create. Probe receipts name the first check: now or its local time, then the interval.
- Owner-requested one-time reminders use `max_reminder_days` (default 365, maximum 3650), including `/pulse add` and agent `at` reminders backed by the current owner request (#80). Recurring wakes, watchers, autonomous follow-ups and children retain their existing lifetime limits; babysit remains bounded to four hours. The reminder bound is never below `max_lifetime_days`, so a profile that raised it keeps its existing reminders. A reminder due within `max_lifetime_days` keeps the old implicit expiry; one due later stays deliverable for `max_lifetime_days` after its due time, so downtime follows the catch-up rule instead of silent expiry. Feasibility, expiry clamps, receipts and the poller share the bound; autonomous edits revoke owner intent and are refused if the shorter lifetime cannot fit. Lifetime refusals name the setting. The skill and tool description forbid stand-in or relay wakes that re-arm later to bypass a refusal.

## 0.1.16 — 2026-10-14

- Times accept `HH:MM` (or `today HH:MM`) for the next occurrence in the chat's timezone, `tomorrow HH:MM`, `in 10 minutes`, `+10m`, and ISO date-times. Durations accept short units and words for seconds, minutes, hours, days and weeks, joined with spaces, commas or `and`; `/pulse add` accepts natural durations and bare clock times.
- Refusals now name invalid times and durations and show accepted forms. Unknown trigger kinds list the valid kinds and a one-time reminder example; `respond` outside a wake turn explains how to reply and manage wakes. These fixes come from the B2 round-3 evidence (#76).
- Recurring wake receipts now name a wake-spacing hold, and say that ticks due in quiet hours are skipped until they end, within the existing 240-character limit (#72).

## 0.1.15 — 2026-10-14

- **Unsupported event filters are refused (#57).** Select the event with `trigger.kind`; `trigger.event` is no longer advertised or accepted. At startup, existing active, paused or proposed event watchers using it are stopped (a stray `event` key on other kinds is ignored, as before) with `invalid_trigger_event`, pending occurrences are cancelled, and one notice per chat asks you to set the watcher up again. Stored task and board filters keep working.
- **First-fire feasibility counts both holds (#68).** Owner-silence waits and event watchers without a deadline now account for quiet hours, and feasibility includes the chat's active wake-spacing window before quiet hours. A wake whose first possible fire is at or after expiry is refused; receipts report the holds.
- The bundled skill tells the agent to set `owner_asked: true` when your current message asks for a one-time reminder, never for its own follow-ups, and to repeat the receipt.

## 0.1.14 — 2026-10-14

- **The agent can tell you when a wake will really arrive (#56).** Arming, updating or resuming a wake now returns a one-line `receipt`, at most 240 characters. It names the owner-silence duration and describes a watcher deadline as a one-time "nothing happened" fallback. It covers when the wake fires; whether quiet hours will hold it and until when; whether the unanswered mute will hold or drop it; the spacing between wakes; when it expires; and whether a reply cancels it. Long receipts end at a sentence boundary where possible, with an ellipsis. The bundled skill tells the agent to repeat the receipt instead of promising its own timing. A proposed wake reads "awaiting /pulse ok", never "armed". `/pulse add`, `/pulse ok` and `/pulse resume` print the same line. Before, an agent could say "fires in 3 minutes" for a reminder that quiet hours would hold until 08:00.
- **A wake that could never fire is refused (#56).** A reminder due after its own expiry was accepted, then expired silently. For example, "in 30 days" against the 14-day maximum. Owner-silence waits must fit before expiry, and a probe's next check must fit when it has no deadline. Quiet hours count at the real dispatch time for one-time wakes and watcher deadlines; a timed quiet override chains into the default quiet hours if they are still active when it ends. Only probe and event watchers accept `deadline`. A one-shot in the past outside the catch-up window is refused. On create, a default lifetime stretches to fit, up to your maximum; the clamp reports the stored expiry. Existing wakes keep their expiry, and an edit past it is refused with a hint to set `until.expires` only when an extension can help; the maximum-lifetime reason takes priority otherwise. A timed resume that cannot fit ends the paused wake with one notice.
- Resuming a paused wake keeps its stored deadline instead of re-reading a relative one ("in 2h") from the moment of resume, the same rule v0.1.11 applied to updates.

## 0.1.13 — 2026-10-14

- **A reminder you ask for in chat fires like your own (#55).** When you said "remind me at 4" in chat, the agent armed the reminder as its own follow-up, so after two unanswered messages, or at a daily cap, it was dropped. Only `/pulse add` reminders were safe. Now a one-time reminder the agent arms because you asked in your current message (it marks the call `owner_asked`) bypasses the unanswered mute and both daily caps. HeartBeat 2.0 checks that the turn really carries your message; a reminder the agent sets on its own, or arms from a wake turn, keeps the agent's limits. A later agent edit or resume without a new request from you removes the exemption. Quiet hours, spacing and expiry still apply.
- The wake text's counter now reads "wake messages today" instead of "unprompted messages today", and the matching setting is labelled "Wake messages per day".

## 0.1.12 — 2026-10-13

- A one-time wake that comes due while you have turned HeartBeat 2.0 off for the chat (`/pulse off`, or the agent's `pulse off` when you ask it to stop) is listed as cancelled (`session_off`), not as failed. It still never runs; recurring wakes stay armed and resume after `/pulse on` (#41).

## 0.1.11 — 2026-10-13

- **Wake replies lead with the result.** When a wake is set to notify you, the agent is now told to lead with what it found, to not describe what it checked or looked up, and that `notify` must be set to true or false. In a live-model bench, wake replies with nothing to report that opened by describing the check went from 3 of 3 to 1 of 3 (read by hand; 0 of 3 by the bench's pattern). Silent wakes and delivered results were unchanged.
- **Moving a wake's deadline or marking it urgent counts as an agent edit (#43).** Before, an agent could change either on a wake you armed, and the wake kept its exemption from the unanswered mute. A `/pulse watch` conversion that sets a deadline still stays yours. Title and reason changes stay cosmetic.
- **An update that doesn't change a wake's trigger keeps its deadline.** Before, any later update, even a title change, re-read a relative deadline such as "in 1h" and quietly pushed it out.

## 0.1.10 — 2026-10-13

- Owner messages are recognised by Hermes' own row id (`message_uid`) instead of by their text (#16). Before, when Hermes rewrote history inside a turn (compaction, merged consecutive messages, task-list snapshots), heartbeat2 could count an old owner message again. That reset the unanswered mute, cancelled owner-reply wakes and suppressed the silence check-in. It could also miss a new message that repeated an earlier one word for word. A row that Hermes has not stored yet (the current message, a fresh steer) keeps the text match until Hermes assigns its id. Platforms without platform message ids, such as Signal and SimpleX, benefit most.

## 0.1.9 — 2026-10-13

- A watcher you ask the agent to set up with `/pulse watch` stays yours. When the agent turns your paused wake into a watcher (a new trigger or schedule only), the unanswered mute no longer holds it. Before, the conversion counted as an agent edit, so your watcher waited for a reply you had no reason to send.
- When the agent edits a wake you armed (its trigger, timing or note), the wake gets the agent's limits: the 55-minute floor for timers without a watcher, and the cache-edge snap. The floor also moves the occurrence that was already scheduled at your faster pace; a wake anchored to a time of day moves to its first anchor slot past the floor. Before, an edited wake kept your 15-minute floor while already losing your mute exemption.

## 0.1.8 — 2026-10-13

- After a restart, a busy flag left by the previous gateway process is always cleared, even when the system clock moved backwards. Before, a flag that looked newer than the restart could block wakes into that chat for up to 5 hours, and a clock step back could clear the busy flag of a turn this process was still running. Busy flags now record which process set them, and a plugin runtime that unloads gives up its flags (#22).

## 0.1.7 — 2026-10-13

- The cache line in `/pulse add` and pulse tool results shows minutes rounded to 0.1 (`9.8m`, `2m`) instead of raw floats such as `9.811217749118805m` (#36).

## 0.1.6 — 2026-10-13

- On platforms without message ids (Signal, SimpleX), an owner message is no longer counted a second time when Hermes appends its task list to it after a context compaction. The second count marked a silent wake as answered, so its text was shown (#33).

## 0.1.5 — 2026-10-13

- Eight numeric settings have upper bounds (ledger history 100 years, wake lifetime 10 years, interval floors and slowed intervals one year, watcher floor one week, slow-down multiplier 100). A larger value is reported by `/pulse status` and ignored, instead of crashing every tick.
- The E2E `/stop` cell names the upstream Telegram duplicate-`/stop` wedge, NousResearch/hermes-agent#137545.

## 0.1.4 — 2026-10-13

- At start a rejected setting falls back to its default; on a live change it keeps its last good value, and your other changes still apply. `/pulse status` and pulse tool results name the ignored setting.
- Explain the error threshold, source switches and ledger retention in the Settings form.
- Hold pending watcher matches through a governor pause and release them after keep, slow or resume.
- Preserve busy flags for turns started in this process when restart recovery runs.
- Use the tool's cache labels on owner commands and governor cards; when-silent check-ins show TTL and last touch without warm/cold claims.
- Count a crash-resumed wake turn's tokens and estimated cost on the wake and the daily ledger. It still never counts as a fire or delivery.
- Keep the provider's full base URL in cache routes (without userinfo, query or fragment), so custom endpoints that declare prompt caching get the TTL Hermes uses. Host-only routes stored by earlier versions still resolve.
- Correct command-watcher documentation: the allowlisted environment does not pass proxy or custom-CA variables yet.

## 0.1.3 — 2026-10-13

- Silence crash-resumed wake replays, including a replay that arrives before restart recovery runs, and reject stale responses. Each replay counts as a model turn toward the daily cap, never as a fire or delivery. Only an admitted owner message (not a bot or unauthorized sender) marks a resumed turn as the owner's.
- Revoke owner-wake privileges when agent tools edit lifetime limits (expiry, max fires, owner-reply cancellation) or babysit.
- Correct empty-usage accounting (known output tokens still count), call-route pricing TTL, transactional learned routes, owner first-wake cache age, and failed probe CAS handling.
- Provider-exhaustion settlement (#27) remains blocked upstream; its strict expected failure is unchanged.

## 0.1.2 — 2026-10-13

- Agent watchers hold and merge under unanswered mute; all watchers hold at daily caps until capacity frees. Wakes you armed bypass mute, and your unedited one-shot reminders bypass daily caps. Agent edits revoke that exemption; the built-in check-in's trigger is Settings-owned. Held expiry and the last ten occurrences are visible in `/pulse why`.
- Count probe checks separately from gate skips, missing verdicts, model turns (refunding only zero-call provider failures), and cache reads/writes with price estimates.
- Cache-aware agent timing and governor backoff preserve warm intervals; the default agent timer floor is 55 minutes. Owner timing stays explicit.

**Upgrade:** restart the gateway right after updating, before opening other Hermes sessions on that profile. Downgrading to an earlier version needs the profile's `plugin-data/heartbeat2` folder removed; this discards stored wakes and history. v0.1.0 and v0.1.1 cannot read the migrated database.

## 0.1.1 — 2026-10-13

- Command watchers sanitize an allowlisted environment with Hermes' helper,
  never pass GH_TOKEN/GITHUB_TOKEN, and refuse gh output-shaping flags
  (`--jq`/`-q`, `--template`/`-t`, including attached and bundled spellings).
- URL watchers reuse Hermes URL safety: cloud metadata is always blocked;
  private/local addresses require `url_allow_private` (default false). Every
  hop checks every resolved address at connect time and dials a vetted IP.
- File watchers honor Hermes read blocks at arm/update, before each open and
  after rotation, and reject device/inode changes between stat and open.
- Pool probes and URL workers retain the watcher's profile home scope.

**Upgrade:** existing watchers using `--jq`/`--template`, a private URL or a
blocked path will error and then pause after 5 errors (the default). Re-arm them
with an allowed command/path, or enable `url_allow_private` for private URLs.
Cloud metadata, including the whole IPv4 link-local range 169.254.0.0/16,
remains blocked with that setting enabled.

gh watchers that authenticated only through `GH_TOKEN`/`GITHUB_TOKEN` in the
gateway environment stop authenticating: run `gh auth login` (hosts.yml or
keyring) for the gateway's user before upgrading. Command watchers also no longer
inherit proxy, custom-CA or D-Bus variables (`HTTPS_PROXY`, `SSL_CERT_FILE`,
`DBUS_SESSION_BUS_ADDRESS`). The command watcher's allowlisted environment
does not pass proxy or custom-CA variables yet; gh has no config workaround for them.

## 0.1.0 — 2026-10-12

First release. Requires Hermes ≥ 0.21.6. Delivers to messaging-gateway DMs.

**Wakes and watchers, in the same chat, on the same model**
- You arm them with `/pulse`; the agent arms them with its `pulse` tool. Kinds:
  - time: `at`, `every`, `cron`, `self`;
  - silence: `when silent` and the built-in check-in, which is off by default;
  - watchers: `file`, `process`, `url`, read-only `command`;
  - Hermes events: kanban, subagent, API error, reactions and edits.
- A watcher records its first check as a starting point and calls the model only when something changes. Any watcher can carry a deadline.
- The agent answers each wake with `pulse respond {outcome, notify, summary}`. `notify=false` keeps the final reply out of the chat, and the model never types a sentinel.

**Reliability**
- The schedule and every fire are durable SQLite claims, using compare-and-set, an attempt marker and refunds.
- One poller runs per profile, guarded by a file lock. Restart reconciliation collapses missed fires into one.
- A turn that started but never finished is recorded as interrupted and never re-run.
- Wakes follow compaction. `/new`, or a switch to another conversation, stops the old conversation's wakes and tells you once.
- Provider outages back off every wake (5 min → 15 min → 1 h → 4 h) until a model call succeeds. A retryable error that Hermes recovers from does not cost the wake.

**Guardrails you own** (all of them are settings in Settings ▸ Plugins)
- Quiet hours, with per-chat overrides and `quiet default`.
- Daily caps on wake turns and on messages.
- A mute after unanswered messages. It covers agent wakes and check-ins; your own wakes still fire.
- Repeat floors, wake spacing, lifetimes, active-item caps, re-arm depth and DM-only delivery.
- Each of your messages counts once as a reply, including one you send while the agent is busy. Hermes' machine turns (background notices, `/loop`, `/goal` continuations, core heartbeat ticks, replays) never count. If you redirect or steer into a running wake turn, that turn is not silenced.
- A no-op governor slows down a wake that keeps finding nothing, suggests a watcher, then pauses it and asks you once.

**Security**
- Command watchers run allowlisted argv prefixes with no shell and no terminal. Output, config, editor, browser and mutating flags and verbs are refused, and the process group is killed after 30 s.
- URL watchers refuse credentials in the URL and send no cookies, auth headers or proxy settings. They have a 10 s hard deadline and a 256 KB cap.
- Watcher regexes run in a separate process with a 5 s deadline, so a pathological pattern cannot freeze the gateway.

**Proof**
- 714 unit tests on Ubuntu and macOS (Python 3.11, 3.13, 3.14), and a real-loader integration test against Hermes v0.21.6 and a pinned main commit.
- A deterministic 23-scenario benchmark of heartbeat-1.0 failure classes, with a mutant test that keeps the benchmark gate honest.
- Live proof on one Telegram DM (GLM-5.3) covered:
  - a delivered follow-up;
  - a silent verdict;
  - surviving a gateway restart;
  - a quiet-hours hold;
  - the no-op governor pausing a wake;
  - an agent-armed file watcher matching its line (its delivery was held by the unanswered mute, as designed; delivery of an agent-armed watcher was proven live on v0.1.12).

**Known issues**: see the README. In brief:
- A wake that fails at the provider shows Hermes's own failure notice (NousResearch/hermes-agent#136749).
- Some platforms show tool progress on silent wakes (#136825).
- Memory providers see wake text as a user turn (#130622).
