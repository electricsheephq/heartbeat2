"""Best-effort route cache observations, scheduling and price hints."""
import json
import re
from types import SimpleNamespace
from urllib.parse import urlsplit
from .model import agent_authored


def route_key(provider='', model='', base_url=''):
    url = urlsplit(base_url or '')
    host = url.hostname or ''
    if ':' in host: host = '['+host+']'
    if host and url.port is not None: host += ':'+str(url.port)
    base = (url.scheme+'://'+host+url.path) if host else ''
    return '|'.join((provider or '', model or '', base))


def _base(part):
    return part if '://' in part else 'https://'+part


def cache_ttl_for(route, *, store=None, platform='', api_mode=''):
    provider, model, part = (route or '||').split('|', 2)
    base = _base(part) if part else ''
    host = urlsplit(base).hostname or ''
    if 'claude' in model.lower():
        try:
            from agent.agent_runtime_helpers import configured_cache_ttl, anthropic_prompt_cache_policy, _raw_cache_ttl_from_config, cache_ttl_means_disabled
            from agent.prompt_caching import effective_cache_ttl, auto_cache_ttl_for_source
            from agent.session_source import session_source_for
            raw = _raw_cache_ttl_from_config('5m')
            if cache_ttl_means_disabled(raw): return None, 'off'
            stub = SimpleNamespace(provider=provider, model=model, base_url=base, api_mode=api_mode, _cache_disabled=False)
            if anthropic_prompt_cache_policy(stub)[0]:
                ttl = configured_cache_ttl()
                if raw == 'auto': ttl = auto_cache_ttl_for_source(session_source_for(platform))
                return {'5m':300, '1h':3600}.get(effective_cache_ttl(ttl, model=model, provider=provider)), 'hermes'
        except Exception:
            pass
    if provider == 'openai' or host == 'api.openai.com' or model.startswith('openai/'):
        match = re.search(r'gpt-(\d+)\.(\d+)|gpt-(\d+)(?:-|$)', model.lower())
        if match and (int(match[1] or match[3]) >= 6 or (int(match[1] or match[3]) == 5 and int(match[2] or 0) >= 6)):
            return 1800, 'openai'
    rows = json.loads(store.get_meta('cache_routes') or '{}') if store else {}
    learned = rows.get(route, rows.get(f'{provider}|{model}|{host}', {}))
    warm, cold = learned.get('max_warm_gap'), learned.get('min_cold_gap')
    return (warm, 'learned') if warm is not None and cold is not None and warm < cold else (None, 'unknown')


def touch(store, session, *, started_at, route, prompt, read, api_mode=''):
    if started_at is None: return
    ratio = read/prompt if prompt else 0
    if session.last_cache_touch_at is not None and started_at <= session.last_cache_touch_at: return
    with store.atomic():
        if not store.cas_session(session.session_key, {'last_cache_touch_at':session.last_cache_touch_at},
            last_cache_touch_at=started_at, last_route=route, last_prompt_tokens=prompt, last_cache_hit_ratio=ratio, last_cache_api_mode=api_mode): return
        if session.last_route == route and session.last_cache_touch_at is not None and prompt:
            gap = min(started_at-session.last_cache_touch_at, 30*86400)
            routes = json.loads(store.get_meta('cache_routes') or '{}')
            row = routes.pop(route, {})
            if ratio >= .5: row['max_warm_gap'] = max(row.get('max_warm_gap',0), gap)
            elif ratio < .1: row['min_cold_gap'] = min(row.get('min_cold_gap',float('inf')), gap)
            routes[route] = row
            store.set_meta('cache_routes',json.dumps(dict(list(routes.items())[-32:])))


def ttl_for_session(store, session):
    return cache_ttl_for(session.last_route, store=store, platform=session.platform, api_mode=session.last_cache_api_mode) if session else (None,'unknown')


def interval(wake, seconds, ttl, floor=0):
    floor = max(floor, wake.pacing_min_s or 0)
    if agent_authored(wake) and wake.kind in {'every','self'} and not wake.trigger_json.get('at') and ttl:
        edge = ttl-max(30,.05*ttl)
        if edge < seconds <= 1.25*ttl and edge >= floor: return edge
    return seconds


def cost(route, *, prompt=0, read=0, written=0, out=0, ttl=None):
    try:
        from agent.usage_pricing import CanonicalUsage, get_pricing_entry, estimate_usage_cost
    except ImportError:
        return None
    provider, model, part = (route or '||').split('|',2)
    base = _base(part) if part else ''
    entry = get_pricing_entry(model, provider=provider, base_url=base)
    if not entry: return None
    usage = CanonicalUsage(input_tokens=max(0,prompt-read-written), cache_read_tokens=read,
        cache_write_tokens=written if entry.cache_write_cost_per_million is not None else 0, output_tokens=out)
    if written and entry.cache_write_cost_per_million is None:
        usage = CanonicalUsage(input_tokens=usage.input_tokens+written, cache_read_tokens=read, output_tokens=out)
    amount = estimate_usage_cost(model,usage,provider=provider,base_url=base).amount_usd
    if amount is None: return None
    correction = written*(2*float(entry.input_cost_per_million)-float(entry.cache_write_cost_per_million))/1e6 if ttl==3600 and 'claude' in model.lower() and entry.cache_write_cost_per_million is not None else 0
    return float(amount)+correction


def minutes(seconds):
    text=f'{seconds/60:.1f}'
    return (text[:-2] if text.endswith('.0') else text)+'m'


def line(store, session, now, *, seconds=None, floor=0, watcher=False, pricing=True, first_wake=False):
    try:
        if not session or not session.last_route: return ''
        ttl, source = ttl_for_session(store,session)
        provider = session.last_route.split('|')[0][:24]
        age = max(0,now-(session.last_cache_touch_at if session.last_cache_touch_at is not None else now))/60
        if not ttl:
            if watcher: return f'Cache: {provider} TTL {source} · last touch {age:.0f}m ago'
            route_provider, model, part = session.last_route.split('|', 2)
            host = urlsplit(_base(part)).hostname or ''
            rows = json.loads(store.get_meta('cache_routes') or '{}')
            row = rows.get(session.last_route, rows.get(f'{route_provider}|{model}|{host}', {}))
            bracket = ', '.join(f'{label}{minutes(row[key])}' for key,label in (('max_warm_gap','warm seen ≤'),('min_cold_gap','cold ≥')) if key in row)
            return f'Cache: {provider} TTL {source}'+(f' ({bracket})' if bracket else '')
        label = f'Cache: {provider} {"1h" if ttl==3600 else minutes(ttl)} · last touch {age:.0f}m ago'
        if watcher: return label
        prefix = session.last_prompt_tokens
        warm = cost(session.last_route,prompt=prefix,read=prefix,ttl=ttl) if pricing else None
        cold = cost(session.last_route,prompt=prefix,written=prefix,ttl=ttl) if pricing else None
        gap = age*60 if seconds is None else seconds
        if first_wake and seconds is not None: gap += age*60
        state = 'warm' if gap < ttl else 'cold'
        price = (f' (~${cold:.2f})' if state=='cold' else f' (~${warm:.2f}) · cold ~${cold:.2f}') if warm is not None and cold is not None else ''
        edge = ttl-max(30,.05*ttl)
        next_hint = 'floor ≥ TTL: wakes cold' if floor>edge else f'next warm ≤{minutes(edge)}'
        text = f'{label} · {"this wake" if seconds is None else minutes(seconds)} {state}{price} · {next_hint}'
        return text[:220] if len(text.split())<=40 else ''
    except Exception:
        return ''


def display(counts):
    if counts.get('warm_wakes',0)+counts.get('cold_wakes',0) and not counts.get('unpriced_calls',0):
        return f'warm {counts.get("warm_wakes",0)} / cold {counts.get("cold_wakes",0)} · ~${counts.get("cost_microusd",0)/1e6:.2f}'
    return 'in / cached / written / out: '+ ' / '.join(str(counts.get(k,0)) for k in ('tokens_in','cache_read_tokens','cache_write_tokens','tokens_out'))


# Keep-warm (#163): a ping pays one cache read of the chat's prefix to spare the next turn a cold rewrite.
KEEP_WARM_MIN_TTL = 1800  # On shorter caches the pings cost more than the cold starts they prevent.
KEEP_WARM_COLD_MARGIN = 60  # Never start a ping this close to the cache's end: a late ping is a cold write.


def warm_lifetime(store, session):
    """The cache lifetime a keep-warm ping can rely on: the route's known TTL, else the longest gap this route stayed warm."""
    ttl, source = ttl_for_session(store, session)
    if ttl: return ttl, source
    if not session or not session.last_route: return None, 'unknown'
    provider, model, part = session.last_route.split('|', 2)
    host = (urlsplit(_base(part)).hostname or '') if part else ''
    rows = json.loads(store.get_meta('cache_routes') or '{}')
    row = rows.get(session.last_route, rows.get(f'{provider}|{model}|{host}', {}))
    warm, cold = row.get('max_warm_gap'), row.get('min_cold_gap')
    # A warm gap is a lower bound on the lifetime, unless a shorter gap was already seen cold.
    if warm and (cold is None or warm < cold): return warm, 'warm_seen'
    return None, 'unknown'


def keep_warm_plan(store, session, policy, now):
    """When this chat's next keep-warm ping is due, or why there is none. Pure: reads the session and route rows only."""
    if session is None or session.last_cache_touch_at is None or not session.last_route:
        return {'reason': 'waiting for the first model call in this chat'}
    touch = session.last_cache_touch_at
    ttl, source = warm_lifetime(store, session)
    if not ttl:
        return {'reason': "this route's cache lifetime isn't known yet", 'touch': touch}
    if ttl < KEEP_WARM_MIN_TTL:
        return {'reason': f'the cache lasts {minutes(ttl)}; keep-warm needs 30m or more', 'touch': touch, 'ttl': ttl}
    if (session.last_prompt_tokens or 0) < policy.keep_warm_min_tokens:
        return {'reason': f'the context is small ({session.last_prompt_tokens or 0} tokens), so a cold start is cheap', 'touch': touch, 'ttl': ttl}
    if session.last_human_at is None or now - session.last_human_at >= policy.keep_warm_idle_hours * 3600:
        return {'reason': f'resting: no message from you in {policy.keep_warm_idle_hours}h; it resumes when you write', 'touch': touch, 'ttl': ttl}
    cold_at = touch + ttl - KEEP_WARM_COLD_MARGIN
    if now >= cold_at:
        return {'reason': 'the cache has gone cold; it resumes after the next model call', 'touch': touch, 'ttl': ttl}
    return {'due_at': touch + ttl - max(120, .08 * ttl), 'cold_at': cold_at, 'touch': touch, 'ttl': ttl, 'source': source}


def keep_warm_key(touch):
    """One ping per cache touch: a real turn moves the touch, and the ping it would have needed is never made."""
    return f'k:{touch:.17g}'
