"""Hybrid routing. Imported only after the runner selects its isolated pin."""
import json
import re

from harness import MARKER, WakeDirector
from tests.fakes.fake_llm_provider import Raw


class LiveDirector(WakeDirector):
    def __init__(self, upstream, forward=()):
        super().__init__()
        self.upstream = upstream
        self.forward = {token for token in forward if not isinstance(forward, dict) or forward[token]}
        self.scripted_wakes = False

    def __call__(self, record):
        body = record['body']
        messages = body.get('messages', [])
        users = [m for m in messages if m.get('role') == 'user']
        current = self._text(users[-1]) if users else ''
        match = MARKER.search(current)
        last_wake = next((MARKER.search(self._text(m)) for m in reversed(users)
                          if MARKER.search(self._text(m))), None)
        active = match or (last_wake if self._active else None)
        if self.scripted_wakes:
            return super().__call__(record)
        if active:
            identity = (active[1], int(active[2]))
            self.wake_requests.append(body)
            if identity not in self.wake_starts:
                self.wake_starts.append(identity)
            self._active = identity
        if active or any(f'[in:{token}]' in current for token in self.forward):
            self.upstream.wake = bool(active)
            if 'capture' in self.forward or '-ctx' in getattr(self.upstream, 'path', ''):
                body = body | {'messages': [m | {'content': re.sub(r' \[in:[^]]+\]', '', m['content'])}
                    if isinstance(m.get('content'), str) else m for m in messages]}
            response, status = self.upstream.forward(body)
            # Errors must retain the active wake for retries/fallback continuations.
            if status < 400 and not any(c.get('message', {}).get('tool_calls')
                                        for c in response.get('choices', [])):
                self._active = None
            return Raw(json.dumps(response), status, 'application/json')
        return super().__call__(record)
