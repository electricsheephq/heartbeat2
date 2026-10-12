"""Loaded only via the isolated gateway's PYTHONPATH_PREPEND."""
import os

if os.environ.get("HEARTBEAT2_E2E_CHILD") == "1":
    from loopback import install

    install()
