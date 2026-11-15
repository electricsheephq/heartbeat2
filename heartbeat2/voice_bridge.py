"""Profile-local v1 voice transport registry. This module never delivers wakes."""
from dataclasses import dataclass
import logging
import sys
import threading
from types import FunctionType
from typing import Callable

LOG = logging.getLogger('heartbeat2')
FIELDS = ('voice_target', 'owner_message', 'wake_ready', 'inject_wake')


@dataclass(frozen=True)
class VoiceBridge:
    voice_target: Callable
    owner_message: Callable
    wake_ready: Callable
    inject_wake: Callable


def _is_live(bridge):
    for name in FIELDS:
        fn = getattr(bridge, name)
        if isinstance(fn, FunctionType):
            # Look the module up by the function's own globals: functools.wraps copies __module__ from the wrapped
            # function, so a transport decorated in a helper module reports a module it does not live in.
            module = sys.modules.get(fn.__globals__.get('__name__'))
            if module is None or fn.__globals__ is not vars(module): return False
    return True


class VoiceBridgeRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._current = None
        self._stamp = 0
        self._withdrawn = set()
        self._warned = set()
        self._closed = False

    def announce(self, /, **payload):
        bridge = None
        if payload.get('withdrawn') is not True and all(callable(payload.get(name)) for name in FIELDS):
            bridge = VoiceBridge(**{name: payload[name] for name in FIELDS})
        # Module/attribute reads stay outside the state lock; never call a transport.
        live = bridge is None or _is_live(bridge)
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
                if not reason and not live: reason = 'transport function module is no longer live'
            if reason:
                if reason not in self._warned:
                    self._warned.add(reason)
                    LOG.warning('heartbeat2 voice bridge ignored: %s', reason)
                return
            if payload.get('withdrawn') is True:
                self._withdrawn.add(stamp)
                # A withdrawal is a lifecycle stamp too: it retires the current copy if that copy is not newer, and older
                # announcements stay out even when this registry never saw the withdrawn copy's own announcement.
                if stamp >= self._stamp: self._current, self._stamp = None, stamp
                return
            if stamp in self._withdrawn or stamp < self._stamp: return
            self._current = bridge
            self._stamp = stamp

    def get(self):
        while True:
            with self._lock:
                current = None if self._closed else self._current
            if current is None: return None
            live = _is_live(current)
            with self._lock:
                if self._closed: return None
                if self._current is not current: continue
                if not live: self._current = None
                return self._current

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
