"""Plugin-agnostic reproduction of upstream post-injection review forks."""
import json
import os
import time
from pathlib import Path

import pytest


class Finding(AssertionError):
    """Only the reproduced review signature satisfies the expected failure."""


@pytest.mark.xfail(strict=True, raises=Finding,
                   reason="upstream: background review runs after plugin-injected turns")
def test_injected_turn_spawns_background_review(make_rig):
    from agent.background_review import (
        _COMBINED_REVIEW_PROMPT, _MEMORY_REVIEW_PROMPT, _SKILL_REVIEW_PROMPT,
    )
    from harness import Director, Text

    prompts = (_MEMORY_REVIEW_PROMPT, _SKILL_REVIEW_PROMPT, _COMBINED_REVIEW_PROMPT)

    def review_prompt(body):
        messages = body.get("messages", [])
        last = Director._text(messages[-1]) if messages else ""
        return next((p for p in prompts if p in last), None)

    class ReviewDirector(Director):
        def __call__(self, record):
            if review_prompt(record["body"]):
                return Text("Nothing to save.")
            last = Director._text(record["body"]["messages"][-1])
            return Text("OK" if "POKE: say OK" in last else "reply to bootstrap")

        auxiliary = __call__

    config = {"memory": {"memory_enabled": True, "user_profile_enabled": True,
                         "nudge_interval": 1},
              "skills": {"creation_nudge_interval": 1},
              "platform_toolsets": {"telegram": ["memory", "skills"]}}
    rigs = {}
    summary = {"heartbeat2_enabled": False, "observation_seconds": 30}
    dest = Path(os.environ.get("HEARTBEAT2_E2E_RESULTS", str(Path.cwd())))
    # Standalone use retains evidence in the rig rather than writing repository artifacts.
    try:
        for label, injected in (("control", False), ("injected", True)):
            rig = make_rig(heartbeat2=False, poke=injected, config=config,
                           director=ReviewDirector())
            rigs[label] = rig
            if not injected:
                rig.wait(lambda: any(review_prompt(r["body"]) for r in rig.llm.requests),
                         "bootstrap owner review (positive detector control)", 30)
                # Let the first review finish before an equivalent second owner turn.
                time.sleep(2)
                rig.owner("POKE: say OK", "OK")
            rig.wait(lambda: any(not review_prompt(r["body"]) and
                                "POKE: say OK" in Director._text(r["body"]["messages"][-1])
                                for r in rig.llm.requests), "POKE reaches model", 30)
            poke = next(r for r in rig.llm.requests if not review_prompt(r["body"]) and
                        "POKE: say OK" in Director._text(r["body"]["messages"][-1]))

            def followed():
                return next((r for r in rig.llm.requests if r["t"] >= poke["t"] and
                             r["t"] - poke["t"] <= 30 and review_prompt(r["body"]) and
                             any("POKE: say OK" in Director._text(m)
                                 for m in r["body"].get("messages", [])[:-1])), None)

            deadline = time.monotonic() + 30
            while not followed() and time.monotonic() < deadline:
                time.sleep(0.05)
            review = followed()
            summary[label] = {"poke_reached_model": True, "review_fired": bool(review)}
            if review:
                messages = review["body"]["messages"]
                summary[label].update(
                    review_prompt_first_120=review_prompt(review["body"])[:120],
                    messages_count=len(messages),
                    total_characters=sum(len(Director._text(m)) for m in messages),
                    seconds_after_poke=review["t"] - poke["t"])
            if injected:
                receipt = rig.gateway.hermes_home / "poke.json"
                rig.wait(receipt.exists, "injection hook receipt", 5)
                summary["hook"] = json.loads(receipt.read_text())
                assert summary["hook"].get("accepted") is True, summary["hook"]
            else:
                assert review, "owner control failed: detector/configuration unproven"
        if summary["injected"]["review_fired"]:
            raise Finding("background review followed plugin-injected POKE: " + json.dumps(summary))
    finally:
        if "HEARTBEAT2_E2E_RESULTS" not in os.environ and rigs:
            dest = next(iter(rigs.values())).root
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "summary.json").write_text(json.dumps(summary, indent=2))
        (dest / "requests.json").write_text(json.dumps({
            label: [{"t": r["t"], "kind": r["kind"], "body": r["body"]}
                    for r in rig.llm.requests] for label, rig in rigs.items()}, indent=2))
