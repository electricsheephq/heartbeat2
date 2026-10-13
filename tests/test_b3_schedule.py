from collections import Counter
from datetime import date, datetime, timedelta
import pytest
from bench.soak import schedule


def test_determinism_and_draws(monkeypatch):
    start = date(2026, 10, 15)
    a = schedule.build(start, 'Asia/Tokyo')
    assert a == schedule.build(start, 'Asia/Tokyo')
    assert a['sha256'] == schedule.digest({k: v for k, v in a.items() if k != 'sha256'})
    assert a['sha256'] != schedule.build(start + timedelta(days=1), 'Asia/Tokyo')['sha256']
    monkeypatch.setattr(schedule, 'SEED', 20261015)
    assert a['sha256'] != schedule.build(start, 'Asia/Tokyo')['sha256']


def test_counts_hours_tasks_and_windows():
    s = schedule.build(date(2026, 10, 15), 'Asia/Tokyo')
    episodes = s['episodes']
    counts = Counter(e['kind'] for e in episodes)
    assert counts['R-clock'] + counts['R-rel'] == 8
    assert counts == Counter({'R-clock': counts['R-clock'], 'R-rel': counts['R-rel'],
                             'M': 2, 'W': 7, 'C-reply': 4, 'C-silent': 3, 'S': 2, 'N': 7})
    assert sum(e['kind'] != 'N' for e in episodes) == 26
    assert Counter(t['task'] for e in episodes for t in e['tasks']) == Counter(t[0] for t in schedule.TASKS)
    for day in range(1, 8):
        es = [e for e in episodes if e['day'] == day]
        assert datetime.fromisoformat(es[0]['sends'][0]['at']).strftime('%H:%M') == '08:30'
        if day in (3, 6): assert es[-1]['kind'] == 'S'
        for e in es:
            for send in e['sends']:
                assert '08:30' <= datetime.fromisoformat(send['at']).strftime('%H:%M') <= '21:45'
            assert all(datetime.fromisoformat(t).strftime('%H:%M') <= '21:30' for t in e['due'])
            assert datetime.fromisoformat(e['latest_close']).strftime('%H:%M') <= '21:45'
        for a, b in zip(es, es[1:]):
            assert datetime.fromisoformat(b['sends'][0]['at']) - datetime.fromisoformat(a['latest_close']) >= timedelta(minutes=5)
    assert all(e['sends'][0]['text'] in schedule.N_TEXTS for e in episodes if e['kind'] == 'N')
    assert all('{soak_dir}/' in e['path'] for e in episodes if e['kind'] == 'W')


def test_generation_never_drops(monkeypatch):
    monkeypatch.setattr(schedule, 'MIN_GAP', 240)
    monkeypatch.setattr(schedule, 'MAX_GAP', 240)
    with pytest.raises(ValueError): schedule.build(date(2026, 10, 15), 'UTC')


def test_registered_global_stream_and_clock_shift():
    import random
    s = schedule.build(date(2026, 10, 15), 'UTC')
    rng = random.Random('20261014:0')
    assert {e['day'] for e in s['episodes'] if e['kind'] == 'C-reply'} == set(rng.sample(range(1, 8), 4))
    for day in (3, 6):
        kind = rng.choice(['R-clock', 'R-rel'])
        assert any(e['day'] == day and e['kind'] == kind for e in s['episodes'])
    tasks = list(schedule.TASKS); rng.shuffle(tasks)
    assert [(t['task'], t['term']) for e in s['episodes'] for t in e['tasks']] == tasks
    e = next(e for e in s['episodes'] if e['kind'] == 'R-clock')
    moved = schedule.shift(e, 300)
    assert datetime.fromisoformat(moved['due'][0]).strftime('%H:%M') in moved['sends'][0]['text']
