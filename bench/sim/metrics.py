"""One evaluator for engine and emulator; bars come from recorded observations."""
from collections import defaultdict
import math
import re

SENTINELS=('[SILENT]','HEARTBEAT_OK','NO_REPLY','⟦pulse')


def percentile(values,p):
    if not values:return None
    values=sorted(values)
    return values[max(0,math.ceil(len(values)*p)-1)]


def latency_samples(turns):
    rows=[x for x in turns if x.get('valid',True)]
    return ([x.get('lateness_s',max(0,x['inject_t']-x['due_t'])) for x in rows],
            [x.get('deferred_s',0) for x in rows])


def compute(scenario,log,turns,humans,snapshot,pause_fires,cards):
    unprompted=[d for d in log if d['unprompted']]
    seen=set();duplicates=0
    for delivery in log:
        identity=(delivery.get('profile','main'),delivery['wake_id'],delivery['occurrence'])
        repeated=delivery['unprompted'] and identity in seen
        if delivery.get('terminal_before',False) or repeated:duplicates+=1
        if delivery['unprompted']:seen.add(identity)
    leaks=sum(any(x in d['text'] for x in SENTINELS) or bool(re.search(r'\{[^{}]*"(?:action|outcome)"\s*:\s*"(?:respond|no_change|progress|done|blocked|needs_attention)"',d['text'])) for d in log)
    wrong=sum(d['session_key']!=d['owner_session_key'] or d.get('profile')!=d.get('owner_profile') for d in log)
    sessions=defaultdict(list)
    for d in unprompted:sessions[(d.get('profile','main'),d['session_key'])].append(d['t'])
    episodes=defaultdict(int)
    for d in unprompted:
        # A human at the same instant may precede a prompted response; prompted
        # deliveries are already excluded above.
        boundaries=[h['t'] for h in humans if h['session_key']==d['session_key'] and h.get('profile','main')==d.get('profile','main') and h['t']<=d['t']]
        episodes[(d.get('profile','main'),d['session_key'],max(boundaries,default=-1))]+=1
    burst=max((sum(t-60<x<=t for x in times) for times in sessions.values() for t in times),default=0)
    required=scenario['expectations'].get('required_fires',[])
    missed=sum(not any(x['ref']==req['wake'] and x.get('valid',True) and req['window'][0]<=x['t']<=req['window'][1] for x in turns) for req in required)
    noops=sum(x['outcome']=='no_change' for x in turns)
    lateness,deferred=latency_samples(turns)
    metrics={'duplicate_deliveries':duplicates,'missed_required_fires':missed,
             'msgs_per_silence_episode':max(episodes.values(),default=0),'model_wakes':len(turns),
             'noop_model_wakes':noops,'noop_ratio':noops/len(turns) if turns else 0,
             'est_tokens':len(turns)*scenario.get('context_tokens',150000),
             'sentinel_leaks':leaks,'wrong_destination':wrong,'max_burst_per_min':burst,
             'lateness_p50_s':percentile(lateness,.5),'lateness_p95_s':percentile(lateness,.95),
             'deferred_p50_s':percentile(deferred,.5),'deferred_p95_s':percentile(deferred,.95),
             'deferred_total_s':sum(deferred),'latency_sample_count':len(lateness),
             'fires_until_pause':pause_fires}
    failures=[]
    for name in ('duplicate_deliveries','sentinel_leaks','wrong_destination','missed_required_fires'):
        if metrics[name]!=0:failures.append(f'{name}={metrics[name]} (required 0)')
    if burst>1:failures.append(f'max_burst_per_min={burst} (required <=1 per session)')
    expect=scenario['expectations']
    if expect.get('checkin') and metrics['msgs_per_silence_episode']>1:
        failures.append(f'msgs_per_silence_episode={metrics["msgs_per_silence_episode"]} (required <=1)')
    for ref in expect.get('noop_pause',[]):
        fires=pause_fires.get(ref)
        threshold=scenario.get('settings',{}).get('noop_pause_after',6)
        if fires is None or fires>threshold:failures.append(f'{ref}: fires_until_pause={fires} (required <= {threshold})')
    for banned in expect.get('forbidden_deliveries',[]):
        matches=[d for d in log if ('wake' not in banned or d['ref']==banned['wake']) and
                 ('window' not in banned or banned['window'][0]<=d['t']<=banned['window'][1]) and
                 ('text' not in banned or banned['text'] in d['text']) and
                 ('unprompted' not in banned or d['unprompted']==banned['unprompted'])]
        if matches:failures.append(f'forbidden_deliveries {banned}: {len(matches)}')
    wakes=[w for p in snapshot.values() for w in p['wakes']]
    for ref,bar in expect.get('bars',{}).items():
        rows=[d for d in log if d['ref']==ref and d['unprompted']]
        started=[t for t in turns if t['ref']==ref and t.get('valid',True)]
        prompted=[d for d in log if d['ref']==ref and d['prompted']]
        if 'prompted_deliveries' in bar and len(prompted)!=bar['prompted_deliveries']: failures.append(f'{ref}: prompted_deliveries={len(prompted)} (required {bar["prompted_deliveries"]})')
        if 'deliveries' in bar and len(rows)!=bar['deliveries']:failures.append(f'{ref}: deliveries={len(rows)} (required {bar["deliveries"]})')
        if 'model_wakes' in bar and len(started)!=bar['model_wakes']:failures.append(f'{ref}: model_wakes={len(started)} (required {bar["model_wakes"]})')
        if 'status' in bar:
            found=[w for w in wakes if w.get('ref')==ref]
            if len(found)!=1 or found[0]['status']!=bar['status']:failures.append(f'{ref}: status mismatch (required {bar["status"]})')
    if 'cards' in expect and len(cards)!=expect['cards']:failures.append(f'cards={len(cards)} (required {expect["cards"]})')
    return metrics,failures
