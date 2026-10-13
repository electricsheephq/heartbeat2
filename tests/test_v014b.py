"""Crash-replay accounting and full-base-URL cache route regressions."""
from dataclasses import replace
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from heartbeat2 import cache, commands
from heartbeat2.schedule import local_day
from test_engine import engine, arm, session, start, KEY
from test_v013b import interrupted, replay


COUNTERS = ('tokens_in', 'tokens_out', 'cache_read_tokens', 'cache_write_tokens',
            'cost_microusd', 'unpriced_calls')


def ledger(engine, wid):
    return engine.store.get_ledger(local_day(engine.clock.now(),
        engine.policy.resolved_timezone()), wid)


@pytest.mark.parametrize('usage', [
    {'input_tokens': 12000, 'output_tokens': 400, 'cache_read_tokens': 9000,
     'cache_write_tokens': 1000},
    SimpleNamespace(prompt_tokens=12000, completion_tokens=400,
                    cache_read_tokens=9000, cache_write_tokens=1000),
], ids=['dict', 'object'])
def test_replay_two_priced_calls_account_without_settling(engine, monkeypatch, usage):
    w, o = interrupted(engine)
    before = dict(ledger(engine, w.id))
    replay(engine, w, o)
    replay(engine, w, o)  # Duplicate admission must not add another model turn.
    engine.store.set_meta('provider_error:' + o.id, 'server')
    prices, ttls, atomic = [], [], []
    monkeypatch.setattr(cache, 'cache_ttl_for', lambda route, **kw:
        ttls.append((route, kw['platform'], kw['api_mode'])) or (3600, 'test'))
    monkeypatch.setattr(cache, 'cost', lambda route, **kw: prices.append(kw) or .05)
    add = engine.ledger
    def add_ledger(wake, **counts):
        atomic.append(engine.store.connection.in_transaction)
        add(wake, **counts)
    monkeypatch.setattr(engine, 'ledger', add_ledger)
    for _ in range(2):
        engine.intake.post_api_request(session_id='sid', turn_id='resumed', usage=usage,
            provider='custom', model='claude-x', base_url='http://localhost:4000/v1',
            platform='telegram', api_mode='chat_completions', started_at=engine.clock.now())
    changed = engine.store.get_wake(w.id)
    expected = dict(zip(COUNTERS, (24000, 800, 18000, 2000, 100000, 0)))
    assert {k: getattr(changed, k) - getattr(w, k) for k in COUNTERS} == expected
    assert {k: ledger(engine, w.id)[k] - before[k] for k in COUNTERS} == expected
    assert atomic == [True, True]
    assert ttls == [('custom|claude-x|http://localhost:4000/v1', 'telegram', 'chat_completions')] * 2
    assert prices == [dict(prompt=12000, read=9000, written=1000, out=400, ttl=3600)] * 2
    assert changed.model_turns == w.model_turns + 1
    assert changed.fires == w.fires and changed.delivered == w.delivered
    assert engine.intake.flights['sid'].invalid
    assert engine.store.get_meta('provider_error:' + o.id) == 'server'
    assert engine.intake.transform_llm_output(session_id='sid', turn_id='resumed',
        response_text='restored') == '[SILENT]'
    assert engine.tool.handle({'action': 'respond', 'outcome': 'progress', 'notify': True},
        session_id='sid')['error'] == 'not_in_flight'
    engine.intake.on_session_end(session_id='sid', turn_id='resumed', completed=True)
    assert engine.store.get_occurrence(w.id, o.dedupe_key) == o
    assert engine.usage_counts(o)['tokens_in'] == o.tokens_in
    assert engine.store.get_wake(w.id) == changed
    assert engine.store.get_meta('provider_error:' + o.id) == 'server'
    monkeypatch.setattr(commands, 'session_env', lambda n: KEY if n == 'HERMES_SESSION_KEY' else '')
    runtime = SimpleNamespace(get_engine=lambda: engine)
    assert '24000 / 18000 / 2000 / 800' in commands.handle('ledger 1d', runtime)
    why = commands.handle('why ' + w.id, runtime)
    assert 'resumed_after_restart' in why and 'interrupted' in why and 'delivered no' in why


@pytest.mark.parametrize('usage,missing_price', [
    (None, False), ({'output_tokens': 7}, False), ({'input_tokens': 100, 'output_tokens': 7}, True),
], ids=['absent', 'no-prompt', 'unknown-price'])
def test_replay_unpriced_usage(engine, monkeypatch, usage, missing_price):
    w, o = interrupted(engine)
    replay(engine, w, o)
    calls = []
    monkeypatch.setattr(cache, 'cost', lambda *a, **kw: calls.append(kw) or None)
    engine.intake.post_api_request(session_id='sid', turn_id='resumed', usage=usage)
    wake = engine.store.get_wake(w.id)
    assert wake.unpriced_calls == w.unpriced_calls + 1
    assert ledger(engine, w.id)['unpriced_calls'] == 1
    assert wake.tokens_out == (7 if usage else 0)
    assert wake.tokens_in == (100 if missing_price else 0)
    assert len(calls) == int(missing_price)
    assert wake.cost_microusd == 0 and wake.warm_wakes == wake.cold_wakes == 0
    assert engine.store.get_occurrence(w.id, o.dedupe_key) == o


@pytest.mark.parametrize('case', ['wrong-turn', 'invalid-marker', 'child'])
def test_unrelated_calls_do_not_account_as_replay(engine, monkeypatch, case):
    w, o = interrupted(engine)
    if case == 'invalid-marker':
        engine.intake.pre_llm_call(session_id='sid', turn_id='resumed', platform='telegram',
            user_message=f'⟦pulse o:{w.id}/{o.dedupe_key} a:{o.attempt}⟧\nstale')
    else:
        replay(engine, w, o)
    before = engine.store.get_wake(w.id)
    before_ledger = dict(ledger(engine, w.id))
    monkeypatch.setattr(cache, 'cost', lambda *a, **kw: .05)
    engine.intake.post_api_request(session_id='sid',
        turn_id='other' if case == 'wrong-turn' else 'resumed',
        platform='subagent' if case == 'child' else 'telegram', usage={'input_tokens': 100})
    assert engine.store.get_wake(w.id) == before
    assert dict(ledger(engine, w.id)) == before_ledger
    assert engine.store.get_occurrence(w.id, o.dedupe_key) == o


def test_replay_accounting_rolls_back_if_ledger_write_fails(engine, monkeypatch):
    w, o = interrupted(engine)
    replay(engine, w, o)
    before = engine.store.get_wake(w.id)
    before_ledger = dict(ledger(engine, w.id))
    calls = []
    def broken(*args, **kwargs):
        calls.append(engine.store.connection.in_transaction)
        raise RuntimeError('fixture ledger failure')
    monkeypatch.setattr(engine, 'ledger', broken)
    engine.intake.post_api_request(session_id='sid', turn_id='resumed', usage={'input_tokens': 100})
    assert calls == [True]
    assert engine.store.get_wake(w.id) == before
    assert dict(ledger(engine, w.id)) == before_ledger
    assert engine.store.get_occurrence(w.id, o.dedupe_key) == o


@pytest.fixture
def declared_route(monkeypatch):
    seen = []
    helpers = ModuleType('agent.agent_runtime_helpers')
    helpers._raw_cache_ttl_from_config = lambda default: '5m'
    helpers.cache_ttl_means_disabled = lambda raw: False
    helpers.configured_cache_ttl = lambda: '5m'
    def policy(stub):
        seen.append(stub.base_url)
        return stub.base_url in {'http://localhost:4000/v1', 'https://gw.example.com/v1',
                                 'https://legacy.example.com'}, False
    helpers.anthropic_prompt_cache_policy = policy
    caching = ModuleType('agent.prompt_caching')
    caching.effective_cache_ttl = lambda ttl, **kw: ttl
    caching.auto_cache_ttl_for_source = lambda source: '1h'
    source = ModuleType('agent.session_source')
    source.session_source_for = lambda platform: platform
    for name, module in [('agent.agent_runtime_helpers', helpers),
                         ('agent.prompt_caching', caching), ('agent.session_source', source)]:
        monkeypatch.setitem(sys.modules, name, module)
    return seen


@pytest.mark.parametrize('base,ttl', [('http://localhost:4000/v1', 300),
    ('https://gw.example.com/v1', 300), ('https://gw.example.com/other', None)])
def test_custom_declared_ttl_and_policy_snap(engine, declared_route, monkeypatch, base, ttl):
    route = cache.route_key('custom', 'claude-x', base)
    session(engine, last_route=route, last_cache_api_mode='chat_completions')
    assert cache.ttl_for_session(engine.store, engine.store.get_session(KEY)) == (
        ttl, 'hermes' if ttl else 'unknown')
    assert declared_route[-1] == base
    engine.policy = replace(engine.policy, min_interval_minutes=1, agent_ungated_floor_minutes=1)
    w = arm(engine, 'every', creator='agent', base_interval_s=300, eff_interval_s=300)
    result = engine.policy.clamp(w, cache_ttl=engine.timing(w)['cache_ttl'])
    assert result.wake.eff_interval_s == (270 if ttl else 300)
    text = engine.cache_line(w, seconds=result.wake.eff_interval_s)
    assert text.startswith('Cache: custom ') and base not in text
    assert ('warm' in text) == bool(ttl)
    monkeypatch.setattr(commands, 'session_env', lambda n: KEY if n == 'HERMES_SESSION_KEY' else '')
    status = commands.handle('status', SimpleNamespace(get_engine=lambda: engine))
    assert ('Claude cache lasts 5 min' in status) == bool(ttl)
    assert commands._ledger(engine, KEY, 1).startswith('Ledger 1d:')


def test_legacy_host_route_still_resolves(engine, declared_route):
    session(engine, last_route='custom|claude-x|legacy.example.com',
            last_cache_api_mode='chat_completions')
    assert cache.ttl_for_session(engine.store, engine.store.get_session(KEY)) == (300, 'hermes')
    assert declared_route[-1] == 'https://legacy.example.com'


@pytest.mark.parametrize('url,base', [
    ('https://fixture-user:fixture-pass@gw.example.com:8443/v1?fixture-query=value#fragment',
     'https://gw.example.com:8443/v1'),
    ('http://[::1]:4000/v1?fixture-query=value', 'http://[::1]:4000/v1'),
    ('https://api.openai.com/v1', 'https://api.openai.com/v1'),
    ('', ''),
])
def test_route_key_sanitizes_credentials_and_keeps_base(url, base):
    assert cache.route_key('custom', 'model', url) == 'custom|model|' + base


@pytest.mark.parametrize('part', ['http://localhost:4000/v1', 'legacy.example.com'])
def test_pricing_receives_full_or_legacy_base(monkeypatch, part):
    seen = []
    pricing = ModuleType('agent.usage_pricing')
    pricing.CanonicalUsage = SimpleNamespace
    def entry(model, **kw):
        seen.append(('entry', kw['base_url']))
        return SimpleNamespace(input_cost_per_million=2, cache_write_cost_per_million=2.5)
    def estimate(model, usage, **kw):
        seen.append(('estimate', kw['base_url']))
        assert usage.input_tokens == 50 and usage.cache_read_tokens == 30
        return SimpleNamespace(amount_usd=.1)
    pricing.get_pricing_entry, pricing.estimate_usage_cost = entry, estimate
    monkeypatch.setitem(sys.modules, 'agent.usage_pricing', pricing)
    assert cache.cost('custom|claude-x|' + part, prompt=100, read=30, written=20, ttl=3600) == pytest.approx(.10003)
    base = part if '://' in part else 'https://' + part
    assert seen == [('entry', base), ('estimate', base)]


@pytest.mark.parametrize('legacy', [False, True], ids=['full-base', 'legacy'])
def test_learned_routes_and_provider_only_display(engine, legacy):
    route = ('custom|qwen|localhost' if legacy else
             cache.route_key('custom', 'qwen', 'http://localhost:4000/v1'))
    now = engine.clock.now()
    for at, read in [(now, 100), (now + 100, 100), (now + 300, 0)]:
        cache.touch(engine.store, engine.store.get_session(KEY), started_at=at,
            route=route, prompt=100, read=read)
    assert cache.ttl_for_session(engine.store, engine.store.get_session(KEY)) == (100, 'learned')
    assert json.loads(engine.store.get_meta('cache_routes'))[route] == {
        'max_warm_gap': 100, 'min_cold_gap': 200}
    text = cache.line(engine.store, engine.store.get_session(KEY), now + 310, pricing=False)
    assert text.startswith('Cache: custom ') and 'localhost' not in text and 'qwen' not in text
    if not legacy:
        assert route != cache.route_key('custom', 'qwen', 'http://localhost:4001/v1')


def test_openai_hostname_ttl_survives_full_base():
    assert cache.cache_ttl_for(cache.route_key('openai-api', 'gpt-6.1-sol',
        'https://api.openai.com/v1')) == (1800, 'openai')


@pytest.mark.parametrize('base', ['https://chatgpt.com/backend-api/codex',
                                'https://chatgpt.com:8443/backend-api/codex'])
@pytest.mark.parametrize('complete', [False, True], ids=['warm-only', 'learned-ttl'])
def test_legacy_learned_row_resolves_for_full_route(engine, base, complete):
    legacy = 'openai-codex|m|chatgpt.com'
    route = cache.route_key('openai-codex', 'm', base)
    row = {'max_warm_gap': 240}
    if complete:
        row['min_cold_gap'] = 600
    engine.store.set_meta('cache_routes', json.dumps({legacy: row}))
    session(engine, last_route=legacy, last_cache_touch_at=engine.clock.now() - 100)
    cache.touch(engine.store, engine.store.get_session(KEY), started_at=engine.clock.now(),
                route=route, prompt=100, read=100)
    assert json.loads(engine.store.get_meta('cache_routes')) == {legacy: row}
    assert cache.cache_ttl_for(route, store=engine.store) == (
        (240, 'learned') if complete else (None, 'unknown'))
    text = cache.line(engine.store, engine.store.get_session(KEY), engine.clock.now(), pricing=False)
    assert ('4.0m' if complete else 'warm seen ≤4m') in text
    assert 'chatgpt.com' not in text


def test_full_route_learned_row_takes_precedence(engine):
    route = 'openai-codex|m|https://chatgpt.com/backend-api/codex'
    engine.store.set_meta('cache_routes', json.dumps({
        'openai-codex|m|chatgpt.com': {'max_warm_gap': 240, 'min_cold_gap': 600},
        route: {'max_warm_gap': 120}}))
    session(engine, last_route=route)
    assert cache.cache_ttl_for(route, store=engine.store) == (None, 'unknown')
    assert 'warm seen ≤2m' in cache.line(engine.store, engine.store.get_session(KEY),
                                       engine.clock.now(), pricing=False)
