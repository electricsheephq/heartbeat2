"""Wall-clock engine. tick() is explicit; only registration owns a timer thread.

Coalescing/claim/refund follows Hermes heartbeat.py; fingerprint gating follows
cron/monitor.py; estop and busy ordering follows gateway/run_goals.py.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import logging
import math
import os
import random
import uuid

from .model import Wake, RECURRING_KINDS, TIME_KINDS, PROBE_KINDS, EVENT_KINDS, agent_authored
from .policy import LOCAL_SURFACES, MESSAGING_PLATFORMS, deliverable_platform, owner_class, agreed_watcher, is_owner, is_watcher, one_shot, holds_messages, is_keep_warm, is_direct_chat, sleeps
from .schedule import time_due, local_day, local_neighbours, parse_duration, next_every, zone
from .store import BACKEND_REINJECT_PREFIX
from . import governor, serving

LOG = logging.getLogger('heartbeat2')
RETRY_DELAYS = (30,60,300,900,3600)
PROBES_PER_TICK = 16
OUTAGE_GAP_S = 60     # a longer gap between ticks means the gateway or the poller was down (#230)
OUTAGES_KEPT = 50
KEEP_WARM_NOTICE_AFTER = 7200  # keep-warm unable to ping this long is worth one line to the owner (#234)
FINAL_SKIP_REASONS = frozenset({'unanswered','asleep','daily_model_cap','daily_cost_cap','daily_message_cap','dm_only',
    'non_gateway','session_off','source_disabled','cancel_on_reply','expired','max_fires','nothing_to_report'})
# Every claim records its poller's epoch; an epoch outside this set was minted
# by another (for a startup replay: the crashed) process.
LOCAL_EPOCHS = set()
CREATED_EPOCHS = set()  # Every poller epoch this process made; never discarded.


def event_matches(w,payload) -> bool:
    filters=w.trigger_json
    if w.kind=='reaction' and filters.get('emojis') and not set(filters['emojis']) & set(payload.get('emojis',[])): return False
    if any(filters.get(k) is not None and payload.get(k)!=filters[k] for k in ('task_id','board')): return False
    if w.kind=='reaction' and filters.get('message_id') is not None:
        mid=payload.get('message_id')
        if mid is None or str(mid).strip()!=str(filters['message_id']).strip(): return False
    return True


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
                 compression_tip=None,session_key_fn=None,probe_runner=None,boot_at=None):
        self.ctx,self.store,self.policy,self.clock=ctx,store,policy,clock
        self.boot_at=clock.now() if boot_at is None else boot_at
        self.paused,self.jitter=paused,jitter or (lambda: random.uniform(-1,1))
        self.compression_tip=compression_tip or self._compression_tip
        from .intake import Intake
        from .tool import Tool,current_session_key
        self.intake=Intake(self)
        self.tool=Tool(self,session_key_fn or current_session_key)
        self.probe_runner=probe_runner or self._probe
        self.epoch=uuid.uuid4().hex
        if store.backend: self.epoch=serving.backend_epoch(self.epoch)
        self._app_closed_until={}
        LOCAL_EPOCHS.add(self.epoch)
        CREATED_EPOCHS.add(self.epoch)
        self.log=LOG
        self.closed=False
        self.process_start=True
        self._lock_file=None
        self.refresh_policy=None
        self._probing=False
        self.restart=lambda: None
        self.signal=lambda: None
        self.needs_restart=self._needs_restart
        from .notifier import NotificationWorker
        self.notifications=NotificationWorker(store)
        self._owns_notifications=True

    def _needs_restart(self):
        last=self.store.get_meta('last_tick_monotonic')
        return bool(last and self.clock.monotonic()-float(last)>180)

    def refresh(self):
        if self.refresh_policy:
            try: self.policy=self.refresh_policy()
            except (ValueError,TypeError,OverflowError) as exc:
                self.store.set_meta('settings_error',json.dumps([{'key':'policy','reason':str(exc),'effective':'previous policy'}]))
            else:
                errors=json.loads(self.store.get_meta('settings_error') or '[]')
                if len(errors)==1 and errors[0].get('key')=='policy':
                    self.store.set_meta('settings_error','')

    def settings_notice(self):
        errors=json.loads(self.store.get_meta('settings_error') or '[]')
        return '\n'.join(f'Setting ignored: {e["key"]} ({e["reason"]}); using {e["effective"]!r}' for e in errors)

    def session_can_receive(self,s):
        """Whether HeartBeat may speak into this chat now: the same session-level checks dispatch applies (#145 review)."""
        now=self.clock.now()
        return bool(self.policy.enabled and not self.paused() and self.injection_allowed() and s is not None and s.enabled
                    and not (s.paused_until is not None and s.paused_until>now)
                    and not (self.policy.dm_only and s.chat_type!='dm') and self.deliverable(s.platform))

    def deliverable(self, platform):
        return deliverable_platform(platform, backend=self.store.backend)

    def injection_allowed(self):
        try:
            fn=getattr(self.ctx,'_gateway_injection_allowed',None)
            if fn: return fn() is True
            from hermes_cli.config import load_config_readonly
            return (load_config_readonly() or {}).get('plugins',{}).get('entries',{}).get('heartbeat2',{}).get('allow_gateway_injection') is True
        except Exception:
            return False

    def eligible(self):
        from .serving import state,SERVED,BACKEND
        return state(getattr(self.ctx,'_manager',None))==(BACKEND if self.store.backend else SERVED)

    def acquire(self):
        if self.closed or not self.eligible(): return False
        if self._lock_file is not None: return True
        f=open(self.store.path.parent/('poller-backend.lock' if self.store.backend else 'poller.lock'),'a+')
        try:
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            return False
        self._lock_file=f
        try:
            # Another process held the lock since this one last did: recover as a takeover.
            # (An in-process replacement keeps its live turns.)
            if self.store.get_meta('epoch') not in (None,'',*CREATED_EPOCHS): self.process_start=True
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

    def other_backend_delivers(self):
        if not self.store.backend or self._lock_file is not None: return False
        pid=self.store.get_meta('poller_pid') or ''
        if not pid.isdecimal() or int(pid)==os.getpid(): return False
        if not serving.backend_epoch_alive(self.store.get_meta('epoch'),pid=int(pid)): return False
        # Commands use a different Poller from the runner. Its PID metadata
        # can outlive the lock, so verify that the recorded peer still blocks it.
        try:
            with (self.store.path.parent/'poller-backend.lock').open('r') as lock:
                fcntl.flock(lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            pass
        return False

    def close(self):
        self.closed=True
        if self._owns_notifications: self.notifications.close()
        # A closed poller's turns are abandoned; a re-registered runtime must not treat them as live.
        LOCAL_EPOCHS.discard(self.epoch)
        self.release()

    def _foreign_backend_claim(self,o):
        return (self.store.backend and o.pid is not None and o.pid!=os.getpid()
                and serving.backend_epoch_alive(o.epoch,pid=o.pid))

    @staticmethod
    def _own_backend_epoch(epoch):
        pid=os.getpid()
        if not epoch or not epoch.startswith(f'{pid}:'): return False
        # Hermes can evict this module during reload, losing its in-memory epoch registry.
        return epoch in CREATED_EPOCHS or serving.backend_epoch_alive(epoch,pid=pid,create_time_tolerance=0)

    def _own_claim(self,o):
        # A replaced delivery runner can leave a queued prompt in this process.
        return o.pid==os.getpid() and self._own_backend_epoch(o.epoch)

    def has_own_work(self):
        return any(self._own_claim(o) for state in ('claimed','started')
                   for o in self.store.list_occurrences(state=state))

    def _foreign_backend_busy(self,session):
        epoch=session.busy_epoch or ''
        return (self.store.backend and not epoch.startswith(f'{os.getpid()}:')
                and serving.backend_epoch_alive(epoch))

    def reconcile(self):
        s=self.store
        with s.atomic():
            noticed=set()
            for w in s.list_wakes() if self.process_start else []:
                reason='invalid_trigger_event' if w.kind in EVENT_KINDS and 'event' in w.trigger_json else (
                    'invalid_fire_on' if w.kind=='command' and w.trigger_json.get('fire_on','change') not in ('change','success','failure','json') else '')
                if reason and w.status in {'active','paused','proposed'}:
                    if s.cas_wake(w.id,w.status,expected_version=w.version,status='failed',status_reason=reason):
                        self.cancel_occurrences(w.id,reason,preserve_started=True)
                        if w.session_key not in noticed:
                            field='event filter' if reason=='invalid_trigger_event' else 'fire_on'
                            self.notice(w.session_key,f'A watcher ({w.id}) used an unsupported {field} and could never fire, so it was stopped. Ask me to set it up again.')
                            noticed.add(w.session_key)
            for o in s.list_occurrences() if self.process_start else []:
                if o.state in {'claimed','started'} and s.backend:
                    if self._own_claim(o) or self._foreign_backend_claim(o): continue
                if o.state=='claimed':
                    self.refund_claimed(o,'restart_refund')
                    s.set_meta('recovered:'+o.id,'1')
                elif o.state=='started': self.interrupt(o,'restart')
            for session in s.list_sessions() if self.process_start else []:
                if session.busy_since is None: continue
                local=(self._own_backend_epoch(session.busy_epoch) if s.backend
                       else session.busy_epoch in LOCAL_EPOCHS)
                if not local and not self._foreign_backend_busy(session):
                    s.cas_session(session.session_key,{'busy_since':session.busy_since},busy_since=None,busy_epoch=None)
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
        return {'cache_ttl':ttl if agent_authored(w) and w.kind in {'every','self'} and not w.trigger_json.get('at') else None,
                'minimum_s':max(self.policy.interval_floor(w),w.pacing_min_s or 0)}

    def daily(self,key):
        return self.store.daily_counts(key,self.policy.resolved_timezone())

    def cancel_occurrences(self,wid,reason,*,preserve_started=False):
        for o in self.store.list_occurrences(wid):
            if reason=='paused' and o.state=='pending' and o.held_since is not None:
                # The pause replaces a message hold's reason, so drop its silence with it, as a held_ deferral does
                # (#217); dispatch checks the message holds again before the turn.
                unsilence={'force_silent':False} if o.force_silent and o.held_reason.startswith('message_held:') else {}
                self.store.update_occurrence(wid,o.dedupe_key,'pending',error='held_paused',held_reason='held_paused',**unsilence)
                self.store.set_meta('not_before:'+o.id,'')
                continue
            if o.state=='started' and (preserve_started or reason=='owner_replied'): continue
            if o.state in {'pending','claimed','started'}:
                self.store.cas_occurrence(wid,o.dedupe_key,o.state,'superseded',
                    attempt=o.attempt if o.state!='pending' else None,force_silent=True,
                    error='expired_held' if o.state=='pending' and o.held_since is not None and reason in {'expired','max_fires','max_lifetime_changed'} else reason)

        wake=self.store.get_wake(wid)
        if wake is not None and wake.platform in LOCAL_SURFACES:
            from .notifier import episode_over, end_episode
            if episode_over(self,wake.session_key): end_episode(self,wake.session_key)

    def recheck_pending(self,old,new):
        target_changed=any(old.trigger_json.get(k)!=new.trigger_json.get(k)
                           for k in ('path','url','command','pattern','pid','cwd','fire_on'))
        watchers=PROBE_KINDS | EVENT_KINDS
        supersede=(old.kind!=new.kind and (old.kind in watchers or new.kind in watchers)
                   or old.kind==new.kind and old.kind in PROBE_KINDS and target_changed)
        refilter=(old.kind==new.kind and new.kind in EVENT_KINDS
                  and any(old.trigger_json.get(k)!=new.trigger_json.get(k) for k in ('emojis','message_id','task_id','board')))
        if new.kind in PROBE_KINDS and (old.kind!=new.kind or target_changed):
            self.store.set_meta('probe_generation:'+new.id,str(int(self.store.get_meta('probe_generation:'+new.id) or 0)+1))
        if not supersede and not refilter: return
        # A wider filter still matches every event folded into a held count; only a narrower one makes it unknowable.
        ot,nt=old.trigger_json,new.trigger_json
        widened=(all(nt.get(k) is None or nt.get(k)==ot.get(k) for k in ('message_id','task_id','board'))
                 and (not nt.get('emojis') or bool(ot.get('emojis')) and set(ot['emojis'])<=set(nt['emojis'])))
        if supersede:
            for o in self.store.list_occurrences(old.id,state='claimed'):
                self.store.set_meta('supersede_on_refund:'+o.id,'trigger_changed')
        for o in self.store.list_occurrences(old.id,state='pending'):
            reason='trigger_changed'
            if refilter:
                if not o.dedupe_key.startswith('e:'): continue
                kept=[p for p in o.payload_json if not isinstance(p,dict) or event_matches(new,p)]
                if kept:
                    updates={'payload_json':kept} if kept!=o.payload_json else {}
                    if o.held_since is not None and not widened: updates['held_matches']=len(kept)
                    if updates: self.store.update_occurrence(old.id,o.dedupe_key,'pending',**updates)
                    continue
                reason='filter_changed'
            if not self.store.cas_occurrence(old.id,o.dedupe_key,'pending','superseded',
                                             force_silent=True,error=reason):
                continue  # A claimed/started occurrence is already in flight.

    def supersede_claimed(self,o):
        """Retire a trigger-invalidated claim before refund or attempt-cap handling."""
        s=self.store
        with s.atomic():
            if s.get_meta('supersede_on_refund:'+o.id):
                # Like an unsent refund, release the daily dispatch reservation.
                # Model turns/spend are charged only after start/API usage.
                return s.cas_occurrence(o.wake_id,o.dedupe_key,'claimed','superseded',attempt=o.attempt,
                    epoch=None,pid=None,claimed_at=None,force_silent=True,error='trigger_changed')
            return False

    def refund_claimed(self,o,error):
        """A claim invalidated by a trigger edit must never return to the queue."""
        s=self.store
        with s.atomic():
            if self.supersede_claimed(o): return True
            return s.refund(o.wake_id,o.dedupe_key,attempt=o.attempt,error=error)

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
            if not self.acquire():
                if self.store.backend and self.eligible() and self.policy.enabled and not self.paused():
                    with self.store.atomic(): self.acks(own_only=True)
                return
            s,now=self.store,self.clock.now()
            self._tick_started_wall=now
            with s.atomic():
                previous=float(s.get_meta('last_tick_at') or now)
                s.set_meta('last_tick_at',str(now))
                s.set_meta('last_tick_monotonic',str(self.clock.monotonic()))
                if now-previous>15:
                    self.log.info('heartbeat2 wall gap %.1fs',now-previous)
                    # A clock gap alone is not proof that a wake was late.
                # The gateway was down (#230). Within one process the monotonic clock must agree, so a wall-clock step
                # is not downtime; after a restart the wall gap is all there is.
                mono,last_mono=self.clock.monotonic(),getattr(self,'_last_tick_mono',None)
                self._last_tick_mono=mono
                if now-previous>OUTAGE_GAP_S and (last_mono is None or mono-last_mono>OUTAGE_GAP_S):
                    self.record_outage(previous,now)
                stopped=self.paused()  # read once per tick, so the outage log and the gate agree (review)
                self.track_switched_off(now,stopped)
                if not self.policy.enabled or stopped: return
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
            if not self.eligible(): return  # Probes can block; the home may be unserved by now.
            with s.atomic():
                for w in s.list_wakes(): self.terminal_one_shot(w)
                self.own_time()
                self.keep_warm()
                self.times()
                self.silence()
                if now-float(s.get_meta('last_gc_at') or 0)>=3600:
                    s.gc(ledger_days=self.policy.ledger_days,timezone=self.policy.resolved_timezone())
                s.set_meta('last_error','')
            self.dispatch_pending()
            self.notifications.tick(self)
        except Exception as exc:
            self.log.warning('heartbeat2 tick failed',exc_info=True)
            try: self.store.set_meta('last_error',type(exc).__name__)
            except Exception: pass

    def dispatch_pending(self):
        occurrences=self.store.list_occurrences(state='pending')
        if self.store.backend:
            self._app_closed_until={o.id:self._app_closed_until[o.id] for o in occurrences if o.id in self._app_closed_until}
        for o in occurrences:
            if not self.eligible(): break
            w=self.store.get_wake(o.wake_id)
            if w is None: continue  # A legacy session may have been stamped after listing.
            cache_line=self.cache_line(w)  # Pricing may fetch metadata; never hold the write lock.
            self.evaluate(w,lambda o=o,cache_line=cache_line:self.dispatch(o,cache_line=cache_line))

    def acks(self, *, own_only=False):
        s=self.store
        try: timeout=float(os.environ.get('HERMES_AGENT_TIMEOUT','1800'))
        except (TypeError,ValueError): timeout=1800
        provider_limit=timeout if math.isfinite(timeout) and timeout>0 else None
        ttl=max((provider_limit or 0)*10,7200)
        now=self.clock.now()
        for o in s.list_occurrences(state='started'):
            if own_only and not self._own_claim(o): continue
            if self._foreign_backend_claim(o): continue
            w=s.get_wake(o.wake_id)
            session=s.get_session(w.session_key)
            activity=max(o.started_at if o.started_at is not None else now,session.last_activity_at or 0 if session else 0)
            limit=provider_limit if provider_limit is not None and s.get_meta('provider_error:'+o.id) else ttl
            if now-activity<limit: continue
            self.interrupt(o,'no_session_end')
        for session in s.list_sessions():
            # A reload retires the hook epoch, but the PID still owns its abandoned mark.
            if own_only and not self._own_backend_epoch(session.busy_epoch): continue
            if (session.busy_since is not None and now-max(session.busy_since,session.last_activity_at or 0)>=ttl
                    and not self._foreign_backend_busy(session)):
                s.cas_session(session.session_key,{'busy_since':session.busy_since,'last_activity_at':session.last_activity_at},busy_since=None,busy_epoch=None)
        for o in s.list_occurrences(state='claimed'):
            if own_only and not self._own_claim(o): continue
            if self._foreign_backend_claim(o): continue
            if o.claimed_at is None or self.clock.now()-o.claimed_at<600: continue
            # A backend can accept a detached chat. Only refund its accept once the full
            # acknowledgement window elapsed with no running turn it could be queued behind.
            if s.backend and (accepted:=int(s.get_meta('accepted:'+o.id) or 0)):
                w=s.get_wake(o.wake_id)
                session=s.get_session(w.session_key)
                busy=session and session.busy_since is not None and (
                    not session.busy_epoch or serving.backend_epoch_alive(session.busy_epoch))
                if not busy:
                    self._hold_backend_app_closed(w,o,accepted=accepted)
                    continue
            if int(s.get_meta('accepted:'+o.id) or o.attempt)>=3:
                if self.supersede_claimed(o): continue
                s.cas_occurrence(o.wake_id,o.dedupe_key,'claimed','dead',attempt=o.attempt,error='ack_timeout')
                self.notice(s.get_wake(o.wake_id).session_key,'A wake was not acknowledged after three attempts.')
                self.terminal_one_shot(s.get_wake(o.wake_id))
                governor.error(self,s.get_wake(o.wake_id))
            else: self.refund_claimed(o,'ack_timeout')

    def _hold_backend_app_closed(self,w,o,*,accepted=0):
        from .notifier import episode, episode_over, end_episode, EPISODE_PREFIX
        s=self.store
        key=w.session_key
        with s.atomic():
            if not self.refund_claimed(o,'app_closed'): return
            now=self.clock.now()
            if accepted:
                s.set_meta('accepted:'+o.id,str(accepted-1))
                s.set_meta('not_before:'+o.id,str(now+1))
                reinject_key=BACKEND_REINJECT_PREFIX+o.id
                if s.get_meta(reinject_key) is None: s.set_meta(reinject_key,'0')
                session=s.get_session(key)
                if session and session.busy_since is not None:
                    s.cas_session(key,{'busy_epoch':session.busy_epoch,'busy_since':session.busy_since},
                                  busy_since=None,busy_epoch=None)
            else:
                # A miss in this registry does not delay a successor with the chat open.
                self._app_closed_until[o.id]=now+60
            if episode_over(self,key): end_episode(self,key)
            if not s.get_meta('held_since:'+o.id): s.set_meta('held_since:'+o.id,str(now))
            if not is_keep_warm(w) and not s.get_meta(EPISODE_PREFIX+key):
                s.set_meta(EPISODE_PREFIX+key,json.dumps(episode(self,key)))

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
        if not event_matches(w,payload): return
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
            # A bounded batch per tick (four waves of the four-worker pool), so many slow watchers can't hold
            # back time wakes for long; the rest stay due and run on the next tick, most overdue first.
            for w in sorted(s.list_wakes(status='active'),key=lambda w: w.next_due_at if w.next_due_at is not None else math.inf):
                if len(due)>=PROBES_PER_TICK: break
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
        if not s.cas_wake(w.id,'active',expected_version=w.version,checks=w.checks+1): return
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
            # A retargeted check starts a fresh namespace, so its first result is never deduplicated against the old target (#149).
            generation=s.get_meta('probe_generation:'+w.id)
            digest=hashlib.sha1((identity+(':g'+generation if generation else '')).encode()).hexdigest()
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

    def bring_role_forward(self, wake_id):
        """Make work due without bypassing any of the normal dispatch gates."""
        s,now=self.store,self.clock.now()
        with s.atomic():
            target=s.get_wake(wake_id)
            if target is not None and target.role and target.status=='active':
                if ((target.next_due_at is None or target.next_due_at>now)
                        and s.cas_wake(target.id,'active',expected_version=target.version,next_due_at=now)):
                    # Marks this time as due on purpose, so follow_zone doesn't take it for one set in another zone.
                    s.set_meta('brought_forward:'+target.id,f'{now:.17g}')

    def handoff(self, source, o):
        s,now=self.store,self.clock.now()
        with s.atomic():
            target=s.get_wake(s.get_meta('handoff_to:'+source.id) or '')
            if (target is None or not target.role or target.session_key!=source.session_key
                    or target.id==source.id or target.status!='active'):
                self.log.info('role handoff skipped: target is not an active role in the same chat')
                return
            key='handoff_in:'+target.id
            items=json.loads(s.get_meta(key) or '[]')
            if any(item['occ']==o.id for item in items): return
            items.append({'from':source.id,'title':source.title or source.note,
                          'summary':o.summary[:500],'occ':o.id,'at':now})
            s.set_meta(key,json.dumps(items[-10:]))
            self.bring_role_forward(target.id)

    def track_switched_off(self,now,stopped):
        """A stretch with HeartBeat switched off (settings.enabled false) or Hermes' emergency stop is an outage too (#230),
        before or after a reminder is queued, so both paths measure it the same way."""
        off=self.store.get_meta('off_since')
        if not self.policy.enabled or stopped:
            if not off: self.store.set_meta('off_since',str(now))
        elif off:
            self.record_outage(float(off),now)
            self.store.set_meta('off_since','')

    def record_outage(self,start,end):
        outages=json.loads(self.store.get_meta('outages') or '[]')
        self.store.set_meta('outages',json.dumps((outages+[[start,end]])[-OUTAGES_KEPT:]))

    def downtime_since(self,since,before=0):
        """`before` plus the outage time after `since`; overlapping outages (down while switched off) count once."""
        now,total,reach=self.clock.now(),0.0,since
        for start,end in sorted(json.loads(self.store.get_meta('outages') or '[]')):
            start,end=max(start,reach),min(end,now)
            if end>start: total,reach=total+end-start,end
        return before+total

    def owner_turn_owed(self,w,now):
        """The owner's own one-time reminder that came due in time keeps its one turn even when its expiry or lifetime passed
        while HeartBeat or the gateway was down (#230). It fires late, saying so, or reports the miss; never silently."""
        if not (owner_class(w) and one_shot(w)): return False
        if w.next_due_at is not None:
            due=w.next_due_at if w.next_due_at<=now else None
        else:
            live=[o for o in self.store.list_occurrences(w.id) if o.state in {'pending','claimed','started'} and o.dedupe_key.startswith('t:')]
            due=float(live[-1].dedupe_key[2:]) if live else None
        if due is None: return False
        bounds=[w.created_at+self.policy.max_life_s(w),w.created_at+self.policy.due_bound_s(w)]+([w.expires_at] if w.expires_at is not None else [])
        return due<=min(bounds)

    def _time(self,w):
        s,now=self.store,self.clock.now()
        if self.end_max_fires(w): return
        if is_keep_warm(w): return  # keep_warm() schedules it from the chat's last cache touch.
        # Retain the existing schedule decisions, isolated per wake/savepoint.
        if w.status=='active':
            owed=self.owner_turn_owed(w,now)
            if w.expires_at is not None and w.expires_at<=now and not owed:
                s.cas_wake(w.id,'active',status='expired',status_reason='expired')
                self.cancel_occurrences(w.id,'expired')
                return
            hard_end=w.created_at+self.policy.max_life_s(w)
            # A reminder still not due at its due bound (the setting was lowered) ends there; the grace is for a due one.
            due_end=w.created_at+self.policy.due_bound_s(w)
            late=due_end<=now and w.next_due_at is not None and w.next_due_at>due_end
            if not (w.role or w.trigger_json.get('builtin')) and (hard_end<=now or late) and not owed:
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
                tz=self.policy.resolved_timezone()
                w=self.follow_zone(w,tz,now)
                # A report-back is due now by design and waits on holds, so the timer catch-up cutoff never misses it (#137).
                # Nor the owner's own one-time reminder (#230): it fires late and says so, or past the late window its turn
                # tells the owner it was missed. Only the agent's own one-shots end as missed.
                owner_reminder=owner_class(w) and one_shot(w)
                due=time_due(w,now=now,catch_up_s=10**9 if w.trigger_json.get('report_back') or owner_reminder else self.policy.catch_up_hours*3600,
                             timezone=tz,jitter_unit=self.jitter(),**self.timing(w))
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
                        when=datetime.fromtimestamp(float(due.dedupe_key[2:]),zone(tz))
                        from .wake_text import escape_text
                        name=escape_text(' '.join((w.title or w.note).split())[:60])
                        self.notice(w.session_key,f'A wake ({w.id}'+(f': {name}' if name else '')+f', due {when:%a %d %b %H:%M}) was too late after downtime and was missed.')
            elif w.kind in PROBE_KINDS|EVENT_KINDS and w.deadline_at is not None and w.deadline_at<=now:
                # Deadline fallback is one fire only, and only when no trigger
                # occurrence has already been produced (including pending).
                # Marked occurrences belong to an old trigger, in any state.
                if not [o for o in s.list_occurrences(w.id)
                        if not (o.state=='superseded' and o.error in {'filter_changed','trigger_changed'})
                        and not s.get_meta('supersede_on_refund:'+o.id)]:
                    s.add_occurrence(w.id,f't:{w.deadline_at:.17g}',payload=[{'message':'deadline passed — nothing happened'}])
                s.cas_wake(w.id,'active',expected_version=w.version,deadline_at=None)

    def follow_zone(self,w,tz,now):
        """A cron or `every … at` time set in another zone moves to the nearest of the wake's local times in this one (#170).

        West, the stored time comes early: it moves on to the local time, which hasn't come yet. East, the local time comes
        first: the wake moves back to it, or, when it has already passed and wasn't served, fires once now in its place.
        Either way the wake fires once for that local time, then the scheduler carries on in the new zone.
        """
        due=w.next_due_at
        if due is None: return w
        # A handoff made the role due now (#138): off its local schedule on purpose, not set in another zone.
        if self.store.get_meta('brought_forward:'+w.id)==f'{due:.17g}': return w
        around=local_neighbours(w,due,tz)
        if around is None: return w
        before,after=around
        target=before if due-before<after-due else after
        if target<=now:
            if due<=now: return w  # Due either way: it fires now.
            served=((w.last_fired_at is not None and w.last_fired_at>=target)
                    or self.store.get_occurrence(w.id,f't:{target:.17g}') is not None)
            # However late, like any overdue recurring time: the move is at most the zone shift, and waiting loses the day.
            target=after if served else now
        if target==due: return w
        if not self.store.cas_wake(w.id,'active',expected_version=w.version,next_due_at=target): return w
        self.log.info('heartbeat2 wake %s: next time %.0f is off its local schedule in this zone, now %.0f',w.id,due,target)
        return self.store.get_wake(w.id)

    def cancel_non_gateway_builtins(self, *, own_time=False):
        for w in self.store.list_wakes():
            # Only live built-ins: a paused one doesn't fire and keeps the owner's or governor's pause.
            if (not w.trigger_json.get('builtin') or bool(w.trigger_json.get('own_time')) != own_time
                    or w.status not in {'active','proposed'}): continue
            session = self.store.get_session(w.session_key)
            if session is not None and (not deliverable_platform(session.platform)
                    or not own_time and w.kind=='owner_silent' and session.platform in LOCAL_SURFACES):
                if self.store.cas_wake(w.id,w.status,expected_version=w.version,status='cancelled',status_reason='non_gateway'):
                    self.cancel_occurrences(w.id,'non_gateway',preserve_started=True)

    def own_time_refusal(self,chat,key):
        """Why own time may not run in this chat (a session or wake), or '' (#177). A chat known to be one-to-one has it
        unless the owner turned it off there; an app chat, group, topic, group DM or a chat whose platform can't tell, only where the
        owner turned it on with /pulse owntime on. Each topic is a chat."""
        if self.store.backend: return 'backend_schedule_only'
        meta=self.store.get_meta
        if meta('own_time_off:'+key)=='1': return 'chat_off'
        if is_direct_chat(chat.platform,chat.chat_type,chat.chat_id): return ''
        if self.policy.dm_only and chat.chat_type!='dm': return 'dm_only'
        return '' if meta('own_time_on:'+key)=='1' else 'not_opted_in'

    def own_time(self):
        s,now=self.store,self.clock.now()
        hours=self.policy.own_time_hours
        for w in s.list_wakes():
            if w.trigger_json.get('own_time') and not hours and (w.status != 'cancelled' or w.status_reason != 'settings'):
                if s.cas_wake(w.id,w.status,expected_version=w.version,status='cancelled',status_reason='settings'):
                    self.cancel_occurrences(w.id,'settings',preserve_started=True)
        if not hours: return
        self.cancel_non_gateway_builtins(own_time=True)
        for w in s.list_wakes():
            # A paused wake is left paused (a governor pause outlasts off and on); dispatch refuses its turns meanwhile.
            if not w.trigger_json.get('own_time') or w.status not in {'active','proposed'}: continue
            session=s.get_session(w.session_key)
            reason=self.own_time_refusal(session if session is not None else w,w.session_key)
            if reason and s.cas_wake(w.id,w.status,expected_version=w.version,status='cancelled',status_reason=reason):
                self.cancel_occurrences(w.id,reason,preserve_started=True)
        floor=max(self.policy.min_interval_minutes,self.policy.agent_ungated_floor_minutes)*60
        interval=max(hours*3600,floor)
        maximum=max(2*hours*3600,floor)
        for session in s.list_sessions():
            if not deliverable_platform(session.platform) or self.own_time_refusal(session,session.session_key): continue
            if session.last_human_at is None or not session.episode_id: continue
            wid='owntime_'+hashlib.sha256(session.session_key.encode()).hexdigest()[:16]
            w=s.get_wake(wid)
            if w is None:
                s.create_wake(Wake(session.session_key,
                    'This is your own time. Look at your desk: work on what you own, follow up on something you noticed, '
                    'think something through, tidy your notes or memory, or rest. Message the owner only if it\'s worth their attention.',
                    'self',id=wid,platform=session.platform,chat_type=session.chat_type,chat_id=session.chat_id,
                    armed_session_id=session.current_session_id,creator='user',title='Own time',reason='Own time.',
                    trigger_json={'builtin':True,'own_time':True,'hours':hours,'floor':floor},base_interval_s=interval,eff_interval_s=interval,
                    pacing_min_s=floor,pacing_max_s=maximum,next_due_at=now+interval))
                continue
            updates={}
            # Every reason own time stops on its own comes back here, once this chat may have it again ('not_dm' is 0.4.1's).
            reactivate=w.status in {'done','orphaned'} or w.status=='cancelled' and w.status_reason in {'settings','non_gateway','not_dm','dm_only','chat_off','not_opted_in'}
            if w.trigger_json.get('hours')!=hours or w.trigger_json.get('floor',floor)!=floor or reactivate:
                updates.update(base_interval_s=interval,eff_interval_s=interval,pacing_min_s=floor,
                               pacing_max_s=maximum,next_due_at=now+interval,
                               trigger_json={**w.trigger_json,'hours':hours,'floor':floor})
            if reactivate:
                updates.update(status='active',status_reason='',armed_session_id=session.current_session_id,
                               fires=0,max_fires=None,babysit=False)
            if updates: s.cas_wake(w.id,w.status,expected_version=w.version,**updates)

    def arm_report_back(self,session_key):
        """One report-back turn per chat, shared by its held watcher results."""
        s,now=self.store,self.clock.now()
        with s.atomic():
            session=s.get_session(session_key)
            if session is None: return
            wid='report_'+hashlib.sha256(session_key.encode()).hexdigest()[:16]
            w=s.get_wake(wid)
            if w and any(o.state in {'pending','claimed','started'} for o in s.list_occurrences(wid)): return
            trigger={'at':datetime.fromtimestamp(now,timezone.utc).isoformat(),'builtin':True,'report_back':True}
            if w is None:
                s.create_wake(Wake(session_key,
                    'Messages were held while you worked. Pass on what still matters from the list below in one short message, or end silently if nothing does.',
                    'at',id=wid,platform=session.platform,chat_type=session.chat_type,chat_id=session.chat_id,
                    armed_session_id=session.current_session_id,creator='agent',created_in='command',title='Report back',
                    reason="An agent watcher's turn left a report while messages were held.",
                    trigger_json=trigger,next_due_at=now))
            elif w.status in {'active','done','failed','cancelled','expired'}:
                # A skipped one-shot has status failed; a later watcher report can arm it again.
                s.cas_wake(w.id,w.status,expected_version=w.version,status='active',status_reason='',
                           next_due_at=now,armed_session_id=session.current_session_id,trigger_json=trigger)

    def keep_warm(self):
        """Queue one silent ping per cache touch, just before the chat's prompt cache would expire (#163)."""
        for w in self.store.list_wakes(status='active'):
            if is_keep_warm(w): self.evaluate(w,lambda w=w:self._keep_warm(w))

    def keep_warm_blocked(self,w,plan,route=''):
        """Tell the owner once when keep-warm can't ping for a reason other than rest or a small context (#234): one line on
        their own next message, after it has lasted two hours on this route. A new kind of reason tells them again."""
        key,card='keep_warm_blocked:'+w.id,'card:'+w.id
        kind=plan.get('blocked') or ''
        old=json.loads(self.store.get_meta(key) or '{}')
        pending=bool(old) and self.store.get_meta(card)==old.get('reason')  # our line, not shown yet
        if old and route and old.get('route') and old['route']!=route:
            # A model switch is a new route: it gets its own two hours, and the old route's unshown line goes (review).
            # No route yet (/new clears it until the next model call) or state without one (0.4.32) is not a switch.
            self.store.set_meta(key,'')
            if pending: self.store.set_meta(card,'')
            old,pending={},False
        if kind:
            now=self.clock.now()
            since=old.get('since',now) if old.get('kind')==kind else now
            told=old.get('told',False) if old.get('kind')==kind else False
            # A route still learning after a model switch is not news: tell the owner once it has lasted two hours (review).
            due=not told and now-since>=KEEP_WARM_NOTICE_AFTER
            self.store.set_meta(key,json.dumps({'kind':kind,'reason':plan['reason'],'since':since,'told':told or due,'route':route}))
            # The same kind with new numbers only refreshes a line not yet shown; a new kind drops it and waits its own two
            # hours, so the owner isn't told twice (review).
            if due or (pending and old.get('kind')==kind): self.store.set_meta(card,plan['reason'])
            elif pending: self.store.set_meta(card,'')
        elif old and 'due_at' in plan:
            # Cleared only when a ping is planned again: rest or a small context in between doesn't make it tell again (review).
            self.store.set_meta(key,'')
            if pending: self.store.set_meta(card,'')

    def _keep_warm(self,w):
        from .cache import keep_warm_plan, keep_warm_key
        s,now=self.store,self.clock.now()
        session=s.get_session(w.session_key)
        if session is None or session.platform not in MESSAGING_PLATFORMS: return
        plan=keep_warm_plan(s,session,self.policy,now)
        self.keep_warm_blocked(w,plan,session.last_route or '')
        due=plan.get('due_at')
        if due!=w.next_due_at:
            if not s.cas_wake(w.id,'active',expected_version=w.version,next_due_at=due): return
            w=s.get_wake(w.id)
        if due is None or now<due: return
        if any(o.state in {'pending','claimed','started'} for o in s.list_occurrences(w.id)): return
        s.add_occurrence(w.id,keep_warm_key(plan['touch']))

    def silence(self):
        s,now=self.store,self.clock.now()
        if not self.policy.source_silence: return
        if self.policy.owner_silent_checkin:
            self.cancel_non_gateway_builtins()
            for session in s.list_sessions():
                if not deliverable_platform(session.platform) or session.platform in LOCAL_SURFACES: continue
                if session.last_human_at is None or not session.episode_id: continue
                identity=hashlib.sha1(session.session_key.encode()).hexdigest()[:16]
                wid='checkin_'+identity
                w=s.get_wake(wid)
                if w is None:
                    s.create_wake(Wake(session.session_key,'Check in gently if there is something helpful to offer. Silence is fine.',
                        'owner_silent',id=wid,platform=session.platform,chat_type=session.chat_type,chat_id=session.chat_id,
                        armed_session_id=session.current_session_id,creator='user',reason='The owner has been quiet.',
                        trigger_json={'after':self.policy.checkin_after_hours*3600,'builtin':True}))
                elif w.status in {'done','orphaned'} or w.status=='cancelled' and w.status_reason=='non_gateway':
                    # This system wake belongs to the chat, not one agent id; it comes back when the chat is deliverable again.
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

    def message_hold_reason(self,w,s):
        """Agent recurring work and watchers may continue while their messages are held."""
        if s is None or not holds_messages(w):
            return ''
        from .commands import quiet_until
        if quiet_until(self,s.session_key,urgent=w.urgent) is not None:
            return 'quiet_hours'
        if self.policy.unanswered_gate(w,s.unanswered_count):
            return 'unanswered'
        if self.daily(s.session_key)['delivered']>=self.policy.max_messages_per_day:
            return 'daily_message_cap'
        return ''

    def gate(self,w,o,s):
        now=self.clock.now()
        if o.state=='pending' and o.held_since is not None and o.error.startswith('held_'):
            self.store.set_meta('not_before:'+o.id,'')
        if w.kind=='owner_silent' and o.dedupe_key.startswith('s:') and (s is None or o.dedupe_key!='s:'+(s.episode_id or '')):
            return 'episode_ended',False,None
        if w.trigger_json.get('own_time'):
            if not self.policy.own_time_hours: return 'setting_off',False,None
            refusal=self.own_time_refusal(s if s is not None else w,w.session_key)
            if refusal in {'chat_off','not_opted_in'}: return refusal,False,None
        elif is_keep_warm(w):
            from .cache import keep_warm_plan, keep_warm_key
            plan=keep_warm_plan(self.store,s,self.policy,now)
            # A real turn since this ping was queued already warmed the cache, or the cache went cold, or the chat rests.
            if 'due_at' not in plan or o.dedupe_key!=keep_warm_key(plan['touch']): return 'keep_warm_not_needed',False,None
        elif w.trigger_json.get('report_back'):
            if not self.store.held_reports(w.session_key): return 'nothing_to_report',False,None
        elif w.trigger_json.get('builtin'):
            if s and s.platform in LOCAL_SURFACES: return 'setting_off',False,None
            if not self.policy.owner_silent_checkin: return 'setting_off',True,None
            after=self.policy.checkin_after_hours*3600*(2 if w.ignored_streak else 1)
            if s and s.last_human_at is not None and now-s.last_human_at<after: return 'checkin_not_due',True,None
        held_paused=o.state=='pending' and w.status=='paused' and (o.held_since is not None or is_watcher(w))
        if w.status!='active' and not held_paused: return 'expired' if w.status=='expired' else 'wake_inactive',False,None
        if w.expires_at is not None and now>=w.expires_at and not self.owner_turn_owed(w,now): return 'expired',False,None
        if w.max_fires is not None and w.fires>=w.max_fires: return 'max_fires',False,None
        if held_paused: return 'held_paused',True,None
        if w.cancel_on_reply and s and s.last_human_at is not None and s.last_human_at>w.created_at: return 'cancel_on_reply',False,None
        if s is None: return 'missing_session',False,None
        if not s.enabled: return 'session_off',False,None
        if s.paused_until is not None and s.paused_until>now: return 'session_paused',True,None
        if self.policy.dm_only and (s.chat_type!='dm' or w.chat_type!='dm'): return 'dm_only',False,None
        if not self.deliverable(s.platform): return 'non_gateway',False,None
        # The owner has been away sleep_after_days: the agent's own work runs no turn until they write (#62). This comes
        # before the message holds, which still run a turn silently. A report-back passes only for agreed results.
        if sleeps(w) and self.policy.asleep(s,now):
            if w.trigger_json.get('report_back'):
                from .desk import agreed_reports
                if not agreed_reports(self.store,s.session_key): return 'asleep',False,None
            elif is_watcher(w): return 'held_asleep',True,None
            else: return 'asleep',False,None
        health=json.loads(self.store.get_meta('provider_health') or '{}')
        if health.get('trial'):
            wid,key=health['trial'].split('/',1)
            trial=self.store.get_occurrence(wid,key)
            if not trial or trial.state not in {'claimed','started'}:
                health.pop('trial'); self.store.set_meta('provider_health',json.dumps(health))
        if health and (health['until']>now or health.get('trial')):
            return 'provider_unavailable',True,health['until'] if health['until']>now else None
        if w.role and self.store.dispatches_today(w.id,self.policy.resolved_timezone())>=self.policy.role_budget(w):
            return 'role_budget',False,None
        if (not owner_class(w) and not is_keep_warm(w) and self.policy.daily_cost_limit_usd>0
                and self.store.agent_cost_today(w.session_key,self.policy.resolved_timezone())['cost_microusd']/1e6
                    >=self.policy.daily_cost_limit_usd):
            return 'daily_cost_cap',False,None
        # Message gates silence agent recurring work and watchers; spend and dispatch gates
        # still apply. Other wakes retain the existing hold/drop ordering.
        message_held=self.message_hold_reason(w,s)
        from .commands import quiet_until
        quiet=quiet_until(self,s.session_key,urgent=w.urgent)
        if quiet is not None and not message_held: return 'quiet_hours',w.kind not in RECURRING_KINDS,quiet
        mute=self.policy.unanswered_gate(w,s.unanswered_count)
        if mute and w.trigger_json.get('report_back'):
            # A watcher the owner asked for may have been held by quiet hours or a cap; the mute doesn't hold its
            # result, so the report-back passes it on (and only it) (#108).
            from .desk import agreed_reports
            if agreed_reports(self.store,s.session_key): mute=None
        if mute and not message_held: return mute,mute=='held_unanswered',None
        totals=self.daily(s.session_key)
        cap='daily_model_cap' if totals['model_turns']>=self.policy.max_model_wakes_per_day else 'daily_message_cap' if totals['delivered']>=self.policy.max_messages_per_day else ''
        # Keep-warm pings are silent cache reads the owner turned on: they neither use nor count against wake turns.
        if cap and not is_keep_warm(w) and not (cap=='daily_message_cap' and message_held) and not (owner_class(w) and one_shot(w)):
            # A report-back turn waits for the next local day like a watcher, so what was held still arrives (#137).
            if is_watcher(w) or w.trigger_json.get('report_back'):
                return 'held_daily_cap',True,None
            return cap,False,None
        flood=json.loads(self.store.get_meta('injections') or '[]')
        if sum(now-t<60 for t in flood)>=5: return 'global_flood',True,None
        if now<float(self.store.get_meta('not_before:'+o.id) or 0):
            return ('app_closed' if o.error=='app_closed' else 'not_before'),True,None
        if o.error=='app_closed' and now<self._app_closed_until.get(o.id,0): return 'app_closed',True,None
        if s.busy_since is not None: return 'busy',True,None
        if s.last_wake_turn_at is not None and now-s.last_wake_turn_at<self.policy.wake_spacing_minutes*60: return 'wake_spacing',True,None
        if self.store.list_occurrences(session_key=s.session_key,state='claimed') or self.store.list_occurrences(session_key=s.session_key,state='started'): return 'in_flight',True,None
        if not self.family_enabled(w): return 'source_disabled',False,None
        if not self.injection_allowed(): return 'injection_disabled',True,None
        return '',False,None

    def dispatch(self,o,*,cache_line=None):
        # The served set can shrink mid-tick; an unserved home's wake stays untouched for its own gateway.
        if not self.eligible(): return
        s=self.store
        # The tick lists occurrences before this transaction; act on the stored row, never a stale copy (#149).
        o=s.get_occurrence(o.wake_id,o.dedupe_key)
        if o is None or o.state!='pending': return
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
                if held and o.force_silent and o.held_reason.startswith('message_held:'):
                    # A held_ reason replaces a message hold; drop its silence with it, or it could never be cleared
                    # (dispatch only clears a message_held: silence) and the owner's result would end as [SILENT].
                    # Dispatch checks the message holds again before the turn.
                    held['force_silent']=False
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
        message_held=self.message_hold_reason(w,session)
        if is_keep_warm(w):
            # A keep-warm ping never speaks, whatever holds apply.
            if not o.force_silent and not s.update_occurrence(w.id,o.dedupe_key,'pending',force_silent=True): return
        elif message_held and (not o.force_silent or o.held_reason.startswith('message_held:')):
            if not s.update_occurrence(w.id,o.dedupe_key,'pending',force_silent=True,
                                       held_reason='message_held:'+message_held): return
        elif not message_held and o.force_silent and o.held_reason.startswith('message_held:'):
            if not s.update_occurrence(w.id,o.dedupe_key,'pending',force_silent=False,held_reason=''): return
        with s.atomic():
            if not s.claim(w.id,o.dedupe_key,epoch=self.epoch,pid=os.getpid()): return
            s.set_meta('spend:'+o.id, 'owner' if owner_class(w) or is_keep_warm(w) else 'agent')
        o=s.get_occurrence(w.id,o.dedupe_key)
        health=json.loads(s.get_meta('provider_health') or '{}')
        if health:
            health['trial']=o.id
            s.set_meta('provider_health',json.dumps(health))
        from .desk import held_report_lines, _plain, _clip, _oneline
        reports=''
        if (w.role or w.trigger_json.get('own_time') or w.trigger_json.get('report_back')) and not message_held and not o.force_silent:
            report_lines,shown=held_report_lines(s,w.session_key,payload_ok=bool(w.trigger_json.get('report_back')),
                agreed_only=bool(w.trigger_json.get('report_back') and (self.policy.unanswered_gate(w,session.unanswered_count)
                                                                        or self.policy.asleep(session,self.clock.now()))))
            if report_lines:
                reports='\n'.join(['Held for the owner (their limits held these earlier). If you message them this turn, include what still matters:', *report_lines])
                s.set_meta('report_shown:'+o.id,json.dumps(shown))
        handoffs=''
        # Message holds silence the reply, not this work input. Other forced-silent turns keep it for later.
        if w.role and (not o.force_silent or o.held_reason.startswith('message_held:')):
            with s.atomic():
                items=json.loads(s.get_meta('handoff_in:'+w.id) or '[]')
                if items:
                    lines=['Handed off to you by another role in this chat (its latest result; continue from it):']
                    for item in items[:3]:
                        title=_plain(_clip(_oneline(item['title']),60))
                        summary=_plain(_clip(_oneline(item['summary']),240))
                        lines.append(f'- {title} ({item["from"]}): {summary}')
                    if len(items)>3:
                        lines.append(f'- … {len(items)-3} more after these (they come next time)')
                    handoffs='\n'.join(lines)
                    s.set_meta('handoff_shown:'+o.id,json.dumps([item['occ'] for item in items[:3]]))
        with s.atomic():
            shown=s.get_meta('stop_hint_shown')
            if shown is None:
                # Migrate old notices once; later own-time deliveries aren't hint deliveries.
                shown='1' if any(chat.first_notice_sent for chat in s.list_sessions()) else '0'
                s.set_meta('stop_hint_shown',shown)
            chat_shown=s.get_meta('stop_hint_chat:'+session.session_key)
            if chat_shown is None:
                chat_shown='1' if session.first_notice_sent else '0'
                s.set_meta('stop_hint_chat:'+session.session_key,chat_shown)
        text=self.render_occurrence(w,o,session,held_by=held_by,message_held=message_held,
                                    cache_line=cache_line,handoffs=handoffs,held_reports=reports)
        from .wake_text import offers_stop_hint
        offered=not is_keep_warm(w) and offers_stop_hint(w,session,stop_hint=self.policy.stop_hint,
            stop_hint_shown=shown=='1',stop_hint_chat_shown=chat_shown=='1',message_held=message_held)
        s.set_meta('stop_hint_offered:'+o.id,'1' if offered else '')
        if not self.eligible():
            # Lost the home after claiming: give the claim back unsent, as it was.
            if health:
                health.pop('trial',None); s.set_meta('provider_health',json.dumps(health))
            self.refund_claimed(o,held_by)
            return
        inject_raised=False
        try: accepted=self.ctx.inject_message(text,session_key=w.session_key)
        except Exception as exc:
            inject_raised=True
            accepted=False
            self.log.warning('heartbeat2 injection failed: %s',type(exc).__name__)
        if accepted:
            if not s.backend:
                if session.platform in LOCAL_SURFACES:
                    from .notifier import end_episode
                    end_episode(self,session.session_key)
                s.set_meta('held_since:'+o.id,'')
            elif (resends:=s.get_meta(BACKEND_REINJECT_PREFIX+o.id)) is not None:
                s.set_meta(BACKEND_REINJECT_PREFIX+o.id,str(int(resends)+1))
            s.set_meta('accepted:'+o.id,str(int(s.get_meta('accepted:'+o.id) or 0)+1))
            flood=[t for t in json.loads(s.get_meta('injections') or '[]') if self.clock.now()-t<60]
            s.set_meta('injections',json.dumps(flood+[self.clock.now()]))
            # A silent ping isn't a wake turn: it must not push the chat's real wakes back by wake spacing.
            if not s.backend and not is_keep_warm(w):
                s.cas_session(session.session_key,{'last_wake_turn_at':session.last_wake_turn_at},last_wake_turn_at=self.clock.now())
        else:
            if health:
                health.pop('trial',None); s.set_meta('provider_health',json.dumps(health))
            if session.platform in LOCAL_SURFACES and accepted is False and not inject_raised:
                if s.backend:
                    self._hold_backend_app_closed(w,o)
                    return
                # A retired window rejects synchronously: no turn ran and no delivery failed. Keep its original
                # waiting time separately from watcher/message holds, so those holds and silence still work unchanged.
                with s.atomic():
                    if self.refund_claimed(o,'app_closed'):
                        from .notifier import episode_over, end_episode
                        if episode_over(self,w.session_key): end_episode(self,w.session_key)
                        if not s.get_meta('held_since:'+o.id): s.set_meta('held_since:'+o.id,str(self.clock.now()))
                        retry_at=self.clock.now()+60
                        s.set_meta('not_before:'+o.id,str(retry_at))
                        if not is_keep_warm(w):
                            from .notifier import episode, EPISODE_PREFIX
                            if not s.get_meta(EPISODE_PREFIX+w.session_key):
                                s.set_meta(EPISODE_PREFIX+w.session_key,json.dumps(episode(self,w.session_key)))
                return
            count=int(s.get_meta('inject_failures:'+o.id) or 0)+1
            s.set_meta('inject_failures:'+o.id,str(count))
            s.set_meta('not_before:'+o.id,str(self.clock.now()+RETRY_DELAYS[min(count-1,4)]))
            if count>=5:
                if self.supersede_claimed(o): return
                s.cas_occurrence(w.id,o.dedupe_key,'claimed','dead',attempt=o.attempt,error='inject_failed')
                self.terminal_one_shot(s.get_wake(w.id))
                governor.error(self,s.get_wake(w.id))
            else: self.refund_claimed(o,'inject_failed')

    def render_occurrence(self,w,o,session,*,held_by='',message_held='',cache_line='',handoffs='',held_reports=''):
        s=self.store
        from .wake_text import render_wake, render_keep_warm, escape_text
        from .desk import render_desk
        from .intake import marker_tag
        shown=s.get_meta('stop_hint_shown')
        if shown is None:
            shown='1' if any(chat.first_notice_sent for chat in s.list_sessions()) else '0'
        chat_shown=s.get_meta('stop_hint_chat:'+session.session_key)
        if chat_shown is None:
            chat_shown='1' if session.first_notice_sent else '0'
        tag=marker_tag(s.marker_secret(),o.id,o.attempt) if session.chat_type!='dm' else None
        if is_keep_warm(w): return render_keep_warm(w,o,marker_tag=tag)
        recent=[x.outcome or x.state for x in s.list_occurrences(w.id) if x.state=='done'][-3:]
        late=downtime=None
        if o.dedupe_key.startswith('t:'):
            try:
                late=max(0,self.clock.now()-float(o.dedupe_key[2:]))
                # Downtime (#230): how late it was when it was queued, plus any outage since (the gateway down, or
                # HeartBeat switched off), so a turn queued or claimed before a crash still counts it. Deliberate holds
                # such as quiet hours don't count.
                due_at=float(o.dedupe_key[2:])
                downtime=self.downtime_since(o.created_at,max(0,o.created_at-due_at)) if o.created_at is not None else None
            except ValueError: pass
        backend_reinject=s.backend and s.get_meta(BACKEND_REINJECT_PREFIX+o.id) is not None
        reminder=owner_class(w) and one_shot(w)
        if backend_reinject:
            # The turn could not start, even though the app acknowledged the injection.
            held_by='app_closed'
            reminder=reminder or agreed_watcher(w)
            matched_at=o.matched_at if o.matched_at is not None else o.created_at
            if late is None and matched_at: late=max(0,self.clock.now()-matched_at)
            if late is not None: downtime=late
        return render_wake(w,o,session,clock=self.clock,timezone=self.policy.resolved_timezone(),
             stop_hint=self.policy.stop_hint,stop_hint_shown=shown=='1',stop_hint_chat_shown=chat_shown=='1',
             recent_outcomes=recent,messages_today=self.daily(session.session_key)['delivered']-int(not o.force_silent and o.state in {'claimed','started'}),
             max_messages=self.policy.max_messages_per_day,governor_hint=governor.hint(self,w),late_by_s=late,
             after_restart=bool(s.get_meta('recovered:'+o.id)),held_by=held_by,message_held=message_held,
             late_window_s=self.policy.late_window_s() if reminder else None,downtime_s=downtime,
             backend_reinject=backend_reinject,
             cache_line=cache_line,marker_tag=tag,handoffs=handoffs,held_reports=held_reports,
             desk=render_desk(self,session,clock=self.clock,timezone=self.policy.resolved_timezone(),policy=self.policy,
                              measure=lambda text: len(escape_text(text)),turn=o)
                  if w.role or w.trigger_json.get('own_time') else '')

    def terminal_one_shot(self,w):
        if not one_shot(w) or w.status!='active' or w.next_due_at is not None: return
        occurrences=[o for o in self.store.list_occurrences(w.id) if o.state!='superseded']
        if not occurrences or any(o.state in {'pending','claimed','started'} for o in occurrences): return
        o=occurrences[-1]
        if o.state in {'interrupted','dead','missed'} or o.state=='skipped' and o.error in FINAL_SKIP_REASONS:
            # The owner ended these on purpose (a reply, or /pulse off), so they read as cancelled, not failed.
            if o.error in {'cancel_on_reply','session_off'}:
                reason='owner_replied' if o.error=='cancel_on_reply' else 'session_off'
                self.store.cas_wake(w.id,'active',expected_version=w.version,status='cancelled',status_reason=reason)
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
            if session:
                may_clear=not (cause=='restart' and session.busy_epoch in LOCAL_EPOCHS)
                if s.backend:
                    # Claim and busy epochs come from different pollers. Check the busy owner itself.
                    may_clear=(may_clear and not self._foreign_backend_busy(session)
                               and (self._own_claim(o) or not self._own_backend_epoch(session.busy_epoch)))
                if may_clear:
                    s.cas_session(session.session_key,{'busy_since':session.busy_since},busy_since=None,busy_epoch=None)
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
        first=w.creator=='user' and w.created_in=='command' and not w.fires
        return line(self.store,self.store.get_session(w.session_key),self.clock.now(),seconds=seconds,
                    floor=max(self.policy.interval_floor(w),w.pacing_min_s or 0),watcher=watcher,
                    pricing=not self.store.connection.in_transaction,first_wake=first)

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
        from .serving import state,REASONS
        here=state(getattr(self.ctx,'_manager',None))
        reason=s.get_meta('last_error') or (local or {}).get('reason') or REASONS.get(here) or ('injection disabled' if not self.injection_allowed() else '')
        stamp=last or started
        if not last and started and now-float(started)<=15: return {'state':'alive','reason':'starting'}
        return {'state':'stalled' if stamp else 'not running','since':float(stamp) if stamp else None,'reason':reason}
