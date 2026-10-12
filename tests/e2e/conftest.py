"""Opt-in real gateway tests; no Hermes imports on the default unit path."""
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def pytest_collection_modifyitems(items):
    for item in items:
        if HERE not in item.path.parents:
            continue
        if not os.environ.get("HEARTBEAT2_E2E_HERMES"):
            item.add_marker(pytest.mark.skip(reason="set HEARTBEAT2_E2E_HERMES to a pinned checkout"))
        else:
            item.add_marker(pytest.mark.timeout(240, method="signal"))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Override the unit fixture: allow loopback only, including the test servers."""
    sys.path.insert(0, str(HERE))
    from loopback import install

    # install uses monkeypatch so socket state is restored even after a failure.
    install(lambda obj, name, value: monkeypatch.setattr(obj, name, value))


@pytest.fixture
def make_rig(request, monkeypatch):
    # Importing Hermes can consult HOME; protect the parent before the first import.
    import tempfile

    raw = os.environ["HEARTBEAT2_E2E_HERMES"]
    pin = Path(raw).resolve()
    live = Path.home() / ".hermes" / "hermes-agent"
    assert pin != live.resolve() and ".hermes" not in pin.parts, "live Hermes is forbidden"
    assert (pin / "tests/fakes/fake_llm_provider.py").is_file(), f"missing helpers: {pin}"
    parent = Path(tempfile.mkdtemp(prefix="hb-parent-"))
    monkeypatch.setenv("HOME", str(parent))
    monkeypatch.setenv("HERMES_HOME", str(parent / ".hermes"))
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(pin))
    from harness import PulseRig

    rigs = []

    def make(**kwargs):
        rig = PulseRig(request.node.name, pin, **kwargs)
        rigs.append(rig)  # teardown also covers a boot failure
        request.node.pulse_evidence = str(rig.root)
        rig.start()
        return rig

    yield make
    for rig in reversed(rigs):
        rig.close()
    sys.path.remove(str(pin))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    # A JSON receipt per scenario/worker, including setup/teardown time and strict xfails.
    if HERE in item.path.parents and os.environ.get("HEARTBEAT2_E2E_RESULTS"):
        import json

        dest = Path(os.environ["HEARTBEAT2_E2E_RESULTS"])
        dest.mkdir(parents=True, exist_ok=True)
        worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
        row = {"test": item.name, "phase": report.when, "outcome": report.outcome,
               "duration": report.duration, "xfail": getattr(report, "wasxfail", None),
               "evidence": getattr(item, "pulse_evidence", None)}
        with (dest / f"{worker}.jsonl").open("a") as stream:
            stream.write(json.dumps(row) + "\n")
