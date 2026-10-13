"""Thin glue around the pinned Hermes helpers, never copied or mocked adapters."""
from __future__ import annotations

import dataclasses
import html
import io
import json
import os
import re
import shutil
import signal
import sqlite3
import tempfile
import time
from pathlib import Path

from tests.e2e.core.history._helpers import Script
from tests.e2e.core.platforms._drv_telegram import TelegramDriver
from tests.e2e.core.platforms._helpers import Director, GatewayUnderTest, _deep_merge
from tests.fakes.fake_llm_provider import Error, FakeLLMServer, Text, ToolCall
from tests.fakes.platforms._standin import wait_until

ROOT = Path(__file__).resolve().parents[2]
MARKER = re.compile(r"⟦pulse o:(\S+) a:(\d+)⟧")


def norm(text):
    return html.unescape(text).replace("\\", "")


class FreshGateway(GatewayUnderTest):
    """The upstream helper's connected predicate otherwise stays true on restart."""
    def start(self, timeout=60):
        started = time.monotonic()
        log = self.hermes_home / "logs/gateway.log"
        offset = log.stat().st_size if log.exists() else 0

        def ready():
            if not any(c.at >= started for c in self.standin.calls_of("getUpdates")):
                return False
            if not log.exists():
                return False
            with log.open() as stream:
                stream.seek(offset)
                # After cold-boot update dropping and the startup restore queue drain.
                return "Press Ctrl+C to stop" in stream.read()

        self._ready = ready
        return super().start(timeout)


class WakeDirector(Director):
    """Route by the current wake marker, never by a marker in older history."""
    def __init__(self):
        super().__init__()
        self.wake_requests = []
        self.wake_starts = []
        self.wake_script = Script()
        self.action = None
        self.notify = True
        self.error = False
        self.final = "WAKE-DELIVERED"
        self.reasoning = None
        self.aux = []
        self._active = None

    def __call__(self, record):
        body = record["body"]
        messages = body.get("messages", [])
        users = [m for m in messages if m.get("role") == "user"]
        current = self._text(users[-1]) if users else ""
        match = MARKER.search(current)
        # ORIGIN owner rows appended mid-turn must still continue the wake script.
        last_wake = next((MARKER.search(self._text(m)) for m in reversed(users)
                          if MARKER.search(self._text(m))), None)
        tool_continuation = messages[-1].get("role") == "tool"
        active = match or (last_wake if self._active else None)
        if active:
            identity = (active[1], int(active[2]))
            self.wake_requests.append(body)
            first = identity not in self.wake_starts
            if first:
                self.wake_starts.append(identity)
                self._active = identity
            if self.error:
                return Error(500, "scripted E2E outage", retry_after=0.1)
            if first and self.action:
                # Reuse Script's request-arrival callback/error capture.
                self.wake_script.actions.append(self.action)
                return self.wake_script(record)
            if self.wake_script.actions:
                return self.wake_script(record)
            if not tool_continuation:
                if first:
                    return self.respond()
                # A drained/steered owner is appended as a USER after tool results.
                self._active = None
                return Text(self.final, reasoning=self.reasoning)
            self._active = None
            return Text(self.final, reasoning=self.reasoning)
        return super().__call__(record)

    def respond(self):
        return ToolCall("pulse", {"action": "respond", "outcome": "progress" if self.notify else "no_change",
                                  "notify": self.notify, "summary": "E2E verdict"})

    def auxiliary(self, record):
        self.aux.append(record["body"])
        return Text("E2E-COMPACTION-SUMMARY. Earlier exchanges were completed.")


class PulseRig:
    def __init__(self, name, pin, *, busy="queue", loud=False, barrier=False, config=None,
                 heartbeat2=True, director=None, poke=False):
        self.name, self.pin, self.busy = name, pin, busy
        self.heartbeat2 = heartbeat2
        self.root = Path(tempfile.mkdtemp(prefix="hb-e2e-"))
        self.driver = TelegramDriver()
        self.director = director or WakeDirector()
        self.llm = FakeLLMServer(self.director, aux=self.director.auxiliary)
        self.driver.start()
        self.llm.start()
        cfg = _deep_merge(self.driver.gateway_config(), {
            "plugins": {"enabled": ["heartbeat2"], "entries": {"heartbeat2": {
                "enabled": True, "allow_gateway_injection": True, "settings": {
                    "quiet_hours": "", "timezone": "UTC", "wake_spacing_minutes": 0,
                    "min_interval_minutes": 1, "agent_ungated_floor_minutes": 1,
                    "probe_floor_seconds": 1, "max_unanswered": 2, "max_active": 10,
                    "max_messages_per_day": 20, "max_model_wakes_per_day": 40,
                    "owner_silent_checkin": False}}}},
            "model": {"supports_vision": True},
            "display": {"busy_input_mode": busy, "tool_progress": "off", "show_reasoning": False,
                        "busy_text_debounce_seconds": 0,
                        "platforms": {"telegram": {"tool_progress": "new" if loud else "off",
                                                   "show_reasoning": loud, "streaming": False}}},
            "streaming": {"enabled": False}, "compression": {"enabled": False},
            "logging": {"level": "DEBUG"},
            "tools": {"tool_search": {"enabled": "off"}},
            "platform_toolsets": {"telegram": ["heartbeat2"]},
            "agent": {"api_max_retries": 1, "max_turns": 12},
        })
        cfg = _deep_merge(cfg, config or {})
        if not heartbeat2:
            cfg["plugins"] = {"enabled": [], "entries": {}}
        env = {**self.driver.gateway_env(), "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONPATH_PREPEND": str(Path(__file__).parent),
               "HEARTBEAT2_E2E_CHILD": "1", "HERMES_GATEWAY_EXTERNAL_SUPERVISOR": "1"}
        self.gateway = FreshGateway(self.root, llm_base_url=self.llm.base_url, config=cfg,
                                        env=env, ready=self.driver.connected)
        self.gateway.standin = self.driver.standin
        plugin = self.gateway.hermes_home / "plugins/heartbeat2"
        if heartbeat2:
            plugin.mkdir(parents=True)
        for part in (("plugin.yaml", "__init__.py", "heartbeat2", "skills") if heartbeat2 else ()):
            src = ROOT / part
            if src.is_dir():
                shutil.copytree(src, plugin / part, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copyfile(src, plugin / part)
        self.pulse_db = self.gateway.hermes_home / "plugin-data/heartbeat2/pulse.db"
        if barrier:
            self.install_barrier()
        if poke:
            self.install_poke()

    def install_poke(self):
        plugin = self.gateway.hermes_home / "plugins/e2e-poke"
        plugin.mkdir(parents=True)
        shutil.copyfile(Path(__file__).parent / "poke_plugin.py", plugin / "__init__.py")
        (plugin / "plugin.yaml").write_text(
            "name: e2e-poke\nversion: 0.0.1\nmanifest_version: 2\napi_version: 1\n"
            "kind: standalone\ndescription: Test-only stock injection\n"
            "provides_hooks: [pre_gateway_dispatch]\n")
        import hermes_yaml as yaml

        path = self.gateway.hermes_home / "config.yaml"
        cfg = yaml.safe_load(path.read_text())
        cfg["plugins"]["enabled"].append("e2e-poke")
        cfg["plugins"]["entries"]["e2e-poke"] = {
            "enabled": True, "allow_gateway_injection": True}
        path.write_text(yaml.safe_dump(cfg, sort_keys=False))

    def install_barrier(self):
        plugin = self.gateway.hermes_home / "plugins/e2e-barrier"
        plugin.mkdir(parents=True)
        shutil.copyfile(Path(__file__).parent / "barrier_plugin.py", plugin / "__init__.py")
        (plugin / "plugin.yaml").write_text(
            "name: e2e-barrier\nversion: 0.0.1\nmanifest_version: 2\napi_version: 1\n"
            "kind: standalone\ndescription: Test-only setup barrier\n"
            "provides_hooks: [pre_llm_call]\n")
        import hermes_yaml as yaml

        path = self.gateway.hermes_home / "config.yaml"
        cfg = yaml.safe_load(path.read_text())
        # Loader insertion order: barrier must precede heartbeat2 pre_llm_call.
        cfg["plugins"]["enabled"] = ["e2e-barrier", "heartbeat2"]
        cfg["plugins"]["entries"]["e2e-barrier"] = {"enabled": True}
        path.write_text(yaml.safe_dump(cfg, sort_keys=False))

    def start(self, *, bootstrap=True):
        # The helper's PYTHONPATH is tied to its checkout; assert provenance before boot.
        import tests.e2e.core.platforms._helpers as helper

        assert helper.REPO_ROOT.resolve() == self.pin
        self.gateway._env["PWD"] = str(self.gateway.home)
        self.gateway.start(timeout=50)
        if self.heartbeat2:
            self.wait(lambda: self.pulse_db.exists(), "plugin database created", 20)
        if bootstrap:
            self.owner("hello [in:bootstrap]", "reply to bootstrap")
        else:
            # An empty prior session avoids onboarding sidecars without adding measured history.
            from hermes_state import SessionDB
            db = SessionDB(self.gateway.db_path)
            try:
                db.create_session("bench-prior", "test")
            finally:
                db.close()

    def ctx(self):
        return f"Evidence: {self.root}\n{self.gateway.tail(3500)}"

    def wait(self, predicate, what, timeout=90):
        return wait_until(predicate, what, timeout=timeout, on_timeout=self.ctx)

    def rows(self, table, where="", args=()):
        assert table in {"occurrences", "sessions", "wakes", "ledger", "meta", "events"}
        if not self.pulse_db.exists():
            return []
        with sqlite3.connect(f"file:{self.pulse_db}?mode=ro", uri=True, timeout=5) as con:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute(f"SELECT * FROM {table} {where}", args)]

    def session(self):
        rows = self.rows("sessions", "WHERE chat_id=?", (self.driver.user_id,))
        assert len(rows) == 1, rows
        return rows[0]

    def events(self):
        return self.rows("events", "WHERE kind='human_message'")

    def texts(self):
        return [norm(v.text) for v in self.driver.visible(self.driver.user_id)]

    def visible_ids(self):
        return {v.message_id for v in self.driver.visible(self.driver.user_id)}

    def new_visible(self, before):
        return [norm(v.text) for v in self.driver.visible(self.driver.user_id) if v.message_id not in before]

    def owner(self, text, reply=None):
        # Hermes's contract barrier has the same bounded re-send for #121393's
        # adapter-close window. Only a read-only /pulse status may be retried;
        # model/arming/owner input is never sent twice or counted twice here.
        attempts = 3 if text == "/pulse status" else 1
        for attempt in range(attempts):
            before = self.visible_ids()
            inbound = self.driver.dm(text)
            try:
                self.wait(lambda: any((reply in t if reply else True) for t in self.new_visible(before)),
                          f"owner reply to {text[:50]}", 30 if attempt == attempts - 1 else 8)
                break
            except AssertionError:
                if attempt == attempts - 1:
                    log = self.gateway.hermes_home / "logs/agent.log"
                    if text == "/pulse status" and log.exists() and "flush-cancelled" in log.read_text():
                        raise AssertionError("Hermes Telegram duplicate /stop wedge (upstream NousResearch/hermes-agent#137545)\n" + self.ctx()) from None
                    raise
        self.gateway.wait_idle(timeout=30)
        return inbound

    def after_stop(self):
        """Wait for interruption completion before sending same-chat status input."""
        self.wait(lambda: any("⚡ Stopped" in t for t in self.texts()), "visible stop ack", 90)

        def ended():
            logs = self.gateway.hermes_home / "logs"
            return any(path.exists() and phrase in path.read_text() for path, phrase in (
                (logs / "gateway.log", "Discarding stale agent result"),
                (logs / "agent.log", "Turn ended: reason=interrupted")))

        self.wait(ended, "stopped agent result discarded or turn ended", 30)
        self.gateway.wait_idle(timeout=30)
        time.sleep(1)

    def injections(self):
        """Gateway dispatches, including repeated dispatches of the same marker."""
        log = self.gateway.hermes_home / "logs/gateway.log"
        return log.read_text().count("Plugin message injection dispatched: plugin=heartbeat2")

    def arm(self, note="e2e-wake", delay="1m"):
        before = {r["id"] for r in self.rows("wakes")}
        self.owner(f"/pulse add in {delay} {note}", "w_")
        created = self.rows("wakes")
        new = [w for w in created if w["id"] not in before]
        assert len(new) == 1 and new[0]["creator"] == "user", new
        return new[0]["id"]

    def occurrence(self, wid):
        rows = self.rows("occurrences", "WHERE wake_id=?", (wid,))
        return rows[-1] if rows else None

    def agent_arm(self, token, trigger, *, note=None, until=None):
        before = {w["id"] for w in self.rows("wakes")}
        args = {"action": "create", "note": note or token, "trigger": trigger}
        if until:
            args["until"] = until
        self.director.script(token, ToolCall("pulse", args), Text(f"ARMED-{token}"))
        self.owner(f"arm the requested item [in:{token}]", f"ARMED-{token}")
        new = [w for w in self.rows("wakes") if w["id"] not in before]
        assert len(new) == 1 and new[0]["creator"] == "agent", new
        self.assert_tools_ok()
        return new[0]["id"]

    def settled(self, wid, states=("done",)):
        def terminal():
            row = self.occurrence(wid)
            return row if row and row["state"] in states else None
        row = self.wait(terminal, f"{wid} settles as {states}", 110)
        self.gateway.wait_idle(timeout=30)
        # /pulse status orders behind final send without changing human accounting.
        self.owner("/pulse status", "HeartBeat 2.0:")
        return row

    def human_once(self, before_events, before_human):
        events = self.events()
        assert len(events) == before_events + 1, f"owner counted {len(events)-before_events} times: {events}"
        session = self.session()
        assert session["last_human_at"] > before_human
        assert session["unanswered_count"] == 0 and session["busy_since"] is None, session

    def busy_action(self, text=None, photo=False):
        def action(record):
            before = self.visible_ids()
            debug = self.gateway.hermes_home / "logs/agent.log"
            offset = debug.stat().st_size if debug.exists() else 0
            if photo:
                from PIL import Image

                buf = io.BytesIO()
                Image.new("RGB", (16, 16), "red").save(buf, format="PNG")
                self.driver.document("owner.png", buf.getvalue(), "image/png", "OWNER-PHOTO")
            else:
                self.driver.dm(text)
            # Queue-mode text has no visible ack. Its real base-adapter log witnesses
            # busy admission; debounce is explicitly zero so the pending row can drain.
            def admitted():
                if self.new_visible(before):
                    return True
                if self.busy == "queue" and text and not text.startswith("/"):
                    with debug.open() as stream:
                        stream.seek(offset)
                        return "debouncing follow-up (busy_text_mode=queue" in stream.read()
                return False
            self.wait(admitted, "busy input accepted during model request", 20)
            return self.director.respond()
        self.director.action = action

    def assert_tools_ok(self):
        for body in self.llm.main_requests():
            for row in body.get("messages", []):
                if row.get("role") == "tool":
                    content = row.get("content", "")
                    assert '"error"' not in content and "does not exist" not in content, row
        self.director.wake_script.raise_errors(self.name)

    def wake_openers(self):
        """Physical initial requests, excluding tool continuations and old markers."""
        return [r for r in self.llm.main_requests() if r["messages"][-1].get("role") == "user"
                and MARKER.match(Director._text(r["messages"][-1]))]

    def tick_barrier(self, seconds=12):
        start = time.time()
        self.wait(lambda: any(r["key"] == "last_tick_at" and float(r["value"]) >= start + seconds
                              for r in self.rows("meta")), "poller observation window", seconds + 15)

    def crash(self):
        proc = self.gateway.proc
        assert proc and proc.poll() is None
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)

    def dump(self):
        # Request bodies only: fake provider auth/headers are deliberately excluded.
        (self.root / "requests.json").write_text(json.dumps(self.llm.main_requests(), indent=2))
        (self.root / "aux.json").write_text(json.dumps(self.llm.aux_requests(), indent=2))
        (self.root / "visible.json").write_text(json.dumps(
            [dataclasses.asdict(v) for v in self.driver.visible(self.driver.user_id)], indent=2))
        (self.root / "pulse-state.json").write_text(json.dumps(
            {t: self.rows(t) for t in ("occurrences", "sessions", "wakes", "ledger", "events", "meta")}, indent=2))

    def close(self):
        (self.root / "barrier.release").touch()
        self.gateway.stop(timeout=15)
        self.dump()
        self.llm.stop()
        self.driver.stop()
        print(f"E2E evidence ({self.name}): {self.root}")
