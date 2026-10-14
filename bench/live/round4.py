"""Round-4 measured-turn evidence and approval wording; no model imports."""
import re
from functools import lru_cache


def text(message):
    content = message.get('content')
    if isinstance(content, list):
        content = ''.join(p.get('text', '') for p in content if isinstance(p, dict))
    return re.sub(r' \[in:[^]]+\]', '', str(content or '')).strip()


def measured_rows(cell):
    rows = cell.get('upstream_rows') or []
    if cell.get('type') != 'capture':
        return [r for r in rows if r.get('wake')]
    owner = cell.get('owner_message', '').strip()
    return [r for r in rows if owner and next((text(m) for m in reversed(
        r.get('request', {}).get('messages', [])) if m.get('role') == 'user'), None) == owner]


@lru_cache(maxsize=1)
def frozen_messages():
    from .round3 import filler_history
    return [(role, pair[key].strip()) for pair in filler_history(120000)[0]
            for role, key in [('user', 'owner'), ('assistant', 'assistant')]]


def context_reached(cell):
    rows, want = measured_rows(cell), frozen_messages()
    for row in rows:
        index = 0
        for message in row.get('request', {}).get('messages', []):
            if index < len(want) and (message.get('role'), text(message)) == want[index]:
                index += 1
        if index != len(want):
            return False
    return bool(rows)


def awaiting_approval(reply):
    reply = reply.replace('’', "'").replace('‘', "'")
    for sentence in re.split(r'[.!?]+', reply):
        for phrase in re.finditer(r'(await\w*|pending|need\w*|require\w*|please|waiting).*?(approv|/pulse ok)|approv\w*.*?(pending|required|needed)', sentence, re.I | re.S):
            words = re.findall(r"\w+(?:'\w+)?", sentence[:phrase.start()])[-4:]
            if not re.search(r"\b(not|no|never|without)\b|n't", ' '.join(words), re.I):
                return True
    return False
