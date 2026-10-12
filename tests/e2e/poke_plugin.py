"""Stock test plugin: inject one text turn, without heartbeat2 code."""
import json
import os
import threading
import time
from pathlib import Path


def register(ctx):
    started = False
    receipt = Path(os.environ["HERMES_HOME"]) / "poke.json"

    def hook(event, gateway, **kwargs):
        nonlocal started
        if started:
            return
        started = True
        key = gateway._session_key_for_source(event.source)
        metadata = {"hook_kwargs": ["event", "gateway", *sorted(kwargs)],
                    "session_key_source": "gateway._session_key_for_source(event.source)"}

        def poke():
            time.sleep(2)
            try:
                metadata["accepted"] = ctx.inject_message("POKE: say OK", session_key=key)
            except Exception as exc:
                metadata["error"] = type(exc).__name__
            receipt.write_text(json.dumps(metadata))

        threading.Thread(target=poke, daemon=True).start()

    ctx.register_hook("pre_gateway_dispatch", hook)
