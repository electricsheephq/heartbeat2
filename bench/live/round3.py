"""Frozen capture matching and scoped round-3 bars; no model or gateway imports."""
import hashlib
import json
import random
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

CTX_IDS = ('L1', 'L2', 'L5', 'A1', 'A3', 'N4')
SEED = 314159
OWNER_SENTENCES = (
    'I like a conversation that feels gentle and easy to follow.',
    'There is something pleasant about keeping the tone light and friendly.',
    'A little silliness can make an ordinary exchange feel more cheerful.',
    'I enjoy a warm reply without too much fuss around it.',
    'Simple words often feel more comfortable than very elaborate expressions do.',
    'I find a playful tone rather charming when it feels natural.',
    'An easygoing exchange suits me better than something stiff and formal.',
    'I like being able to ramble without needing a grand point.',
    'A friendly little tangent can be enjoyable just for its sound.',
    'Soft humor makes this sort of conversation feel a bit lighter.',
    'I appreciate a reply that leaves plenty of room for ambiguity.',
    'There is a nice balance between being curious and being relaxed.',
    'I prefer a little warmth rather than an overly polished tone.',
    'Casual chatter can be pleasant without being especially clever or deep.',
    'I like the way a gentle phrase can soften an exchange.',
    'An unhurried style feels comfortable even when the subject is vague.',
    'I enjoy little turns of phrase that have a playful bounce.',
    'A conversation can feel welcoming without needing any special flourish either.',
    'I am fond of mild enthusiasm rather than a dramatic reaction.',
    'There is something cozy about words that do not demand much.',
    'I like a bit of whimsy mixed into otherwise plain chatter.',
    'A relaxed tone makes it easier to enjoy the wording itself.',
    'I find understated humor more appealing than a very loud joke.',
    'It is pleasant when an exchange feels open rather than rigid.',
    'I enjoy an agreeable rhythm without needing every phrase to sparkle.',
    'A slightly odd expression can be endearing if it stays gentle.',
    'I like a friendly response that does not feel overly rehearsed.',
    'There is room for both quiet warmth and a little playfulness.',
    'I appreciate simple chatter that leaves a soft impression behind it.',
    'An informal voice feels inviting without having to explain itself much.',
    'I enjoy the small difference between cheerful wording and gushy wording.',
    'A mellow exchange can be satisfying even without a striking remark.',
    'I like curious phrases that do not turn into heavy questions.',
    'There is something sweet about a reply with a gentle smile.',
    'I prefer natural wording even if it is a little uneven.',
    'A touch of harmless nonsense can give chatter some extra charm.',
    'I enjoy words that feel friendly without being full of flattery.',
    'A soft chuckle seems to fit this kind of easy conversation.',
    'I like leaving a little space between ideas instead of crowding.',
    'There is no need for every bit of chatter to impress.',
)
ASSISTANT_SENTENCES = (
    'That gentle tone has an appealing ease to it, with enough warmth to make even a simple exchange feel comfortable and welcoming.',
    'A little playful wording can be lovely, especially when the humor stays soft and lets the conversation feel loose rather than overly polished.',
    'There is a pleasant texture to plain language, where a friendly phrase can carry plenty of warmth without needing much decoration around it.',
    'The relaxed feeling comes through nicely, and a bit of harmless silliness seems quite at home in this sort of casual friendly chatter.',
    'A warm response can have a light touch, leaving the words easy to enjoy without making every little expression carry a heavy meaning.',
    'That sounds like a comfortable style, with room for mild curiosity and gentle humor to mingle without becoming too elaborate or too formal.',
    'Simple chatter has its own charm, especially when the phrasing feels natural and there is no pressure to make each remark particularly impressive.',
    'An easy rhythm can make even vague conversation feel inviting, as though the words have enough breathing room to settle into something pleasant.',
    'There is something agreeable about understated enthusiasm, since it can keep the exchange cheerful while still leaving space for a quieter kind of warmth.',
    'A soft little joke can give a phrase some bounce, without pulling the whole exchange into a performance or making the tone feel forced.',
    'The appeal of informal wording is easy to understand, because a little unevenness can feel more personable than an expression polished until perfectly smooth.',
    'Friendly language can be quite modest, with just enough warmth in the wording to make the exchange feel open and comfortable without excessive fuss.',
    'That bit of whimsy fits a relaxed exchange nicely, bringing a gentle sparkle while leaving the surrounding words plain enough to feel pleasantly familiar.',
    'A mellow tone can be enjoyable in its own right, even when the conversation wanders loosely and never reaches for a particularly clever remark.',
    'There is plenty of charm in a light response, especially if its humor feels more like a small shared smile than a grand flourish.',
    'A little ambiguity can make casual wording feel spacious, with room to enjoy the sound of a phrase without pinning down every possible meaning.',
    'The friendly feeling does not need much ornament, since an ordinary sentence can still be quite inviting when its tone stays gentle and sincere.',
    'A playful expression can feel endearing without being especially unusual, as long as it blends easily into the warm and relaxed flow of chatter.',
    'That preference for simple words makes sense, and it gives the exchange an easy clarity that can feel pleasant without becoming stiff or formal.',
    'There is a soft kind of humor in mildly odd phrasing, where the unexpected wording feels charming rather than demanding a big dramatic reaction.',
    'A comfortable exchange leaves room for different shades of warmth, from quiet agreement to light amusement, without requiring every response to sound equally bright.',
    'The casual tone has a nice looseness, making it easy for small expressions to feel friendly even when they are not particularly deep or striking.',
    'Harmless nonsense can be surprisingly pleasant in little amounts, adding a playful sound to an exchange that otherwise stays simple and easy to follow.',
    'A gentle reply can feel complete without much elaboration, particularly when the words already carry the relaxed and welcoming feeling that makes chatter enjoyable.',
    'There is an appealing softness to that style, with enough curiosity to keep the wording lively and enough ease to avoid making it feel intense.',
    'A little warmth can make plain phrasing feel quite generous, even if the expression itself is modest and does not reach for anything especially fancy.',
    'The balance between cheerfulness and restraint can be lovely, letting a response feel upbeat without turning each small exchange into an outpouring of enthusiasm.',
    'A friendly tangent can have its own quiet charm, with the pleasure coming from the easy flow of words rather than a particularly important point.',
    'That relaxed approach gives the conversation a pleasant feel, where even a small bit of rambling can seem comfortable and welcome without much explanation.',
    'Soft humor often blends well with simple language, adding just enough lightness to make the exchange cheerful while keeping the phrasing natural and easygoing.',
    'There is something inviting about a response that stays unassuming, offering a little warmth without asking the surrounding conversation to become more elaborate or serious.',
    'A gentle smile in the wording can be enough, since small expressions of amusement often feel quite comfortable within an otherwise mellow and informal exchange.',
    'The slight unevenness of casual phrasing can be part of its charm, giving the words a friendly feel without making them seem carefully arranged.',
    'An open tone leaves room for a little wandering, and that looseness can make a conversation enjoyable even when the expressions remain very ordinary.',
    'A mild reaction can still feel warm and responsive, especially when the surrounding wording has enough softness to make simple agreement sound pleasantly friendly.',
    'There is a nice ease in letting chatter stay light, with the words free to be a little playful without carrying an especially grand meaning.',
    'That small touch of curiosity feels quite comfortable, giving the exchange a bit of liveliness without making the conversation turn sharp or overly demanding.',
    'Plain language can have a surprisingly pleasant rhythm, with familiar words fitting together in a way that feels warm without needing any special decoration.',
    'A bit of quiet amusement suits this gentle tone, bringing some lightness to the exchange while leaving plenty of room for calm and simple wording.',
    'The friendly quality can come from very small choices in phrasing, so an easygoing expression may feel welcoming even without much extra enthusiasm or flourish.',
)


def filler_history(tokens):
    rng = random.Random(SEED)
    pairs, size, previous = [], 2, None
    while size // 4 < tokens:
        pair = {}
        for role, sentences in [('owner', OWNER_SENTENCES), ('assistant', ASSISTANT_SENTENCES)]:
            text = ' '.join(rng.sample(sentences, rng.randint(2, 6)))
            while text == previous:
                text = ' '.join(rng.sample(sentences, rng.randint(2, 6)))
            pair[role], previous = text, text
        added = len(json.dumps(pair, ensure_ascii=False, separators=(',', ':'))) + bool(pairs)
        if pairs and abs((size + added) // 4 - tokens) > abs(size // 4 - tokens):
            break
        pairs.append(pair)
        size += added
    prefix = json.dumps(pairs, ensure_ascii=False, separators=(',', ':'))
    return pairs, prefix, hashlib.sha256(prefix.encode()).hexdigest()


CTX_HASH = 'e526720ce096d3059cdb40856f2b3730565706cd5c160e6e0cc771a158980e61'


def resolve(text, now, timezone, fixtures):
    local = datetime.fromtimestamp(now, ZoneInfo(timezone))
    text = text.replace('{now+20m:HH:MM}', (local + timedelta(minutes=20)).strftime('%H:%M'))
    text = text.replace('{tomorrow:10:00}', 'tomorrow 10:00')
    for key, value in fixtures.items():
        text = text.replace('{' + key + '}', str(value))
    return text


def target(spec, cell):
    from heartbeat2.schedule import parse_at
    due, now, tz = spec['match']['due'], cell['sent_at'], cell['timezone']
    if due == '{now+20m:HH:MM}':
        return (datetime.fromtimestamp(now, ZoneInfo(tz)) + timedelta(minutes=20)).replace(second=0, microsecond=0).timestamp()
    return parse_at(resolve(due, now, tz, {}), now=now, timezone=tz)


def matching(wake, spec, cell):
    from heartbeat2.schedule import next_cron
    from croniter import croniter
    match = spec['match']
    trigger = wake.get('trigger_json', {})
    trigger = json.loads(trigger) if isinstance(trigger, str) else trigger
    if (wake.get('kind') not in match['families'] or wake.get('status') != match['status'] or
        bool(wake.get('cancel_on_reply')) != match.get('cancel_on_reply', False) or
        match['term'].lower() not in wake.get('note', '').lower()):
        return False
    try:
        if 'due' in match:
            if spec['id'] == 'T1' and not wake.get('owner_requested'):
                return False
            tolerance = 3600 if match.get('tolerance') == '1h' else 720 if spec['id'] == 'A2' else 120
            return abs(wake['next_due_at'] - target(spec, cell)) <= tolerance
        if spec['id'] in ('A3', 'A4') and (wake.get('deadline_at') is not None or 'deadline' in trigger):
            return False
        if spec['id'] == 'A3':
            return (trigger.get('path') == cell['fixtures']['path'] and bool(re.search(trigger.get('pattern', ''), 'DONE')) and
                    not any(re.search(trigger.get('pattern', ''), line) for line in ('RUNNING', 'export started')))
        if spec['id'] == 'A4':
            return trigger.get('command') == cell['fixtures']['command'] and trigger.get('fire_on') == 'failure'
        if croniter.expand(trigger['cron'])[0] != [[0], [9], ['*'], ['*'], [1, 2, 3, 4, 5]]:
            return False
        due = wake['created_at']
        for _ in range(5):
            due = next_cron(trigger['cron'], now=due, timezone=cell['timezone'])
        return (wake.get('max_fires') is None or wake['max_fires'] >= 5) and due < wake['expires_at']
    except (ValueError, TypeError, KeyError, re.error):
        return False


def set_claim(text):
    text = text.replace('’', "'").replace('‘', "'")
    for sentence in re.split(r'[.!?]+', text):
        for claim in re.finditer(r"\b(set|armed|scheduled|will remind|I'll remind|reminder is on)\b", sentence, re.I):
            before = sentence[:claim.start()]
            words = re.findall(r"[\w]+(?:['’][\w]+)?", before)[-4:]
            if re.search(r"\b(not|cannot|unable|failed to|never|no)\b|n't", ' '.join(words), re.I):
                continue
            if re.search(r'\b(if|once|when|after)\b', before, re.I) and re.search(r'approv|/pulse ok', sentence, re.I):
                continue
            return True
    return False


def turn_pulse(rows):
    from .run import tool_arguments
    calls, outputs = {}, {}
    for row in rows:
        messages = row.get('request', {}).get('messages', [])
        start = max((i for i, m in enumerate(messages) if m['role'] == 'user'), default=-1)
        for message in messages[start + 1:]:
            for call in message.get('tool_calls', []):
                if call['function']['name'] == 'pulse':
                    calls[call['id']] = tool_arguments(call)
            if message['role'] == 'tool' and message.get('tool_call_id') in calls:
                raw = message.get('content', '')
                try:
                    outputs[message['tool_call_id']] = json.loads(raw)
                except (TypeError, ValueError):
                    outputs[message['tool_call_id']] = {'unparsed': raw}
    return list(calls.values()), list(outputs.values())


def augment(score, results, specs, round=3):
    score['round'] = round
    score['bars'] = {f'B2-{i}': f'Frozen RUBRIC.md Round {round} pre-registration (ctx scoped separately).' for i in range(1, 11)}
    for result in results:
        cell = next(c for c in score['paths'][result['path']]['cells'] if (c['scenario'], c['rep']) == (result['scenario'], result['rep']))
        cell.update(result)
        if '-ctx' in result['path'] and (result.get('context_tokens') != 120000 or result.get('context_hash') != CTX_HASH):
            cell['status'] = 'PENDING'
        if round == 4 and '-ctx' in result['path']:
            from .round4 import context_reached
            if not context_reached(result):
                cell['status'] = 'PENDING'
        spec = specs[result['scenario']]
        if spec.get('type') == 'capture':
            created = [w for w in result.get('wake_set', []) if w['id'] not in result.get('prearm_ids', [])]
            matches = [w for w in created if spec.get('match') and matching(w, spec, result)]
            cell.update(created=len(created), matched=len(created) == len(matches) == 1,
                        false_arms=len(created) - min(1, len(matches)) if spec['label'] == 'MUST_ARM' or round == 4 and spec['id'] == 'T1' else len(created) if spec['label'] == 'MUST_NOT_ARM' else 0,
                        flagged=bool(len(created) == len(matches) == 1 and matches[0].get('owner_requested')),
                        negative_flag=spec['label'] == 'MUST_NOT_ARM' and any(w.get('owner_requested') for w in result.get('wake_set', [])))
    return score


def bars(cells, texts, reps, ctx=False, round=3):
    from .score import bars as old_bars
    ids = CTX_IDS if ctx else tuple(f'L{i}' for i in range(1, 10)) + tuple(f'A{i}' for i in range(1, 8)) + tuple(f'N{i}' for i in range(1, 8)) + ('T1', 'T2')
    def ready(wanted):
        return reps == 3 and all(sorted(c['rep'] for c in cells if c['scenario'] == sid and c['status'] == 'COMPLETE' and
            c.get('upstream') != 'fake' and tuple(map(int, c.get('plugin_version', '0.0.0').split('.'))) >= (0, 1, 18 if round == 4 else 14) and
            (not ctx or c.get('context_tokens') == 120000 and c.get('context_hash') == CTX_HASH)) == [1, 2, 3] for sid in wanted)
    def verdict(ok, wanted=ids, judged=False):
        complete = ready(wanted) and (not judged or all('verdict' in t for t in texts))
        return ('PASS' if ok else 'FAIL') if complete else 'PENDING'
    select = lambda wanted: [c for c in cells if c['scenario'] in wanted]
    output = old_bars([c for c in cells if c['scenario'].startswith('L')], [t for t in texts if t['scenario'].startswith('L')], reps)
    output['B2-1'] = verdict(all(c['delivered'] and any(not t['missing_tokens'] and t.get('verdict', {}).get('answers_the_wake') is not False for t in texts if (t['scenario'], t['rep']) == (c['scenario'], c['rep'])) for c in select(('L1', 'L2') if ctx else ('L1', 'L2', 'L7', 'L9'))), ('L1', 'L2') if ctx else ('L1', 'L2', 'L7', 'L9'))
    output['B2-2'] = verdict(output['B2-2'] == 'PASS' and not any(t['narration'] or t.get('verdict', {}).get('narration') for t in texts if t['scenario'].startswith('L')))
    output['B2-3'] = verdict(not any(t.get('verdict', {}).get('fabricated') or (not ctx or t['scenario'] == 'L5') and t.get('verdict', {}).get('guilt_or_escalation') for t in texts) and (ctx or sum(t.get('verdict', {}).get('stale', False) for t in texts) <= 1), judged=True)
    output['B2-4'] = verdict(all(c['wake_set_ended'] for c in select(('L8',))), ('L8',))
    arm = select(('A1', 'A3') if ctx else tuple(f'A{i}' for i in range(1, 8)))
    output['B2-6'] = verdict(sum(not c.get('matched') for c in arm) <= (0 if ctx else 1), ('A1', 'A3') if ctx else tuple(f'A{i}' for i in range(1, 8)))
    output['B2-7'] = verdict(not any(c.get('created') for c in select(('N4',))) if ctx else not any(c.get('false_arms') for c in cells), ('N4',) if ctx else tuple(f'A{i}' for i in range(1, 8)) + tuple(f'N{i}' for i in range(1, 8)) + (('T1',) if round == 4 else ()))
    flag_ids = ('A1',) if ctx else ('A1', 'A2', 'A6', 'A7', 'T2') + (('T1',) if round == 4 else ())
    output['B2-8'] = verdict(sum(c.get('flagged', False) for c in select(flag_ids)) >= (3 if ctx else 17 if round == 4 else 14) and not any(c.get('negative_flag') for c in cells), flag_ids if ctx else flag_ids + tuple(f'N{i}' for i in range(1, 8)))
    if round == 4:
        from .round4 import awaiting_approval
        truthful = all(c.get('matched') for c in select(('T1',)))
    else:
        awaiting_approval = lambda text: bool(re.search(r'(await\w*|pending|need\w*|require\w*|please|waiting).*?(approv|/pulse ok)|approv\w*.*?(pending|required|needed)', text, re.I | re.S))
        truthful = all(not c.get('created') and any(a.get('action') == 'create' and r.get('error') for a, r in zip(c.get('pulse_calls', []), c.get('pulse_results', []))) and not any(set_claim(t) for t in c['delivered']) for c in select(('T1',)))
    truthful &= all(c.get('matched') and not any(set_claim(t) for t in c['delivered']) and awaiting_approval(' '.join(c['delivered'])) for c in select(('T2',)))
    output['B2-9'] = verdict(truthful and not any(t.get('verdict', {}).get('overpromise') for t in texts), judged=True)
    output['B2-10'] = verdict(all(c['wake_set_ended'] for c in select(('N7',))), ('N7',))
    if ctx:
        output.update({f'B2-{i}': 'NOT_APPLICABLE' for i in (2, 4, 9, 10)})
    if round == 4:
        output['cost_ready'] = ready(ids)
    return output
