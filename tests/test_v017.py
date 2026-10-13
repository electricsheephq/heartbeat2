"""The cache line shows minutes rounded to 0.1, never raw floats (#36)."""
import json
import re

import pytest

from heartbeat2 import cache
from test_engine import engine, session, KEY


@pytest.mark.parametrize('seconds,shown', [(None, 'this wake cold'), (119.9999969998995, '2m warm'),
                                            (180, '3m warm'), (95, '1.6m warm')])
def test_learned_ttl_and_gap_are_rounded(engine, seconds, shown):
    route = 'zai|glm|api.z.ai'
    engine.store.set_meta('cache_routes', json.dumps({route: {'max_warm_gap': 588.67307, 'min_cold_gap': 600}}))
    session(engine, last_route=route, last_cache_touch_at=engine.clock.now() - 216 * 60)
    text = cache.line(engine.store, engine.store.get_session(KEY), engine.clock.now(),
                      seconds=seconds, floor=3600, pricing=False)
    assert text.startswith('Cache: zai 9.8m · last touch 216m ago · ' + shown), text
    assert not re.search(r'\d\.\d{2,}m', text), text


def test_next_warm_hint_is_rounded(engine):
    route = 'zai|glm|api.z.ai'
    engine.store.set_meta('cache_routes', json.dumps({route: {'max_warm_gap': 588.67307, 'min_cold_gap': 600}}))
    session(engine, last_route=route, last_cache_touch_at=engine.clock.now())
    text = cache.line(engine.store, engine.store.get_session(KEY), engine.clock.now(), seconds=60, pricing=False)
    assert 'next warm ≤9.3m' in text and '1m warm' in text, text


def test_unknown_ttl_bracket_is_rounded(engine):
    route = 'zai|glm|api.z.ai'
    engine.store.set_meta('cache_routes', json.dumps({route: {'max_warm_gap': 588.67307}}))
    session(engine, last_route=route, last_cache_touch_at=engine.clock.now())
    text = cache.line(engine.store, engine.store.get_session(KEY), engine.clock.now(), pricing=False)
    assert text == 'Cache: zai TTL unknown (warm seen ≤9.8m)', text


@pytest.mark.parametrize('seconds,shown', [(700 * 86400, '1008000m'), (59, '1m'), (61.5, '1m'), (3 * 60 + 6, '3.1m'),
                                           (999999.9 * 60, '999999.9m')])
def test_minutes_never_use_scientific_notation(seconds, shown):
    assert cache.minutes(seconds) == shown
