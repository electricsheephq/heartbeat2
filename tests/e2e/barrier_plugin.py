"""Test-only hook that blocks before heartbeat2 sees the wake setup."""
import os
import time
from pathlib import Path


def register(ctx):
    root = Path(os.environ["HERMES_HOME"]).parents[1]

    def barrier(user_message="", **kwargs):
        if "⟦pulse o:" not in user_message or (root / "barrier.release").exists():
            return
        (root / "barrier.entered").touch()
        deadline = time.monotonic() + 35
        while not (root / "barrier.release").exists():
            if time.monotonic() > deadline:
                raise RuntimeError("test setup barrier was not released")
            time.sleep(0.02)

    ctx.register_hook("pre_llm_call", barrier)
