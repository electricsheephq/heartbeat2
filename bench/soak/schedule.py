"""Frozen B3-lite seeded schedule; no profile paths or compressed time."""
import argparse
from copy import deepcopy
from datetime import date, datetime, time, timedelta
import hashlib
import json
from pathlib import Path
import random
from zoneinfo import ZoneInfo

SEED = 20261014
MIN_GAP, MAX_GAP = 5, 30
TASKS = [('call the bank', 'bank'), ('check the deploy', 'deploy'), ('stretch', 'stretch'),
         ('send the invoice', 'invoice'), ('water the plants', 'plants'), ('review the draft', 'draft'),
         ('book the dentist', 'dentist'), ('back up the laptop', 'back'), ('pay the rent', 'rent'),
         ('reply to Sam', 'Sam'), ('order printer paper', 'paper'), ('renew the passport', 'passport'),
         ('charge the headphones', 'headphones'), ('move the car', 'car')]
N_TEXTS = ["thanks, that's all for now", "what's 15% of 80?", "I'll remind myself later, no need",
           'the meeting moved to 4pm', 'how do reminders work here?',
           'can you remind me what we talked about this morning?']


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(obj):
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


def shift(episode, seconds):
    e = deepcopy(episode)
    move = lambda t: (datetime.fromisoformat(t) + timedelta(seconds=seconds)).isoformat()
    e['sends'] = [s | {'at': move(s['at'])} for s in e['sends']]
    e['due'] = [move(t) for t in e['due']]
    if e['kind'] == 'R-clock':
        e['sends'][0]['text'] = f'remind me at {datetime.fromisoformat(e["due"][0]):%H:%M} to {e["tasks"][0]["task"]}'
    for key in ('flip', 'reply_at', 'latest_close'):
        if e.get(key): e[key] = move(e[key])
    return e


def layout(episodes, start, gaps):
    result, cursor = [], start
    for i, e in enumerate(episodes):
        e = shift(e, (cursor - datetime.fromisoformat(e['sends'][0]['at'])).total_seconds())
        result.append(e)
        cursor = datetime.fromisoformat(e['latest_close']) + timedelta(minutes=gaps[i])
    if datetime.fromisoformat(result[-1]['latest_close']).time() > time(21, 45):
        raise ValueError('day does not fit; no episode dropped')
    return result


def build(start: date, tz: str) -> dict:
    zone, global_rng = ZoneInfo(tz), random.Random(f'{SEED}:0')
    reply_days = global_rng.sample(range(1, 8), 4)
    r_kinds = {d: global_rng.choice(['R-clock', 'R-rel']) for d in (3, 6)}
    tasks = list(TASKS); global_rng.shuffle(tasks)
    task_iter, episodes = iter(tasks), []
    for day in range(1, 8):
        rng = random.Random(f'{SEED}:{day}')
        kinds = (['R-clock', 'R-rel', 'W', 'C', 'N'] if day in (1, 4, 7) else
                 ['M', 'W', 'C', 'N'] if day in (2, 5) else [r_kinds[day], 'W', 'C', 'N'])
        rng.shuffle(kinds)
        if day in (3, 6): kinds.append('S')
        origin = datetime.combine(start + timedelta(days=day-1), time(8, 30), zone)
        daily, gaps, cursor = [], [], origin
        for index, kind in enumerate(kinds, 1):
            if kind == 'C': kind = 'C-reply' if day in reply_days else 'C-silent'
            e = dict(id=f'd{day}-e{index}', day=day, kind=kind, sends=[], due=[], tasks=[])
            send = lambda text, at=cursor: e['sends'].append({'at': at.isoformat(), 'text': text})
            if kind.startswith('R') or kind == 'M':
                offsets = [20, 40, 60] if kind == 'M' else [rng.choice([15, 30, 45, 60]) if kind == 'R-rel' else 5 * round(rng.randint(20, 90)/5)]
                for n, offset in enumerate(offsets):
                    task, term = next(task_iter)
                    sent = cursor + timedelta(minutes=n)
                    due = sent + timedelta(minutes=offset)
                    text = (f'remind me at {due:%H:%M} to {task}' if kind == 'R-clock' else
                            f'in {offset} minutes, remind me to {task}' if kind == 'R-rel' else
                            f'remind me in {offset} minutes to {task}')
                    send(text, sent); e['due'].append(due.isoformat()); e['tasks'].append(dict(task=task, term=term))
                close = due + timedelta(minutes=15)
            elif kind == 'W':
                e['job'] = f'soak-d{day}-e{index}'
                e['path'] = '{soak_dir}/' + e['job'] + '.status'
                send(f"the {e['job']} job writes its status to {e['path']}; tell me when it says DONE")
                flip = cursor + timedelta(minutes=rng.randint(10, 30))
                e['flip'] = flip.isoformat(); close = flip + timedelta(minutes=30)
            elif kind.startswith('C'):
                n = rng.choice([30, 45, 60]); due = cursor + timedelta(minutes=n)
                send(f"check back in {n} minutes unless I've replied by then")
                e['due'] = [due.isoformat()]
                if kind == 'C-reply': e['reply_at'] = (cursor + timedelta(minutes=n/2)).isoformat()
                close = due + timedelta(minutes=15)
            elif kind == 'S':
                send('check on me every hour today'); close = cursor + timedelta(minutes=245)
            else:
                send(rng.choice(N_TEXTS)); close = cursor + timedelta(minutes=10)
            e['latest_close'] = close.isoformat(); daily.append(e)
            gap = rng.randint(MIN_GAP, MAX_GAP) if index < len(kinds) else MIN_GAP
            gaps.append(gap); cursor = close + timedelta(minutes=gap)
        try: daily = layout(daily, origin, gaps)
        except ValueError: daily = layout(daily, origin, [MIN_GAP]*len(daily))
        episodes.extend(daily)
    obj = dict(seed=SEED, start=start.isoformat(), tz=tz, episodes=episodes)
    return obj | {'sha256': digest(obj)}


def main():
    p = argparse.ArgumentParser(); p.add_argument('--start', type=date.fromisoformat, required=True)
    p.add_argument('--tz', required=True); p.add_argument('--out', type=Path, required=True)
    a = p.parse_args(); s = build(a.start, a.tz); a.out.mkdir(parents=True, exist_ok=True)
    (a.out/'schedule.json').write_text(canonical(s) + '\n')
    (a.out/'schedule.sha256').write_text(s['sha256'] + '\n'); print(s['sha256'])


if __name__ == '__main__': main()
