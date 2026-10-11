"""Wall-clock engine. tick() is explicit; only registration owns a timer thread.

Coalescing/claim/refund follows Hermes heartbeat.py; fingerprint gating follows
cron/monitor.py; estop and busy ordering follows gateway/run_goals.py.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import fcntl
import hashlib
import json
import logging
import math
import os
import random
import uuid

from .model import Wake, RECURRING_KINDS, TIME_KINDS, PROBE_KINDS, EVENT_KINDS
from .policy import MESSAGING_PLATFORMS
from .schedule import time_due, local_day, parse_duration, next_every
from . import governor

LOG = logging.getLogger('heartbeat2')
RETRY_DELAYS = (30,60,300,900,3600)


def check_paused():
    try:
        from agent.estop import check_paused as paused
        return paused('heartbeat2',LOG)
    except ImportError:
        return False
    except Exception:
        # An unreadable stop state must never authorize a claim.
        return True


class Poller:
    def __init__(self,ctx,store,policy,clock,*,paused=check_paused,jitter=None,
                 compression_tip=None,session_key_fn=None,probe_runner=None):
        self.ctx,self.store,self.policy,self.clock=ctx,store,policy,clock
        self.paused,self.jitter=paused,jitter or (lambda: random.uniform(-1,1))
        self.compression_tip=compression_tip or self._compression_tip
        from .intake import Intake
        from .tool import Tool,current_session_key
        self.intake=Intake(self)
        self.tool=Tool(self,session_key_fn or current_session_key)
        self.probe_runner=probe_runner or self._probe
        self.epoch=uuid.uuid4().hex
        self.log=LOG
        self.closed=False
        self.process_start=True
        self._lock_file=None
        self.refresh_policy=None
        self.restart=lambda: None
        self.signal=lambda: None
        self.needs_restart=self._needs_restart

    def _needs_restart(self):
        last=self.store.get_meta('last_tick_monotonic')
        return bool(last and self.clock.monotonic()-float(last)>180)

    def refresh(self):
        if self.refresh_policy:
            self.policy=self.refresh_policy()

    def injection_allowed(self):
        try:
            fn=getattr(self.ctx,'_gateway_injection_allowed',None)
            if fn: return fn() is True
            from hermes_cli.config import load_config_readonly
            return (load_config_readonly() or {}).get('plugins',{}).get('entries',{}).get('heartbeat2',{}).get('allow_gateway_injection') is True
        except Exception:
            return False

    def eligible(self):
        manager=getattr(self.ctx,'_manager',None)
        return manager is not None and getattr(manager,'_cli_ref',None) is None and bool(getattr(manager,'has_gateway_message_injector',False))

    def acquire(self):
        if self.closed or not self.eligible(): return False
        if self._lock_file is not None: return True
        f=open(self.store.path.parent/'poller.lock','a+')
        try:
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            return False
        self._lock_file=f
        try:
            self.reconcile()
        except BaseException:
            self.release()
            raise
        return True

    def release(self):
        if self._lock_file:
            fcntl.flock(self._lock_file,fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file=None

    def close(self):
        self.closed=True
        self.release()

    def reconcile(self):
        s=self.store
        with s.atomic():
            for o in s.list_occurrences() if self.process_start else []:
                if o.state=='claimed': s.refund(o.wake_id,o.dedupe_key,attempt=o.attempt,error='restart_refund')
                elif o.state=='started':
                    s.cas_occurrence(o.wake_id,o.dedupe_key,'started','interrupted',attempt=o.attempt,error='restart')
            for session in s.list_sessions() if self.process_start else []:
                if session.busy_since is not None:
                    s.cas_session(session.session_key,{'busy_since':session.busy_since},busy_since=None)
            for w in s.list_wakes(status='active') if self.process_start else []:
                if w.kind in RECURRING_KINDS and w.next_due_at is not None:
                    # An overdue interval becomes one immediate tick, then the
                    # next schedule is anchored at now, never the old cadence.
                    due=min(w.next_due_at,self.clock.now())
                    s.cas_wake(w.id,'active',expected_version=w.version,next_due_at=due)
            s.set_meta('epoch',self.epoch)
            s.set_meta('poller_pid',str(os.getpid()))
            s.set_meta('poller_started_at',str(self.clock.now()))
            self.process_start=False

    def _compression_tip(self,sid):
        try:
            from hermes_state import SessionDB
            with SessionDB(db_path=self.ctx._manager.home_path/'state.db',read_only=True) as db:
                return db.get_compression_tip(sid)
        except Exception:
            return None

    def _probe(self,wake):
        from .probes import check
        return check(wake,policy=self.policy,home=self.ctx._manager.home_path)

    @staticmethod
    def child_key(flight):
        return f'children:{flight.wake_id}/{flight.dedupe_key}:{flight.attempt}'

    def notice(self,key,text):
        name='notice:'+key
        current=json.loads(self.store.get_meta(name) or '[]')
        if text not in current: self.store.set_meta(name,json.dumps((current+[text])[-20:]))

    def ledger(self,wake,**counts):
        self.store.add_ledger(local_day(self.clock.now(),self.policy.resolved_timezone()),wake.id,**counts)

    def daily(self,key):
        day=local_day(self.clock.now(),self.policy.resolved_timezone())
        totals={'model_turns':0,'delivered':0}
        for w in self.store.list_wakes(key):
            row=self.store.get_ledger(day,w.id) or {}
            for k in totals: totals[k]+=row.get(k,0)
        # Reserve daily capacity for accepted work that has not ended yet.
        for o in self.store.list_occurrences(session_key=key):
            if o.state in {'claimed','started'}:
                if o.state=='claimed': totals['model_turns']+=1
                totals['delivered']+=1
        return totals

    def cancel_occurrences(self,wid,reason,*,preserve_started=False):
        for o in self.store.list_occurrences(wid):
            if o.state=='started' and (preserve_started or reason=='owner_replied'): continue
            if o.state in {'pending','claimed','started'}:
                self.store.cas_occurrence(wid,o.dedupe_key,o.state,'superseded',
                    attempt=o.attempt if o.state!='pending' else None,force_silent=True,error=reason)

    def evaluate(self,w,fn):
        """One wake's invalid stored data cannot roll back healthy siblings."""
        s=self.store
        if self.clock.now()<float(s.get_meta('eval_not_before:'+w.id) or 0): return
        try:
            with s.atomic(): return fn()
        except Exception as exc:
            self.log.warning('heartbeat2 wake evaluation failed (%s)',type(exc).__name__)
            governor.error(self,s.get_wake(w.id))
            current=s.get_wake(w.id)
            s.set_meta('wake_error:'+w.id,type(exc).__name__)
            s.set_meta('eval_not_before:'+w.id,str(self.clock.now()+RETRY_DELAYS[min(max(0,current.error_streak-1),4)]))

    def tick(self):
        if self.closed or not self.eligible(): return
        try:
            self.refresh()
            if not self.acquire(): return
            s,now=self.store,self.clock.now()
            with s.atomic():
                previous=float(s.get_meta('last_tick_at') or now)
                s.set_meta('last_tick_at',str(now))
                s.set_meta('last_tick_monotonic',str(self.clock.monotonic()))
                if now-previous>15:
                    self.log.info('heartbeat2 wall gap %.1fs',now-previous)
                    for session in s.list_sessions():
                        self.notice(session.session_key,'Some wakes were late / scheduler was stalled.')
                if not self.policy.enabled or self.paused(): return
                governor.settle_ignored(self)
                self.acks()
                from .commands import resume_timed
                resume_timed(self)
                self.events()
            self.probes()
            with s.atomic():
                self.times()
                self.silence()
                for o in s.list_occurrences(state='pending'):
                    self.evaluate(s.get_wake(o.wake_id),lambda o=o:self.dispatch(o))
                if now-float(s.get_meta('last_gc_at') or 0)>=3600:
                    s.gc(ledger_days=self.policy.ledger_days,timezone=self.policy.resolved_timezone())
                s.set_meta('last_error','')
        except Exception as exc:
            self.log.warning('heartbeat2 tick failed',exc_info=True)
            try: self.store.set_meta('last_error',type(exc).__name__)
            except Exception: pass

    def acks(self):
        s=self.store
        try: timeout=float(os.environ.get('HERMES_AGENT_TIMEOUT','1800'))
        except (TypeError,ValueError): timeout=1800
        if not math.isfinite(timeout): timeout=1800
        ttl=max(timeout*10,7200)
        now=self.clock.now()
        for o in s.list_occurrences(state='started'):
            w=s.get_wake(o.wake_id)
            session=s.get_session(w.session_key)
            activity=max(o.started_at if o.started_at is not None else now,session.last_activity_at or 0 if session else 0)
            if now-activity<ttl: continue
            if s.cas_occurrence(o.wake_id,o.dedupe_key,'started','interrupted',attempt=o.attempt,error='no_session_end',force_silent=True):
                s.cas_wake(w.id,w.status,expected_version=w.version,tokens_in=w.tokens_in+o.tokens_in,tokens_out=w.tokens_out+o.tokens_out)
                self.ledger(w,tokens_in=o.tokens_in,tokens_out=o.tokens_out)
                governor.error(self,s.get_wake(w.id))
        for session in s.list_sessions():
            if session.busy_since is not None and now-max(session.busy_since,session.last_activity_at or 0)>=ttl:
                s.cas_session(session.session_key,{'busy_since':session.busy_since,'last_activity_at':session.last_activity_at},busy_since=None)
        for o in s.list_occurrences(state='claimed'):
            if o.claimed_at is None or self.clock.now()-o.claimed_at<600: continue
            if int(s.get_meta('accepted:'+o.id) or o.attempt)>=3:
                s.cas_occurrence(o.wake_id,o.dedupe_key,'claimed','dead',attempt=o.attempt,error='ack_timeout')
                self.notice(s.get_wake(o.wake_id).session_key,'A wake was not acknowledged after three attempts.')
                governor.error(self,s.get_wake(o.wake_id))
            else: s.refund(o.wake_id,o.dedupe_key,attempt=o.attempt,error='ack_timeout')

    def family_enabled(self,w):
        family='silence' if w.kind=='owner_silent' else 'events' if w.kind in EVENT_KINDS else w.kind if w.kind in PROBE_KINDS else None
        return not family or getattr(self.policy,'source_'+family)

    def events(self):
        s=self.store
        for event in s.pending_events():
            for w in s.list_wakes(status='active'):
                if w.kind==event['kind']:
                    self.evaluate(w,lambda w=w,event=event:self._event(w,event))
            s.consume_event(event['seq'])

    def _event(self,w,event):
        s=self.store
        if not self.family_enabled(w): return
        if event['session_key'] and event['session_key']!=w.session_key: return
        if event['chat_id'] and event['chat_id']!=w.chat_id: return
        payload=event['payload_json']
        filters=w.trigger_json
        if w.kind=='reaction' and filters.get('emojis') and not set(filters['emojis']) & set(payload.get('emojis',[])): return
        if any(filters.get(k) is not None and payload.get(k)!=filters[k] for k in ('event','task_id','board')): return
        if w.kind=='api_error':
            last=float(s.get_meta('api_cooldown:'+w.id) or 0)
            if last and self.clock.now()-last<1800: return
        pending=s.list_occurrences(w.id,state='pending')
        if pending and s.coalesce(w.id,pending[-1].dedupe_key,[payload]): return
        identity=hashlib.sha1(event['dedupe_key'].encode()).hexdigest()
        s.add_occurrence(w.id,'e:'+identity,payload=[payload])
        if w.kind=='api_error': s.set_meta('api_cooldown:'+w.id,str(self.clock.now()))

    def probes(self):
        s,now=self.store,self.clock.now()
        due=[]
        with s.atomic():
            for w in s.list_wakes(status='active'):
                if w.kind not in PROBE_KINDS or not self.family_enabled(w): continue
                if w.expires_at is not None and w.expires_at<=now: continue
                if w.next_due_at is not None and w.next_due_at<=now:
                    def prepare_probe(w=w):
                        interval=max(self.policy.probe_floor_seconds,parse_duration(w.trigger_json.get('check_every',self.policy.probe_floor_seconds)))
                        s.cas_wake(w.id,'active',expected_version=w.version,next_due_at=now+interval)
                        due.append(w)
                    self.evaluate(w,prepare_probe)
        # U3 implements per-kind timeouts. Fixed four-worker pool never exceeds
        # four probe jobs; it does not hold SQLite across probe I/O.
        if due:
            with ThreadPoolExecutor(max_workers=4,thread_name_prefix='pulse-probe') as pool:
                results=list(pool.map(self._safe_probe,due))
            with s.atomic():
                for original,result in zip(due,results):
                    self.evaluate(original,lambda original=original,result=result:self._probe_result(original,result))

    def _probe_result(self,original,result):
        s=self.store
        w=s.get_wake(original.id)
        if w.status!='active' or result.get('unavailable'): return
        if result.get('error'):
            governor.error(self,w)
            return
        if result.get('fire'):
            identity=str(result.get('identity') or json.dumps(result.get('state',result.get('message','')),sort_keys=True))
            digest=hashlib.sha1(identity.encode()).hexdigest()
            if s.add_occurrence(w.id,'e:'+digest,payload=[{'message':result.get('message','')}]) and 'state' in result:
                s.cas_wake(w.id,'active',expected_version=w.version,probe_state_json=result['state'])
        else:
            updates={'gated_skips':w.gated_skips+1}
            # Command probe state is committed only on successful fire.
            if 'state' in result and w.kind!='command': updates['probe_state_json']=result['state']
            s.cas_wake(w.id,'active',expected_version=w.version,**updates)
            self.ledger(w,gated_skips=1)

    def _safe_probe(self,w):
        try: return self.probe_runner(w)
        except Exception: return {'error':'probe_failed'}

    def times(self):
        for w in self.store.list_wakes(status='active'):
            self.evaluate(w,lambda w=w:self._time(w))

    def _time(self,w):
        s,now=self.store,self.clock.now()
        # Retain the existing schedule decisions, isolated per wake/savepoint.
        if w.status=='active':
            if w.expires_at is not None and w.expires_at<=now:
                s.cas_wake(w.id,'active',status='expired',status_reason='expired')
                self.cancel_occurrences(w.id,'expired')
                return
            hard_end=w.created_at+min(self.policy.max_lifetime_days*86400,14400 if w.babysit else float('inf'))
            if not w.trigger_json.get('builtin') and hard_end<=now:
                s.cas_wake(w.id,'active',status='expired',status_reason='max_lifetime_changed')
                self.cancel_occurrences(w.id,'max_lifetime_changed')
                return
            if w.kind in RECURRING_KINDS:
                clamped=self.policy.clamp(w).wake
                changes={k:getattr(clamped,k) for k in ('base_interval_s','eff_interval_s','pacing_min_s','pacing_max_s','expires_at','max_fires') if getattr(clamped,k)!=getattr(w,k)}
                if changes:
                    s.cas_wake(w.id,'active',expected_version=w.version,**changes)
                    w=s.get_wake(w.id)
            if w.kind in TIME_KINDS:
                due=time_due(w,now=now,catch_up_s=self.policy.catch_up_hours*3600,
                             timezone=self.policy.resolved_timezone(),jitter_unit=self.jitter())
                if due:
                    if not (w.kind in RECURRING_KINDS and due.state=='pending' and s.list_occurrences(w.id,state='pending')):
                        s.add_occurrence(w.id,due.dedupe_key,state=due.state)
                    s.cas_wake(w.id,'active',expected_version=w.version,next_due_at=due.next_due_at)
                    if due.state=='missed':
                        s.cas_wake(w.id,'active',status='done',status_reason='missed')
                        self.notice(w.session_key,'A wake was too late after restart and was missed.')
            elif w.kind in PROBE_KINDS|EVENT_KINDS and w.deadline_at is not None and w.deadline_at<=now:
                # Deadline fallback is one fire only, and only when no trigger
                # occurrence has already been produced (including pending).
                if not s.list_occurrences(w.id):
                    s.add_occurrence(w.id,f't:{w.deadline_at:.17g}',payload=[{'message':'deadline passed — nothing happened'}])
                s.cas_wake(w.id,'active',expected_version=w.version,deadline_at=None)

    def silence(self):
        s,now=self.store,self.clock.now()
        if not self.policy.source_silence: return
        if self.policy.owner_silent_checkin:
            for session in s.list_sessions():
                if session.last_human_at is None or not session.episode_id: continue
                identity=hashlib.sha1(session.session_key.encode()).hexdigest()[:16]
                wid='checkin_'+identity
                w=s.get_wake(wid)
                if w is None:
                    s.create_wake(Wake(session.session_key,'Check in gently if there is something helpful to offer. Silence is fine.',
                        'owner_silent',id=wid,platform=session.platform,chat_type=session.chat_type,chat_id=session.chat_id,
                        armed_session_id=session.current_session_id,creator='user',reason='The owner has been quiet.',
                        trigger_json={'after':self.policy.checkin_after_hours*3600,'builtin':True}))
                elif w.status in {'done','orphaned'}:
                    # This system wake belongs to the chat, not one agent id.
                    s.cas_wake(w.id,w.status,expected_version=w.version,status='active',status_reason='',armed_session_id=session.current_session_id)
        for w in s.list_wakes(status='active'):
            if w.kind!='owner_silent': continue
            self.evaluate(w,lambda w=w:self._silence(w))

    def _silence(self,w):
        s,now=self.store,self.clock.now()
        session=s.get_session(w.session_key)
        if session is None or session.last_human_at is None or not session.episode_id: return
        builtin=bool(w.trigger_json.get('builtin'))
        if builtin and not self.policy.owner_silent_checkin: return
        after=self.policy.checkin_after_hours*3600 if builtin else parse_duration(w.trigger_json.get('after',self.policy.checkin_after_hours*3600))
        if builtin and w.ignored_streak: after*=2
        if now-session.last_human_at>=after:
            s.add_occurrence(w.id,'s:'+session.episode_id)

    def gate(self,w,o,s):
        now=self.clock.now()
        if w.trigger_json.get('builtin'):
            if not self.policy.owner_silent_checkin: return 'setting_off',True,None
            after=self.policy.checkin_after_hours*3600*(2 if w.ignored_streak else 1)
            if s and s.last_human_at is not None and now-s.last_human_at<after: return 'checkin_not_due',True,None
        if w.status!='active': return 'expired' if w.status=='expired' else 'wake_inactive',False,None
        if w.expires_at is not None and now>=w.expires_at: return 'expired',False,None
        if w.max_fires is not None and w.fires>=w.max_fires: return 'max_fires',False,None
        if w.cancel_on_reply and s and s.last_human_at is not None and s.last_human_at>w.created_at: return 'cancel_on_reply',False,None
        if s is None: return 'missing_session',False,None
        if not s.enabled: return 'session_off',False,None
        if s.paused_until is not None and s.paused_until>now: return 'session_paused',True,None
        if self.policy.dm_only and (s.chat_type!='dm' or w.chat_type!='dm'): return 'dm_only',False,None
        if s.platform not in MESSAGING_PLATFORMS: return 'non_gateway',False,None
        mute=self.policy.unanswered_gate(w,s.unanswered_count)
        if mute: return mute,False,None
        from .commands import quiet_until
        quiet=quiet_until(self,s.session_key,urgent=w.urgent)
        if quiet is not None: return 'quiet_hours',w.kind not in RECURRING_KINDS,quiet
        totals=self.daily(s.session_key)
        if totals['model_turns']>=self.policy.max_model_wakes_per_day: return 'daily_model_cap',False,None
        if totals['delivered']>=self.policy.max_messages_per_day: return 'daily_message_cap',False,None
        flood=json.loads(self.store.get_meta('injections') or '[]')
        if sum(now-t<60 for t in flood)>=5: return 'global_flood',True,None
        if now<float(self.store.get_meta('not_before:'+o.id) or 0): return 'not_before',True,None
        if s.busy_since is not None: return 'busy',True,None
        if s.last_wake_turn_at is not None and now-s.last_wake_turn_at<self.policy.wake_spacing_minutes*60: return 'wake_spacing',True,None
        if self.store.list_occurrences(session_key=s.session_key,state='claimed') or self.store.list_occurrences(session_key=s.session_key,state='started'): return 'in_flight',True,None
        if not self.family_enabled(w): return 'source_disabled',False,None
        if not self.injection_allowed(): return 'injection_disabled',True,None
        return '',False,None

    def dispatch(self,o):
        s=self.store
        w=s.get_wake(o.wake_id)
        session=s.get_session(w.session_key)
        reason,defer,until=self.gate(w,o,session)
        if reason:
            if defer:
                s.update_occurrence(w.id,o.dedupe_key,'pending',error=reason)
                if until: s.set_meta('not_before:'+o.id,str(until))
            else:
                s.cas_occurrence(w.id,o.dedupe_key,'pending','skipped',error=reason)
                self.ledger(w,gated_skips=1)
            return
        # Check again at the claim barrier, not just at tick entry.
        if self.paused() or not self.policy.enabled: return
        if int(s.get_meta('accepted:'+o.id) or 0)>=3:
            s.cas_occurrence(w.id,o.dedupe_key,'pending','dead',error='ack_timeout')
            self.notice(w.session_key,'A wake was not acknowledged after three attempts.')
            governor.error(self,w)
            return
        if not s.claim(w.id,o.dedupe_key,epoch=self.epoch,pid=os.getpid()): return
        o=s.get_occurrence(w.id,o.dedupe_key)
        from .wake_text import render_wake
        recent=[x.outcome or x.state for x in s.list_occurrences(w.id) if x.state=='done'][-3:]
        late=None
        if o.dedupe_key.startswith('t:'):
            try: late=max(0,self.clock.now()-float(o.dedupe_key[2:]))
            except ValueError: pass
        text=render_wake(w,o,session,clock=self.clock,timezone=self.policy.resolved_timezone(),
             recent_outcomes=recent,messages_today=self.daily(session.session_key)['delivered']-1,
             max_messages=self.policy.max_messages_per_day,governor_hint=governor.hint(self,w),late_by_s=late)
        try: accepted=self.ctx.inject_message(text,session_key=w.session_key)
        except Exception: accepted=False
        if accepted:
            s.set_meta('accepted:'+o.id,str(int(s.get_meta('accepted:'+o.id) or 0)+1))
            flood=[t for t in json.loads(s.get_meta('injections') or '[]') if self.clock.now()-t<60]
            s.set_meta('injections',json.dumps(flood+[self.clock.now()]))
            s.cas_session(session.session_key,{'last_wake_turn_at':session.last_wake_turn_at},last_wake_turn_at=self.clock.now())
        else:
            count=int(s.get_meta('inject_failures:'+o.id) or 0)+1
            s.set_meta('inject_failures:'+o.id,str(count))
            s.set_meta('not_before:'+o.id,str(self.clock.now()+RETRY_DELAYS[min(count-1,4)]))
            if count>=5:
                s.cas_occurrence(w.id,o.dedupe_key,'claimed','dead',attempt=o.attempt,error='inject_failed')
                governor.error(self,w)
            else: s.refund(w.id,o.dedupe_key,attempt=o.attempt,error='inject_failed')

    def status(self):
        runtime=getattr(self,'runtime_status',None)
        if runtime:
            status=runtime()
            if status: return status
        last=self.store.get_meta('last_tick_at')
        reason=('no gateway in this process' if not self.eligible() else
                'injection disabled' if not self.injection_allowed() else
                'lock held elsewhere' if not runtime and not self._lock_file else self.store.get_meta('last_error') or '')
        if last and self.clock.now()-float(last)<=15: return {'state':'alive','reason':reason}
        return {'state':'stalled' if last else 'not running','since':float(last) if last else None,'reason':reason}
