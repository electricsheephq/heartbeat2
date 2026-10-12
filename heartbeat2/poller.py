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
from .policy import MESSAGING_PLATFORMS, is_owner, is_watcher, one_shot
from .schedule import time_due, local_day, parse_duration, next_every
from . import governor

LOG = logging.getLogger('heartbeat2')
RETRY_DELAYS = (30,60,300,900,3600)
FINAL_SKIP_REASONS = frozenset({'unanswered','daily_model_cap','daily_message_cap','dm_only',
    'non_gateway','session_off','source_disabled','cancel_on_reply','expired','max_fires'})


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
        self._probing=False
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
                if o.state=='claimed':
                    s.refund(o.wake_id,o.dedupe_key,attempt=o.attempt,error='restart_refund')
                    s.set_meta('recovered:'+o.id,'1')
                elif o.state=='started': self.interrupt(o,'restart')
            for session in s.list_sessions() if self.process_start else []:
                if session.busy_since is not None:
                    s.cas_session(session.session_key,{'busy_since':session.busy_since},busy_since=None)
            # Persisted future deadlines remain future. time_due() coalesces
            # genuinely overdue intervals at the next tick without changing them
            # during process admission.
            for w in s.list_wakes(status='active') if self.process_start and s.get_meta('epoch') else []:
                if w.next_due_at is not None and w.next_due_at < self.clock.now():
                    s.set_meta('restart_due:'+w.id,str(w.next_due_at))
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
        from .registration import home_scope
        with home_scope(self.ctx._manager.home_path):
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

    def timing(self,w):
        from .cache import ttl_for_session
        ttl=ttl_for_session(self.store,self.store.get_session(w.session_key))[0]
        return {'cache_ttl':ttl if w.creator=='agent' and w.kind in {'every','self'} and not w.trigger_json.get('at') else None,
                'minimum_s':max(self.policy.interval_floor(w),w.pacing_min_s or 0)}

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
            if reason=='paused' and o.state=='pending' and o.held_since is not None:
                self.store.update_occurrence(wid,o.dedupe_key,'pending',error='held_paused',held_reason='held_paused')
                self.store.set_meta('not_before:'+o.id,'')
                continue
            if o.state=='started' and (preserve_started or reason=='owner_replied'): continue
            if o.state in {'pending','claimed','started'}:
                self.store.cas_occurrence(wid,o.dedupe_key,o.state,'superseded',
                    attempt=o.attempt if o.state!='pending' else None,force_silent=True,
                    error='expired_held' if o.state=='pending' and o.held_since is not None and reason in {'expired','max_fires','max_lifetime_changed'} else reason)

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
            self._tick_started_wall=now
            with s.atomic():
                previous=float(s.get_meta('last_tick_at') or now)
                s.set_meta('last_tick_at',str(now))
                s.set_meta('last_tick_monotonic',str(self.clock.monotonic()))
                if now-previous>15:
                    self.log.info('heartbeat2 wall gap %.1fs',now-previous)
                    # A clock gap alone is not proof that a wake was late.
                if not self.policy.enabled or self.paused(): return
                governor.settle_ignored(self)
                self.acks()
                from .commands import resume_timed
                resume_timed(self)
                self.events()
            self._probing=True
            s.set_meta('probing_since',str(self.clock.now()))
            s.set_meta('probing_epoch',self.epoch)
            try: self.probes()
            finally:
                self._probing=False
                s.set_meta('probing_since','')
                s.set_meta('probing_until','')
                s.set_meta('last_tick_at',str(self.clock.now()))
                s.set_meta('last_tick_monotonic',str(self.clock.monotonic()))
            with s.atomic():
                for w in s.list_wakes(): self.terminal_one_shot(w)
                self.times()
                self.silence()
                if now-float(s.get_meta('last_gc_at') or 0)>=3600:
                    s.gc(ledger_days=self.policy.ledger_days,timezone=self.policy.resolved_timezone())
                s.set_meta('last_error','')
            for o in s.list_occurrences(state='pending'):
                w=s.get_wake(o.wake_id)
                cache_line=self.cache_line(w)  # Pricing may fetch metadata; never hold the write lock.
                self.evaluate(w,lambda o=o,cache_line=cache_line:self.dispatch(o,cache_line=cache_line))
        except Exception as exc:
            self.log.warning('heartbeat2 tick failed',exc_info=True)
            try: self.store.set_meta('last_error',type(exc).__name__)
            except Exception: pass

    def acks(self):
        s=self.store
        try: timeout=float(os.environ.get('HERMES_AGENT_TIMEOUT','1800'))
        except (TypeError,ValueError): timeout=1800
        provider_limit=timeout if math.isfinite(timeout) and timeout>0 else None
        ttl=max((provider_limit or 0)*10,7200)
        now=self.clock.now()
        for o in s.list_occurrences(state='started'):
            w=s.get_wake(o.wake_id)
            session=s.get_session(w.session_key)
            activity=max(o.started_at if o.started_at is not None else now,session.last_activity_at or 0 if session else 0)
            limit=provider_limit if provider_limit is not None and s.get_meta('provider_error:'+o.id) else ttl
            if now-activity<limit: continue
            self.interrupt(o,'no_session_end')
        for session in s.list_sessions():
            if session.busy_since is not None and now-max(session.busy_since,session.last_activity_at or 0)>=ttl:
                s.cas_session(session.session_key,{'busy_since':session.busy_since,'last_activity_at':session.last_activity_at},busy_since=None)
        for o in s.list_occurrences(state='claimed'):
            if o.claimed_at is None or self.clock.now()-o.claimed_at<600: continue
            if int(s.get_meta('accepted:'+o.id) or o.attempt)>=3:
                s.cas_occurrence(o.wake_id,o.dedupe_key,'claimed','dead',attempt=o.attempt,error='ack_timeout')
                self.notice(s.get_wake(o.wake_id).session_key,'A wake was not acknowledged after three attempts.')
                self.terminal_one_shot(s.get_wake(o.wake_id))
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
        if self.end_max_fires(w): return
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
                session=s.get_session(w.session_key)
                if session is None or not session.enabled or session.paused_until is not None and session.paused_until>now: continue
                if self.end_max_fires(w): continue
                if w.expires_at is not None and w.expires_at<=now: continue
                if w.next_due_at is not None and w.next_due_at<=now:
                    def prepare_probe(w=w):
                        interval=max(self.policy.probe_floor_seconds,parse_duration(w.trigger_json.get('check_every',self.policy.probe_floor_seconds)))
                        if s.cas_wake(w.id,'active',expected_version=w.version,next_due_at=now+interval):
                            due.append(s.get_wake(w.id))
                    self.evaluate(w,prepare_probe)
        # U3 implements per-kind timeouts. Fixed four-worker pool never exceeds
        # four probe jobs; it does not hold SQLite across probe I/O.
        if due:
            from .probes import COMMAND_DEADLINE_S, URL_DEADLINE_S
            waves=math.ceil(len(due)/4)
            s.set_meta('probing_until',str(self.clock.now()+waves*max(COMMAND_DEADLINE_S,URL_DEADLINE_S)+10))
            with ThreadPoolExecutor(max_workers=4,thread_name_prefix='pulse-probe') as pool:
                results=list(pool.map(self._safe_probe,due))
            with s.atomic():
                for original,result in zip(due,results):
                    self.evaluate(original,lambda original=original,result=result:self._probe_result(original,result))

    def _probe_result(self,original,result):
        s=self.store
        w=s.get_wake(original.id)
        if w.status!='active' or result.get('unavailable'): return
        session=s.get_session(w.session_key)
        if session is None or not session.enabled or session.paused_until is not None and session.paused_until>self.clock.now(): return
        # The prepared wake version is the probe generation. Any concurrent
        # mutation discards the result, including A -> B -> A retargets.
        if w.version!=original.version or w.kind!=original.kind or w.trigger_json!=original.trigger_json: return
        s.cas_wake(w.id,'active',expected_version=w.version,checks=w.checks+1)
        self.ledger(w,checks=1)
        w=s.get_wake(w.id)
        if result.get('error'):
            governor.error(self,w)
            return
        if w.error_streak:
            s.cas_wake(w.id,'active',expected_version=w.version,error_streak=0)
            w=s.get_wake(w.id)
        if result.get('fire'):
            identity=str(result.get('identity') or json.dumps(result.get('state',result.get('message','')),sort_keys=True))
            digest=hashlib.sha1(identity.encode()).hexdigest()
            payload=[{'message':result.get('message','')}]
            pending=s.list_occurrences(w.id,state='pending')
            coalesced=bool(pending and s.coalesce(w.id,pending[-1].dedupe_key,payload))
            if (coalesced or s.add_occurrence(w.id,'e:'+digest,payload=payload)) and 'state' in result:
                s.cas_wake(w.id,'active',expected_version=w.version,probe_state_json=result['state'])
        else:
            updates={}
            # Change-mode commands also commit their first successful baseline.
            if 'state' in result and (w.kind!='command' or result.get('baseline') and not w.probe_state_json): updates['probe_state_json']=result['state']
            if updates: s.cas_wake(w.id,'active',expected_version=w.version,**updates)

    def _safe_probe(self,w):
        try: return self.probe_runner(w)
        except Exception: return {'error':'probe_failed'}

    def times(self):
        for w in self.store.list_wakes(status='active'):
            self.evaluate(w,lambda w=w:self._time(w))

    def _time(self,w):
        s,now=self.store,self.clock.now()
        if self.end_max_fires(w): return
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
                clamped=self.policy.clamp(w,cache_ttl=self.timing(w)['cache_ttl']).wake
                changes={k:getattr(clamped,k) for k in ('base_interval_s','eff_interval_s','pacing_min_s','pacing_max_s','expires_at','max_fires') if getattr(clamped,k)!=getattr(w,k)}
                if changes:
                    s.cas_wake(w.id,'active',expected_version=w.version,**changes)
                    w=s.get_wake(w.id)
            if w.kind in TIME_KINDS:
                due=time_due(w,now=now,catch_up_s=self.policy.catch_up_hours*3600,
                             timezone=self.policy.resolved_timezone(),jitter_unit=self.jitter(),**self.timing(w))
                if due:
                    if not (w.kind in RECURRING_KINDS and due.state=='pending' and s.list_occurrences(w.id,state='pending')):
                        s.add_occurrence(w.id,due.dedupe_key,state=due.state)
                    if s.get_meta('restart_due:'+w.id):
                        for pending in s.list_occurrences(w.id,state='pending'):
                            s.set_meta('recovered:'+pending.id,'1')
                        s.set_meta('restart_due:'+w.id,'')
                    s.cas_wake(w.id,'active',expected_version=w.version,next_due_at=due.next_due_at)
                    if due.state=='pending' and getattr(self,'_tick_started_wall',now)-float(due.dedupe_key[2:])>15:
                        self.notice(w.session_key,'Some wakes were late / scheduler was stalled.')
                    if due.state=='missed':
                        self.terminal_one_shot(s.get_wake(w.id))
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
        if self.end_max_fires(w): return
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
        if o.state=='pending' and o.held_since is not None and o.error.startswith('held_'):
            self.store.set_meta('not_before:'+o.id,'')
        if w.kind=='owner_silent' and o.dedupe_key.startswith('s:') and (s is None or o.dedupe_key!='s:'+(s.episode_id or '')):
            return 'episode_ended',False,None
        if w.trigger_json.get('builtin'):
            if not self.policy.owner_silent_checkin: return 'setting_off',True,None
            after=self.policy.checkin_after_hours*3600*(2 if w.ignored_streak else 1)
            if s and s.last_human_at is not None and now-s.last_human_at<after: return 'checkin_not_due',True,None
        held_paused=o.state=='pending' and o.held_since is not None and w.status=='paused'
        if w.status!='active' and not held_paused: return 'expired' if w.status=='expired' else 'wake_inactive',False,None
        if w.expires_at is not None and now>=w.expires_at: return 'expired',False,None
        if w.max_fires is not None and w.fires>=w.max_fires: return 'max_fires',False,None
        if held_paused: return 'held_paused',True,None
        if w.cancel_on_reply and s and s.last_human_at is not None and s.last_human_at>w.created_at: return 'cancel_on_reply',False,None
        if s is None: return 'missing_session',False,None
        if not s.enabled: return 'session_off',False,None
        if s.paused_until is not None and s.paused_until>now: return 'session_paused',True,None
        if self.policy.dm_only and (s.chat_type!='dm' or w.chat_type!='dm'): return 'dm_only',False,None
        if s.platform not in MESSAGING_PLATFORMS: return 'non_gateway',False,None
        health=json.loads(self.store.get_meta('provider_health') or '{}')
        if health.get('trial'):
            wid,key=health['trial'].split('/',1)
            trial=self.store.get_occurrence(wid,key)
            if not trial or trial.state not in {'claimed','started'}:
                health.pop('trial'); self.store.set_meta('provider_health',json.dumps(health))
        if health and (health['until']>now or health.get('trial')):
            return 'provider_unavailable',True,health['until'] if health['until']>now else None
        # Windows hold pending work before mute/caps can drop it. Re-evaluate
        # every gate once the window ends; owner reminders only bypass mute.
        from .commands import quiet_until
        quiet=quiet_until(self,s.session_key,urgent=w.urgent)
        if quiet is not None: return 'quiet_hours',w.kind not in RECURRING_KINDS,quiet
        mute=self.policy.unanswered_gate(w,s.unanswered_count)
        if mute: return mute,mute=='held_unanswered',None
        totals=self.daily(s.session_key)
        cap='daily_model_cap' if totals['model_turns']>=self.policy.max_model_wakes_per_day else 'daily_message_cap' if totals['delivered']>=self.policy.max_messages_per_day else ''
        if cap and not (is_owner(w) and one_shot(w)):
            if is_watcher(w):
                return 'held_daily_cap',True,None
            return cap,False,None
        flood=json.loads(self.store.get_meta('injections') or '[]')
        if sum(now-t<60 for t in flood)>=5: return 'global_flood',True,None
        if now<float(self.store.get_meta('not_before:'+o.id) or 0): return 'not_before',True,None
        if s.busy_since is not None: return 'busy',True,None
        if s.last_wake_turn_at is not None and now-s.last_wake_turn_at<self.policy.wake_spacing_minutes*60: return 'wake_spacing',True,None
        if self.store.list_occurrences(session_key=s.session_key,state='claimed') or self.store.list_occurrences(session_key=s.session_key,state='started'): return 'in_flight',True,None
        if not self.family_enabled(w): return 'source_disabled',False,None
        if not self.injection_allowed(): return 'injection_disabled',True,None
        return '',False,None

    def dispatch(self,o,*,cache_line=None):
        s=self.store
        w=s.get_wake(o.wake_id)
        session=s.get_session(w.session_key)
        reason,defer,until=self.gate(w,o,session)
        if reason:
            if defer:
                first=o.error!=reason
                if reason.startswith('held_'):
                    existing=next((x for x in s.list_occurrences(w.id,state='pending') if x.id!=o.id and x.held_since is not None),None)
                    if existing:
                        s.update_occurrence(w.id,existing.dedupe_key,'pending',payload_json=o.payload_json[-1:],held_matches=existing.held_matches+max(1,len(o.payload_json)))
                        s.cas_occurrence(w.id,o.dedupe_key,'pending','superseded',error='held_merged')
                        return
                held={'held_since':o.held_since if o.held_since is not None else self.clock.now(),
                      'held_reason':reason,'held_matches':o.held_matches or max(1,len(o.payload_json)),
                      'payload_json':o.payload_json[-1:]} if reason.startswith('held_') else {}
                s.update_occurrence(w.id,o.dedupe_key,'pending',error=reason,**held)
                if reason=='provider_unavailable':
                    if first: self.ledger(w,provider_deferrals=1)
                elif until: s.set_meta('not_before:'+o.id,str(until))
            else:
                s.cas_occurrence(w.id,o.dedupe_key,'pending','skipped',error='expired_held' if o.held_since is not None and reason in {'expired','max_fires'} else reason)
                s.cas_wake(w.id,w.status,expected_version=w.version,gated_skips=w.gated_skips+1)
                w=s.get_wake(w.id)
                if reason=='cancel_on_reply' and w.status=='active':
                    s.cas_wake(w.id,'active',expected_version=w.version,status='cancelled',status_reason='owner_replied')
                    self.cancel_occurrences(w.id,'owner_replied')
                elif w.status=='active' and reason in FINAL_SKIP_REASONS:
                    self.terminal_one_shot(s.get_wake(w.id))
                self.ledger(w,gated_skips=1)
            return
        # Check again at the claim barrier, not just at tick entry.
        if self.paused() or not self.policy.enabled: return
        if int(s.get_meta('accepted:'+o.id) or 0)>=3:
            s.cas_occurrence(w.id,o.dedupe_key,'pending','dead',error='ack_timeout')
            self.notice(w.session_key,'A wake was not acknowledged after three attempts.')
            self.terminal_one_shot(s.get_wake(w.id))
            governor.error(self,s.get_wake(w.id))
            return
        if cache_line is None:
            cache_line='' if s.connection.in_transaction else self.cache_line(w)
        held_by=o.error  # Claim clears the stored deferral; retain it for this prompt.
        if not s.claim(w.id,o.dedupe_key,epoch=self.epoch,pid=os.getpid()): return
        o=s.get_occurrence(w.id,o.dedupe_key)
        health=json.loads(s.get_meta('provider_health') or '{}')
        if health:
            health['trial']=o.id
            s.set_meta('provider_health',json.dumps(health))
        from .wake_text import render_wake
        recent=[x.outcome or x.state for x in s.list_occurrences(w.id) if x.state=='done'][-3:]
        late=None
        if o.dedupe_key.startswith('t:'):
            try: late=max(0,self.clock.now()-float(o.dedupe_key[2:]))
            except ValueError: pass
        text=render_wake(w,o,session,clock=self.clock,timezone=self.policy.resolved_timezone(),
             recent_outcomes=recent,messages_today=self.daily(session.session_key)['delivered']-1,
             max_messages=self.policy.max_messages_per_day,governor_hint=governor.hint(self,w),late_by_s=late,
             after_restart=bool(s.get_meta('recovered:'+o.id)),held_by=held_by,cache_line=cache_line)
        try: accepted=self.ctx.inject_message(text,session_key=w.session_key)
        except Exception: accepted=False
        if accepted:
            s.set_meta('accepted:'+o.id,str(int(s.get_meta('accepted:'+o.id) or 0)+1))
            flood=[t for t in json.loads(s.get_meta('injections') or '[]') if self.clock.now()-t<60]
            s.set_meta('injections',json.dumps(flood+[self.clock.now()]))
            s.cas_session(session.session_key,{'last_wake_turn_at':session.last_wake_turn_at},last_wake_turn_at=self.clock.now())
        else:
            if health:
                health.pop('trial',None); s.set_meta('provider_health',json.dumps(health))
            count=int(s.get_meta('inject_failures:'+o.id) or 0)+1
            s.set_meta('inject_failures:'+o.id,str(count))
            s.set_meta('not_before:'+o.id,str(self.clock.now()+RETRY_DELAYS[min(count-1,4)]))
            if count>=5:
                s.cas_occurrence(w.id,o.dedupe_key,'claimed','dead',attempt=o.attempt,error='inject_failed')
                self.terminal_one_shot(s.get_wake(w.id))
                governor.error(self,s.get_wake(w.id))
            else: s.refund(w.id,o.dedupe_key,attempt=o.attempt,error='inject_failed')

    def terminal_one_shot(self,w):
        if not one_shot(w) or w.status!='active' or w.next_due_at is not None: return
        occurrences=[o for o in self.store.list_occurrences(w.id) if o.state!='superseded']
        if not occurrences or any(o.state in {'pending','claimed','started'} for o in occurrences): return
        o=occurrences[-1]
        if o.state in {'interrupted','dead','missed'} or o.state=='skipped' and o.error in FINAL_SKIP_REASONS:
            if o.error=='cancel_on_reply':
                self.store.cas_wake(w.id,'active',expected_version=w.version,status='cancelled',status_reason='owner_replied')
                return
            self.store.cas_wake(w.id,w.status,expected_version=w.version,
                status='failed',status_reason=o.state+(':'+o.error if o.error else ''))

    def interrupt(self,o,cause,*,count_error=True):
        """One fenced settlement, shared by hooks, TTL and restart recovery."""
        s=self.store
        with s.atomic():
            provider=s.get_meta('provider_error:'+o.id)
            error='provider_error:'+provider if provider else cause
            if not s.cas_occurrence(o.wake_id,o.dedupe_key,o.state,'interrupted',attempt=o.attempt,error=error,force_silent=True): return False
            if provider and o.api_calls == 0:
                self.refund_model_count(o)
            w=s.get_wake(o.wake_id)
            counts=self.usage_counts(o)
            s.cas_wake(w.id,w.status,expected_version=w.version,**{k:getattr(w,k)+v for k,v in counts.items()})
            self.ledger(w,**counts)
            self.terminal_one_shot(s.get_wake(w.id))
            if count_error or provider: governor.error(self,s.get_wake(w.id))
            self.end_max_fires(s.get_wake(w.id))
            if provider and not s.get_meta('provider_health'): self.provider_failure(provider)
            session=s.get_session(w.session_key)
            if session: s.cas_session(session.session_key,{'busy_since':session.busy_since},busy_since=None)
            return True

    @staticmethod
    def usage_counts(o):
        from .store import CACHE_FIELDS
        return {**{k:getattr(o,k) for k in CACHE_FIELDS | {'tokens_in','tokens_out'}},'no_verdict':int(o.outcome in {None,'unreported'})}

    def refund_model_count(self,o):
        if o.api_calls or o.started_at is None or self.store.get_meta('model_refunded:'+o.id): return
        w=self.store.get_wake(o.wake_id)
        self.store.cas_wake(w.id,w.status,expected_version=w.version,model_turns=max(0,w.model_turns-1))
        day=local_day(o.started_at,self.policy.resolved_timezone())
        self.store.connection.execute('UPDATE ledger SET model_turns=MAX(0,model_turns-1) WHERE day=? AND wake_id=?',(day,w.id))
        self.store.set_meta('model_refunded:'+o.id,'1')

    def cache_line(self,w,*,seconds=None,watcher=False):
        from .cache import line
        return line(self.store,self.store.get_session(w.session_key),self.clock.now(),seconds=seconds,
                    floor=max(self.policy.interval_floor(w),w.pacing_min_s or 0),watcher=watcher,
                    pricing=not self.store.connection.in_transaction)

    def end_max_fires(self,w):
        """Retire a spent cap once the last running turn has settled."""
        if w.max_fires is None or w.fires<w.max_fires: return False
        if w.status in {'active','paused'} and not any(o.state in {'claimed','started'} for o in self.store.list_occurrences(w.id)):
            if self.store.cas_wake(w.id,w.status,expected_version=w.version,status='done',status_reason='max_fires'):
                self.cancel_occurrences(w.id,'max_fires')
        return True

    def provider_failure(self,kind):
        health=json.loads(self.store.get_meta('provider_health') or '{}')
        if health and health.get('until',0)>self.clock.now() and not health.get('trial'): return
        step=min(int(health.get('step',-1))+1,3)
        self.store.set_meta('provider_health',json.dumps({'reason':'provider_unavailable','class':kind,
            'step':step,'until':self.clock.now()+(300,900,3600,14400)[step]}))
        self.signal()

    def status(self):
        # A command process may not own the gateway's thread or lock.
        s=self.store; now=self.clock.now()
        last=s.get_meta('last_tick_at'); started=s.get_meta('poller_started_at')
        probing=s.get_meta('probing_since')
        until=float(s.get_meta('probing_until') or (float(probing)+40 if probing else 0))
        if probing and s.get_meta('probing_epoch')==s.get_meta('epoch') and float(probing)<=now<=until:
            return {'state':'alive','reason':'checking probes'}
        if last and now-float(last)<=15 and s.get_meta('poller_pid') and s.get_meta('epoch'):
            return {'state':'alive','reason':s.get_meta('last_error') or ''}
        runtime=getattr(self,'runtime_status',None)
        local=runtime() if runtime else None
        reason=s.get_meta('last_error') or (local or {}).get('reason') or ('no gateway in this process' if not self.eligible() else 'injection disabled' if not self.injection_allowed() else '')
        stamp=last or started
        if not last and started and now-float(started)<=15: return {'state':'alive','reason':'starting'}
        return {'state':'stalled' if stamp else 'not running','since':float(stamp) if stamp else None,'reason':reason}
