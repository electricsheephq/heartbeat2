"""Deterministic value accounting, bounded backoff and human-turn-only cards.

The no-op digest idea follows Hermes hermes_cli/loops.py::_digest_response;
all value and pause decisions here use structured occurrence fields.
"""
from .model import RECURRING_KINDS
from .schedule import local_day, next_cron, next_every

VALUABLE = {'progress','done','needs_attention'}


def value(engine,o):
    if engine.store.get_meta('ignored:'+o.id) or o.force_silent or not o.notify:
        return 0
    if engine.store.get_meta('engaged:'+o.id):
        return int(o.delivered)
    return int(o.outcome in VALUABLE)


def pause(engine,wake,rule):
    s=engine.store
    current=s.get_wake(wake.id)
    if current.status!='active':
        return
    if s.cas_wake(current.id,current.status,expected_version=current.version,
                  status='paused',status_reason=f'governor:{rule}'):
        s.set_meta('card:'+current.id,rule)
        for o in s.list_occurrences(current.id,state='started'):
            if o.outcome is None:
                s.update_occurrence(o.wake_id,o.dedupe_key,'started',attempt=o.attempt,force_silent=True)


def owner_answer(engine,wake,answer):
    """User /pulse card-answer seam; ordinary tool resume never resets history."""
    s,p=engine.store,engine.policy
    with s.atomic():
        w=s.get_wake(wake.id)
        if w.status!='paused' or not w.status_reason.startswith('governor:'):
            return {'error':'not_governor_paused'}
        if answer not in {'keep','slow'}: return {'error':'unknown_answer'}
        updates={'status':'active','status_reason':''}
        interval=w.eff_interval_s or w.base_interval_s or p.interval_floor(w)
        if answer=='keep':
            updates.update(noop_streak=0,ignored_streak=0,error_streak=0)
            interval=max(w.base_interval_s or p.interval_floor(w),p.interval_floor(w))
        else:
            interval*=2
            if w.pacing_max_s is not None: interval=min(interval,w.pacing_max_s)
            updates['base_interval_s']=max(w.base_interval_s or p.interval_floor(w),interval)
        if w.kind in RECURRING_KINDS:
            updates['eff_interval_s']=interval
            updates['next_due_at']=(next_cron(w.trigger_json['cron'],now=engine.clock.now(),timezone=p.resolved_timezone(),minimum_interval_s=interval)
                if w.kind=='cron' else next_every(engine.clock.now(),interval,anchor=w.trigger_json.get('at') if w.kind=='every' else None,timezone=p.resolved_timezone()))
        if not s.cas_wake(w.id,'paused',expected_version=w.version,**updates): return {'error':'concurrent_change'}
        if answer=='keep': s.set_meta('ratio_since:'+w.id,str(engine.clock.now()))
        s.set_meta('card:'+w.id,'')
        engine.signal()
        return {'ok':True}


def apply(engine,wake,*,next_check=None):
    s,p=engine.store,engine.policy
    w=s.get_wake(wake.id)
    if w.status!='active': return
    k=max(0,w.noop_streak-(p.noop_slowdown_after-1))
    updates={}
    if w.kind in RECURRING_KINDS:
        floor=max(p.interval_floor(w),w.pacing_min_s or 0)
        ceiling=min(p.noop_max_backoff_hours*3600,w.pacing_max_s or float('inf'))
        # Saturate the exponent before computing it; arbitrary stale streaks
        # cannot overflow the scheduling math.
        backoff=min(ceiling,(w.base_interval_s or floor)*p.noop_backoff_factor**min(k,32))
        backoff=max(floor,backoff)
        eff=backoff
        if w.kind=='self' and next_check is not None:
            eff=max(next_check,backoff) if k else max(next_check,floor)
            eff=min(eff,w.pacing_max_s or float('inf'))
        elif next_check is not None and w.kind!='cron':
            eff=min(max(next_check,backoff),w.pacing_max_s or float('inf'))
        updates['eff_interval_s']=eff
        if w.kind=='cron':
            due=next_cron(w.trigger_json['cron'],now=engine.clock.now(),timezone=p.resolved_timezone(),
                          minimum_interval_s=w.base_interval_s or 0)
            skips=p.noop_backoff_factor**min(k,16)-1
            # A bounded number of cron times is materialised; a larger backoff
            # jumps the cron search boundary rather than burning a long loop.
            if skips>256:
                due=next_cron(w.trigger_json['cron'],now=engine.clock.now(),timezone=p.resolved_timezone(),minimum_interval_s=eff)
            else:
                for _ in range(skips):
                    s.add_occurrence(w.id,f't:{due:.17g}',state='skipped')
                    s.connection.execute("UPDATE occurrences SET error='governor' WHERE wake_id=? AND dedupe_key=? AND state='skipped'",(w.id,f't:{due:.17g}'))
                    due=next_cron(w.trigger_json['cron'],now=due,timezone=p.resolved_timezone(),minimum_interval_s=w.base_interval_s or 0)
                if skips: engine.ledger(w,governor_skips=skips)
            updates['next_due_at']=due
        else:
            updates['next_due_at']=next_every(engine.clock.now(),eff,
                anchor=w.trigger_json.get('at') if w.kind=='every' else None,
                timezone=p.resolved_timezone(),jitter_unit=0 if w.trigger_json.get('at') else engine.jitter())
    if updates:
        s.cas_wake(w.id,w.status,expected_version=w.version,**updates)
        w=s.get_wake(w.id)
    since=float(s.get_meta('ratio_since:'+w.id) or '-inf')
    history=[o for o in s.list_occurrences(w.id) if o.state=='done' and o.ended_at is not None and o.ended_at>since][-20:]
    rule=('noop' if w.noop_streak>=p.noop_pause_after else
          'ignored' if w.ignored_streak>=p.ignored_pause_after else
          'error' if w.error_streak>=p.error_pause_after else
          'ratio' if len(history)>=12 and sum(value(engine,o) for o in history)/len(history)<.1 else None)
    if rule: pause(engine,w,rule)


def finish(engine,wake,occurrence):
    s=engine.store
    with s.atomic():
        if s.get_meta('governed:'+occurrence.id): return
        s.set_meta('governed:'+occurrence.id,'1')
        w=s.get_wake(wake.id)
        useful=value(engine,occurrence)
        s.cas_wake(w.id,w.status,expected_version=w.version,
            noop_streak=0 if useful else w.noop_streak+1,
            error_streak=0,delivered=w.delivered+int(occurrence.delivered),
            tokens_in=w.tokens_in+occurrence.tokens_in,tokens_out=w.tokens_out+occurrence.tokens_out)
        engine.ledger(w,noops=1-useful,valuable=useful,delivered=int(occurrence.delivered),
                      tokens_in=occurrence.tokens_in,tokens_out=occurrence.tokens_out)
        w=s.get_wake(w.id)
        apply(engine,w,next_check=occurrence.next_check_s)
        history=[o for o in s.list_occurrences(w.id) if o.state=='done']
        babysit_done=w.babysit and (occurrence.notify or
            len(history)>=3 and all(o.outcome=='no_change' for o in history[-3:]))
        if babysit_done or w.kind=='at' or occurrence.outcome=='done':
            w=s.get_wake(w.id)
            if w.status=='active': s.cas_wake(w.id,'active',status='done',status_reason='completed')


def error(engine,wake):
    s=engine.store
    with s.atomic():
        w=s.get_wake(wake.id)
        s.cas_wake(w.id,w.status,expected_version=w.version,error_streak=w.error_streak+1)
        engine.ledger(w,errors=1)
        w=s.get_wake(w.id)
        if w.error_streak>=engine.policy.error_pause_after: pause(engine,w,'error')


def engagement(engine,o):
    s=engine.store
    with s.atomic():
        if s.get_meta('engaged:'+o.id) or s.get_meta('ignored:'+o.id): return
        s.set_meta('engaged:'+o.id,'1')
        w=s.get_wake(o.wake_id)
        s.cas_wake(w.id,w.status,expected_version=w.version,engaged=w.engaged+1,noop_streak=0,ignored_streak=0)
        engine.ledger(w,engaged=1)
        engine.evaluate(s.get_wake(w.id),lambda:apply(engine,s.get_wake(w.id)))


def settle_ignored(engine):
    s=engine.store
    with s.atomic():
        for o in s.list_occurrences(state='done'):
            if not o.delivered or o.ended_at is None or engine.clock.now()-o.ended_at<=engine.policy.engagement_window_hours*3600:
                continue
            if s.get_meta('engaged:'+o.id) or s.get_meta('ignored:'+o.id): continue
            s.set_meta('ignored:'+o.id,'1')
            w=s.get_wake(o.wake_id)
            s.cas_wake(w.id,w.status,expected_version=w.version,ignored_streak=w.ignored_streak+1)
            engine.ledger(w,ignored=1)
            engine.evaluate(s.get_wake(w.id),lambda:apply(engine,s.get_wake(w.id)))


def hint(engine,wake):
    if wake.noop_streak==engine.policy.suggest_gate_after:
        return 'Consider a file|process|url|command watcher via pulse update, push next_check out, or cancel.'
    return ''
