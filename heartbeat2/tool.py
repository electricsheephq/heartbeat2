"""Session-bound pulse tool. No setting writes or destination arguments."""
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
import json
import re

from .model import Wake, Session, TIME_KINDS, RECURRING_KINDS, PROBE_KINDS, EVENT_KINDS
from .policy import NON_GATEWAY_PLATFORMS, MESSAGING_PLATFORMS, owner_class
from .schedule import parse_at, parse_duration, next_every, next_cron


def obj(properties):
    return {'type': 'object', 'properties': properties, 'additionalProperties': False}


STR = {'type': 'string'}
BOOL = {'type': 'boolean'}
PARAMETERS = obj({
    'action': {'type': 'string', 'enum': ['create', 'list', 'update', 'pause', 'resume', 'cancel', 'respond', 'off']},
    'id': STR, 'title': STR, 'note': STR, 'reason': STR,
    'trigger': obj({**{k: STR for k in ('kind','at','every','cron','after','path','pattern','url','check_every','deadline','cwd')},
                    'fire_on': {'type':'string','enum':['change','success','failure','json']},
                    'pid': {'type':'integer'}, 'command': {'type':'array','items':STR},
                    'emojis': {'type':'array','items':STR}}),
    'until': obj({'expires':STR, 'max_fires':{'type':'integer'}, 'owner_replies':BOOL}),
    'pacing': obj({'min':STR,'max':STR}), 'babysit':BOOL, 'urgent':BOOL, 'owner_asked':BOOL,
    'outcome': {'type':'string','enum':['no_change','progress','done','blocked','needs_attention']},
    'notify':BOOL, 'summary':STR, 'next_check':STR, 'scratch':STR,
})
PARAMETERS['required'] = ['action']
DESCRIPTION = ('Use create to arm a wake/watcher (note and trigger.kind required); list to inspect; '
               'update to change instructions or trigger; pause to suspend; resume to restart a paused wake; '
               'cancel to end it; off to stop checks for this chat. During a wake turn, use respond to set '
               'outcome and notify, optionally scratch/next_check. Bounds and clamps are reported. Never type silence markers. '
               'Results include a `cache` line; pick intervals the line marks warm. '
               'Results include a receipt line; tell the owner what it says, not a promise of your own. '
               'When a create is refused, tell the owner why and what can be done; never arm a stand-in or relay wake '
               'that re-arms later to get around a limit. '
               'For a one-time "at" wake the owner asks for in their current message, set owner_asked=true: '
               "it then fires like the owner's own reminder (not muted, not capped). Never set it for your own follow-ups.")
SCHEMA = {'name':'pulse','description':DESCRIPTION,'parameters':PARAMETERS}
NOT_IN_FLIGHT = {'error':'not_in_flight','detail':'respond is only for answering a wake turn; in an ordinary conversation, just reply to the owner (use create/update/cancel to manage wakes).'}


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


class Infeasible(ValueError):
    pass


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
        result=self._handle(args,session_id=session_id,**kwargs)
        notice=self.engine.settings_notice()
        if notice: result['settings_error']=notice
        return result

    def owner_turn_evidence(self, session_id, flight):
        e = self.engine
        return (flight is None and bool(session_id) and bool(turn := e.intake.turns.get(session_id))
                and e.intake.owner_turns.get(session_id) == turn)

    def _handle(self, args, *, session_id='', **kwargs):
        e = self.engine
        try:
            e.refresh()
            if not isinstance(args, dict) or set(args) - set(PARAMETERS['properties']):
                return {'error':'unknown_parameter'}
            if 'owner_asked' in args and not isinstance(args['owner_asked'], bool):
                raise ValueError('owner_asked must be a boolean')
            for field in ('trigger','until','pacing'):
                if field in args and not isinstance(args[field],dict):
                    raise ValueError(f'invalid request: {field} must be an object')
            s = self.identity()
            if s is None:
                return {'error':'missing_session'}
            action = args.get('action')
            with e.store.atomic():
                s = e.store.get_session(s.session_key)
                flight = e.intake.flight_for(session_id, s.session_key)
                evidence = self.owner_turn_evidence(session_id, flight)
                created_in = 'wake_turn' if flight else 'human_turn'
                if action == 'respond':
                    return self.respond(args, flight)
                if action == 'list':
                    return {'ok':True,'wakes':[{**asdict(w),'cache':self.cache_result(w)} for w in e.store.list_wakes(s.session_key)]}
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
                    kind = (args.get('trigger') or {})['kind']
                    requested = args.get('owner_asked') is True and evidence and kind == 'at'
                    w = Wake(s.session_key, args.get('note',''), (args.get('trigger') or {})['kind'],
                             platform=s.platform, chat_type=s.chat_type, chat_id=s.chat_id,
                             armed_session_id=session_id or s.current_session_id, created_in=created_in,
                             depth=parent.depth+1 if parent else 0, parent_wake_id=parent.id if parent else None,
                             status=gate.status, owner_requested=requested)
                    result = self.prepare(w,args)
                    w = e.store.create_wake(result.wake)
                    if child_key:
                        e.store.set_meta(child_key,'1')
                    item = {'ok':True,'wake':asdict(w),'clamped':result.clamped,'cache':self.cache_result(w),'receipt':self.receipt(w,s)}
                    if args.get('owner_asked') is True and not requested:
                        item['owner_asked_ignored'] = 'wake_turn' if flight is not None else 'not_owner_turn' if not evidence else 'not_one_shot'
                    return item
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
                    if action == 'update' and w.trigger_json.get('builtin') and 'trigger' in args:
                        return {'error':'builtin_settings_owned','detail':'The check-in is controlled in Settings.'}
                    gate = e.policy.mutation_gate(w, action=action, session_key=s.session_key,
                        created_in=created_in, new_kind=(args.get('trigger') or {}).get('kind'),
                        babysit=args.get('babysit'), untrusted_payload=bool(flight and flight.untrusted))
                    if not gate.allowed:
                        if args.get('id') != 'all':
                            return {'error':gate.reason, **({'detail':gate.detail} if gate.detail else {})}
                        results.append({'id':w.id,'error':gate.reason, **({'detail':gate.detail} if gate.detail else {})})
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
                        # An update without a trigger keeps a stored relative deadline instead of re-reading it.
                        fresh = 'trigger' in args
                        trig = args.get('trigger')
                        new_kind = trig.get('kind',w.kind) if isinstance(trig,dict) else w.kind
                        # The owner's grant applies before the first feasibility pass, so a requested retime gets the reminder bound.
                        base = (replace(w,owner_requested=True) if action == 'update' and w.creator == 'agent'
                                and not w.trigger_json.get('builtin') and w.parent_wake_id is None
                                and args.get('owner_asked') is True and evidence and new_kind == 'at' else w)
                        result = self.reschedule(w) if action=='resume' else self.prepare(base,args,reschedule=fresh,reparse_deadline=fresh)
                        candidate, clamps = result.wake, result.clamped
                        until = args.get('until') if isinstance(args.get('until'),dict) else {}
                        timing = ('trigger_json','kind','next_due_at','deadline_at','base_interval_s','eff_interval_s','pacing_min_s','pacing_max_s')
                        # Urgency changes quiet-hours behaviour; title and reason are cosmetic and keep owner status.
                        edited = [*timing,'note','urgent']
                        # Lifetime fields revoke only when the agent set them, never via a policy re-clamp.
                        edited += [f for f,present in (('expires_at','expires' in until),('max_fires','max_fires' in until),
                                                        ('cancel_on_reply','owner_replies' in until),('babysit','babysit' in args)) if present]
                        changed = {k for k in edited if getattr(candidate,k) != getattr(w,k)}
                        if action == 'update':
                            watch_conversion = (w.status_reason == 'watch_requested'
                                and candidate.kind in PROBE_KINDS | EVENT_KINDS and changed <= set(timing)
                                and all(getattr(candidate,k) == getattr(w,k) for k in ('title','reason','urgent')))
                            if w.creator == 'user' and changed and not watch_conversion:
                                candidate = replace(candidate,agent_edited=True)
                                result = self.prepare(candidate,{'until':until},reschedule=fresh,reparse_deadline=fresh)
                                # The raised floor also covers the occurrence already due at the owner's cadence.
                                if ('trigger' not in args and candidate.kind in RECURRING_KINDS
                                        and (result.wake.eff_interval_s or 0) > (w.eff_interval_s or 0)):
                                    due = self.prepare(result.wake,{'until':until},reparse_deadline=False).wake.next_due_at
                                    boundary = e.clock.now() + result.wake.eff_interval_s
                                    anchor = result.wake.trigger_json.get('at') if candidate.kind == 'every' else None
                                    if anchor and due < boundary:  # Stay on the anchor grid, past the raised floor.
                                        due = next_every(boundary,result.wake.eff_interval_s,anchor=anchor,
                                                         timezone=e.policy.resolved_timezone())
                                    result = replace(result,wake=replace(result.wake,next_due_at=max(due,w.next_due_at or due)))
                                candidate = result.wake
                                clamps.update(result.clamped)
                            if w.status_reason == 'watch_requested':
                                candidate = replace(candidate,status_reason='')
                        if w.creator == 'agent' and not w.trigger_json.get('builtin') and w.parent_wake_id is None:
                            granted = args.get('owner_asked') is True and evidence and candidate.kind == 'at'
                            if granted:
                                candidate = replace(candidate,owner_requested=True)
                            elif action == 'resume' or changed:
                                candidate = replace(candidate,owner_requested=False)
                        if action == 'resume':
                            candidate = replace(candidate,status='active',status_reason='')
                        result = e.policy.clamp(candidate,cache_ttl=e.timing(candidate)['cache_ttl'])
                        candidate = result.wake
                        clamps.update(result.clamped)
                        candidate = self.feasible(candidate,explicit_expiry='expires' in until)
                    else:
                        candidate = replace(w,status='paused' if action=='pause' else 'cancelled',status_reason='agent')
                    updates = {k:v for k,v in asdict(candidate).items() if k not in {'id','session_key','created_at','updated_at','version'} and v != getattr(w,k)}
                    if updates and not e.store.cas_wake(w.id,w.status,expected_version=w.version,**updates):
                        return {'error':'concurrent_change'}
                    if action in {'pause','cancel'}:
                        e.cancel_occurrences(w.id,'paused' if action=='pause' else 'cancelled',preserve_started=True)
                    if action == 'resume': e.store.set_meta('card:'+w.id,'')
                    w=e.store.get_wake(w.id)
                    item={'ok':True,'wake':asdict(w),'clamped':clamps,'cache':self.cache_result(w)}
                    if action in {'update','resume'}: item['receipt']=self.receipt(w,s)
                    results.append(item)
                return results[0] if args.get('id') != 'all' and results else {'ok':True,'results':results}
        except ValueError as exc:
            return {'error':'invalid_request','detail':str(exc)}
        except (TypeError, KeyError, OverflowError, AttributeError):
            e.log.warning('pulse tool rejected a malformed request', exc_info=True)
            return {'error':'invalid_request','detail':'invalid request: check the field types and required trigger values'}
        except Exception:
            e.log.warning('pulse tool failed', exc_info=True)
            return {'error':'engine_error'}

    def cache_result(self,w):
        from .policy import is_watcher
        seconds = max(0,w.next_due_at-self.engine.clock.now()) if w.kind=='at' and w.next_due_at is not None else w.eff_interval_s
        return self.engine.cache_line(w,seconds=seconds,watcher=is_watcher(w) or w.kind=='owner_silent')

    def feasible(self,w,*,explicit_expiry=False):
        """Check the final due/deadline against the bounded lifetime before storing."""
        e,p=self.engine,self.engine.policy
        point,label=w.next_due_at,'due'
        if w.kind=='at' and point is not None and point<e.clock.now()-p.catch_up_hours*3600:
            raise Infeasible('that time has already passed')
        watcher=w.kind in PROBE_KINDS | EVENT_KINDS
        if watcher:
            point,label=(w.deadline_at,'deadline') if w.deadline_at is not None else (w.next_due_at if w.kind in PROBE_KINDS else e.clock.now(),'due')
        elif w.kind=='owner_silent':
            if w.trigger_json.get('builtin'): return w
            last=getattr(e.store.get_session(w.session_key),'last_human_at',None) or e.clock.now()
            point=max(e.clock.now(),last+parse_duration(w.trigger_json.get('after',p.checkin_after_hours*3600)))
        elif w.kind not in TIME_KINDS: return w
        if point is None or w.expires_at is None: return w
        session=e.store.get_session(w.session_key)
        if p.wake_spacing_minutes>0 and session and session.last_wake_turn_at is not None:
            point=max(point,session.last_wake_turn_at+p.wake_spacing_minutes*60)
        if w.kind in {'at','owner_silent'} or watcher:
            from .commands import quiet_until
            point=max(point,e.clock.now())
            point=quiet_until(e,w.session_key,urgent=w.urgent,now=point) or point
        max_life=p.max_life_s(w)
        hard_end=(w.created_at or e.clock.now())+max_life
        due_bound=p.due_bound_s(w)
        latest=(w.created_at or e.clock.now())+due_bound
        reminder=p.lifetime_setting(w)=='max_reminder_days' and not w.babysit
        if point>latest or (point==latest and not reminder):
            raise Infeasible(f'{label} after the maximum lifetime of {due_bound/86400:g} days ({p.lifetime_setting(w)} setting)')
        if point<w.expires_at: return w
        if explicit_expiry or e.store.get_wake(w.id) is not None:
            local=lambda t: datetime.fromtimestamp(t,p.resolved_timezone()).strftime('%a %d %b %H:%M')
            raise Infeasible(f'{label} after expiry: {local(point)} >= {local(w.expires_at)}; set until.expires to extend it')
        # A long owner reminder keeps the grace a short one has, so downtime at its due time doesn't drop it.
        grace=p.max_lifetime_days*86400 if p.lifetime_setting(w)=='max_reminder_days' and not w.babysit else 3600
        end=min(hard_end,point+grace)
        return replace(w,expires_at=end)

    def receipt(self,w,s):
        """One truthful line from the stored wake and current chat limits."""
        e,p=self.engine,self.engine.policy
        local=lambda t: datetime.fromtimestamp(t,p.resolved_timezone()).strftime('%a %d %b %H:%M')
        def duration(seconds):
            for unit,size in (('d',86400),('h',3600),('m',60)):
                if seconds>=size and seconds%size==0: return f'{seconds/size:g}{unit}'
            return f'{seconds:g}s'
        watcher=w.kind in PROBE_KINDS | EVENT_KINDS
        if w.status=='proposed': parts=[f'Proposed, not armed: awaiting /pulse ok {w.id}.']
        elif w.status=='paused': parts=['Paused.']
        elif w.kind=='at': parts=['Already due; awaiting its wake turn.' if w.next_due_at is None else
            'Due now; it fires on the next check.' if w.next_due_at<=e.clock.now() else f'Fires once at {local(w.next_due_at)}.']
        elif w.kind in RECURRING_KINDS:
            parts=[f'Next at {local(w.next_due_at)}'+(' (cron).' if w.kind=='cron' else f', then every {duration(w.eff_interval_s)}.')]
        else:
            first=('Checks now' if w.next_due_at is None or w.next_due_at<=e.clock.now() else f'First check {local(w.next_due_at)}')
            lead=(f'{first}, then every {duration(w.trigger_json["check_every"])}' if w.kind in PROBE_KINDS else
                  f'Waits for {w.kind} events' if watcher else
                  f'Waits for {duration(parse_duration(w.trigger_json.get("after",p.checkin_after_hours*3600)))} of owner silence')
            parts=[lead+(f'; if nothing happens by {local(w.deadline_at)}, it tells you once' if watcher and w.deadline_at is not None else '')+'.']
        if w.status not in {'proposed','paused'}:
            from .policy import owner_requested
            if owner_requested(w): parts.append('Counts as your own reminder: not muted or capped.')
            if w.kind in TIME_KINDS or watcher or w.kind=='owner_silent' and not w.trigger_json.get('builtin'):
                from .commands import quiet_until
                due=((s.last_human_at or e.clock.now())+parse_duration(w.trigger_json.get('after',p.checkin_after_hours*3600)) if w.kind=='owner_silent' else
                     (w.next_due_at if w.next_due_at is not None else e.clock.now()) if w.kind in TIME_KINDS else
                     w.deadline_at if w.deadline_at is not None else w.next_due_at if w.kind in PROBE_KINDS and w.next_due_at is not None else e.clock.now())
                due=max(due,e.clock.now())
                skip=w.kind in RECURRING_KINDS  # Poller.gate drops a recurring tick in quiet hours before spacing
                release=quiet_until(e,w.session_key,urgent=w.urgent,now=due) if skip else None
                if release and release>due: parts.append(f'Quiet hours skip its ticks until {local(release)}.'); due=release
                if p.wake_spacing_minutes>0 and s.last_wake_turn_at is not None and not (release and skip and due==release):
                    spacing=s.last_wake_turn_at+p.wake_spacing_minutes*60
                    if spacing>due: parts.append(f'Wake spacing holds it until {local(spacing)}.')
                    due=max(due,spacing)
                release=quiet_until(e,w.session_key,urgent=w.urgent,now=due)
                if release and release>due: parts.append(f'Quiet hours {"skip its ticks" if skip else "hold it"} until {local(release)}.')
            if not owner_class(w) and s.unanswered_count>=p.max_unanswered:
                parts.append('Muted: it waits for your reply.' if watcher else 'Muted: it is dropped unless you reply first.')
            if p.wake_spacing_minutes>0 and (w.kind in TIME_KINDS or watcher or w.kind=='owner_silent'):
                parts.append(f'Wakes are at least {p.wake_spacing_minutes:g} min apart, so it can land a few minutes late.')
        if w.expires_at is not None: parts.append(f'Expires {local(w.expires_at)}.')
        if w.cancel_on_reply: parts.append('Cancels if you reply.')
        text=' '.join(parts)
        if len(text)>240: text=text.replace('Wakes are at least ','Wakes at least ').replace(' min apart, so it can land a few minutes late.',' min apart; may land a few minutes late.')
        if len(text)>240: text=' '.join(x for x in parts if not x.startswith('Wakes are at least '))
        if len(text)>240:
            text=text[:239]
            boundary=text.rfind('. ')
            text=(text[:boundary+1] if boundary>=0 else text)+'…'
        return text

    def reschedule(self,w):
        """Shared tool/command resume: preserve stored one-shot due, fresh key."""
        if w.kind=='at': w=replace(w,next_due_at=max(w.next_due_at or self.engine.clock.now(),self.engine.clock.now()))
        result=self.prepare(w,{},reschedule=w.kind!='at',reparse_deadline=False)
        if w.kind=='at':
            due=max(w.next_due_at or self.engine.clock.now(),self.engine.clock.now())
            while self.engine.store.get_occurrence(w.id,f't:{due:.17g}') is not None:
                due+=.001
            result=replace(result,wake=replace(result.wake,next_due_at=due))
        return replace(result,wake=self.feasible(result.wake))

    def prepare(self,w,args,*,reschedule=True,reparse_deadline=True):
        e=self.engine
        updates={k:args[k] for k in ('title','note','reason','babysit','urgent') if k in args}
        trigger=dict(args.get('trigger',w.trigger_json))
        if 'trigger' in args and 'event' in trigger:
            raise ValueError('trigger.event is not supported; choose the event with trigger.kind (for example kanban_completed)')
        if 'trigger' in args and set(trigger) - set(PARAMETERS['properties']['trigger']['properties']):
            raise ValueError('unknown trigger parameter')
        if w.trigger_json.get('builtin'):
            trigger['builtin'] = True
        kind=trigger.get('kind',w.kind)
        if 'deadline' in args.get('trigger',{}) and kind not in PROBE_KINDS | EVENT_KINDS:
            raise ValueError('deadline works only for watchers (file, process, url, command and event kinds)')
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
        if 'deadline' in trigger and reparse_deadline:
            updates['deadline_at']=parse_at(trigger['deadline'],now=e.clock.now(),timezone=e.policy.resolved_timezone())
        if kind=='command':
            mode=trigger.get('fire_on','change')
            if mode not in ('change','success','failure','json'):
                raise ValueError(f"invalid fire_on '{mode}': use failure (alert when the command exits nonzero), success, change, or json")
            argv=trigger.get('command')
            if not isinstance(argv,list) or not all(isinstance(x,str) for x in argv) or not e.policy.command_allowed(argv):
                raise ValueError('command probe is not allowlisted')
            raw=trigger.get('cwd')
            if raw is not None and not isinstance(raw,str): raise ValueError('command cwd must be a directory')
            home=e.ctx._manager.home_path
            try: cwd=Path(raw or home).expanduser()
            except RuntimeError: raise ValueError('command cwd must be a directory') from None  # ~unknown-user
            if not cwd.is_absolute(): cwd=Path(home)/cwd
            if not cwd.is_dir(): raise ValueError('command cwd must be a directory')
        if kind=='url':
            from .probes import classify_url
            classify_url(trigger.get('url'), e.policy.url_allow_private)
        if kind=='file':
            from .probes import file_path, check_file_read
            from .registration import home_scope
            home=e.ctx._manager.home_path
            with home_scope(home):
                path=file_path(trigger.get('path'), home)
                check_file_read(path)
                if path.exists() and not path.is_file(): raise ValueError('file probe requires a regular file')
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
        candidate=replace(w,**updates)
        result=e.policy.clamp(candidate,cache_ttl=e.timing(candidate)['cache_ttl'])
        w=result.wake
        if reschedule:
            now,tz=e.clock.now(),e.policy.resolved_timezone()
            if kind=='at':
                if 'at' not in trigger: raise ValueError('at requires a time')
                due=parse_at(trigger['at'],now=now,timezone=tz)
            elif kind in {'every','self'}:
                due=next_every(now,w.eff_interval_s,anchor=trigger.get('at') if kind=='every' else None,timezone=tz,**e.timing(w))
            elif kind=='cron':
                due=next_cron(trigger['cron'],now=now,timezone=tz,minimum_interval_s=w.eff_interval_s or 0)
            elif kind in PROBE_KINDS: due=now
            else: due=None
            w=replace(w,next_due_at=due)
        feasible=self.feasible(w,explicit_expiry='expires' in until)
        if feasible.expires_at!=w.expires_at:
            result.clamped['expires']=f'{feasible.expires_at} (covers the first due time)'
        return replace(result,wake=feasible)

    def respond(self,args,flight):
        e=self.engine
        if flight is None or flight.invalid:
            return dict(NOT_IN_FLIGHT)
        o=e.store.get_occurrence(flight.wake_id,flight.dedupe_key)
        if o is None or o.state!='started' or o.attempt!=flight.attempt:
            return dict(NOT_IN_FLIGHT)
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
                from .cache import interval
                bounded=interval(w,bounded,e.timing(w)['cache_ttl'],e.timing(w)['minimum_s'])
                changes['next_check_s']=bounded
                if requested!=bounded: clamps['next_check']=f'{requested} → {bounded} (pacing)'
        if not e.store.update_occurrence(o.wake_id,o.dedupe_key,'started',attempt=o.attempt,**changes):
            return dict(NOT_IN_FLIGHT)
        if scratch is not None: flight.scratch=scratch
        return {'ok':True,'clamped':clamps}
