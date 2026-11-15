"""Profile-local v1 voice transport registry. This module never delivers wakes."""
from dataclasses import dataclass
import logging
import threading
from typing import Callable

LOG = logging.getLogger('heartbeat2')
FIELDS = ('voice_target', 'owner_message', 'wake_ready', 'inject_wake')


@dataclass(frozen=True)
class VoiceBridge:
    voice_target: Callable
    owner_message: Callable
    wake_ready: Callable
    inject_wake: Callable


class VoiceBridgeRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._current = None
        self._stamp = 0
        self._withdrawn = set()
        self._warned = set()
        self._closed = False

    def announce(self, **payload):
        with self._lock:
            if self._closed: return
            reason = ''
            stamp = payload.get('registered_at')
            if type(payload.get('version')) is not int or payload['version'] != 1:
                reason = 'version must be integer 1'
            elif type(stamp) is not int or stamp <= 0:
                reason = 'registered_at must be a positive integer'
            elif payload.get('withdrawn') is not True:
                reason = next((name+' must be callable' for name in FIELDS
                               if not callable(payload.get(name))), '')
            if reason:
                if reason not in self._warned:
                    self._warned.add(reason)
                    LOG.warning('heartbeat2 voice bridge ignored: %s', reason)
                return
            if payload.get('withdrawn') is True:
                self._withdrawn.add(stamp)
                if stamp == self._stamp: self._current = None
                return
            if stamp in self._withdrawn or stamp < self._stamp: return
            self._current = VoiceBridge(**{name: payload[name] for name in FIELDS})
            self._stamp = stamp

    def get(self):
        with self._lock:
            return None if self._closed else self._current

    def close(self):
        with self._lock:
            self._closed = True
            self._current = None


def voice_bridge_for(engine):
    """Return the engine's runtime-owned transport, or None; never invoke it."""
    try:
        accessor = getattr(engine, 'voice_bridge', None)
        return accessor() if callable(accessor) else None
    except Exception:
        return None
