"""Deterministic value accounting, bounded backoff and human-turn-only cards.

The no-op digest idea follows Hermes hermes_cli/loops.py::_digest_response;
all value and pause decisions here use structured occurrence fields.
"""
from dataclasses import replace
import logging
from .model import RECURRING_KINDS, PROBE_KINDS
from .policy import cap_counts, is_keep_warm
from .schedule import local_day, next_cron, next_every, parse_duration

VALUABLE = {'progress','done','needs_attention'}


def value(engine,o):
    # Ignored deliveries retain their existing ratio/engagement semantics.
    if engine.store.get_meta('ignored:'+o.id):
        return 0
    if o.outcome in VALUABLE and o.summary.strip():
        return 1
    if o.force_silent or not o.notify:
        return 0
    if engine.store.get_meta('engaged:'+o.id):
        return int(o.delivered)
    return int(o.delivered and o.outcome in VALUABLE)


def pause(engine,wake,rule):
    s=engine.store
    current=s.get_wake(wake.id)
    if current.status!='active':
        return
    if s.cas_wake(current.id,current.status,expected_version=current.version,
                  status='paused',status_reason=f'governor:{rule}'):
        cache_line=engine.tool.cache_result(current)
        written=rule+(' · '+cache_line if cache_line else '')
        s.set_meta('card:'+current.id,written)
        paused=s.get_wake(current.id)
        def price_card():
            # Display only: a failure keeps the unpriced card and never reaches the committed tick.
            try:
                label=engine.tool.cache_result(paused)
                with s.atomic():
                    latest=s.get_wake(paused.id)
                    # An owner turn may have shown and cleared the card meanwhile: only replace the card this pause wrote.
                    if latest.version==paused.version and latest.status=='paused' and s.get_meta('card:'+paused.id)==written:
                        s.set_meta('card:'+paused.id,rule+(' · '+label if label else ''))
            except Exception:
                logging.getLogger('heartbeat2').warning('governor card pricing failed; the card stays unpriced', exc_info=True)
        s.after_commit(price_card)
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
        if answer=='slow' and w.kind not in RECURRING_KINDS|PROBE_KINDS:
            return {'error':'slow applies to recurring wakes and watchers'}
        updates={'status':'active','status_reason':''}
        interval=w.eff_interval_s or w.base_interval_s or p.interval_floor(w)
        if answer=='keep':
            updates.update(noop_streak=0,ignored_streak=0,error_streak=0)
            interval=max(w.base_interval_s or p.interval_floor(w),p.interval_floor(w))
        else:
            interval*=2
            if w.pacing_max_s is not None: interval=min(interval,w.pacing_max_s)
            from .cache import interval as snap
            interval=snap(w,interval,engine.timing(w)['cache_ttl'],engine.timing(w)['minimum_s'])
            updates['base_interval_s']=max(w.base_interval_s or p.interval_floor(w),interval)
        if w.kind in RECURRING_KINDS:
            updates['eff_interval_s']=interval
            updates['next_due_at']=(next_cron(w.trigger_json['cron'],now=engine.clock.now(),timezone=p.resolved_timezone(),minimum_interval_s=interval)
                if w.kind=='cron' else next_every(engine.clock.now(),interval,anchor=w.trigger_json.get('at') if w.kind=='every' else None,timezone=p.resolved_timezone(),**engine.timing(w)))
        if answer=='slow' and w.kind in PROBE_KINDS:
            interval=max(p.probe_floor_seconds,parse_duration(w.trigger_json.get('check_every',p.probe_floor_seconds)))
            interval=max(interval,min(interval*2,p.noop_max_backoff_hours*3600,w.pacing_max_s or float('inf')))
            updates.update(trigger_json={**w.trigger_json,'check_every':interval},next_due_at=engine.clock.now()+interval)
        candidate=engine.tool.feasible(replace(w,**updates))
        updates['expires_at']=candidate.expires_at
        if not s.cas_wake(w.id,'paused',expected_version=w.version,**updates): return {'error':'concurrent_change'}
        if answer=='keep': s.set_meta('ratio_since:'+w.id,str(engine.clock.now()))
        s.set_meta('card:'+w.id,'')
        engine.signal()
        return {'ok':True,'cadence_s':interval,'cache':engine.cache_line(w,seconds=interval)} if answer=='slow' else {'ok':True}


def apply(engine,wake,*,next_check=None,not_after=None):
    s,p=engine.store,engine.policy
    w=s.get_wake(wake.id)
    if w.status!='active': return
    k=max(0,w.noop_streak-(p.noop_slowdown_after-1))
    updates={}
    if w.kind in RECURRING_KINDS:
        floor=max(p.interval_floor(w),w.pacing_min_s or 0)
        ceiling=max(min(p.noop_max_backoff_hours*3600,w.pacing_max_s or float('inf')),w.base_interval_s or 0)
        # Saturate the exponent before computing it; arbitrary stale streaks
        # cannot overflow the scheduling math.
        backoff=min(ceiling,(w.base_interval_s or floor)*p.noop_backoff_factor**min(k,32))
        backoff=max(floor,backoff)
        ttl=engine.timing(w)['cache_ttl']
        edge=ttl-max(30,.05*ttl) if ttl else None
        if edge is not None and floor<=edge and (w.base_interval_s or floor)<=edge: backoff=min(backoff,edge)
        eff=backoff
        if w.kind=='self' and next_check is not None:
            eff=max(next_check,backoff) if k else max(next_check,floor)
            eff=min(eff,w.pacing_max_s or float('inf'))
        elif next_check is not None and w.kind!='cron':
            eff=min(max(next_check,backoff),w.pacing_max_s or float('inf'))
        from .cache import interval
        eff=interval(w,eff,ttl,floor)
        updates['eff_interval_s']=eff
        if w.kind=='cron':
            due=next_cron(w.trigger_json['cron'],now=engine.clock.now(),timezone=p.resolved_timezone(),
                          minimum_interval_s=w.base_interval_s or 0)
            skips=p.noop_backoff_factor**min(k,16)-1
            kept,added=set(),0
            # A bounded number of cron times is materialised; a larger backoff
            # jumps the cron search boundary rather than burning a long loop.
            if skips>256:
                due=next_cron(w.trigger_json['cron'],now=engine.clock.now(),timezone=p.resolved_timezone(),minimum_interval_s=eff)
            else:
                for _ in range(skips):
                    key=f't:{due:.17g}'
                    kept.add(key)
                    if s.add_occurrence(w.id,key,state='skipped'):
                        s.connection.execute("UPDATE occurrences SET error='governor' WHERE wake_id=? AND dedupe_key=? AND state='skipped'",(w.id,key))
                        added+=1
                    due=next_cron(w.trigger_json['cron'],now=due,timezone=p.resolved_timezone(),minimum_interval_s=w.base_interval_s or 0)
            # A re-apply (an owner engaging, an ignored delivery settling) recomputes the backoff: governor skips of future
            # cron times it no longer makes are removed, or the first of them would swallow the time it now fires at (#161).
            now=engine.clock.now()
            stale=[o.dedupe_key for o in s.list_occurrences(w.id,state='skipped')
                   if o.error=='governor' and o.dedupe_key.startswith('t:') and o.dedupe_key not in kept and float(o.dedupe_key[2:])>now]
            for key in stale:
                s.connection.execute("DELETE FROM occurrences WHERE wake_id=? AND dedupe_key=? AND state='skipped' AND error='governor'",(w.id,key))
            if added: engine.ledger(w,governor_skips=added)
            # A cron time the wake was already due at stays: an owner reply only ever brings a wake sooner.
            if not_after is not None and not kept: due=min(due,not_after)
            updates['next_due_at']=due
        else:
            updates['next_due_at']=next_every(engine.clock.now(),eff,
                anchor=w.trigger_json.get('at') if w.kind=='every' else None,
                timezone=p.resolved_timezone(),jitter_unit=0 if w.trigger_json.get('at') else engine.jitter(),**engine.timing(w))
            if not_after is not None: updates['next_due_at']=min(updates['next_due_at'],not_after)
    if updates:
        s.cas_wake(w.id,w.status,expected_version=w.version,**updates)
        w=s.get_wake(w.id)
    rule=pause_rule(engine,w)
    if rule: pause(engine,w,rule)


def pause_rule(engine,w):
    """The governor rule a wake's current streaks and value history break, if any."""
    s,p=engine.store,engine.policy
    since=float(s.get_meta('ratio_since:'+w.id) or '-inf')
    history=[o for o in s.list_occurrences(w.id) if o.state=='done' and o.ended_at is not None and o.ended_at>since][-20:]
    return ('noop' if w.noop_streak>=p.noop_pause_after else
            'ignored' if w.ignored_streak>=p.ignored_pause_after else
            'error' if w.error_streak>=p.error_pause_after else
            'ratio' if len(history)>=12 and sum(value(engine,o) for o in history)/len(history)<.1 else None)


def finish(engine,wake,occurrence):
    s=engine.store
    with s.atomic():
        if s.get_meta('governed:'+occurrence.id): return
        s.set_meta('governed:'+occurrence.id,'1')
        w=s.get_wake(wake.id)
        if is_keep_warm(w):
            # A keep-warm ping is a silent cache read, not work: no value, no idle streak, no backoff (#163).
            counts=engine.usage_counts(occurrence)
            counts['no_verdict']=0
            s.cas_wake(w.id,w.status,expected_version=w.version,error_streak=0,
                **{k:getattr(w,k)+v for k,v in counts.items()})
            engine.ledger(w,**counts)
            return
        useful=value(engine,occurrence)
        s.cas_wake(w.id,w.status,expected_version=w.version,
            noop_streak=0 if useful else w.noop_streak+1,
            error_streak=0,delivered=w.delivered+int(occurrence.delivered),
            **{k:getattr(w,k)+v for k,v in engine.usage_counts(occurrence).items()})
        engine.ledger(w,noops=1-useful,valuable=useful,delivered=int(occurrence.delivered),
                      **engine.usage_counts(occurrence))
        w=s.get_wake(w.id)
        if engine.end_max_fires(w): return
        apply(engine,w,next_check=occurrence.next_check_s)
        history=[o for o in s.list_occurrences(w.id) if o.state=='done']
        babysit_done=w.babysit and (occurrence.notify or
            len(history)>=3 and all(o.outcome=='no_change' for o in history[-3:]))
        if babysit_done or w.kind=='at' or occurrence.outcome=='done':
            w=s.get_wake(w.id)
            if w.status in {'active','paused'}: s.cas_wake(w.id,w.status,status='done',status_reason='completed')


def error(engine,wake):
    s=engine.store
    with s.atomic():
        w=s.get_wake(wake.id)
        s.cas_wake(w.id,w.status,expected_version=w.version,error_streak=w.error_streak+1)
        engine.ledger(w,errors=1)
        w=s.get_wake(w.id)
        if w.error_streak>=engine.policy.error_pause_after: pause(engine,w,'error')


def engagement(engine,o,*,next_check=None,late=False):
    """Credit an owner reply to a delivered occurrence.

    late=True is an owner message after the occurrence finished (Intake.human). Its turn already set the schedule
    with the next check the agent asked for, so the credit only undoes what the idle streaks did: a backoff in
    effect, or a pause the owner has not seen yet. Without a backoff the agent's pace stands untouched (#161)."""
    s,p=engine.store,engine.policy
    with s.atomic():
        if s.get_meta('engaged:'+o.id) or s.get_meta('ignored:'+o.id): return
        s.set_meta('engaged:'+o.id,'1')
        w=s.get_wake(o.wake_id)
        backoff=w.noop_streak>=p.noop_slowdown_after
        s.cas_wake(w.id,w.status,expected_version=w.version,engaged=w.engaged+1,noop_streak=0,ignored_streak=0)
        engine.ledger(w,engaged=1)
        if not late:
            engine.evaluate(s.get_wake(w.id),lambda:apply(engine,s.get_wake(w.id),next_check=next_check))
        elif s.get_meta('governed:'+o.id):  # Not yet governed: finish applies the schedule with this credit counted.
            engine.evaluate(s.get_wake(w.id),lambda:_late(engine,w.id,backoff))


def _late(engine,wake_id,backoff):
    s=engine.store
    w=s.get_wake(wake_id)
    # A pause on idle streaks the owner hasn't been shown yet, and that no rule supports now, is lifted: the owner
    # engaging is the answer to it. A pause already shown stays for /pulse keep|slow|stop; error pauses always stay.
    lifted=False
    if (w.status=='paused' and w.status_reason in {'governor:noop','governor:ignored','governor:ratio'}
            and s.get_meta('card:'+w.id) and pause_rule(engine,w) is None
            # The slot it freed may have been taken meanwhile: never lift a pause past the active-wake limits.
            and (w.trigger_json.get('builtin') or engine.policy.cap_gate(*cap_counts(s,w.session_key)).allowed)):
        if s.cas_wake(w.id,'paused',expected_version=w.version,status='active',status_reason=''):
            s.set_meta('card:'+w.id,'')
            backoff=lifted=True
            w=s.get_wake(w.id)
    if w.status!='active' or not backoff or w.kind not in RECURRING_KINDS: return
    done=[o for o in s.list_occurrences(w.id,state='done') if o.ended_at is not None]
    latest=max(done,key=lambda o:o.ended_at) if done else None
    now=engine.clock.now()
    # The agent's own next check, counted from now, and never later than the wake was already due: an overdue wake
    # stays due. A lifted pause's old due time is stale (paused wakes aren't scheduled), so it doesn't fire at once.
    stale=w.next_due_at is None or lifted and w.next_due_at<=now
    apply(engine,w,next_check=latest.next_check_s if latest else None,not_after=None if stale else w.next_due_at)


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
    if wake.noop_streak!=engine.policy.suggest_gate_after: return ''
    # Cron follows its expression (next_check is ignored there), and a role stays recurring: offer only what works.
    pace=('change its cron schedule via pulse update' if wake.kind=='cron' else
          'push next_check out' if wake.kind in RECURRING_KINDS else '')
    if wake.trigger_json.get('builtin') and not wake.trigger_json.get('own_time'):
        return ''  # The check-in is settings-owned: none of these edits are the agent's to make.
    if wake.trigger_json.get('own_time'):
        # Own time is settings-owned: ending it or changing its rhythm is the owner's call.
        return 'Your own time keeps finding nothing: push next_check out or rest; the owner sets how often own time comes.'
    if wake.role:
        return (f'This role keeps finding nothing: {pace}, lower its budget, arm a separate watcher for what it waits on, '
                'or end it with outcome=done.')
    return f'Consider a file|process|url|command watcher via pulse update, {pace + ", " if pace else ""}or cancel.'
