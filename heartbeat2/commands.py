"""Compact /pulse controls; gateway-bound session is the only authority."""
from datetime import datetime, timedelta
import json
import re

from .model import Wake, Session
from . import governor
from . import cache
from .policy import INJECTION_CONFIG, MESSAGING_PLATFORMS, owner_requested
from .schedule import parse_duration, quiet_end, local_day, _DURATION
from .tool import session_env, Infeasible

DURATIONS = _DURATION.pattern+r'(?:\s*(?:,\s*)?(?:and\s*)?'+_DURATION.pattern+r')*'  # one way to split whitespace: linear
HELP = ('/pulse status | list | ledger [7d] | why <id> | add in 2h <note> | '
        'add every 1d at 09:00 <note> | add when silent 24h <note> | '
        'pause|resume|cancel <id|all> [for 3d] | off|on | '
        'quiet <22:00-07:30|off|default|for 3h> | ok|keep|slow|stop|watch <id>')


def quiet_until(engine, session_key, *, urgent=False, now=None):
    if urgent and engine.policy.quiet_hours_urgent: return None
    now = engine.clock.now() if now is None else now
    override = json.loads(engine.store.get_meta('quiet:'+session_key) or '{}')
    if override.get('until', 0) > now:
        until=override['until']
        return max(until,engine.policy.quiet_until(urgent=urgent,now=until) or until)
    if 'range' in override:
        return quiet_end(now, override['range'], engine.policy.resolved_timezone())
    return engine.policy.quiet_until(urgent=urgent, now=now)


def _activation(engine, session, wake):
    if not session.enabled: raise ValueError('this chat is off; use /pulse on')
    gate = engine.policy.creation_gate(session_key=session.session_key, platform=session.platform,
        chat_type=session.chat_type, allow_gateway_injection=engine.injection_allowed(),
        creator='user', kind=wake.kind,
        active_count=sum(w.status in {'active','proposed'} and w.id != wake.id for w in engine.store.list_wakes(session.session_key)),
        global_active_count=sum(w.status in {'active','proposed'} and w.id != wake.id for w in engine.store.list_wakes()))
    if not gate.allowed: raise ValueError(gate.reason + ('\n'+gate.detail if gate.detail else ''))


def _reschedule(engine,wake):
    return engine.tool.reschedule(wake)


def resume_timed(engine):
    """Only command-paused items with an explicit timer may auto-resume."""
    for wake in engine.store.list_wakes(status='paused'):
        until = float(engine.store.get_meta('resume_after:'+wake.id) or 0)
        if not until or until > engine.clock.now() or wake.status_reason != 'command': continue
        session = engine.store.get_session(wake.session_key)
        if session is None: continue
        try:
            _activation(engine, session, wake)
            candidate = _reschedule(engine,wake).wake
        except Infeasible:
            if engine.store.cas_wake(wake.id,'paused',expected_version=wake.version,status='expired',status_reason='expired'):
                engine.cancel_occurrences(wake.id,'expired')
                engine.store.set_meta('resume_after:'+wake.id,'')
                engine.notice(wake.session_key,f'A paused wake ({wake.id}) could not resume before it expires, so it ended.')
            continue
        except (ValueError, TypeError, KeyError):
            continue
        if engine.store.cas_wake(wake.id, 'paused', expected_version=wake.version,
            status='active', status_reason='', **{name:getattr(candidate,name) for name in
                ('next_due_at','deadline_at','base_interval_s','eff_interval_s','pacing_min_s','pacing_max_s','expires_at','max_fires','trigger_json')}):
            engine.store.set_meta('resume_after:'+wake.id, '')


def _time(engine, epoch):
    return datetime.fromtimestamp(epoch, engine.policy.resolved_timezone()).strftime('%Y-%m-%d %H:%M %Z') if epoch is not None else 'none'


def _item(engine, wake):
    due='paused · resumes on keep/resume' if wake.status=='paused' else 'next '+_time(engine,wake.next_due_at or wake.deadline_at)
    return f'{wake.id} {wake.status} · {wake.title or wake.note[:60]} · {wake.kind} · {due}' + (' · you asked' if owner_requested(wake) else '') + (f' · {wake.status_reason}' if wake.status_reason else '')


def _occurrences(engine, wake):
    reasons={'held_unanswered':'held: waiting for your reply', 'held_daily_cap':'held: waiting for daily capacity', 'held_paused':'held: wake paused',
        'unanswered':'dropped: unanswered mute', 'daily_model_cap':f'dropped: daily model cap ({engine.policy.max_model_wakes_per_day})',
        'daily_message_cap':f'dropped: daily message cap ({engine.policy.max_messages_per_day})', 'expired_held':'expired while held'}
    return '\n'.join(f'{o.dedupe_key} · {o.state} · {reasons.get(o.error,o.error.replace("_"," ") or o.outcome or "none")} · '
        f'matched/created {_time(engine,o.matched_at) if o.matched_at is not None else _time(engine,o.created_at) if o.created_at else "before v0.1.2"} · held-since {_time(engine,o.held_since)} · delivered {"yes" if o.delivered else "no"}'
        for o in engine.store.list_occurrences(wake.id)[-10:])


def _ledger(engine, key, days):
    today = datetime.fromisoformat(local_day(engine.clock.now(), engine.policy.resolved_timezone())).date()
    lines = [f'Ledger {days}d: fires · checks · model wakes · messages · engaged · cache/cost · provider deferrals · no_verdict · verdict']
    for wake in engine.store.list_wakes(key):
        counts = {}
        for n in range(days):
            row = engine.store.get_ledger((today-timedelta(days=n)).isoformat(), wake.id) or {}
            for name, value in row.items():
                if isinstance(value, int): counts[name] = counts.get(name, 0)+value
        fires, skips = counts.get('fires',0), counts.get('gated_skips',0)
        valuable, turns = counts.get('valuable',0), counts.get('model_turns',0)
        verdict = 'useful' if valuable else 'no value yet' if turns else 'waiting'
        lines.append(f'{wake.id} {wake.title or wake.note[:40]}: {fires} · {counts.get("checks",0)} · {turns} · '
                     f'{counts.get("delivered",0)} · {counts.get("engaged",0)} · '
                     f'{cache.display(counts)} · {counts.get("provider_deferrals",0)} · {counts.get("no_verdict",0)} · {verdict}')
    return '\n'.join(lines)


def _add(engine, session, text):
    clock = re.fullmatch(r'(\d{1,2}):(\d{2})(?:\s*([ap])\.?m\b\.?)?\s+(.+)', text, re.I|re.DOTALL)
    match = re.fullmatch(r'in (?P<duration>'+DURATIONS+r')\s+(?P<note>.+)', text, re.I|re.DOTALL)
    if clock:
        hour = int(clock[1])
        if not clock[3] and re.match(r'[ap]\.?m\b', clock[4], re.I): raise ValueError('add a note after the time')
        if clock[3]:
            if not 1 <= hour <= 12: raise ValueError('use 1:30pm or 13:30, not both')
            hour = hour%12+(12 if clock[3].lower()=='p' else 0)
        trigger, note = {'kind':'at','at':f'{hour:02d}:{clock[2]}'}, clock[4]
    elif match:
        trigger, note = {'kind':'at','at':'in '+match['duration']}, match['note']
    else:
        match = re.fullmatch(r'every (?P<duration>'+DURATIONS+r'|\S+)\s+(?P<note>.+)', text, re.I|re.DOTALL)
        if match:
            trigger, note = {'kind':'every','every':match['duration']}, match['note']
            if re.match(r'at\s+\d',note):
                anchor=re.fullmatch(r'at\s+(\d{1,2})(?::(\d{2}))?(?:\s*(am|pm)\b)?\s+(.+)',note,re.I|re.DOTALL)
                if not anchor or anchor[2] is None and anchor[3] is None:
                    raise ValueError('use at H:MM, 9am or 9:00pm followed by a note')
                hour,minute=int(anchor[1]),int(anchor[2] or 0)
                if minute>59 or (not anchor[3] and hour>23) or (anchor[3] and not 1<=hour<=12):
                    raise ValueError('invalid clock anchor')
                if anchor[3]: hour=hour%12+(12 if anchor[3].lower()=='pm' else 0)
                trigger['at']=f'{hour:02d}:{minute:02d}'
                note=anchor[4]
        else:
            match = re.fullmatch(r'when silent (?P<duration>'+DURATIONS+r'|\S+)\s+(?P<note>.+)', text, re.I|re.DOTALL)
            if not match: raise ValueError('use add in 2h <note>, add 16:30 <note>, every 1d at 09:00 <note>, or when silent 24h <note>')
            parse_duration(match['duration'])
            trigger, note = {'kind':'owner_silent','after':match['duration']}, match['note']
    wake = Wake(session.session_key, note, trigger['kind'], creator='user', created_in='command',
        platform=session.platform, chat_type=session.chat_type, chat_id=session.chat_id,
        armed_session_id=session.current_session_id, title=note[:80])
    _activation(engine,session,wake)
    result = engine.tool.prepare(wake, {'trigger':trigger})
    created = engine.store.create_wake(result.wake)
    return _item(engine, created)+_clamps(result.clamped)+'\n'+engine.tool.cache_result(created)+'\n'+engine.tool.receipt(created,session)


def _clamps(clamped):
    return ''.join(f'\nClamped {key}: {value}' for key,value in clamped.items())


def handle(raw_args, runtime):
    key = session_env('HERMES_SESSION_KEY')
    if not key or key == 'default': return 'Error: /pulse needs the invoking gateway session.'
    try:
        engine = runtime.get_engine()
        engine.refresh()
        action, _, rest = (raw_args or 'status').strip().partition(' ')
        rest = rest.strip()
        with engine.store.atomic():
            session = engine.store.get_session(key)
            bound={name:session_env('HERMES_SESSION_'+name.upper()) for name in ('platform','chat_type','chat_id')}
            if bound['platform'] in MESSAGING_PLATFORMS:
                engine.store.upsert_session(Session(key,**bound,current_session_id=session_env('HERMES_SESSION_ID')))
                session=engine.store.get_session(key)
                engine.store.cas_session(key,{'platform':session.platform,'chat_type':session.chat_type,'chat_id':session.chat_id},**bound)
                session=engine.store.get_session(key)
            if session is None or session.platform not in MESSAGING_PLATFORMS:
                return 'Error: /pulse needs a known messaging-gateway session.'
            if engine.policy.dm_only and session.chat_type != 'dm': return 'Error: /pulse requires a DM.'
            if action == 'status' and not rest:
                status = engine.status()
                q = quiet_until(engine,key)
                override=json.loads(engine.store.get_meta('quiet:'+key) or '{}')
                quiet_label='until '+_time(engine,q) if q else 'off now'
                if 'range' in override:
                    quiet_label=(override['range']+' (chat override)' if override['range'] else 'off for this chat (override)')
                elif override.get('until',0)>engine.clock.now(): quiet_label+=' (chat override)'
                daily = engine.daily(key)
                counts={}
                for wake in engine.store.list_wakes(key):
                    row=engine.store.get_ledger(local_day(engine.clock.now(),engine.policy.resolved_timezone()),wake.id) or {}
                    for name,value in row.items():
                        if isinstance(value,int): counts[name]=counts.get(name,0)+value
                active = [w for w in engine.store.list_wakes(key) if w.status in {'active','proposed','paused'}]
                next_due = min((x for w in active if w.status=='active' for x in (w.next_due_at,w.deadline_at) if x is not None),default=None)
                lines = [f'HeartBeat 2.0: {"on" if session.enabled and engine.policy.enabled else "off"} · poller {status["state"]}'
                         + (f' ({status["reason"]})' if status.get('reason') else ''),
                         f'allow_gateway_injection: {str(engine.injection_allowed()).lower()}',
                         f'Local time: {_time(engine,engine.clock.now())} · quiet: {quiet_label}',
                         f'Today: fires {counts.get("fires",0)} · checks {counts.get("checks",0)} · model wakes {daily["model_turns"]}/{engine.policy.max_model_wakes_per_day} · messages {daily["delivered"]}/{engine.policy.max_messages_per_day} · engaged {counts.get("engaged",0)} · {cache.display(counts)}',
                         f'Active items: {sum(w.status=="active" for w in active)} · next due: {_time(engine,next_due)}']
                held=sum(o.held_since is not None for o in engine.store.list_occurrences(session_key=key,state='pending'))
                if held: lines.append(f'Held items: {held}')
                if engine.settings_notice(): lines.append(engine.settings_notice())
                if cache.ttl_for_session(engine.store,session)==(300,'hermes'):
                    lines.append('your Claude cache lasts 5 min, so every wake is cold; set `prompt_caching: {cache_ttl: auto}` (1h for chat) in config.yaml')
                health=json.loads(engine.store.get_meta('provider_health') or '{}')
                if health:
                    lines.append(f'Provider gate: provider_unavailable ({health["class"]}) · next recovery attempt: {_time(engine,health["until"])}; any successful model call clears it.')
                if status.get('state')=='stalled': lines.append('Stalled since '+_time(engine,status.get('since')))
                if not engine.injection_allowed(): lines.append(INJECTION_CONFIG)
                lines.extend(_item(engine,w) for w in active)
                lines.extend(f'{w.id}: keep / slow / stop / watch' for w in active if w.status_reason.startswith('governor:'))
                return '\n'.join(lines)
            if action == 'list' and not rest:
                return '\n'.join(_item(engine,w) for w in engine.store.list_wakes(key)) or 'No wakes or watchers in this chat.'
            if action == 'ledger':
                if not re.fullmatch(r'\d+d',rest or '7d'): raise ValueError('ledger expects 7d')
                days = int((rest or '7d')[:-1])
                if not 1 <= days <= engine.policy.ledger_days: raise ValueError('ledger range exceeds retained days')
                return _ledger(engine,key,days)
            if action == 'add': return _add(engine,session,rest)
            if action in {'off','on'} and not rest:
                if not engine.store.cas_session(key, {'enabled':session.enabled}, enabled=action=='on'):
                    raise ValueError('concurrent session change')
                engine.signal()
                return f'HeartBeat 2.0 {action} for this chat.'
            if action == 'quiet':
                if rest=='default':
                    engine.store.connection.execute('DELETE FROM meta WHERE key=?',('quiet:'+key,))
                    override=None
                elif rest.startswith('for '):
                    override = {'until':engine.clock.now()+parse_duration(rest[4:])}
                elif rest == 'off': override = {'range':''}
                else:
                    quiet_end(engine.clock.now(),rest,engine.policy.resolved_timezone())
                    if not rest: raise ValueError('quiet expects a range, off, default, or for 3h')
                    override = {'range':rest}
                if override is not None: engine.store.set_meta('quiet:'+key,json.dumps(override))
                for occurrence in engine.store.list_occurrences(session_key=key,state='pending'):
                    if occurrence.error=='quiet_hours':
                        engine.store.set_meta('not_before:'+occurrence.id,'')
                engine.signal()
                if rest=='default': return 'Quiet hours follow your settings again: '+(engine.policy.quiet_hours or 'none')
                return f'Quiet for this chat: {rest}.'
            if action not in {'why','pause','resume','cancel','ok','keep','slow','stop','watch'}:
                raise ValueError(HELP)
            match = re.fullmatch(r'(\S+)(?: for (\S+))?',rest)
            if not match: raise ValueError('expected an item id or all')
            wid, duration = match.groups()
            if duration and action != 'pause': raise ValueError('for is supported only with pause')
            seconds = parse_duration(duration) if duration else None
            if wid == 'all' and action in {'pause','resume','cancel'}:
                wakes = engine.store.list_wakes(key)
            else:
                wake = engine.store.get_wake(wid)
                if wake is None or wake.session_key != key: return 'Error: unknown item in this chat.'
                wakes = [wake]
            lines = []
            for wake in wakes:
                if action == 'why':
                    reason=wake.reason or ('you asked (via /pulse add)' if wake.creator=='user' else 'you asked in chat' if owner_requested(wake) else 'the named task')
                    return f'{_item(engine,wake)}\nWhy: {reason}\nNote: {wake.note}\nStatus reason: {wake.status_reason or "none"}\nFires {wake.fires}, no-ops {wake.noop_streak}, ignored {wake.ignored_streak}, errors {wake.error_streak}\n'+_occurrences(engine,wake)
                try:
                    with engine.store.atomic():
                        lines.append(_transition(engine,session,wake,action,seconds))
                except (ValueError,TypeError,KeyError) as exc:
                    if wid!='all': raise
                    lines.append(f'{wake.id}: skipped ({exc})')
            engine.signal()
            return '\n'.join(lines) or 'No matching items in this chat.'
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return 'Error: '+str(exc)
    except Exception:
        return 'Error: HeartBeat 2.0 is unavailable.'


def _transition(engine,session,wake,action,seconds):
    if wake.status in {'done','cancelled','expired','failed','orphaned'}:
        raise ValueError('item is terminal')
    if action in {'keep','slow'}:
        if wake.status!='paused' or not wake.status_reason.startswith('governor:'):
            raise ValueError('keep/slow answer a governor card; use /pulse resume')
        _activation(engine,session,wake)
        result=governor.owner_answer(engine,wake,action)
        if not result.get('ok'): raise ValueError(result.get('error','owner answer failed'))
        return _item(engine,engine.store.get_wake(wake.id)) + (f'\nNew cadence: {result["cadence_s"]:g} seconds.\n{result.get("cache","")}' if action=='slow' else '')
    if wake.status=='proposed' and action not in {'ok','cancel','stop'}:
        raise ValueError('proposed item: approve with /pulse ok or cancel')
    if action=='pause' and wake.status!='active': raise ValueError('pause needs an active item')
    if action=='watch' and wake.status not in {'active','paused'}: raise ValueError('watch needs an active or paused item')
    if action=='resume' and wake.status!='paused': raise ValueError('resume needs a paused item')
    if action=='ok' and wake.status!='proposed': raise ValueError('ok needs a proposed item')
    clamps={}
    if action in {'resume','ok'}:
        _activation(engine,session,wake)
        result=_reschedule(engine,wake)
        candidate,clamps=result.wake,result.clamped
        changes={name:getattr(candidate,name) for name in ('next_due_at','base_interval_s','eff_interval_s','pacing_min_s','pacing_max_s','expires_at','max_fires','trigger_json')}
        changes.update(status='active',status_reason='')
    elif action in {'cancel','stop'}: changes={'status':'cancelled','status_reason':'command'}
    else: changes={'status':'paused','status_reason':'watch_requested' if action=='watch' else 'command'}
    if not engine.store.cas_wake(wake.id,wake.status,expected_version=wake.version,**changes):
        raise ValueError('concurrent item change')
    engine.store.set_meta('resume_after:'+wake.id,str(engine.clock.now()+seconds) if action=='pause' and seconds else '')
    if changes['status'] in {'paused','cancelled'}: engine.cancel_occurrences(wake.id,changes['status'])
    engine.store.set_meta('card:'+wake.id,'')
    output=_item(engine,engine.store.get_wake(wake.id))+_clamps(clamps)
    if action in {'ok','resume'}: output+='\n'+engine.tool.receipt(engine.store.get_wake(wake.id),session)
    if action=='watch': output+='\nPaused. Ask your agent to turn it into a watcher (pulse update with a file, process, url or command trigger).'
    return output
