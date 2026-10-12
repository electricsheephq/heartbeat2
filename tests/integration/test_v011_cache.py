"""Exercise actual Hermes TTL, marker and pricing helpers at the pinned PYTHONPATH."""
import pytest
from test_real_loader import _load_hermes


@pytest.mark.parametrize('setting,platform,ttl', [('5m','telegram',300),('1h','telegram',3600),('auto','telegram',3600),('auto','cron',300),('off','telegram',None), (None,'telegram',None)])
def test_v011_actual_ttl_helpers(monkeypatch, setting, platform, ttl):
    _load_hermes()
    from agent import agent_runtime_helpers as helpers
    from heartbeat2.cache import cache_ttl_for
    monkeypatch.setattr(helpers,'_raw_cache_ttl_from_config',lambda default: setting)
    from gateway.session_context import set_session_vars, clear_session_vars
    token=set_session_vars(platform=platform,source=platform)
    try:
        assert cache_ttl_for('anthropic|claude-opus-5.5|api.anthropic.com',platform=platform,api_mode='anthropic_messages')[0]==ttl
    finally:
        clear_session_vars(token)


@pytest.mark.parametrize('route,mode,ttl', [
    ('openrouter|anthropic/claude-opus-5.5|openrouter.ai','chat_completions',3600),
    ('custom:relay|claude-opus-5.5|relay.example','anthropic_messages',3600),
    ('openai|claude-opus-5.5|api.openai.com','codex_responses',None),
    ('zai|glm-5.3|api.z.ai','chat_completions',None),
    ('openai|gpt-6.1-sol|api.openai.com','codex_responses',1800)])
def test_v011_actual_marker_routes(monkeypatch, route, mode, ttl):
    _load_hermes()
    from agent import agent_runtime_helpers as helpers
    from heartbeat2.cache import cache_ttl_for
    monkeypatch.setattr(helpers,'_raw_cache_ttl_from_config',lambda default: '1h')
    assert cache_ttl_for(route,platform='telegram',api_mode=mode)[0]==ttl


def test_v011_actual_canonical_usage_and_1h_pricing():
    _load_hermes()
    from agent.usage_pricing import normalize_usage
    from heartbeat2.cache import cost
    usage=normalize_usage({'input_tokens':5,'cache_read_input_tokens':70,'cache_creation_input_tokens':25,'output_tokens':2},provider='anthropic',api_mode='anthropic_messages')
    assert usage.prompt_tokens==100 and usage.cache_read_tokens==70 and usage.cache_write_tokens==25
    route='anthropic|claude-opus-5.5|api.anthropic.com'
    assert cost(route,prompt=120000,read=120000,ttl=3600)==pytest.approx(.024)
    assert cost(route,prompt=120000,written=120000,ttl=3600)==pytest.approx(.96)
