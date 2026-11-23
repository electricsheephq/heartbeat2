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


# A Claude prompt cache lives at least 5 minutes on every provider, so on a Claude model a miss after a shorter gap means
# the prompt changed (compression, a model or tool switch), not that the cache expired, and says nothing about the
# lifetime (#203). Other routes may evict early (local LRU caches), so every miss counts there.
COLD_GAP_FLOOR = 300


def _floor(route):
    model = (route or '||').split('|', 2)[1]
    return COLD_GAP_FLOOR if 'claude' in model.lower() else 0


def _gaps(row, route):
    """A route row's longest warm gap and shortest cold gap, without colds that can't be expiry on that route."""
    cold = row.get('min_cold_gap')
    return row.get('max_warm_gap'), (cold if cold is not None and cold >= _floor(route) else None)


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
    warm, cold = _gaps(learned, route)
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
            if ratio >= .5:
                row['max_warm_gap'] = max(row.get('max_warm_gap',0), gap)
                # The cache survived a real gap: cold pings before this were changed prompts, not a lifetime (#228).
                if gap > KEEP_WARM_RESET_GAP: clear_cold_pings(store, session.session_key, route)
            elif ratio < .1 and gap >= _floor(route):
                # A stored cold below the floor (recorded before #203) must not hide this real one.
                row['min_cold_gap'] = min(_gaps(row, route)[1] or float('inf'), gap)
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
            bracket = ', '.join(f'{label}{minutes(gap)}' for gap,label in zip(_gaps(row,session.last_route),('warm seen ≤','cold ≥')) if gap is not None)
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


KEEP_WARM_COLD_PINGS = 2  # Pings in a row that found the cache cold before keep-warm stops pinging (#228).
KEEP_WARM_RESET_GAP = 900  # A warm reuse after this long clears that count; gaps run from call start, so leave margin (review).
CLAUDE_MAX_TTL = 3600


def _cold_key(session_key):
    return 'keep_warm_cold:'+session_key


def _cold_state(store, session_key):
    """{route: cold pings in a row} for one chat."""
    try:
        state = json.loads(store.get_meta(_cold_key(session_key)) or '{}')
        return {str(k): int(v) for k, v in state.items()} if isinstance(state, dict) else {}
    except (ValueError, TypeError):
        return {}


def cold_pings(store, session_key, route):
    """Pings in a row on this chat's route whose first model call found the cache cold."""
    return _cold_state(store, session_key).get(route, 0)


def note_cold_ping(store, session_key, route):
    state = _cold_state(store, session_key)
    state[route] = state.get(route, 0) + 1
    store.set_meta(_cold_key(session_key), json.dumps(dict(list(state.items())[-8:])))


def clear_cold_pings(store, session_key, route=None):
    """A warm reuse on a route clears that route's count; route None (the owner's /pulse warm on) clears every route."""
    state = _cold_state(store, session_key)
    if route is None: state = {}
    elif route in state: del state[route]
    else: return
    store.set_meta(_cold_key(session_key), json.dumps(state) if state else '')


def warm_lifetime(store, session):
    """The cache lifetime a keep-warm ping can rely on: the route's known TTL, else the longest gap this route stayed warm."""
    ttl, source = ttl_for_session(store, session)
    # A lifetime learned from gaps on a Claude route is still capped at the hour (review): gaps run long by call time.
    # A Claude cache lasts 5 minutes or 1 hour, so a learned lifetime past 5 minutes is the hour (#234).
    if ttl and source == 'learned' and _floor(session.last_route):
        return (CLAUDE_MAX_TTL if ttl > COLD_GAP_FLOOR else ttl), source
    if ttl or source == 'off': return ttl, source  # Caching turned off: old warm gaps say nothing now.
    if not session or not session.last_route: return None, 'unknown'
    provider, model, part = session.last_route.split('|', 2)
    host = (urlsplit(_base(part)).hostname or '') if part else ''
    rows = json.loads(store.get_meta('cache_routes') or '{}')
    row = rows.get(session.last_route, rows.get(f'{provider}|{model}|{host}', {}))
    warm, cold = _gaps(row, session.last_route)
    # A Claude cache lasts 5 minutes or 1 hour. Staying warm past 5 minutes proves the hour, so a shorter miss was a changed
    # prompt (a compaction, a rebuilt system prompt), not expiry (#228). Cold pings in a row stop it instead (keep_warm_plan).
    # Capped at the hour: a measured gap runs long by call time and retries, and a ping after the hour lands cold (review).
    # Staying warm past 5 minutes proves the hour, so the lifetime is the hour, not the measured gap (#234): a new route
    # whose longest warm gap so far is 22 minutes still has a 1-hour cache. Cold pings in a row stop it if that is wrong.
    if warm and _floor(session.last_route) and warm > COLD_GAP_FLOOR: return CLAUDE_MAX_TTL, 'warm_seen'
    # Elsewhere a warm gap is a lower bound on the lifetime, unless a shorter gap was already seen cold.
    if warm and (cold is None or warm < cold): return warm, 'warm_seen'
    return None, 'unknown'


def _unknown_reason(store, session):
    """Why keep-warm can't tell this route's cache lifetime, from its learned gaps (#203)."""
    try:
        provider, model, part = session.last_route.split('|', 2)
        host = (urlsplit(_base(part)).hostname or '') if part else ''
        rows = json.loads(store.get_meta('cache_routes') or '{}')
        warm, cold = _gaps(rows.get(session.last_route, rows.get(f'{provider}|{model}|{host}', {})), session.last_route)
    except Exception:
        warm = cold = None
    if warm and cold:
        # A changed prompt reads as cold too, so this can't be settled from gaps alone (#203).
        return (f"this route's cache lifetime isn't known: it stayed warm across {minutes(warm)} but went cold after {minutes(cold)}. "
                "A changed prompt also reads as cold, so pings stay off here rather than pay for cold ones")
    return "this route's cache lifetime isn't known yet: no call has reused the cache after a gap"


def keep_warm_plan(store, session, policy, now):
    """When this chat's next keep-warm ping is due, or why there is none. Pure: reads the session and route rows only."""
    if session is None or session.last_cache_touch_at is None or not session.last_route:
        return {'reason': 'waiting for the first model call in this chat'}
    touch = session.last_cache_touch_at
    ttl, source = warm_lifetime(store, session)
    if source == 'off':
        return {'reason': 'prompt caching is off for this route', 'touch': touch, 'blocked': 'off'}
    if not ttl:
        return {'reason': _unknown_reason(store, session), 'touch': touch, 'blocked': 'unknown'}
    if ttl < KEEP_WARM_MIN_TTL:
        # A Claude cache is 5 minutes or 1 hour, so a shorter learned gap reads as the 5-minute cache, not "1m" (review).
        lasts = '5m or less' if _floor(session.last_route) and ttl <= COLD_GAP_FLOOR else minutes(ttl)
        return {'reason': f'the cache lasts {lasts}; keep-warm needs 30m or more', 'touch': touch, 'ttl': ttl, 'blocked': 'short'}
    if (session.last_prompt_tokens or 0) < policy.keep_warm_min_tokens:
        return {'reason': f'the context is small ({session.last_prompt_tokens or 0} tokens), so a cold start is cheap', 'touch': touch, 'ttl': ttl}
    if session.last_human_at is None or now - session.last_human_at >= policy.keep_warm_idle_hours * 3600:
        return {'reason': f'resting: no message from you in {policy.keep_warm_idle_hours}h; it resumes when you write', 'touch': touch, 'ttl': ttl}
    # Only where a Claude route's lifetime was learned from gaps (#228): a route that reports its TTL pings as before.
    learned = source in {'warm_seen', 'learned'} and _floor(session.last_route)
    cold = cold_pings(store, session.session_key, session.last_route) if learned else 0
    if cold >= KEEP_WARM_COLD_PINGS:
        return {'reason': f'the last {cold} pings found the cache cold, so pings stay off until a reply reuses the cache after '
                          'more than 15 minutes, or you run /pulse warm on', 'touch': touch, 'ttl': ttl, 'blocked': 'cold_pings'}
    cold_at = touch + ttl - KEEP_WARM_COLD_MARGIN
    if now >= cold_at:
        return {'reason': 'the cache has gone cold; it resumes after the next model call', 'touch': touch, 'ttl': ttl}
    return {'due_at': touch + ttl - max(120, .08 * ttl), 'cold_at': cold_at, 'touch': touch, 'ttl': ttl, 'source': source}


def keep_warm_key(touch):
    """One ping per cache touch: a real turn moves the touch, and the ping it would have needed is never made."""
    return f'k:{touch:.17g}'


def no_ping_reason(store, session, policy, now):
    """Why an active keep-warm has no ping due, for /pulse status, /pulse list and the agent's list."""
    try:
        return keep_warm_plan(store, session, policy, now).get('reason') or 'a ping time is set at the next check'
    except Exception:
        return '/pulse warm says why'
