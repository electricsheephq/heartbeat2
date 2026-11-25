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
from .policy import LOCAL_SURFACES, agreed_watcher, is_keep_warm, owner_class
from .schedule import local_day
from .store import FAILURE_NOTICE_PREFIX
from .model import Wake

NOTIFY_AFTER_S = 600
ACCEPTED_NOTIFY_AFTER_S = 900
EPISODE_PREFIX = 'notify_episode:'
ACCEPTED_HOLD_TEXT = 'Still queued in the app. Runs when the chat gets to it, or retries once if it was dropped.'


def failure_notices(store, key):
    for w in store.list_wakes(key,status='failed'):
        for o in store.list_occurrences(w.id)[-1:]:
            if data := store.get_meta(FAILURE_NOTICE_PREFIX+o.id):
                yield w,o,json.loads(data)

def rearm_failed_wake(engine, key, sid):
    s,now=engine.store,engine.clock.now()
    if not engine.session_can_receive(s.get_session(key)) or not engine.policy.cap_gate(*s.active_wake_counts(key)).allowed: return
    recent=((w,o,d) for w,o,d in failure_notices(s,key) if w.armed_session_id==sid and 0<=now-d.get('queued',-86400)<86400)
    latest=max(recent,key=lambda row:row[2]['queued'],default=None)
    if latest is None: return
    w,o,data=latest
    if data.get('rearmed'): return
    s.create_wake(Wake(key,w.note,'at',platform=w.platform,chat_type=w.chat_type,chat_id=w.chat_id,
                       armed_session_id=sid,creator=w.creator,owner_requested=w.owner_requested,
                       title=w.title,reason=w.reason,urgent=w.urgent,
                       cancel_on_reply=w.cancel_on_reply,next_due_at=now))
    s.set_meta(FAILURE_NOTICE_PREFIX+o.id,json.dumps({**data,'rearmed':True}))
    return ' '.join(w.title.split()) or 'Your wake'


def held_wakes(engine, key, *, include_paused=False):
    """Count active wakes whose occurrence still waits on app_closed."""
    rows = []
    for w in engine.store.list_wakes(key):
        if (w.status != 'active' and not (include_paused and w.status == 'paused')) or is_keep_warm(w): continue
        store = engine.store
        occurrences = store.list_occurrences(w.id, state='pending')
        if store.backend:
            occurrences += store.list_occurrences(w.id, state='claimed')
        for o in occurrences:
            if o.error != 'app_closed': continue
            stamp = engine.store.get_meta('held_since:'+o.id)
            if stamp: rows.append((w, o, float(stamp)))
    return rows


def _accepted_notice_eligible(wake):
    return owner_class(wake) or agreed_watcher(wake) or wake.urgent


def _backend_notice_deadlines(store, rows, data):
    for row in rows:
        wake, occurrence, since = row
        if occurrence.state == 'pending':
            yield row, since + NOTIFY_AFTER_S
        elif _accepted_notice_eligible(wake):
            if 'accepted_restarted_at' in data and occurrence.id in data['accepted_noticed']:
                start = data['accepted_restarted_at']
            else:
                due = occurrence.created_at
                if occurrence.dedupe_key.startswith('t:'):
                    try: due = float(occurrence.dedupe_key[2:])
                    except ValueError: pass
                accepted = store.backend_reinject_state(occurrence.id).get('accepted_at')
                start = min(stamp for stamp in (due, occurrence.claimed_at, accepted) if stamp is not None)
            yield row, start + ACCEPTED_NOTIFY_AFTER_S


def _ready_backend_notices(store, rows, data, now):
    ready = [row for row, deadline in _backend_notice_deadlines(store, rows, data) if now >= deadline]
    # A due closed-window notice keeps its existing text and grouping for injector refusals.
    if any(o.state == 'pending' for _, o, _ in ready):
        return [row for row in rows if row[1].state == 'pending']
    return ready


def stamped(engine, key):
    """Whether any wake in the chat still carries a closed-window stamp, whatever holds it right now.

    A held wake can be deferred for another reason meanwhile (quiet hours overnight, a provider outage, a pause). Its
    episode, and the notice already sent for it, last until the stamp clears on delivery or the wake ends.
    """
    for w in engine.store.list_wakes(key):
        if w.status not in {'active', 'paused'} or is_keep_warm(w): continue
        if any(engine.store.get_meta('held_since:'+o.id) for o in engine.store.list_occurrences(w.id, state='pending')):
            return True
        if engine.store.backend and any(engine.store.get_meta('held_since:'+o.id)
               for o in engine.store.list_occurrences(w.id, state='claimed')):
            return True
    return False


def episode_over(engine, key, now=None):
    """Whether a chat's closed-window wait has ended, so a later hold may start a new one with its own notice.

    Before a notice is queued, the wait lasts while any wake still carries a closed-window stamp. Once a notice is queued it
    lasts, stamps or not, until the window is known to have reopened (a gateway accept or backend wake start ends it, or the
    owner writes in that chat) or a day has passed since the notice. Ending it clears the stamps (end_episode), so a wake
    still waiting starts a fresh wait: at most one notice per wait, and at most one a day.
    """
    data = json.loads(engine.store.get_meta(EPISODE_PREFIX+key) or 'null') or {}
    if 'queued' not in data:
        # Its notice is being sent: the send's result decides, so a pause or cancel meanwhile can't lose the receipt.
        if engine.store.notice_reserved(key): return False
        if stamped(engine, key): return False
        # A retry can be between holds for several ticks. Keep its original reset clock until it starts or ends.
        return not (engine.store.backend and _unfinished_noticed(engine.store, key, data.get('accepted_noticed')))
    now = engine.clock.now() if now is None else now
    chat = engine.store.get_session(key)
    return now-data['queued'] >= 86400 or bool(chat and chat.last_human_at and chat.last_human_at > data['queued'])


def end_episode(engine, key):
    # Delivery ends the whole chat episode, even if sibling wakes still wait. Keep-warm pings are never part of
    # an episode (held_wakes and stamped skip them), so their stamps keep the time they started waiting.
    with engine.store.atomic():
        data = json.loads(engine.store.get_meta(EPISODE_PREFIX+key) or 'null') or {} if engine.store.backend else {}
        engine.store.connection.execute('DELETE FROM meta WHERE key=?', (EPISODE_PREFIX+key,))
        if 'queued' in data or data.get('accepted_noticed'):
            _restart_accepted_holds(engine, key, data)
        for w in engine.store.list_wakes(key):
            if is_keep_warm(w): continue
            for o in engine.store.list_occurrences(w.id, state='pending'):
                engine.store.set_meta('held_since:'+o.id, '')


def _unfinished_noticed(store, key, noticed):
    if not noticed: return []
    ids = set(noticed)
    return sorted(o.id for state in ('pending', 'claimed')
                  for o in store.list_occurrences(state=state, session_key=key) if o.id in ids)


def _restart_accepted_holds(engine, key, data):
    if 'accepted_noticed' in data:
        noticed = data['accepted_noticed']
    else:
        # Legacy receipts lack IDs, and a resend can replace the held stamp after the notice.
        noticed = [o.id for w, o, _ in held_wakes(engine, key, include_paused=True)
                   if o.state == 'claimed' and _accepted_notice_eligible(w)]
    remaining = _unfinished_noticed(engine.store, key, noticed)
    if remaining:
        # The next hold supplies the episode ID and actual held-since time.
        fresh = {'accepted_noticed': remaining, 'accepted_restarted_at': engine.clock.now()}
        engine.store.set_meta(EPISODE_PREFIX+key, json.dumps(fresh))


def episode(engine, key, rows=None):
    rows = held_wakes(engine,key) if rows is None else rows
    if not rows: return None
    data = json.loads(engine.store.get_meta(EPISODE_PREFIX+key) or '{}') or {}
    if 'id' not in data:
        data.update(id=uuid.uuid4().hex, since=min(row[2] for row in rows), attempts=0)
    return data


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
    rows=held_wakes(engine,key)
    data=episode(engine,key,rows)
    if data is None: return ''
    error=' · '+data['error'][:80] if data.get('error') else ''
    if 'queued' in data: return 'queued at '+_time(engine,data['queued'])
    if engine.store.backend:
        ready_at=min((deadline for _,deadline in _backend_notice_deadlines(engine.store,rows,data)),default=None)
        if ready_at is None: return ''
    else:
        ready_at=data['since']+NOTIFY_AFTER_S
    if data.get('failed') or data.get('first') is not None and engine.clock.now()-data['first'] >= 86400:
        return 'notify failed'+error
    reason,until=_gate(engine,chat,data,engine.clock.now())
    if reason and until is None: return reason
    if until is not None: return 'waiting until '+_time(engine,until)+' ('+reason+')'+error
    if data.get('state') in {'may repeat','notifier not ready'}: return data['state']+error
    return 'waiting until '+_time(engine,max(ready_at,data.get('next',0)))+error


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
    surface='Desktop' if chat.platform=='desktop' else 'TUI'
    if rows[0][0].status=='failed':
        title=' '.join(rows[0][0].title.split()) or 'Your wake'
        return f"{title} didn't run; say 'again' in its {surface} chat to retry"
    if engine.store.backend and rows[0][1].state=='claimed':
        from .commands import _clip, _oneline
        wakes={w.id: w for w,_,_ in rows}
        if len(wakes)>1:
            return (f"{len(wakes)} wakes are due but their {surface} chat isn't ready; "
                    "they'll run on your next message there.")
        wake=next(iter(wakes.values()))
        title=_oneline(wake.title) or _clip(_oneline(wake.note),60)
        return f"{title} is due but its {surface} chat isn't ready; it'll run on your next message there."
    try:
        from hermes_constants import profile_name_for_home
        profile=profile_name_for_home(engine.ctx._manager.home_path) or 'default'
    except (ImportError, AttributeError): profile='default'
    # Profile names are local metadata, never wake-provided text. Bound even malformed host metadata.
    profile=' '.join(str(profile).split())[:64]
    when=datetime.fromtimestamp(since,engine.policy.resolved_timezone()).strftime('%H:%M %Z')[:32]
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
            candidates=[(key,chat,None) for key,chat in chats.items()]
            if s.backend:
                candidates.extend((key,chat,failed) for key,chat in chats.items() for failed in failure_notices(s,key)
                                  if 'queued' not in failed[2] and not failed[2].get('failed'))
            for key,chat,failed in candidates:
                rows=[(*failed[:2],now)] if failed else held_wakes(engine,key)
                if not rows: continue
                name=FAILURE_NOTICE_PREFIX+failed[1].id if failed else EPISODE_PREFIX+key
                data=failed[2] if failed else episode(engine,key,rows)
                # Keep the oldest currently held stamp while this episode lasts.
                data['since']=min(x[2] for x in rows)
                if data.get('first') is not None and now-data['first']>=86400 and 'queued' not in data:
                    data['failed']=True
                s.set_meta(name,json.dumps(data))
                if 'queued' in data or data.get('failed') or now<data.get('next',0): continue
                if s.backend:
                    if not failed and not _ready_backend_notices(s,rows,data,now): continue
                elif now<data['since']+NOTIFY_AFTER_S: continue
                if self.job is not None or _gate(engine,chat,data,now)[0]: continue
                transport=notifier_for(engine)
                if transport is None: continue
                self.job=(engine,key,data['id'],transport,name,failed and failed[:2])
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
            engine,key,*_=job
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
        for attempt in range(10):
            if self.closed and (not self.store.backend or attempt): return
            try:
                self.store.release_backend_notice(key,epoch)
                return
            except sqlite3.OperationalError as exc:
                if (getattr(exc,'sqlite_errorcode',0)&0xff) not in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):
                    LOG.warning('heartbeat2 notice reservation cleanup failed',exc_info=True)
                    return
                if attempt<9: time.sleep(.1)
            except Exception:
                LOG.warning('heartbeat2 notice reservation cleanup failed',exc_info=True)
                return
        LOG.warning('heartbeat2 notice reservation cleanup exhausted SQLite retries')

    def _send(self, engine, key, ident, transport, name, failed):
        s=self.store
        with s.atomic():
            if self.closed: return
            data=json.loads(s.get_meta(name) or '{}')
            chat=s.get_session(key)
            rows=[(*failed,engine.clock.now())] if failed else held_wakes(engine,key)
            since=data.get('since')
            if s.backend and not failed:
                rows=_ready_backend_notices(s,rows,data,engine.clock.now())
                if rows: since=min(row[2] for row in rows)
            # Exclude our own reservation while rechecking the cap.
            s.notify_reservation=None
            if s.backend and (s.notice_reserved(key) or 'queued' in data or engine.clock.now()<data.get('next',0)): return
            if (data.get('id')!=ident or not rows or chat is None or chat.platform not in LOCAL_SURFACES
                    or _gate(engine,chat,data,engine.clock.now())[0] or notifier_for(engine) is not transport): return
            now=sent_at=engine.clock.now()
            if not s.backend: s.notify_reservation=(key,local_day(now,engine.policy.resolved_timezone()))
            s.reserve_backend_notice(key,engine.epoch)
            data.setdefault('first',now)
            data['attempts']+=1
            data['next']=now+(300 if data['attempts']<3 else 3600)
            s.set_meta(name,json.dumps(data))
            target=engine.policy.notify_via.split(':',1)[1]
            text=notice_text(engine,chat,rows,since)
            wake=rows[0][0]
        try:
            from .registration import home_scope
            with home_scope(engine.ctx._manager.home_path): result=transport.send(target,text)
        except Exception as exc:
            result={'ok':False,'error':str(exc) or type(exc).__name__}
        if not isinstance(result,dict): result={'ok':False,'error':'invalid notifier result'}
        now=engine.clock.now()
        with s._lock:
            if self.closed:
                if s.backend: s.release_backend_notice(key,engine.epoch)
                return
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
                    if s.backend:
                        covered = {o.id for _, o, _ in rows if o.state == 'claimed'}
                        data['accepted_noticed'] = sorted(set(data.get('accepted_noticed', ())) | covered)
                    data.pop('error',None)
                else:
                    error=str(result.get('error') or 'notifier refused').replace(text,'[notice]')
                    lower=error.lower()
                    data['error']=error[:120]
                    data['state']=('notifier not ready' if any(x in lower for x in ('no token','no-token','no_token',"doesn't have the birdie_hermes_token secret")) else
                                   'may repeat' if any(x in lower for x in ("can't reach birdie",'timeout','timed out','timedout')) else '')
                    data['failed']=now-data['first']>=86400
                current=json.loads(s.get_meta(name) or '{}')
                if current.get('id')==ident: s.set_meta(name,json.dumps(data))
