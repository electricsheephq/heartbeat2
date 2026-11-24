"""Profile-local v1 notifier registry and bounded closed-window notices."""
from dataclasses import dataclass
import logging
import sqlite3
import sys
import threading
import time
from types import FunctionType
from typing import Callable

LOG = logging.getLogger('heartbeat2')
FIELDS = ('send',)


@dataclass(frozen=True)
class Notifier:
    send: Callable


def _is_live(bridge):
    for name in FIELDS:
        fn = getattr(bridge, name)
        if isinstance(fn, FunctionType):
            # Look the module up by the function's own globals: functools.wraps copies __module__ from the wrapped
            # function, so a transport decorated in a helper module reports a module it does not live in.
            module = sys.modules.get(fn.__globals__.get('__name__'))
            if module is None or fn.__globals__ is not vars(module): return False
    return True


class NotifierRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._current = None
        self._stamp = 0
        self._withdrawn = set()
        self._warned = set()
        self._closed = False

    def announce(self, /, **payload):
        bridge = None
        if payload.get('withdrawn') is not True and all(callable(payload.get(name)) for name in FIELDS):
            bridge = Notifier(**{name: payload[name] for name in FIELDS})
        # Module/attribute reads stay outside the state lock; never call a transport.
        live = bridge is None or _is_live(bridge)
        with self._lock:
            if self._closed: return
            reason = ''
            stamp = payload.get('registered_at')
            if type(payload.get('version')) is not int or payload['version'] != 1:
                reason = 'version must be integer 1'
            elif type(stamp) is not int or stamp <= 0:
                reason = 'registered_at must be a positive integer'
            elif payload.get('withdrawn') is not True:
                reason = next((name+' must be callable' for name in FIELDS
                               if not callable(payload.get(name))), '')
                if not reason and not live: reason = 'transport function module is no longer live'
            if reason:
                if reason not in self._warned:
                    self._warned.add(reason)
                    LOG.warning('heartbeat2 notifier ignored: %s', reason)
                return
            if payload.get('withdrawn') is True:
                self._withdrawn.add(stamp)
                # A withdrawal is a lifecycle stamp too: it retires the current copy if that copy is not newer, and older
                # announcements stay out even when this registry never saw the withdrawn copy's own announcement.
                if stamp >= self._stamp: self._current, self._stamp = None, stamp
                return
            if stamp in self._withdrawn or stamp < self._stamp: return
            self._current = bridge
            self._stamp = stamp

    def get(self):
        while True:
            with self._lock:
                current = None if self._closed else self._current
            if current is None: return None
            live = _is_live(current)
            with self._lock:
                if self._closed: return None
                if self._current is not current: continue
                if not live: self._current = None
                return self._current

    def close(self):
        with self._lock:
            self._closed = True
            self._current = None


def notifier_for(engine):
    """Return the engine's runtime-owned transport, or None; never invoke it."""
    try:
        accessor = getattr(engine, 'notifier', None)
        return accessor() if callable(accessor) else None
    except Exception:
        return None


import json
import uuid
from datetime import datetime, timedelta
from .policy import LOCAL_SURFACES, is_keep_warm
from .schedule import local_day

NOTIFY_AFTER_S = 600
EPISODE_PREFIX = 'notify_episode:'


def held_wakes(engine, key):
    """Count only active wakes whose pending occurrence still waits on app_closed."""
    rows = []
    for w in engine.store.list_wakes(key):
        if w.status != 'active' or is_keep_warm(w): continue
        for o in engine.store.list_occurrences(w.id, state='pending'):
            if o.error != 'app_closed': continue
            stamp = engine.store.get_meta('held_since:'+o.id)
            if stamp: rows.append((w, o, float(stamp)))
    return rows


def stamped(engine, key):
    """Whether any wake in the chat still carries a closed-window stamp, whatever holds it right now.

    A held wake can be deferred for another reason meanwhile (quiet hours overnight, a provider outage, a pause). Its
    episode, and the notice already sent for it, last until the stamp clears on an accepted injection or the wake ends.
    """
    for w in engine.store.list_wakes(key):
        if w.status not in {'active', 'paused'} or is_keep_warm(w): continue
        if any(engine.store.get_meta('held_since:'+o.id) for o in engine.store.list_occurrences(w.id, state='pending')):
            return True
    return False


def episode_over(engine, key, now=None):
    """Whether a chat's closed-window wait has ended, so a later hold may start a new one with its own notice.

    Before a notice is queued, the wait lasts while any wake still carries a closed-window stamp. Once a notice is queued it
    lasts, stamps or not, until the window is known to have reopened (an accepted injection ends it in end_episode, or the
    owner writes in that chat) or a day has passed since the notice. Ending it clears the stamps (end_episode), so a wake
    still waiting starts a fresh wait: at most one notice per wait, and at most one a day.
    """
    data = json.loads(engine.store.get_meta(EPISODE_PREFIX+key) or 'null') or {}
    if 'queued' not in data:
        # Its notice is being sent: the send's result decides, so a pause or cancel meanwhile can't lose the receipt.
        if engine.store.notice_reserved(key): return False
        return not stamped(engine, key)
    now = engine.clock.now() if now is None else now
    chat = engine.store.get_session(key)
    return now-data['queued'] >= 86400 or bool(chat and chat.last_human_at and chat.last_human_at > data['queued'])


def end_episode(engine, key):
    # Accepted injection ends the whole chat episode, even if sibling wakes still wait. Keep-warm pings are never part of
    # an episode (held_wakes and stamped skip them), so their stamps keep the time they started waiting.
    with engine.store.atomic():
        engine.store.connection.execute('DELETE FROM meta WHERE key=?', (EPISODE_PREFIX+key,))
        for w in engine.store.list_wakes(key):
            if is_keep_warm(w): continue
            for o in engine.store.list_occurrences(w.id, state='pending'):
                engine.store.set_meta('held_since:'+o.id, '')


def episode(engine, key, rows=None):
    rows = held_wakes(engine,key) if rows is None else rows
    if not rows: return None
    return json.loads(engine.store.get_meta(EPISODE_PREFIX+key) or '{}') or {
        'id':uuid.uuid4().hex, 'since':min(x[2] for x in rows), 'attempts':0}


def _time(engine, stamp):
    return datetime.fromtimestamp(stamp, engine.policy.resolved_timezone()).strftime('%Y-%m-%d %H:%M %Z')


def _gate(engine, chat, data, now):
    """Notice gates are chat-level; unanswered and wake urgency do not apply."""
    if not engine.policy.enabled or engine.paused(): return 'profile paused', None
    if not engine.eligible() or not engine.injection_allowed(): return 'injection disabled', None
    if not chat.enabled: return 'chat off', None
    if chat.paused_until is not None and chat.paused_until > now: return 'chat paused', chat.paused_until
    if not engine.policy.notify_via: return 'disabled', None
    if notifier_for(engine) is None: return 'not connected', None
    from .commands import quiet_until
    until = quiet_until(engine, chat.session_key, now=now)
    if until is not None: return 'quiet hours', until
    if engine.daily(chat.session_key)['delivered'] >= engine.policy.max_messages_per_day:
        tz=engine.policy.resolved_timezone()
        tomorrow=datetime.fromtimestamp(now,tz).replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)
        return 'daily message cap', tomorrow.timestamp()
    return '', None


def notice_state(engine, key):
    chat=engine.store.get_session(key)
    if chat is None or chat.platform not in LOCAL_SURFACES: return ''
    data=episode(engine,key)
    if data is None: return ''
    error=' · '+data['error'][:80] if data.get('error') else ''
    if 'queued' in data: return 'queued at '+_time(engine,data['queued'])
    if data.get('failed') or data.get('first') is not None and engine.clock.now()-data['first'] >= 86400:
        return 'notify failed'+error
    reason,until=_gate(engine,chat,data,engine.clock.now())
    if reason and until is None: return reason
    if until is not None: return 'waiting until '+_time(engine,until)+' ('+reason+')'+error
    if data.get('state') in {'may repeat','notifier not ready'}: return data['state']+error
    return 'waiting until '+_time(engine,max(data['since']+NOTIFY_AFTER_S,data.get('next',0)))+error


def notifier_line(engine, key=None):
    invalid=getattr(engine,'notify_via_invalid',False)
    setting=engine.policy.notify_via
    if not setting and not invalid: return ''
    state='invalid' if invalid else 'connected' if setting and notifier_for(engine) else 'not connected'
    text=f'notify_via: {setting or "blank"}, {state}'
    if key:
        notice=notice_state(engine,key)
        if notice: text+=' · '+notice
    return text


def notice_text(engine, chat, rows, since):
    try:
        from hermes_constants import profile_name_for_home
        profile=profile_name_for_home(engine.ctx._manager.home_path) or 'default'
    except (ImportError, AttributeError): profile='default'
    # Profile names are local metadata, never wake-provided text. Bound even malformed host metadata.
    profile=' '.join(str(profile).split())[:64]
    when=datetime.fromtimestamp(since,engine.policy.resolved_timezone()).strftime('%H:%M %Z')[:32]
    surface='Desktop' if chat.platform=='desktop' else 'TUI'
    prefix=f"HeartBeat: {len({w.id for w,_,_ in rows})} wake(s) in "
    suffix=(f"'s {surface} chat have been waiting since {when} because the app window is closed. "
            'Open that chat to let them run.')
    return prefix+profile[:max(0,240-len(prefix)-len(suffix))]+suffix


class NotificationWorker:
    """One lazy daemon and one job slot per runtime, shared by engine and runner.

    Only the plugin call happens outside the store lock. The slot is also a daily
    message reservation, so concurrent wake claims cannot spend the same capacity.
    """
    def __init__(self, store):
        self.store=store
        self.closed=False
        self.job=None
        self.thread=None
        self.ready=threading.Event()
        self.idle=threading.Event()
        self.idle.set()

    def stop(self):
        # Never wait for the store while Runtime closes under its engine lock.
        self.closed=True
        self.ready.set()

    def close(self):
        self.stop()
        # A transport can hang despite its contract. Never hold up Hermes unload indefinitely.
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)

    def tick(self, engine):
        s,now=self.store,engine.clock.now()
        with s.atomic():
            if self.closed: return
            chats={chat.session_key:chat for chat in s.list_sessions() if chat.platform in LOCAL_SURFACES}
            for key in s.session_meta_keys(EPISODE_PREFIX):
                if key not in chats or episode_over(engine,key,now):
                    end_episode(engine,key)
            for key,chat in chats.items():
                rows=held_wakes(engine,key)
                if not rows: continue
                data=episode(engine,key,rows)
                # Keep the oldest currently held stamp while this episode lasts.
                data['since']=min(x[2] for x in rows)
                if data.get('first') is not None and now-data['first']>=86400 and 'queued' not in data:
                    data['failed']=True
                s.set_meta(EPISODE_PREFIX+key,json.dumps(data))
                if 'queued' in data or data.get('failed') or now<data['since']+NOTIFY_AFTER_S or now<data.get('next',0): continue
                if self.job is not None or _gate(engine,chat,data,now)[0]: continue
                transport=notifier_for(engine)
                if transport is None: continue
                self.job=(engine,key,data['id'],transport)
                s.notify_reservation=(key,local_day(now,engine.policy.resolved_timezone()))
                self.idle.clear()
                if self.thread is None:
                    self.thread=threading.Thread(target=self._run,name='heartbeat2-notifier',daemon=True)
                    self.thread.start()
                self.ready.set()
                break

    def _run(self):
        while True:
            self.ready.wait()
            self.ready.clear()
            with self.store._lock:
                if self.closed: return
                job=self.job
            if job is None: continue
            engine,key,_,_=job
            try: self._send(*job)
            except Exception:
                LOG.warning('heartbeat2 notifier worker failed',exc_info=True)
            finally:
                with self.store._lock:
                    self.store.notify_reservation=None
                    self.job=None
                    self.idle.set()
                self._release_reservation(key,engine.epoch)

    def _release_reservation(self, key, epoch):
        # Keep a transient write lock from stranding a reservation while this process is live.
        while True:
            try:
                self.store.release_backend_notice(key,epoch)
                return
            except sqlite3.OperationalError as exc:
                if (getattr(exc,'sqlite_errorcode',0)&0xff) not in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):
                    LOG.warning('heartbeat2 notice reservation cleanup failed',exc_info=True)
                    return
                time.sleep(.1)
            except Exception:
                LOG.warning('heartbeat2 notice reservation cleanup failed',exc_info=True)
                return

    def _send(self, engine, key, ident, transport):
        s=self.store
        with s.atomic():
            if self.closed: return
            data=json.loads(s.get_meta(EPISODE_PREFIX+key) or '{}')
            chat=s.get_session(key)
            rows=held_wakes(engine,key)
            # Exclude our own reservation while rechecking the cap.
            s.notify_reservation=None
            if s.backend and (s.notice_reserved(key) or 'queued' in data or engine.clock.now()<data.get('next',0)): return
            if (data.get('id')!=ident or not rows or chat is None or chat.platform not in LOCAL_SURFACES
                    or _gate(engine,chat,data,engine.clock.now())[0] or notifier_for(engine) is not transport): return
            now=sent_at=engine.clock.now()
            s.notify_reservation=(key,local_day(now,engine.policy.resolved_timezone()))
            s.reserve_backend_notice(key,engine.epoch)
            data.setdefault('first',now)
            data['attempts']+=1
            data['next']=now+(300 if data['attempts']<3 else 3600)
            s.set_meta(EPISODE_PREFIX+key,json.dumps(data))
            target=engine.policy.notify_via.split(':',1)[1]
            text=notice_text(engine,chat,rows,data['since'])
            wake=rows[0][0]
        try:
            from .registration import home_scope
            with home_scope(engine.ctx._manager.home_path): result=transport.send(target,text)
        except Exception as exc:
            result={'ok':False,'error':str(exc) or type(exc).__name__}
        if not isinstance(result,dict): result={'ok':False,'error':'invalid notifier result'}
        now=engine.clock.now()
        with s._lock:
            if self.closed: return
            with s.atomic():
                s.notify_reservation=None
                s.release_backend_notice(key,engine.epoch)
                if result.get('ok') is True:
                    engine.ledger(wake,delivered=1)
                    # A notify event is a receipt, never an input to a watcher and never contains text.
                    dedupe='notify:'+ident
                    s.add_event('notify',dedupe,{'status':'queued'},session_key=key)
                    s.connection.execute('UPDATE events SET consumed_at=? WHERE dedupe_key=?',(now,dedupe))
                    data['queued']=sent_at  # the send's start: an owner write during the send ends this wait
                    data.pop('error',None)
                else:
                    error=str(result.get('error') or 'notifier refused').replace(text,'[notice]')
                    lower=error.lower()
                    data['error']=error[:120]
                    data['state']=('notifier not ready' if any(x in lower for x in ('no token','no-token','no_token',"doesn't have the birdie_hermes_token secret")) else
                                   'may repeat' if any(x in lower for x in ("can't reach birdie",'timeout','timed out','timedout')) else '')
                    data['failed']=now-data['first']>=86400
                current=json.loads(s.get_meta(EPISODE_PREFIX+key) or '{}')
                if current.get('id')==ident: s.set_meta(EPISODE_PREFIX+key,json.dumps(data))
