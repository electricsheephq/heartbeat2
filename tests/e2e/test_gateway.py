"""Acceptance scenarios against a real gateway and real Telegram adapter."""
import threading
import time

import pytest


class Finding(AssertionError):
    """Only a reproduced named product defect may satisfy an expected-failure gate."""


def busy_case(make_rig, *, mode="queue", command=False, photo=False):
    rig = make_rig(busy=mode)
    # Queue mode makes a later owner turn; steer/redirect fold the owner into the
    # current wake, where even notify=false MUST allow the direct owner answer.
    rig.director.notify = mode == "queue" and not command and not photo
    rig.director.busy = mode
    rig.director.final = "WAKE-OWNER-ANSWER"
    before_events = len(rig.events())
    before_human = rig.session()["last_human_at"]
    rig.busy_action("/steer OWNER-BUSY" if command else "OWNER-BUSY", photo=photo)
    wid = rig.arm()
    row = rig.settled(wid)
    rig.assert_tools_ok()
    rig.human_once(before_events, before_human)
    assert len(rig.director.wake_starts) == 1
    assert len(rig.wake_openers()) == 1
    if not photo:
        assert any("WAKE-OWNER-ANSWER" in t for t in rig.texts()), rig.texts()
    return rig, row


def test_owner_at_wake_delivers_once(make_rig):
    rig = make_rig()
    wid = rig.arm()
    before = rig.visible_ids()
    row = rig.settled(wid)
    shown = [s for s in rig.new_visible(before) if "HeartBeat 2.0:" not in s]
    assert len(shown) == 1 and "WAKE-DELIVERED" in shown[0], shown
    assert row["state"] == "done" and row["delivered"] == 1, row
    assert len(rig.director.wake_starts) == 1
    assert len(rig.wake_openers()) == 1
    rig.assert_tools_ok()


@pytest.mark.parametrize("loud", [False, True], ids=["default", "tool-progress-reasoning"])
def test_silent_verdict_shows_nothing(make_rig, loud):
    rig = make_rig(loud=loud)
    rig.director.notify = False
    rig.director.reasoning = "E2E reasoning fixture"
    wid = rig.arm()
    before = rig.visible_ids()
    row = rig.settled(wid)
    rig.assert_tools_ok()
    assert row["state"] == "done" and row["outcome"] == "no_change", row
    assert row["notify"] == 0 and row["delivered"] == 0, row
    shown = [t for t in rig.new_visible(before) if "HeartBeat 2.0:" not in t]
    assert shown == [], shown


def test_owner_message_during_wake_queue(make_rig):
    busy_case(make_rig, mode="queue")


def test_owner_message_during_wake_interrupt(make_rig):
    busy_case(make_rig, mode="interrupt")


def test_owner_steer_during_wake(make_rig):
    busy_case(make_rig, command=True)


def test_steer_in_setup_window(make_rig):
    rig = make_rig(barrier=True)
    before_events = len(rig.events())
    before_human = rig.session()["last_human_at"]
    wid = rig.arm()
    rig.wait(lambda: (rig.root / "barrier.entered").exists(), "pre_llm_call setup barrier", 90)
    # This hook runs before heartbeat2 sets busy_since, with the agent already published.
    assert rig.session()["busy_since"] is None
    before = rig.visible_ids()
    rig.driver.dm("/steer OWNER-SETUP")
    rig.wait(lambda: any("OWNER-SETUP" in t for t in rig.new_visible(before)), "setup steer accepted", 20)
    # Still before heartbeat2's pre_llm_call: an abandoned barrier would take the busy path.
    assert rig.session()["busy_since"] is None
    (rig.root / "barrier.release").touch()
    rig.settled(wid)
    rig.assert_tools_ok()
    rig.human_once(before_events, before_human)


def test_stop_during_wake(make_rig):
    rig = make_rig()
    before_events = len(rig.events())

    def stop(record):
        rig.driver.dm("/stop")
        rig.wait(lambda: any(o["state"] == "interrupted" for o in rig.rows("occurrences")),
                 "stop interrupts occurrence", 20)
        from tests.fakes.fake_llm_provider import Text

        return Text("STOPPED-WAKE-MUST-NOT-SHOW")

    rig.director.action = stop
    wid = rig.arm()
    row = rig.settled(wid, ("interrupted",))
    rig.director.wake_script.raise_errors("stop")
    assert not row["error"].startswith("provider_error"), row
    wake = next(w for w in rig.rows("wakes") if w["id"] == wid)
    assert wake["error_streak"] == 0
    assert sum(r["errors"] for r in rig.rows("ledger")) == 0
    rig.tick_barrier()
    assert len(rig.director.wake_starts) == 1
    assert len(rig.wake_openers()) == 1, "stopped wake was sent to the model again"
    assert not any("WAKE-DELIVERED" in t for t in rig.texts())
    # /stop is an owner action; it may clear mute, but must never add a provider error.
    assert len(rig.events()) == before_events + 1
    assert not any("STOPPED-WAKE-MUST-NOT-SHOW" in t for t in rig.texts())


def test_owner_photo_during_wake(make_rig):
    rig, row = busy_case(make_rig, photo=True)
    assert any(part.get("type") in {"image_url", "input_image"}
               for req in rig.llm.main_requests() for m in req["messages"]
               if isinstance(m.get("content"), list) for part in m["content"]), "model never saw image"


def test_midturn_compaction_carrier(make_rig):
    from tests.fakes.fake_llm_provider import Text, ToolCall

    rig = make_rig(busy="steer", config={"compression": {
        "enabled": True, "threshold_tokens": 15000, "protect_first_n": 1, "protect_last_n": 8}})
    # Put compressible material BEFORE the owner row so the real compressor
    # carries that row in its lean tail instead of dropping it into the summary.
    for i in range(3):
        token = f"bulk{i}"
        rig.director.script(token, Text(f"BULK-{i} " + " ".join(f"detail-{i}-{j}" for j in range(1800)),
                                        chunk_chars=2000))
        rig.owner(f"earlier details [in:{token}]", f"BULK-{i}")
    # Establish an actual Hermes ORIGIN row BEFORE the wake. Compaction must carry it
    # without treating it as a new owner reply or un-silencing a later silent wake.
    def carried_owner(record):
        before = rig.visible_ids()
        rig.driver.dm("/steer OWNER-CARRIER")
        rig.wait(lambda: any("OWNER-CARRIER" in t for t in rig.new_visible(before)), "carrier steer ack", 20)
        return ToolCall("pulse", {"action": "list"})

    # Director's owner queues do not execute callbacks; Script supplies arrival timing.
    from tests.e2e.core.history._helpers import Script

    script = Script()
    script.actions = [carried_owner, Text("CARRIER-CREATED")]
    original = rig.llm._responder
    rig.llm._responder = script
    rig.owner("make a carried owner row [in:carrier]", "CARRIER-CREATED")
    script.raise_errors("carrier creation")
    rig.llm._responder = original
    assert any("OWNER-CARRIER" in str(r) and "Gateway message origin" in str(r) for r in rig.llm.main_requests())
    before_events, before_human = len(rig.events()), rig.session()["last_human_at"]
    rig.director.notify = False
    rig.director.wake_script.actions = [ToolCall("pulse", {"action": "list"}),
                                         ToolCall("pulse", {"action": "list"}), rig.director.respond()]
    rig.llm.prompt_tokens_fn = lambda body: 20000 if any("⟦pulse o:" in str(m.get("content"))
                                                        for m in body["messages"]) else 100
    wid = rig.arm()
    before = rig.visible_ids()
    aux_before = len(rig.llm.aux_requests())
    row = rig.settled(wid)
    rig.assert_tools_ok()
    assert len(rig.llm.aux_requests()) > aux_before, "midturn compaction did not run"
    assert "E2E-COMPACTION-SUMMARY" not in str(rig.director.wake_requests[0]), "compaction ran before the wake turn"
    assert any("E2E-COMPACTION-SUMMARY" in str(r) for r in rig.director.wake_requests), "no summary on wake wire"
    assert any("E2E-COMPACTION-SUMMARY" in str(r) and "OWNER-CARRIER" in str(r)
               for r in rig.director.wake_requests), "compaction dropped owner row; carrier path not exercised"
    assert len(rig.events()) == before_events, "carried owner row recounted"
    assert rig.session()["last_human_at"] == before_human
    assert row["delivered"] == 0 and not any("WAKE-DELIVERED" in t for t in rig.new_visible(before)), "false fold"


def test_restart_with_pending_wake(make_rig):
    from tests.fakes.fake_llm_provider import Hang

    rig = make_rig()
    wid = rig.arm()
    assert rig.rows("wakes")[0]["next_due_at"] > time.time()
    rig.gateway.stop(timeout=15)
    # Hermes deliberately returns 1 for an unplanned SIGTERM so supervisors revive it.
    assert rig.gateway.proc.returncode in {0, 1}
    rig.gateway.start(timeout=50)
    row = rig.settled(wid)
    assert row["attempt"] == 1 and len(rig.director.wake_starts) == 1
    assert sum("WAKE-DELIVERED" in t for t in rig.texts()) == 1
    arrived = threading.Event()

    def hanging(record):
        arrived.set()
        return Hang(seconds=40)

    rig.director.action = hanging
    second = rig.arm("crash-started", delay="5s")
    assert arrived.wait(30), "second wake never reached provider"
    assert rig.occurrence(second)["state"] == "started"
    rig.crash()
    visible_before = rig.visible_ids()
    rig.gateway.start(timeout=50)
    # A status command here can supersede Hermes's queued native auto-resume and
    # hide the replay. Observe recovery without any inbound control/model traffic.
    row = rig.wait(lambda: (o if (o := rig.occurrence(second)) and o["state"] == "interrupted" else None),
                   "crashed wake recovers as interrupted", 30)
    rig.tick_barrier(seconds=20)
    assert row["attempt"] == 1 and len(rig.director.wake_starts) == 2
    # Hermes may replay at the model (NousResearch/hermes-agent#137358).
    # heartbeat2 must silence the turn without adopting/delivering the occurrence.
    assert not rig.new_visible(visible_before), "crash-resumed turn emitted visible text"
    assert rig.occurrence(second)["delivered"] == 0
    assert next(w for w in rig.rows("wakes") if w["id"] == second)["status_reason"] == "resumed_after_restart"
    assert sum("WAKE-DELIVERED" in t for t in rig.texts()) == 1


@pytest.mark.xfail(strict=True, raises=Finding,
                   reason="provider error leaves occurrence started and busy after gateway idle (#136749)")
def test_provider_error_backoff(make_rig):
    rig = make_rig()
    rig.director.error = True
    wid = rig.arm()
    rig.wait(lambda: any(r["key"].startswith("provider_error:") and r["value"] == "server"
                         for r in rig.rows("meta")), "wake provider failure recorded", 90)
    rig.gateway.wait_idle(timeout=30)
    row = rig.occurrence(wid)
    health = next(r["value"] for r in rig.rows("meta") if r["key"] == "provider_health")
    import json

    assert json.loads(health)["until"] > time.time(), health
    rig.tick_barrier()
    assert len(rig.director.wake_starts) == 1 and row["attempt"] == 1
    # Hermes may omit on_session_end on API failure: settlement then waits for the
    # HERMES_AGENT_TIMEOUT fallback (default 30 minutes), outside this scenario's bound.
    assert row["state"] in {"started", "interrupted"}, row
    # Visible provider-failure copy is an upstream observation, not an assertion.
    after = rig.occurrence(wid)
    if after["state"] == "started" and rig.session()["busy_since"] is not None:
        raise Finding("provider error recorded backoff but omitted turn-end settlement; started/busy after idle")
    assert after["state"] == "interrupted" and after["error"] == "provider_error:server", after


def test_owner_reply_clears_mute(make_rig):
    rig = make_rig()
    # Create all timers in ONE owner turn, then allow two unanswered notifications.
    from tests.fakes.fake_llm_provider import Text, ToolCall

    rig.director.script("mute", *[ToolCall("pulse", {"action": "create", "note": f"mute-{i}",
                              "trigger": {"kind": "at", "at": f"in {10+15*i}s"}}) for i in range(3)],
                        Text("MUTE-ARMED"))
    rig.owner("arm three notifications [in:mute]", "MUTE-ARMED")
    wakes = sorted(rig.rows("wakes"), key=lambda w: w["next_due_at"])
    assert len(wakes) == 3 and all(w["creator"] == "agent" for w in wakes)
    for w in wakes[:2]:
        rig.settled(w["id"])
    assert rig.session()["unanswered_count"] == 2
    assert sum("WAKE-DELIVERED" in t for t in rig.texts()) == 2
    third = rig.settled(wakes[2]["id"], ("skipped",))
    assert third["error"] == "unanswered" and len(rig.director.wake_starts) == 2
    rig.owner("I am back [in:returned]", "reply to returned")
    assert rig.session()["unanswered_count"] == 0
    fourth = rig.agent_arm("after-reply", {"kind": "at", "at": "in 5s"})
    row = rig.settled(fourth)
    assert row["delivered"] == 1 and len(rig.director.wake_starts) == 3
    assert sum("WAKE-DELIVERED" in t for t in rig.texts()) == 3
    rig.assert_tools_ok()


def test_new_session_orphans_wake(make_rig):
    rig = make_rig()
    wid = rig.arm(delay="20s")
    old_sid = rig.session()["current_session_id"]
    rig.owner("/new")
    rig.wait(lambda: any(w["status"] == "orphaned" for w in rig.rows("wakes")), "wake orphaned", 20)
    assert rig.session()["current_session_id"] != old_sid
    from tests.fakes.fake_llm_provider import Text

    def notice(record):
        users = [m for m in record["body"]["messages"] if m["role"] == "user"]
        # Scripted model surfaces the notice supplied by the plugin, no invented notice.
        content = str(users[-1]["content"])
        phrase = "Wakes from the old conversation were orphaned after /new."
        assert phrase in content, content
        return Text(phrase)

    from tests.e2e.core.history._helpers import Script

    script = Script()
    script.actions = [notice]
    rig.llm._responder = script
    rig.owner("hello again [in:new-session]", "Wakes from the old conversation were orphaned after /new.")
    script.raise_errors("orphan notice")
    rig.llm._responder = rig.director
    rig.tick_barrier(seconds=25)
    assert len(rig.director.wake_starts) == 0
    assert rig.wake_openers() == [], "orphaned wake reached the model"
    assert sum("Wakes from the old conversation were orphaned after /new." in t for t in rig.texts()) == 1


def test_api_error_watcher_no_self_loop(make_rig):
    """One wake API error yields exactly one coalesced watcher occurrence, held by provider
    backoff. A self-loop spanning backoff periods is not observable in this window."""
    rig = make_rig()
    watcher = rig.agent_arm("watch-errors", {"kind": "api_error"})
    # A real wake errors while the API-error watcher is active. Never seed a fake
    # event or advance the provider's five-minute backoff with a clock/DB write.
    rig.director.error = True
    wid = rig.arm("self-error", delay="5s")
    rig.wait(lambda: any(r["key"].startswith("provider_error:") and r["value"] == "server"
                         for r in rig.rows("meta")), "wake error while watcher armed", 30)
    rig.gateway.wait_idle(timeout=30)
    rig.tick_barrier(seconds=20)
    assert len(rig.director.wake_starts) == 1
    watcher_rows = rig.rows("occurrences", "WHERE wake_id=?", (watcher,))
    assert len(watcher_rows) == 1, watcher_rows
    assert watcher_rows[0]["state"] == "pending" and watcher_rows[0]["error"] == "provider_unavailable"
    assert any(r["key"] == "api_cooldown:" + watcher and float(r["value"]) > 0 for r in rig.rows("meta"))
    assert sum(r["fires"] for r in rig.rows("ledger")) == 1
