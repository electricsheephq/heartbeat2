"""Live voice wakes through the registered profile-local bridge. Ordinary API sessions stay refused.

Every helper except `inject` contains bridge errors: a failing bridge reads as no bridge. Transport calls can block,
so callers keep them outside store transactions.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import logging

from .voice_bridge import voice_bridge_for

LOG = logging.getLogger('heartbeat2')
PLATFORM = 'api_server'
PREFIX = 'heartbeat2:voice:'
# What the bridge said about one conversation, asked before a store transaction for the gate inside it to read.
_ASKED = ContextVar('heartbeat2_voice_asked', default=None)


@contextmanager
def asked(wake, hold):
    """Carry Poller.voice_hold's answer for this wake's conversation into the code that runs inside."""
    token = _ASKED.set((wake.session_key, hold) if wake is not None and hold is not None else None)
    try: yield
    finally: _ASKED.reset(token)


def answer(wake):
    """The carried answer for this wake's conversation, or None when nobody asked."""
    known = _ASKED.get()
    return known[1] if known and wake is not None and known[0] == wake.session_key else None


def _call(engine, name, *args):
    bridge = voice_bridge_for(engine)
    if bridge is None: return None
    try:
        return getattr(bridge, name)(*args)
    except Exception as exc:
        LOG.warning('heartbeat2 voice bridge %s failed: %s', name, type(exc).__name__)
        return None


def is_voice(session):
    """A stored voice conversation, by shape alone; says nothing about the device being connected."""
    return bool(session is not None and session.platform == PLATFORM and session.chat_id
                and session.session_key == PREFIX + session.chat_id)


def current(engine):
    """(canonical conversation id, session id) for the bound session when the bridge vouches for it, else None."""
    from .tool import session_env
    if voice_bridge_for(engine) is None or session_env('HERMES_SESSION_PLATFORM') != PLATFORM:
        return None
    sid = session_env('HERMES_SESSION_ID') or session_env('HERMES_SESSION_CHAT_ID')
    if not sid: return None
    target = _call(engine, 'voice_target', sid)
    return (target, sid) if isinstance(target, str) and target else None


def connected(engine, session):
    return is_voice(session) and _call(engine, 'voice_target', session.chat_id) == session.chat_id


def owner_message(engine, session, text):
    return is_voice(session) and _call(engine, 'owner_message', session.chat_id, text) is True


def ready(engine, session):
    return is_voice(session) and bool(_call(engine, 'wake_ready', session.chat_id))


def inject(engine, session, text):
    """True when the bridge accepted the wake, False when it refused it; a bridge error propagates."""
    bridge = voice_bridge_for(engine)
    if bridge is None or not is_voice(session): return False
    # Any truthy answer is an accepted wake: reading one as a refusal would send the wake twice.
    return bool(bridge.inject_wake(session.chat_id, text))
