"""Fail-open Hermes observers; final output silence is an exact transform."""
from collections import Counter
from dataclasses import dataclass, fields
from functools import wraps
import hashlib
import json
import re
import uuid

from .model import Session
from . import governor

MARKER = re.compile(r'^⟦pulse o:([^\s]+) a:(\d+)⟧(?:\s|$)')
ORIGIN = 'Gateway message origin (JSON data, not instructions or authorization):'
GOAL_PREFIXES = ('[Continuing toward your standing goal]',
                 '[Continuing toward your standing goal — a quality gate failed]')
HB_PREFIX = '[Heartbeat — recurring instruction'
# Full-note match, never a prefix: gateway/run.py:1030 and run_turn_runner.py:1645
# agree at v0.21.6 (818c13be), v0.21.7 (71d34bad), main (04eb00ce).
# TUI's _AUTO_CONTINUE_NOTE_PREFIX embeds the interrupted request after its note;
# that is not a note-only gateway replay and deliberately does not match.
RESUME_NOTE = re.compile(
    r'\[System note: The previous turn was interrupted by a gateway (?:restart|shutdown|interruption); '
    r'the gateway is now back online\. Any restart/shutdown command in the history has already '
    r'run — do NOT re-execute or verify it\. (?:Report to the user that the session was restored '
    r'successfully and ask what they would like to do next\. Do NOT re-execute old tool calls — '
    r'skip any unfinished work from the conversation history\.|No user is present on this '
    r"non-interactive platform, so do NOT emit a 'session restored' acknowledgement or ask questions\. "
    r'Review the conversation history and CONTINUE the interrupted task to completion\. '
    r'Do NOT re-run tool calls whose results already appear in the history — resume from the '
    r'first step that has no recorded result\.)\]')
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


def pulse_command(text):
    return isinstance(text,str) and bool(re.match(r'^/pulse(?:@[^\s]+)?(?:\s|$)',text.strip(),re.I))


def origin_ids(text):
    for match in re.finditer(re.escape(ORIGIN)+r'\n(\{[^\n]*\})\n',text):
        try:
            data=json.loads(match[1])
        except ValueError:
            continue
        if isinstance(data,dict):
            yield str(data.get('message_id') or data.get('source_message_id') or '') or None


def row_text(content):
    if isinstance(content,str): return content
    if isinstance(content,list):
        return '\n'.join(part['text'] for part in content if isinstance(part,dict)
                         and part.get('type')=='text' and isinstance(part.get('text'),str))
    return ''


COMPACTION_MARKERS = ('--- END OF CONTEXT SUMMARY', '[STILL IN PROGRESS — this is the active request')
STEER_COMMAND = re.compile(r'^/(steer|s)(@\S+)?(\s|$)',re.I)


def origin_rows(history):
    for row in history or []:
        if not isinstance(row,dict) or row.get('role')!='user': continue
        # Compaction rewrites carried rows in place; a rewrite is not new owner text.
        if row.get('_compressed_summary'): continue
        text=row_text(row.get('content',''))
        if ORIGIN in text and not any(m in text for m in COMPACTION_MARKERS):
            yield hashlib.sha256(text.encode()).hexdigest()[:16],text


def keys_for(key,mid):
    return [f'{key}:{mid}',f'{key}:{hashlib.sha256(str(mid).encode()).hexdigest()[:12]}']


def mark_fold(flight):
    flight.owner_replied=True


def delegated_child(platform=''):
    if platform=='subagent': return True
    try:
        from agent.delegation_context import is_delegated_child_context
        return is_delegated_child_context()
    except Exception:
        return False


def bounded_payload(payload):
    """Keep hook text within the inbox budget before Store.add_event.

    Bound decoded UTF-8, including JSON escaping. Identifiers are retained;
    free text shares the remaining 4 KiB budget rather than losing the event.
    """
    free_text={'summary','child_summary','reason','text'}
    result=dict(payload)
    for key in free_text & result.keys():
        if isinstance(result[key],str):
            result[key]=result[key].encode('utf-8')[:1500].decode('utf-8',errors='ignore')
    def size(): return len(json.dumps(result,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode())
    while size()>4096:
        candidates=[k for k in free_text & result.keys() if isinstance(result[k],str) and result[k]]
        if not candidates: break  # malformed/oversized identifiers remain refused by Store
        key=max(candidates,key=lambda k:len(result[k].encode('utf-8')))
        value=result[key].encode('utf-8')
        result[key]=value[:len(value)//2].decode('utf-8',errors='ignore')
    return result


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
        self.anchors={}
        self.inbounds=set()
        self.owner_turns={}

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
            if old in self.anchors: self.anchors[sid]=self.anchors.pop(old)
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
            identities=[dedupe] if isinstance(dedupe,str) else dedupe or []
            if any(s.get_meta('human:'+d) or s.get_meta('nonhuman:'+d) for d in identities): return
            s.upsert_session(Session(key,platform=platform,chat_type=source.get('chat_type',''),chat_id=str(source.get('chat_id') or '')))
            old=s.get_session(key)
            now=e.clock.now()
            updates={'last_human_at':now,'last_activity_at':now,'episode_id':uuid.uuid4().hex,'unanswered_count':0}
            if platform: updates['platform']=platform
            if 'chat_type' in source: updates['chat_type']=source['chat_type'] or ''
            if 'chat_id' in source: updates['chat_id']=str(source['chat_id'] or '')
            s.cas_session(key,{f.name:getattr(old,f.name) for f in fields(Session) if f.name!='session_key'},**updates)
            for d in identities: s.set_meta('human:'+d,'1')
            s.set_meta('human_context:'+key,'1')
            for o in s.list_occurrences(session_key=key,state='done'):
                if o.delivered and o.ended_at is not None and 0<=now-o.ended_at<=e.policy.engagement_window_hours*3600:
                    governor.engagement(e,o)
            for w in s.list_wakes(key):
                if w.depth:
                    s.cas_wake(w.id,w.status,expected_version=w.version,depth=0)
                if w.cancel_on_reply and w.status in {'active','paused','proposed'}:
                    s.cas_wake(w.id,w.status,status='cancelled',status_reason='owner_replied')
                    e.cancel_occurrences(w.id,'owner_replied')
            s.add_event('human_message',identities[0] if identities else uuid.uuid4().hex,{},session_key=key)
        e.signal()

    def count(self,key,mid,platform='',source=None):
        self.human(key,platform,source,keys_for(key,mid) if mid else None)

    def _count_origins(self,key,text,turn_id,fp,ordinal=1,platform=''):
        for mid in origin_ids(text):
            if mid: self.count(key,mid,platform)
            else:
                dedupe=f'{key}:origin:{turn_id}:{fp}:{ordinal}'
                self.human(key,platform,dedupe=dedupe)

    def _session_switch(self,w,session_id):
        """A known non-lineage switch has the same ownership boundary as /new."""
        e,s=self.engine,self.engine.store
        if w.trigger_json.get('builtin'):
            s.cas_wake(w.id,w.status,expected_version=w.version,armed_session_id=session_id)
            return False
        if w.status not in {'active','paused','proposed'}: return False
        if not s.cas_wake(w.id,w.status,expected_version=w.version,status='orphaned',status_reason='session_switched'): return False
        e.cancel_occurrences(w.id,'session_switched')
        count=int(s.get_meta('switched_notice:'+w.session_key) or 0)+1
        s.set_meta('switched_notice:'+w.session_key,str(count))
        return True

    @fail_open
    def pre_gateway_dispatch(self,event=None,gateway=None,**kwargs):
        if event is None or getattr(event,'internal',False) or pulse_command(getattr(event,'text','')): return None
        text=getattr(event,'text','') or ''
        if getattr(event,'_heartbeat_session_id',None) or text.startswith((HB_PREFIX,)+GOAL_PREFIXES): return None
        source=getattr(event,'source',None)
        platform=getattr(getattr(source,'platform',None),'value','')
        chat_id=str(getattr(source,'chat_id','') or '')
        key=''
        resolver=getattr(gateway,'_session_key_for_source',None)
        if callable(resolver):
            try: key=resolver(source)
            except Exception: pass
        resolved=isinstance(key,str) and bool(key)
        if not resolved:
            matches=[s for s in self.engine.store.list_sessions() if s.platform==platform and s.chat_id==chat_id]
            if not chat_id or len(matches)>1: return None
            key=matches[0].session_key if matches else ''
        mid=getattr(event,'message_id','')
        if key: self.inbounds.add(key)
        # SessionSource.to_dict() drops is_bot; preserve the real event verdict
        # for the later admission/human branch, never inspect an absent dict key.
        if getattr(source,'is_bot',False):
            if mid and key: self.engine.store.set_meta(f'nonhuman:{key}:{mid}','1')
            return None
        authorized=getattr(gateway,'_is_user_authorized_for_source',None)
        if authorized and not authorized(source): return None
        if not mid and getattr(source,'message_id',None): return None
        # A leftover ORIGIN steer always becomes a turn; B owns its count.
        if text.startswith(ORIGIN): return None
        self.engine.refresh()
        session=self.engine.store.get_session(key)
        # A busy-session /steer becomes an ORIGIN row that C counts; an idle one
        # becomes a plain turn with no ORIGIN, so A must count it.
        if STEER_COMMAND.match(text) and session and session.busy_since: return None
        if not session:
            stamp=f'pending_human:key:{key}' if resolved else f'pending_human:{platform}:{chat_id}'
            self.engine.store.set_meta(stamp,
                                       json.dumps({'mid':mid or None,'at':self.engine.clock.now()}))
            return None
        route={'chat_type':getattr(source,'chat_type',''),'chat_id':chat_id}
        self.count(key,mid,platform,route)
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
        payload=bounded_payload(payload)
        digest=hashlib.sha1(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        s.add_event(event_type,f'{platform}:{event_type}:{digest}',payload,session_key=key,chat_id=matches[0].chat_id)
        self.engine.signal()
        return None

    @fail_open
    def pre_llm_call(self,session_id='',turn_id='',user_message='',platform='',conversation_history=None,**kwargs):
        if delegated_child(platform): return None
        e,s=self.engine,self.engine.store
        e.refresh()
        self._follow(session_id)
        key=self._key(session_id)
        if not key: return None
        identity=e.tool.identity()
        if identity is None: return None
        key=identity.session_key
        owner_seen=key in self.inbounds or self.owner_turns.get(session_id)==turn_id
        self.inbounds.discard(key)
        self.anchors[session_id]=(turn_id,Counter(fp for fp,_ in origin_rows(conversation_history)))
        for stamp in (f'pending_human:key:{identity.session_key}',
                      f'pending_human:{identity.platform}:{identity.chat_id}'):
            pending=s.get_meta(stamp)
            if pending:
                owner_seen=True
                pending=json.loads(pending)
                if 0<=e.clock.now()-pending['at']<=3600:
                    self.count(key,pending.get('mid'),identity.platform)
                s.set_meta(stamp,'')
        parsed=marker(user_message)
        if not parsed:
            row=conversation_history[-1] if conversation_history else None
            if isinstance(row,dict) and row.get('role')=='user' and not row.get('display_kind'):
                text=row_text(row.get('content',''))
                if not text.startswith(GOAL_PREFIXES) and not pulse_command(text):
                    if ORIGIN in text:
                        owner_seen=True
                        self._count_origins(key,text,turn_id,hashlib.sha256(text.encode()).hexdigest()[:16],platform=platform)
                    elif row.get('platform_message_id'):
                        owner_seen=True
                        self.count(key,row['platform_message_id'],platform)
        if owner_seen: self.owner_turns[session_id]=turn_id
        previous=self.flights.get(session_id)
        if previous and turn_id and previous.turn_id and previous.turn_id!=turn_id:
            # Apply the new owner row before terminal cleanup of a prior flight.
            self.on_session_end(session_id=session_id,turn_id=previous.turn_id,failed=True)
        self.bindings[session_id]=key
        self.turns[session_id]=turn_id
        now=e.clock.now()
        with s.atomic():
            session=s.get_session(key)
            s.cas_session(key,{'current_session_id':session.current_session_id},current_session_id=session_id,busy_since=now,last_activity_at=now)
            if not parsed and not owner_seen and RESUME_NOTE.fullmatch(user_message.strip()):
                history=list(conversation_history or [])
                # pre_llm_call receives a separate trailing recovery USER row;
                # the API sanitizer later merges it into the marker USER row.
                while history and isinstance(history[-1],dict) and history[-1].get('role')=='user' and RESUME_NOTE.fullmatch(row_text(history[-1].get('content','')).strip()):
                    history.pop()
                row=history[-1] if history else None
                replay=marker(row_text(row.get('content',''))) if isinstance(row,dict) and row.get('role')=='user' else None
                if replay:
                    wid,dedupe,attempt=replay
                    w,o=s.get_wake(wid),s.get_occurrence(wid,dedupe)
                    if w and w.session_key==key and w.armed_session_id==session_id and o and o.attempt==attempt and o.state not in {'claimed','started'}:
                        self.flights[session_id]=Flight(key,session_id,turn_id,wid,dedupe,attempt,invalid=True)
                        stamp='resumed_turn:'+hashlib.sha256((session_id+':'+turn_id).encode()).hexdigest()
                        if not s.get_meta(stamp) and s.cas_wake(w.id,w.status,expected_version=w.version,model_turns=w.model_turns+1,status_reason='resumed_after_restart'):
                            e.ledger(w,model_turns=1)
                            s.set_meta(stamp,'1')
                        return None
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
                    else:
                        invalid=True
                        if tip is not None:
                            self._session_switch(w,session_id)
                            w=s.get_wake(wid)
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
                    tip=e.compression_tip(w.armed_session_id)
                    if tip==session_id:
                        s.cas_wake(w.id,w.status,expected_version=w.version,armed_session_id=session_id)
                    elif tip is not None:
                        self._session_switch(w,session_id)
            # Human turn context never turns a model call into a wake.
            self.flights.pop(session_id,None)
            if not s.get_meta('human_context:'+key): return None
            s.set_meta('human_context:'+key,'')
            lines=[]
            count=int(s.get_meta('switched_notice:'+key) or 0)
            if count:
                lines.append(f'{count} wakes armed in your previous conversation were stopped; /pulse list')
                s.set_meta('switched_notice:'+key,'')
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
                    lines.append(f'{w.title or "A wake"} ({w.id}) paused ({card}): keep / slow / stop via /pulse; convert with /pulse watch {w.id}.')
                    s.set_meta('card:'+w.id,'')
            limits={f.name:getattr(e.policy,f.name) for f in fields(e.policy) if f.name not in {'clock','profile_timezone'}}
            fingerprint=hashlib.sha256(json.dumps(limits,sort_keys=True).encode()).hexdigest()
            session=s.get_session(key)
            if session.limits_fingerprint and session.limits_fingerprint!=fingerprint:
                lines.append('HeartBeat 2.0 limits changed outside /pulse; current bounds now apply.')
            s.cas_session(key,{'limits_fingerprint':session.limits_fingerprint},limits_fingerprint=fingerprint)
            return {'context':'\n'.join(lines)} if lines else None

    @fail_open
    def pre_api_request(self,session_id='',turn_id='',conversation_history=None,**kwargs):
        self._follow(session_id)
        anchor=self.anchors.get(session_id)
        if not anchor or anchor[0]!=turn_id: return None
        key=self.bindings.get(session_id)
        if not key: return None
        rows=list(origin_rows(conversation_history))
        current=Counter(fp for fp,_ in rows)
        baseline=anchor[1]
        new=current-baseline
        texts=dict(rows)
        f=self.flights.get(session_id)
        for fp,n in new.items():
            for j in range(baseline[fp]+1,baseline[fp]+n+1):
                self._count_origins(key,texts[fp],turn_id,fp,j)
            if f and f.turn_id==turn_id: mark_fold(f)
        baseline.update(new)
        return None

    @fail_open
    def api_request_error(self,session_id='',turn_id='',reason='',status_code=None,error=None,**kwargs):
        self.event('api_error',session_id=session_id,
            dedupe_extra=turn_id,
            reason=reason or ('http_%s'%status_code if status_code else 'unknown'),
            **{k:kwargs[k] for k in ('task_id',) if k in kwargs})
        # Hermes agent/api_request_hooks.py emits this per physical retry.
        # Retain only the bounded classification, never request/error text.
        if delegated_child(kwargs.get('platform','')): return None
        kinds={'rate_limit':'rate_limit','upstream_rate_limit':'rate_limit','quota':'quota','quota_exhausted':'quota',
               'auth':'auth','auth_permanent':'auth','billing':'billing','server_error':'server','overloaded':'server'}
        kind=kinds.get(reason)
        if not kind:
            kind='rate_limit' if status_code==429 else 'auth' if status_code in {401,403} else 'billing' if status_code==402 else 'server' if isinstance(status_code,int) and status_code>=500 else None
        if not kind: return None
        e,s=self.engine,self.engine.store
        self._follow(session_id)
        f=self.flights.get(session_id)
        if f and turn_id and f.turn_id and turn_id!=f.turn_id: return None
        with s.atomic():
            stamp='provider_failure:'+hashlib.sha256((session_id+':'+turn_id).encode()).hexdigest()
            if f:
                o=s.get_occurrence(f.wake_id,f.dedupe_key)
                if o and o.state=='started' and o.attempt==f.attempt:
                    first=not s.get_meta('provider_error:'+o.id)
                    s.set_meta('provider_error:'+o.id,kind)
                    # The physical attempt may recover through retries, refresh
                    # or fallback. Only G5/end/timeout can settle the turn.
                    if first: e.provider_failure(kind)
                    s.set_meta(stamp,'1')
            elif session_id and turn_id:
                if not s.get_meta(stamp):
                    s.set_meta(stamp,'1'); e.provider_failure(kind)
        return None

    @fail_open
    def post_api_request(self,session_id='',turn_id='',usage=None,**kwargs):
        e,s=self.engine,self.engine.store
        s.set_meta('provider_health','')
        e.signal()
        if delegated_child(kwargs.get('platform','')): return None
        self._follow(session_id)
        key=self.bindings.get(session_id)
        has_prompt=any((usage.get(name) if isinstance(usage,dict) else getattr(usage,name,None)) is not None
                       for name in ('prompt_tokens','input_tokens'))
        if not has_prompt: usage=None
        def count(*names):
            for name in names:
                v=usage.get(name) if isinstance(usage,dict) else getattr(usage,name,None)
                if v is not None: return max(0,int(v))
            return 0
        from . import cache
        route=cache.route_key(kwargs.get('provider'),kwargs.get('model'),kwargs.get('base_url'))
        prompt,read,written=count('prompt_tokens','input_tokens'),count('cache_read_tokens'),count('cache_write_tokens')
        if key and (not turn_id or not self.turns.get(session_id) or self.turns[session_id]==turn_id):
            session=s.get_session(key)
            s.cas_session(key,{'last_activity_at':session.last_activity_at},last_activity_at=e.clock.now())
            if usage is not None:
                cache.touch(s,session,started_at=kwargs.get('started_at'),route=route,prompt=prompt,read=read,api_mode=kwargs.get('api_mode',''))
        f=self.flights.get(session_id)
        if not f or f.invalid or turn_id and f.turn_id and turn_id!=f.turn_id: return None
        o=s.get_occurrence(f.wake_id,f.dedupe_key)
        if o and o.attempt==f.attempt: s.set_meta('provider_error:'+o.id,'')
        price=None
        if usage is not None:
            try: price=cache.cost(route,prompt=prompt,read=read,written=written,out=count('completion_tokens','output_tokens'),ttl=cache.cache_ttl_for(route,store=s,platform=s.get_session(key).platform,api_mode=kwargs.get('api_mode',''))[0])
            except Exception: pass
        with s.atomic():
            o=s.get_occurrence(f.wake_id,f.dedupe_key)
            if o and o.state=='started':
                s.update_occurrence(o.wake_id,o.dedupe_key,'started',attempt=f.attempt,
                    api_calls=o.api_calls+1,
                    cache_read_tokens=o.cache_read_tokens+read,cache_write_tokens=o.cache_write_tokens+written,
                    warm_wakes=o.warm_wakes+int(not o.api_calls and prompt>0 and read/prompt>=.5),
                    cold_wakes=o.cold_wakes+int(usage is not None and not o.api_calls and (not prompt or read/prompt<.5)),
                    cost_microusd=o.cost_microusd+(round(price*1e6) if price is not None else 0),unpriced_calls=o.unpriced_calls+int(price is None),
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
            if f.owner_replied: return None
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
                reason=kwargs.get('turn_exit_reason','') or ''
                owner_interrupt=interrupted and reason in {'interrupted_by_user','interrupted_during_api_call'}
                e.interrupt(o,'owner_interrupted' if owner_interrupt else 'failed' if failed else 'interrupted',
                            count_error=not (f.owner_replied or owner_interrupt))
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
                reason='interrupted_by_user' if kwargs.get('invalidation_reason')=='stop_command' else ''
                self.on_session_end(session_id=sid,interrupted=True,turn_exit_reason=reason)
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
    def event(self,kind,session_id='',parent_session_id='',dedupe_extra='',**kwargs):
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
        payload=bounded_payload({k:kwargs[k] for k in allowed if k in kwargs})
        digest=hashlib.sha1(json.dumps([kind,key,payload,dedupe_extra],sort_keys=True,default=str).encode()).hexdigest()
        self.engine.store.add_event(kind,'hook:'+digest,payload,session_key=key or None)
        self.engine.signal()
        return None
