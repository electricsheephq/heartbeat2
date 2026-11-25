"""Read only the queue in the Hermes process that accepted a backend wake."""
from importlib import import_module
import sys


def queued_or_inflight(session_key: str, marker: str) -> bool | None:
    """Return presence, or None when a reliable snapshot is unavailable."""
    if not isinstance(session_key, str) or not session_key or not isinstance(marker, str) or not marker:
        return None
    try:
        # Only resolve an already loaded server. Importing it cold would initialize
        # host state, and an import in progress could block this acknowledgement.
        loaded = sys.modules.get('tui_gateway.server')
        if loaded is None or getattr(getattr(loaded, '__spec__', None), '_initializing', False):
            return None
        server = import_module('tui_gateway.server')
        registry, registry_lock = server._sessions, server._sessions_lock
        if not isinstance(registry, dict) or not registry_lock.acquire(blocking=False):
            return None
        try:
            matches = []
            for sid, session in registry.items():
                if not isinstance(session, dict) or not isinstance(session.get('session_key'), str):
                    return None
                if session['session_key'] == session_key:
                    matches.append((sid, session))
            if len(matches) != 1:
                return None
            sid, session = matches[0]
        finally:
            registry_lock.release()

        # As in Hermes' plugin injector: release the registry before taking history.
        history_lock = session['history_lock']
        if not history_lock.acquire(blocking=False):
            return None
        try:
            result = _prompt_presence(session, marker)
        finally:
            history_lock.release()
        if result is not False:
            return result

        # A closed, replaced or rekeyed session cannot prove absence.
        if not registry_lock.acquire(blocking=False):
            return None
        try:
            if server._sessions is registry and registry.get(sid) is session and session['session_key'] == session_key:
                return False
        finally:
            registry_lock.release()
    except Exception:
        # No prompt content leaves the probe, even when Hermes changes shape.
        return None
    return None


def _prompt_presence(session, marker):
    if session.get('_closing') or session.get('_finalized') or session.get('lazy'):
        return None
    head, tail = session.get('queued_prompt'), session.get('queued_prompts', [])
    if not isinstance(tail, list) or (head is None and tail):
        return None
    entries = tail if head is None else [head, *tail]
    if any(not isinstance(entry, dict) or not isinstance(entry.get('text'), str) for entry in entries):
        return None

    turn, running = session['inflight_turn'], session['running']
    if not isinstance(running, bool):
        return None
    if turn is not None:
        if not isinstance(turn, dict) or not isinstance(turn.get('user'), str):
            return None
        status = turn.get('status')
        if status not in (None, 'error'):
            return None
        # A refused turn is retained for rendering an error on reconnect.
        if status == 'error':
            turn = None

    if any(marker in entry['text'] for entry in entries) or (turn is not None and marker in turn['user']):
        return True
    if running and turn is None:
        # Drain removes the head before admission writes the in-flight prompt.
        return None
    return False
