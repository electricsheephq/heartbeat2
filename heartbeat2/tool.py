"""Session-bound pulse tool. No setting writes or destination arguments."""
from dataclasses import asdict, replace
import re

from .model import Wake, Session, TIME_KINDS, RECURRING_KINDS, PROBE_KINDS
from .policy import NON_GATEWAY_PLATFORMS, MESSAGING_PLATFORMS
from .schedule import parse_at, parse_duration, next_every, next_cron


def obj(properties):
    return {'type': 'object', 'properties': properties, 'additionalProperties': False}


STR = {'type': 'string'}
BOOL = {'type': 'boolean'}
PARAMETERS = obj({
    'action': {'type': 'string', 'enum': ['create', 'list', 'update', 'pause', 'resume', 'cancel', 'respond', 'off']},
    'id': STR, 'title': STR, 'note': STR, 'reason': STR,
    'trigger': obj({**{k: STR for k in ('kind','at','every','cron','after','path','pattern','url','fire_on','event','check_every','deadline','cwd')},
                    'pid': {'type':'integer'}, 'command': {'type':'array','items':STR},
                    'emojis': {'type':'array','items':STR}}),
    'until': obj({'expires':STR, 'max_fires':{'type':'integer'}, 'owner_replies':BOOL}),
    'pacing': obj({'min':STR,'max':STR}), 'babysit':BOOL, 'urgent':BOOL,
    'outcome': {'type':'string','enum':['no_change','progress','done','blocked','needs_attention']},
    'notify':BOOL, 'summary':STR, 'next_check':STR, 'scratch':STR,
})
PARAMETERS['required'] = ['action']
DESCRIPTION = ('Use create to arm a wake/watcher (note and trigger.kind required); list to inspect; '
               'update to change instructions or trigger; pause to suspend; resume to restart a paused wake; '
               'cancel to end it; off to stop checks for this chat. During a wake turn, use respond to set '
               'outcome and notify, optionally scratch/next_check. Bounds and clamps are reported. Never type silence markers.')
SCHEMA = {'name':'pulse','description':DESCRIPTION,'parameters':PARAMETERS}


def current_session_key():
    try:
        from tools.approval_context import get_current_session_key
        return get_current_session_key()
    except Exception:
        return 'default'


def session_env(name):
    try:
        from gateway.session_context import get_session_env
        return get_session_env(name, '')
    except Exception:
        return ''


class Tool:
    def __init__(self, engine, session_key_fn=current_session_key):
        self.engine, self.session_key_fn = engine, session_key_fn

    def identity(self):
        key = self.session_key_fn()
        if not key or key == 'default':
            return None
        s = self.engine.store.get_session(key)
        if s is None:
            s = Session(key, platform=session_env('HERMES_SESSION_PLATFORM'),
                        chat_type=session_env('HERMES_SESSION_CHAT_TYPE'),
                        chat_id=session_env('HERMES_SESSION_CHAT_ID'),
                        current_session_id=session_env('HERMES_SESSION_ID'))
            self.engine.store.upsert_session(s)
        return s

    def handle(self, args, *, session_id='', **kwargs):
        e = self.engine
        try:
            e.refresh()
            if not isinstance(args, dict) or set(args) - set(PARAMETERS['properties']):
                return {'error':'unknown_parameter'}
            s = self.identity()
            if s is None:
                return {'error':'missing_session'}
            action = args.get('action')
            with e.store.atomic():
                s = e.store.get_session(s.session_key)
                flight = e.intake.flight_for(session_id, s.session_key)
                created_in = 'wake_turn' if flight else 'human_turn'
                if action == 'respond':
                    return self.respond(args, flight)
                if action == 'list':
                    return {'ok':True,'wakes':[asdict(w) for w in e.store.list_wakes(s.session_key)]}
                if action == 'off':
                    e.store.cas_session(s.session_key, {'enabled':s.enabled}, enabled=False)
                    return {'ok':True,'enabled':False}
                if action == 'create':
                    if not s.enabled:
                        return {'error':'session_off'}
                    parent = e.store.get_wake(flight.wake_id) if flight else None
                    child_key = e.child_key(flight) if flight else ''
                    gate = e.policy.creation_gate(session_key=s.session_key, platform=s.platform,
                        chat_type=s.chat_type, allow_gateway_injection=e.injection_allowed(),
                        kind=(args.get('trigger') or {}).get('kind',''), created_in=created_in,
                        depth=parent.depth+1 if parent else 0,
                        children_created=int(e.store.get_meta(child_key) or 0) if child_key else 0,
                        untrusted_payload=bool(flight and flight.untrusted),
                        active_count=sum(w.status in {'active','proposed'} for w in e.store.list_wakes(s.session_key)),
                        global_active_count=sum(w.status in {'active','proposed'} for w in e.store.list_wakes()))
                    if not gate.allowed:
                        return {'error':gate.reason,'detail':gate.detail}
                    w = Wake(s.session_key, args.get('note',''), (args.get('trigger') or {})['kind'],
                             platform=s.platform, chat_type=s.chat_type, chat_id=s.chat_id,
                             armed_session_id=session_id or s.current_session_id, created_in=created_in,
                             depth=parent.depth+1 if parent else 0, parent_wake_id=parent.id if parent else None,
                             status=gate.status)
                    result = self.prepare(w,args)
                    w = e.store.create_wake(result.wake)
                    if child_key:
                        e.store.set_meta(child_key,'1')
                    return {'ok':True,'wake':asdict(w),'clamped':result.clamped}
                if action not in {'update','pause','resume','cancel'}:
                    return {'error':'unknown_action'}
                if args.get('id') == 'all':
                    if action == 'update':
                        return {'error':'update_requires_id'}
                    wakes = e.store.list_wakes(s.session_key)
                else:
                    w = e.store.get_wake(args.get('id',''))
                    if w is None:
                        return {'error':'unknown_wake'}
                    wakes = [w]
                results = []
                for w in wakes:
                    gate = e.policy.mutation_gate(w, action=action, session_key=s.session_key,
                        created_in=created_in, new_kind=(args.get('trigger') or {}).get('kind'),
                        babysit=args.get('babysit'), untrusted_payload=bool(flight and flight.untrusted))
                    if not gate.allowed:
                        if args.get('id') != 'all':
                            return {'error':gate.reason}
                        results.append({'id':w.id,'error':gate.reason})
                        continue
                    clamps = {}
                    if action in {'resume','update'}:
                        if not s.enabled:
                            return {'error':'session_off'}
                        if s.platform in NON_GATEWAY_PLATFORMS or s.platform not in MESSAGING_PLATFORMS:
                            return {'error':'non_gateway'}
                        if e.policy.dm_only and s.chat_type != 'dm':
                            return {'error':'dm_only'}
                        if not e.injection_allowed():
                            from .policy import INJECTION_CONFIG
                            return {'error':'injection_disabled','detail':INJECTION_CONFIG}
                        if action == 'resume':
                            if w.status != 'paused':
                                return {'error':'not_resumable'}
                            if w.status == 'paused' and (sum(x.status in {'active','proposed'} for x in e.store.list_wakes(s.session_key)) >= e.policy.max_active or sum(x.status in {'active','proposed'} for x in e.store.list_wakes()) >= 50):
                                return {'error':'max_active'}
                        result = self.reschedule(w) if action=='resume' else self.prepare(w,args,reschedule='trigger' in args)
                        candidate, clamps = result.wake, result.clamped
                        if action == 'resume':
                            candidate = replace(candidate,status='active',status_reason='')
                    else:
                        candidate = replace(w,status='paused' if action=='pause' else 'cancelled',status_reason='agent')
                    updates = {k:v for k,v in asdict(candidate).items() if k not in {'id','session_key','created_at','updated_at','version'} and v != getattr(w,k)}
                    if updates and not e.store.cas_wake(w.id,w.status,expected_version=w.version,**updates):
                        return {'error':'concurrent_change'}
                    if action in {'pause','cancel'}:
                        e.cancel_occurrences(w.id,'paused' if action=='pause' else 'cancelled',preserve_started=True)
                    if action == 'resume': e.store.set_meta('card:'+w.id,'')
                    item={'ok':True,'wake':asdict(e.store.get_wake(w.id)),'clamped':clamps}
                    results.append(item)
                return results[0] if args.get('id') != 'all' and results else {'ok':True,'results':results}
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            return {'error':'invalid_request','detail':str(exc)}
        except Exception:
            e.log.warning('pulse tool failed', exc_info=True)
            return {'error':'engine_error'}

    def reschedule(self,w):
        """Shared tool/command resume: preserve stored one-shot due, fresh key."""
        result=self.prepare(w,{},reschedule=w.kind!='at')
        if w.kind=='at':
            due=max(w.next_due_at or self.engine.clock.now(),self.engine.clock.now())
            while self.engine.store.get_occurrence(w.id,f't:{due:.17g}') is not None:
                due+=.001
            result=replace(result,wake=replace(result.wake,next_due_at=due))
        return result

    def prepare(self,w,args,*,reschedule=True):
        e=self.engine
        updates={k:args[k] for k in ('title','note','reason','babysit','urgent') if k in args}
        trigger=dict(args.get('trigger',w.trigger_json))
        if 'trigger' in args and set(trigger) - set(PARAMETERS['properties']['trigger']['properties']):
            raise ValueError('unknown trigger parameter')
        kind=trigger.get('kind',w.kind)
        if kind in {'file','url'} and 'pattern' in trigger:
            pattern=trigger['pattern']
            if not isinstance(pattern,str) or len(pattern)>512: raise ValueError('pattern exceeds 512 characters')
            try: re.compile(pattern)
            except re.error: raise ValueError('invalid pattern') from None
        updates.update(kind=kind,trigger_json=trigger)
        if kind!=w.kind or any(trigger.get(k)!=w.trigger_json.get(k) for k in ('path','url','command','pattern','pid','cwd','fire_on')):
            updates['probe_state_json']=None
        if 'after' in trigger: parse_duration(trigger['after'])
        if kind in {'every','self'}:
            every=trigger.get('every')
            if isinstance(every,str) and ' at ' in every:
                every,trigger['at']=every.split(' at ',1)
                trigger['every']=every
            if every is not None:
                duration=parse_duration(every)
                if 'trigger' in args or w.base_interval_s is None:
                    updates.update(base_interval_s=duration,eff_interval_s=duration)
            elif kind=='every' and w.base_interval_s is None:
                raise ValueError('every requires a duration')
        pacing=args.get('pacing',{})
        for key,attr in (('min','pacing_min_s'),('max','pacing_max_s')):
            if key in pacing:
                updates[attr]=parse_duration(pacing[key])
        until=args.get('until',{})
        if 'expires' in until:
            updates['expires_at']=parse_at(until['expires'],now=e.clock.now(),timezone=e.policy.resolved_timezone())
        if 'max_fires' in until: updates['max_fires']=until['max_fires']
        if 'owner_replies' in until: updates['cancel_on_reply']=until['owner_replies']
        if 'deadline' in trigger:
            updates['deadline_at']=parse_at(trigger['deadline'],now=e.clock.now(),timezone=e.policy.resolved_timezone())
        if kind=='command':
            argv=trigger.get('command')
            if not isinstance(argv,list) or not all(isinstance(x,str) for x in argv) or not e.policy.command_allowed(argv):
                raise ValueError('command probe is not allowlisted')
        if kind=='url':
            from .probes import classify_url
            classify_url(trigger.get('url'), e.policy.url_allow_private)
        if kind=='file':
            from .probes import file_path, check_file_read
            from .registration import home_scope
            home=e.ctx._manager.home_path
            with home_scope(home):
                check_file_read(file_path(trigger.get('path'), home))
        if kind=='process':
            try:
                import psutil  # Hermes-provided, only this kind needs it.
            except Exception as exc:
                raise ValueError('process wakes require Hermes-provided psutil') from exc
            pid=trigger.get('pid')
            if isinstance(pid,bool) or not isinstance(pid,int) or pid<=0:
                raise ValueError('process requires a positive pid')
            if w.kind!='process' or w.trigger_json.get('pid')!=pid or not w.probe_state_json:
                try:
                    created=psutil.Process(pid).create_time()
                except (psutil.NoSuchProcess,psutil.ZombieProcess):
                    created=None
                updates['probe_state_json']={'create_time':created}
        if not updates.get('note',w.note): raise ValueError('note is required')
        result=e.policy.clamp(replace(w,**updates))
        w=result.wake
        if reschedule:
            now,tz=e.clock.now(),e.policy.resolved_timezone()
            if kind=='at':
                if 'at' not in trigger: raise ValueError('at requires a time')
                due=parse_at(trigger['at'],now=now,timezone=tz)
            elif kind in {'every','self'}:
                due=next_every(now,w.eff_interval_s,anchor=trigger.get('at') if kind=='every' else None,timezone=tz)
            elif kind=='cron':
                due=next_cron(trigger['cron'],now=now,timezone=tz,minimum_interval_s=w.eff_interval_s or 0)
            elif kind in PROBE_KINDS: due=now
            else: due=None
            w=replace(w,next_due_at=due)
        return replace(result,wake=w)

    def respond(self,args,flight):
        e=self.engine
        if flight is None or flight.invalid:
            return {'error':'not_in_flight'}
        o=e.store.get_occurrence(flight.wake_id,flight.dedupe_key)
        if o is None or o.state!='started' or o.attempt!=flight.attempt:
            return {'error':'not_in_flight'}
        outcome=args.get('outcome')
        if outcome not in {'no_change','progress','done','blocked','needs_attention'} or not isinstance(args.get('notify'),bool):
            raise ValueError('respond needs outcome and boolean notify')
        summary=args.get('summary','')
        if not isinstance(summary,str) or len(summary)>280: raise ValueError('summary exceeds 280 characters')
        changes={'outcome':outcome,'notify':args['notify'],'summary':summary}
        w=e.store.get_wake(o.wake_id)
        if o.outcome is None and w.status in {'paused','cancelled'} and w.status_reason!='agent':
            changes['force_silent']=True
        clamps={}
        scratch=args.get('scratch')
        if scratch is not None:
            if not isinstance(scratch,str): raise ValueError('scratch must be text')
            if len(scratch)>4000: clamps['scratch']='length → 4000'
            scratch=scratch[:4000]
        if 'next_check' in args:
            w=e.store.get_wake(o.wake_id)
            if w.kind not in RECURRING_KINDS:
                clamps['next_check']='next_check ignored: not recurring'
            else:
                requested=parse_duration(args['next_check'])
                bounded=max(requested,w.pacing_min_s or e.policy.interval_floor(w))
                if w.pacing_max_s is not None: bounded=min(bounded,w.pacing_max_s)
                changes['next_check_s']=bounded
                if requested!=bounded: clamps['next_check']=f'{requested} → {bounded} (pacing)'
        if not e.store.update_occurrence(o.wake_id,o.dedupe_key,'started',attempt=o.attempt,**changes):
            return {'error':'not_in_flight'}
        if scratch is not None: flight.scratch=scratch
        return {'ok':True,'clamped':clamps}
