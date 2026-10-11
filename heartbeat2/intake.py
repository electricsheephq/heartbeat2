"""Fail-open Hermes observers; final output silence is an exact transform."""
from dataclasses import dataclass, fields
from functools import wraps
import hashlib
import json
import re
import uuid

from .model import Session
from . import governor

MARKER = re.compile(r'^⟦pulse o:([^\s]+) a:(\d+)⟧(?:\s|$)')
BARE_SILENCE = {'[silent]','silent','no_reply','[no_reply]','heartbeat_ok','no changes',
                'no changes.','no change','no change.','nothing to report','nothing to report.'}


def marker(text):
    # Never search payloads, transcripts, or later lines for authority.
    first=text.split('\n',1)[0] if isinstance(text,str) else ''
    match=MARKER.match(first)
    if not match: return None
    try:
        wid,key=match[1].split('/',1)
        return wid,key,int(match[2])
    except ValueError: return None


def fail_open(fn):
    @wraps(fn)
    def call(self,*args,**kwargs):
        try:
            return fn(self,*args,**kwargs)
        except Exception:
            # No hook payload or raw exception text in log messages.
            self.engine.log.warning('heartbeat2 hook %s failed',fn.__name__,exc_info=True)
            return None
    return call


def delegated_child(platform=''):
    if platform=='subagent': return True
    try:
        from agent.delegation_context import is_delegated_child_context
        return is_delegated_child_context()
    except Exception:
        return False


@dataclass
class Flight:
    session_key: str
    session_id: str
    turn_id: str
    wake_id: str
    dedupe_key: str
    attempt: int
    untrusted: bool=False
    invalid: bool=False
    scratch: str | None=None
    owner_replied: bool=False


class Intake:
    def __init__(self,engine):
        self.engine=engine
        self.flights={}
        self.bindings={}
        self.turns={}

    def flight_for(self,sid,key):
        # Tools in delegate children inherit the key, but have a different id.
        self._follow(sid)
        for f in self.flights.values():
            if f.session_key!=key: continue
            o=self.engine.store.get_occurrence(f.wake_id,f.dedupe_key)
            if f.invalid and f.session_id==sid or o and o.state=='started' and o.attempt==f.attempt:
                return f
        return None

    def _follow(self,sid):
        if not sid or sid in self.bindings or sid in self.flights: return
        e,s=self.engine,self.engine.store
        for old in list(self.bindings):
            if e.compression_tip(old)!=sid: continue
            key=self.bindings.pop(old)
            self.bindings[sid]=key
            self.turns[sid]=self.turns.pop(old,'')
            f=self.flights.pop(old,None)
            if f:
                f.session_id=sid
                self.flights[sid]=f
            with s.atomic():
                session=s.get_session(key)
                s.cas_session(key,{'current_session_id':old},current_session_id=sid)
                for w in s.list_wakes(key):
                    if w.armed_session_id==old:
                        s.cas_wake(w.id,w.status,expected_version=w.version,armed_session_id=sid)
            return

    def _key(self,sid='',key=''):
        if key and key!='default': return key
        current=self.engine.tool.session_key_fn()
        if current and current!='default': return current
        return self.bindings.get(sid,'')

    def human(self,key,platform='',source=None,dedupe=None):
        e,s=self.engine,self.engine.store
        if not key or key=='default': return
        source=source or {}
        with s.atomic():
            if dedupe and s.get_meta('human:'+dedupe): return
            if dedupe and s.get_meta('nonhuman:'+dedupe): return
            s.upsert_session(Session(key,platform=platform,chat_type=source.get('chat_type',''),chat_id=str(source.get('chat_id') or '')))
            old=s.get_session(key)
            now=e.clock.now()
            updates={'last_human_at':now,'last_activity_at':now,'episode_id':uuid.uuid4().hex,'unanswered_count':0}
            if platform: updates['platform']=platform
            if 'chat_type' in source: updates['chat_type']=source['chat_type'] or ''
            if 'chat_id' in source: updates['chat_id']=str(source['chat_id'] or '')
            s.cas_session(key,{f.name:getattr(old,f.name) for f in fields(Session) if f.name!='session_key'},**updates)
            if dedupe: s.set_meta('human:'+dedupe,'1')
            for f in self.flights.values():
                if f.session_key==key and not f.invalid:
                    f.owner_replied=True
            for o in s.list_occurrences(session_key=key,state='done'):
                if o.delivered and o.ended_at is not None and 0<=now-o.ended_at<=e.policy.engagement_window_hours*3600:
                    governor.engagement(e,o)
            for w in s.list_wakes(key):
                if w.depth:
                    s.cas_wake(w.id,w.status,expected_version=w.version,depth=0)
                if w.cancel_on_reply and w.status in {'active','paused','proposed'}:
                    s.cas_wake(w.id,w.status,status='cancelled',status_reason='owner_replied')
                    e.cancel_occurrences(w.id,'owner_replied')
            s.add_event('human_message',dedupe or uuid.uuid4().hex,{},session_key=key)
        e.signal()

    @fail_open
    def pre_gateway_dispatch(self,event=None,gateway=None,**kwargs):
        if event is None or getattr(event,'internal',False): return None
        source=getattr(event,'source',None)
        platform=getattr(getattr(source,'platform',None),'value','')
        chat_id=str(getattr(source,'chat_id','') or '')
        matches=[s for s in self.engine.store.list_sessions() if s.platform==platform and s.chat_id==chat_id]
        if not chat_id or len(matches)!=1: return None
        key=matches[0].session_key
        mid=getattr(event,'message_id','')
        identity=f'{key}:{mid}' if mid else None
        # SessionSource.to_dict() drops is_bot; preserve the real event verdict
        # for the later admission/human branch, never inspect an absent dict key.
        if getattr(source,'is_bot',False):
            if identity: self.engine.store.set_meta('nonhuman:'+identity,'1')
            return None
        authorized=getattr(gateway,'_is_user_authorized_for_source',None)
        if authorized and not authorized(source): return None
        self.engine.refresh()
        self.human(key,platform,{'chat_type':getattr(source,'chat_type',''),'chat_id':chat_id},identity)
        return None

    @fail_open
    def post_gateway_admission(self,session_key='',platform='',source=None,message_id='',text='',**kwargs):
        self.engine.refresh()
        if not isinstance(source,dict): return None
        if kwargs.get('internal'): return None
        identity=f'{session_key}:{message_id}' if message_id else None
        self.human(session_key,platform,source,identity)
        return None

    @fail_open
    def pre_command(self,surface='',session_key='',platform='',**kwargs):
        if surface=='gateway': self.human(session_key,platform)
        return None

    @fail_open
    def gateway_platform_event(self,platform='',event_type='',payload=None,**kwargs):
        if event_type not in {'reaction','message_edited'} or not isinstance(payload,dict): return None
        s=self.engine.store
        matches=[x for x in s.list_sessions() if x.platform==platform and x.chat_id==str(payload.get('chat_id') or '')]
        # Chat mapping must be unambiguous (topics/groups can share chat ids).
        if len(matches)!=1: return None
        key=matches[0].session_key
        self.human(key,platform)
        digest=hashlib.sha1(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        s.add_event(event_type,f'{platform}:{event_type}:{digest}',payload,session_key=key,chat_id=matches[0].chat_id)
        self.engine.signal()
        return None

    @fail_open
    def pre_llm_call(self,session_id='',turn_id='',user_message='',platform='',**kwargs):
        if delegated_child(platform): return None
        e,s=self.engine,self.engine.store
        e.refresh()
        self._follow(session_id)
        previous=self.flights.get(session_id)
        if previous and turn_id and previous.turn_id and previous.turn_id!=turn_id:
            self.on_session_end(session_id=session_id,turn_id=previous.turn_id,failed=True)
        key=self._key(session_id)
        if not key: return None
        identity=e.tool.identity()
        if identity is None: return None
        key=identity.session_key
        self.bindings[session_id]=key
        self.turns[session_id]=turn_id
        now=e.clock.now()
        with s.atomic():
            session=s.get_session(key)
            s.cas_session(key,{'current_session_id':session.current_session_id},current_session_id=session_id,busy_since=now,last_activity_at=now)
            parsed=marker(user_message)
            if parsed:
                wid,dedupe,attempt=parsed
                w,o=s.get_wake(wid),s.get_occurrence(wid,dedupe)
                # A marker is authoritative only for its original session.
                if w is None or w.session_key!=key or o is None: return None
                invalid=o.attempt!=attempt or o.state not in {'claimed','started'}
                if w.armed_session_id and w.armed_session_id!=session_id:
                    tip=e.compression_tip(w.armed_session_id)
                    if tip==session_id:
                        s.cas_wake(w.id,w.status,expected_version=w.version,armed_session_id=session_id)
                        w=s.get_wake(wid)
                    else: invalid=True
                if w.status!='active': invalid=True
                if w.cancel_on_reply and session.last_human_at is not None and o.claimed_at is not None and session.last_human_at>o.claimed_at:
                    invalid=True
                flight=Flight(key,session_id,turn_id,wid,dedupe,attempt,bool(o.payload_json),invalid)
                existing=self.flights.get(session_id)
                if existing and existing.wake_id==wid and existing.dedupe_key==dedupe and existing.attempt==attempt:
                    return None
                self.flights[session_id]=flight
                if invalid:
                    if o.attempt==attempt and o.state in {'claimed','started'}:
                        s.cas_occurrence(wid,dedupe,o.state,'superseded',attempt=o.attempt,force_silent=True,error='stale_turn')
                    return None
                if o.state=='claimed' and s.start(wid,dedupe,attempt=attempt):
                    s.cas_wake(w.id,w.status,expected_version=w.version,fires=w.fires+1,model_turns=w.model_turns+1,last_fired_at=now)
                    e.ledger(w,fires=1,model_turns=1)
                return None
            # Compression is a lineage continuation, including when only a
            # human turn has visited the new tip before /new arrives.
            for w in s.list_wakes(key):
                if w.status in {'active','paused','proposed'} and w.armed_session_id and w.armed_session_id!=session_id:
                    if e.compression_tip(w.armed_session_id)==session_id:
                        s.cas_wake(w.id,w.status,expected_version=w.version,armed_session_id=session_id)
            # Human turn context never turns a model call into a wake.
            self.flights.pop(session_id,None)
            from .tool import session_env
            mid=kwargs.get('message_id') or session_env('HERMES_SESSION_MESSAGE_ID')
            dedupe=f'{key}:{mid}' if mid else f'{key}:turn:{session_id}:{turn_id}' if turn_id else None
            if not kwargs.get('internal'):
                self.human(key,platform,dedupe=dedupe)
            lines=[]
            last_tick=s.get_meta('last_tick_at')
            if e.needs_restart():
                stamp='stall_notice:'+key
                last_tick=last_tick or 'before_first_tick'
                if s.get_meta(stamp)!=last_tick:
                    e.restart()
                    s.set_meta(stamp,last_tick)
                    lines.append('The scheduler was stalled; it is restarting. Some wakes may be late.')
            notices=json.loads(s.get_meta('notice:'+key) or '[]')
            lines+=notices
            if notices: s.set_meta('notice:'+key,'[]')
            armed=[w for w in s.list_wakes(key) if w.status in {'active','proposed'}]
            if e.policy.presence_line and armed:
                lines.append(f'HeartBeat 2.0: {len(armed)} armed; /pulse shows status, /pulse off stops checks.')
            for w in s.list_wakes(key):
                card=s.get_meta('card:'+w.id)
                if card:
                    lines.append(f'{w.title or "A wake"} paused ({card}): keep / slow / stop / convert via /pulse.')
                    s.set_meta('card:'+w.id,'')
            limits={f.name:getattr(e.policy,f.name) for f in fields(e.policy) if f.name not in {'clock','profile_timezone'}}
            fingerprint=hashlib.sha256(json.dumps(limits,sort_keys=True).encode()).hexdigest()
            session=s.get_session(key)
            if session.limits_fingerprint and session.limits_fingerprint!=fingerprint:
                lines.append('HeartBeat 2.0 limits changed outside /pulse; current bounds now apply.')
            s.cas_session(key,{'limits_fingerprint':session.limits_fingerprint},limits_fingerprint=fingerprint)
            return {'context':'\n'.join(lines)} if lines else None

    @fail_open
    def post_api_request(self,session_id='',turn_id='',usage=None,**kwargs):
        if delegated_child(kwargs.get('platform','')): return None
        e,s=self.engine,self.engine.store
        self._follow(session_id)
        key=self.bindings.get(session_id)
        if key and (not turn_id or not self.turns.get(session_id) or self.turns[session_id]==turn_id):
            session=s.get_session(key)
            s.cas_session(key,{'last_activity_at':session.last_activity_at},last_activity_at=e.clock.now())
        f=self.flights.get(session_id)
        if not f or f.invalid or turn_id and f.turn_id and turn_id!=f.turn_id: return None
        if usage is None: return None
        def count(*names):
            for name in names:
                v=usage.get(name) if isinstance(usage,dict) else getattr(usage,name,None)
                if v is not None: return max(0,int(v))
            return 0
        with s.atomic():
            o=s.get_occurrence(f.wake_id,f.dedupe_key)
            if o and o.state=='started':
                s.update_occurrence(o.wake_id,o.dedupe_key,'started',attempt=f.attempt,
                    tokens_in=o.tokens_in+count('prompt_tokens','input_tokens'),
                    tokens_out=o.tokens_out+count('completion_tokens','output_tokens'))
        return None

    @fail_open
    def transform_llm_output(self,response_text='',session_id='',turn_id='',**kwargs):
        if delegated_child(kwargs.get('platform','')): return None
        e,s=self.engine,self.engine.store
        self._follow(session_id)
        f=self.flights.get(session_id)
        if not f or turn_id and f.turn_id and f.turn_id!=turn_id: return None
        with s.atomic():
            o=s.get_occurrence(f.wake_id,f.dedupe_key)
            if f.owner_replied and not f.invalid: return None
            if f.invalid or not o or o.state!='started' or o.attempt!=f.attempt: return '[SILENT]'
            w=s.get_wake(o.wake_id)
            session=s.get_session(f.session_key)
            silence=(not e.policy.enabled or not session.enabled or e.paused() or
                     o.force_silent or not o.notify) or (
                o.outcome is None and response_text.strip().casefold() in BARE_SILENCE)
            if o.notify and o.summary.strip() and o.summary_hash:
                silence=silence or any(x.delivered and x.summary.strip() and x.summary_hash==o.summary_hash and x.ended_at is not None and
                    0<=e.clock.now()-x.ended_at<=86400 for x in s.list_occurrences(o.wake_id,state='done'))
            if silence:
                s.update_occurrence(o.wake_id,o.dedupe_key,'started',attempt=f.attempt,force_silent=True)
                return '[SILENT]'
        return None

    @fail_open
    def on_session_end(self,session_id='',turn_id='',completed=None,failed=False,interrupted=False,**kwargs):
        if delegated_child(kwargs.get('platform','')): return None
        e,s=self.engine,self.engine.store
        self._follow(session_id)
        f=self.flights.get(session_id)
        if turn_id and self.turns.get(session_id) and turn_id!=self.turns[session_id]: return None
        key=f.session_key if f else self.bindings.get(session_id)
        if not key: return None
        with s.atomic():
            session=s.get_session(key)
            if session:
                s.cas_session(key,{'busy_since':session.busy_since},busy_since=None)
            self.flights.pop(session_id,None)
            if f is None or f.invalid: return None
            o=s.get_occurrence(f.wake_id,f.dedupe_key)
            if o is None or o.state!='started' or o.attempt!=f.attempt: return None
            if failed or interrupted or completed is False:
                s.cas_occurrence(o.wake_id,o.dedupe_key,'started','interrupted',attempt=f.attempt,error='failed' if failed else 'interrupted',force_silent=True)
                w=s.get_wake(o.wake_id)
                s.cas_wake(w.id,w.status,expected_version=w.version,
                           tokens_in=w.tokens_in+o.tokens_in,tokens_out=w.tokens_out+o.tokens_out)
                e.ledger(w,tokens_in=o.tokens_in,tokens_out=o.tokens_out)
                if not f.owner_replied: governor.error(e,s.get_wake(o.wake_id))
                return None
            # Recheck cancellation after the final transform: a human can reply
            # between claim/start/transform and successful finalization.
            w=s.get_wake(o.wake_id)
            silenced=o.force_silent or not o.notify or not e.policy.enabled or not session.enabled or e.paused()
            delivered=bool(o.notify and not silenced and not f.owner_replied)
            if not s.finish(o.wake_id,o.dedupe_key,attempt=o.attempt,outcome=o.outcome or 'unreported',delivered=delivered,force_silent=silenced): return None
            if f.scratch is not None:
                s.cas_wake(w.id,w.status,expected_version=w.version,scratch=f.scratch)
            if delivered and session:
                session=s.get_session(key)
                s.cas_session(key,{'unanswered_count':session.unanswered_count},
                              unanswered_count=session.unanswered_count+1,first_notice_sent=True)
            governor.finish(e,s.get_wake(o.wake_id),s.get_occurrence(o.wake_id,o.dedupe_key))
        e.signal()
        return None

    @fail_open
    def agent_loop_stopped(self,session_key='',**kwargs):
        for sid,f in list(self.flights.items()):
            if f.session_key==session_key:
                self.on_session_end(session_id=sid,interrupted=True)
        return None

    @fail_open
    def on_session_reset(self,old_session_id='',new_session_id='',session_id='',**kwargs):
        e,s=self.engine,self.engine.store
        old=old_session_id or session_id
        if not old: return None
        with s.atomic():
            keys=set()
            for w in s.list_wakes():
                if w.trigger_json.get('builtin') and w.armed_session_id==old:
                    s.cas_wake(w.id,w.status,expected_version=w.version,armed_session_id=new_session_id)
                    session=s.get_session(w.session_key)
                    if session: s.cas_session(w.session_key,{'current_session_id':session.current_session_id},current_session_id=new_session_id,busy_since=None)
                    continue
                if w.armed_session_id==old and w.status in {'active','paused','proposed'}:
                    s.cas_wake(w.id,w.status,status='orphaned',status_reason='session_reset')
                    e.cancel_occurrences(w.id,'session_reset')
                    keys.add(w.session_key)
            for key in keys:
                session=s.get_session(key)
                if session: s.cas_session(key,{'current_session_id':session.current_session_id},current_session_id=new_session_id,busy_since=None)
                e.notice(key,'Wakes from the old conversation were orphaned after /new.')
            for f in self.flights.values():
                if f.session_id==old: f.invalid=True
        return None

    @fail_open
    def event(self,kind,session_id='',parent_session_id='',**kwargs):
        key=self._key()
        # Board events can be profile-wide; API/subagent events must resolve to
        # their actual parent session rather than a foreign ambient turn.
        if parent_session_id or session_id:
            target=parent_session_id or session_id
            key=self.bindings.get(target,'')
            if not key:
                matches=[s for s in self.engine.store.list_sessions() if s.current_session_id==target]
                if len(matches)==1: key=matches[0].session_key
            if not key: return None
        allowed=('task_id','board','assignee','run_id','profile_name','summary','reason',
                 'worker_pid','exit_kind','exit_code','outcome','child_session_id','child_status','child_summary')
        payload={k:kwargs[k] for k in allowed if k in kwargs}
        digest=hashlib.sha1(json.dumps([kind,key,payload],sort_keys=True,default=str).encode()).hexdigest()
        self.engine.store.add_event(kind,'hook:'+digest,payload,session_key=key or None)
        self.engine.signal()
        return None
