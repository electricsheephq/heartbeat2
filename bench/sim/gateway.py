"""Messaging DM harness around production Poller, intake hooks and pulse handlers.

Each profile has its own immutable home/SQLite file. Restarts discard Poller and
Store instances but keep that file. Queued injections survive an engine restart:
a stale accepted delivery must therefore pass production attempt fencing.
"""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from heartbeat2 import commands
from heartbeat2.intake import marker
from heartbeat2.model import Session, TERMINAL_OCCURRENCE_STATES
from heartbeat2.policy import Policy
from heartbeat2.poller import Poller
from heartbeat2.store import Store
from .clock import FakeClock
from .injector import FakeInjector
from .model import ScriptedModel


class Context:
    def __init__(self, gateway, profile, home):
        self._manager=SimpleNamespace(home_path=home,scope_key=str(home),_cli_ref=None,has_gateway_message_injector=True)
        self.gateway, self.profile = gateway, profile
    def _gateway_injection_allowed(self): return True
    def inject_message(self, text, *, session_key):
        return self.gateway.injectors[self.profile].inject(text,session_key)


class FakeGateway:
    def __init__(self, scenario, home):
        self.scenario,self.home=scenario,Path(home)
        self.clock=FakeClock(scenario.get('epoch',1791806400.0))
        self.origin=self.clock.now()
        self.model=ScriptedModel(scenario['model_policy'])
        self.current_key=''
        self.engines={}; self.injectors={}; self.contexts={}; self.standbys={}
        self.sessions={}; self.refs={}; self.tips={}; self.stopped=False
        self.deliveries=[]; self.turns=[]; self.humans=[]; self.cards=[]; self.command_log=[]; self.trace=[]
        self.pause_fires={}; self.counter=0
        self.occ_seen={}; self.holds={}; self.lock_violations=[]; self.claim_violations=[]; self.stop_windows=[]
        profiles={x.get('profile','main') for x in scenario.get('sessions',[{'name':'owner'}])}
        for profile in sorted(profiles):
            self.injectors[profile]=FakeInjector(self,profile)
            self.contexts[profile]=Context(self,profile,self.home/profile)
            self.build(profile)
        for data in scenario.get('sessions',[{'name':'owner'}]):
            name=data['name']; platform=data.get('platform','telegram'); ctype=data.get('chat_type','dm')
            self.sessions[name]={'name':name,'profile':data.get('profile','main'),'platform':platform,'chat_type':ctype,
                                 'chat_id':data.get('chat_id',name),'key':f'agent:{platform}:{ctype}:{data.get("chat_id",name)}','sid':f'sid-{name}'}
            session=self.sessions[name]
            self.engine_for(name).store.upsert_session(Session(session['key'],platform,ctype,
                str(session['chat_id']),session['sid']))
            self.human(name,'Begin the named task.')
        for arm in scenario.get('arms',[]): self.arm(arm)

    @property
    def t(self):return self.clock.now()-self.origin

    def build(self,profile):
        ctx=self.contexts[profile]
        store=Store(ctx._manager.home_path/'plugin-data/heartbeat2/pulse.db',self.clock)
        settings={'quiet_hours':'','timezone':'UTC',**self.scenario.get('settings',{})}
        settings.update(self.scenario.get('profile_settings',{}).get(profile,{}))
        engine=Poller(ctx,store,Policy.from_settings(settings,clock=self.clock),self.clock,
                      session_key_fn=lambda:self.current_key,paused=lambda:self.stopped,jitter=lambda:0,
                      compression_tip=lambda sid:self.tips.get(sid,sid))
        real_gate=engine.gate
        def observed_gate(w,o,s):
            identity=(profile,w.id,o.dedupe_key)
            self.occ_seen.setdefault(identity,self.t)
            reason,defer,until=real_gate(w,o,s)
            holds=self.holds.setdefault(identity,[])
            if defer and reason in {'quiet_hours','busy','wake_spacing','provider_unavailable','session_paused','global_flood'}:
                if not holds or holds[-1][1] is not None: holds.append([self.t,None])
            elif holds and holds[-1][1] is None:
                holds[-1][1]=self.t  # observed release; 15 s tick resolution
            return reason,defer,until
        engine.gate=observed_gate
        self.engines[profile]=engine

    @contextmanager
    def bound(self,session):
        old=self.current_key; self.current_key=session['key']
        env={'HERMES_SESSION_KEY':session['key'],'HERMES_SESSION_PLATFORM':session['platform'],
             'HERMES_SESSION_CHAT_TYPE':session['chat_type'],'HERMES_SESSION_CHAT_ID':session['chat_id'],
             'HERMES_SESSION_ID':session['sid']}
        with patch.object(commands,'session_env',lambda name:env.get(name,'')):
            try:yield
            finally:self.current_key=old

    def engine_for(self,name):return self.engines[self.sessions[name]['profile']]

    def human(self,name,text,*,drained=False,mid_turn=False,finish=True,response_text='Acknowledged.',reply=None):
        session=self.sessions[name]; e=self.engine_for(name)
        self.counter+=1; mid=f'human-{self.counter}'; turn=f'human-turn-{self.counter}'
        self.humans.append({'t':self.t,'session_key':session['key'],'profile':session['profile'],'text':text,'drained':drained})
        with self.bound(session):
            if not drained:
                source=SimpleNamespace(platform=SimpleNamespace(value=session['platform']),chat_type=session['chat_type'],chat_id=session['chat_id'],message_id=mid,is_bot=False)
                e.intake.pre_gateway_dispatch(event=SimpleNamespace(internal=False,source=source,message_id=mid,text=text))
            if not mid_turn:
                context=e.intake.pre_llm_call(session_id=session['sid'],turn_id=turn,platform=session['platform'],user_message=text,
                    conversation_history=[{'role':'user','content':text,'platform_message_id':mid}])
                if context:
                    for line in context.get('context','').splitlines():
                        if '/pulse watch' in line:self.cards.append({'t':self.t,'session_key':session['key'],'text':line})
                if finish:
                    transformed=e.intake.transform_llm_output(session_id=session['sid'],turn_id=turn,response_text=response_text)
                    answer=transformed if transformed is not None else response_text
                    if reply and answer!='[SILENT]':
                        self.deliveries.append({**reply,'t':self.t,'session_key':session['key'],
                            'profile':session['profile'],'owner_profile':session['profile'],
                            'text':answer,'turn_id':turn,'prompted':True,'unprompted':False,'terminal_before':False})
                    e.intake.on_session_end(session_id=session['sid'],turn_id=turn,completed=True)
        self.trace.append({'t':self.t,'event':'human','session':name,'drained':drained})
        return turn

    def command(self,name,text):
        session=self.sessions[name];e=self.engine_for(name)
        with self.bound(session):
            result=commands.handle(text,e_runtime(e))
        self.command_log.append({'t':self.t,'session_key':session['key'],'command':text,'result':result})
        return result

    def arm(self,action):
        name=action.get('session','owner');session=self.sessions[name]; e=self.engine_for(name)
        ref=action['ref']
        if ref in self.refs:raise ValueError('duplicate wake ref')
        if 'command' in action:
            before={w.id for w in e.store.list_wakes()}
            result=self.command(name,action['command'])
            after=[w for w in e.store.list_wakes() if w.id not in before]
            if len(after)!=1:raise ValueError('command arming failed: '+result)
            wid=after[0].id
        else:
            args={'action':'create',**action['args']}
            args=json.loads(json.dumps(args).replace('$HOME',str(e.ctx._manager.home_path)))
            with self.bound(session):result=e.tool.handle(args,session_id=session['sid'])
            if not result.get('ok'):raise ValueError('tool arming failed: '+str(result))
            wid=result['wake']['id']
        self.refs[ref]=(session['profile'],wid)
        self.trace.append({'t':self.t,'event':'arm','ref':ref,'wake_id':wid})

    def ref_for(self,profile,wid):
        for ref, pair in self.refs.items():
            if pair==(profile,wid):return ref
        if wid.startswith('checkin_'):return 'checkin'
        return wid

    def wake_turn(self,profile,item):
        e=self.engines[profile];wid,occ,attempt=marker(item['text'])
        w=e.store.get_wake(wid);o=e.store.get_occurrence(wid,occ)
        if w is None or o is None:raise ValueError('queued wake lost durable identity')
        session=next(s for s in self.sessions.values() if s['key']==item['session_key'] and s['profile']==profile)
        sid=session['sid'];self.counter+=1;turn=f'wake-turn-{self.counter}'
        terminal_before=o.state in TERMINAL_OCCURRENCE_STATES
        valid=not terminal_before and o.attempt==attempt and o.state in {'claimed','started'}
        ref=self.ref_for(profile,wid);step=self.model.verdict(ref,valid)
        with self.bound(session):
            e.intake.pre_llm_call(session_id=sid,turn_id=turn,platform=session['platform'],user_message=item['text'])
            if step.get('compact'):
                new=sid+'-compact';self.tips[sid]=new;session['sid']=new;sid=new
            result=self.model.respond(e,sid,step)
            raw=self.model.raw(step)
            transformed=e.intake.transform_llm_output(response_text=raw,session_id=sid,turn_id=turn,platform=session['platform'])
            text=transformed if transformed is not None else raw
            flight=e.intake.flights.get(sid)
            active_turn=e.intake.turns.get(sid)==turn
            prompted=bool(active_turn and (flight and flight.owner_replied or not flight and not turn.startswith('wake-turn-')))
            identity=(profile,wid,occ)
            due=float(occ[2:])-self.origin if occ.startswith('t:') else self.occ_seen.get(identity,item['t'])
            inj=item['t']
            spans=sorted((max(begin,due),min(inj if end is None else end,inj))
                         for begin,end in self.holds.get(identity,[])+self.stop_windows)
            held=0; last=due
            for begin,end in spans:
                if end>max(begin,last): held+=end-max(begin,last)
                last=max(last,end)
            opened=due+held
            self.turns.append({'t':self.t,'inject_t':item['t'],'session_key':session['key'],'profile':profile,'wake_id':wid,
                              'ref':ref,'occurrence':occ,'attempt':attempt,'valid':valid,'outcome':step['outcome'] if result.get('ok') else 'unreported',
                              'respond':result,'due_t':due,'gates_opened_t':opened,
                              'lateness_s':max(0,inj-due-held),'deferred_s':held})
            if text!='[SILENT]':
                self.deliveries.append({'t':self.t,'session_key':session['key'],'text':text,'wake_id':wid,'ref':ref,
                    'occurrence':occ,'profile':profile,'owner_profile':profile,'prompted':prompted,'unprompted':not prompted,'owner_session_key':w.session_key,'terminal_before':terminal_before})
            e.intake.post_api_request(session_id=sid,turn_id=turn,usage={'input_tokens':self.scenario.get('context_tokens',150000),'output_tokens':0})
            e.intake.on_session_end(session_id=sid,turn_id=turn,completed=True,platform=session['platform'])
            if step.get('drained_human'):
                # Hermes drains only after run_conversation finalized the wake.
                self.human(session['name'],step['drained_human'],drained=True,
                    response_text=step.get('human_raw','Acknowledged.'),
                    reply={'wake_id':wid,'ref':ref,'occurrence':occ,'owner_session_key':w.session_key})
        w=e.store.get_wake(wid)
        if w.status_reason.startswith('governor:') and ref not in self.pause_fires:self.pause_fires[ref]=w.fires

    def build_standby(self,profile):
        e=self.engines[profile]
        standby=Poller(self.contexts[profile],Store(e.store.path,self.clock),e.policy,self.clock,
                       paused=lambda:self.stopped,session_key_fn=lambda:self.current_key,jitter=lambda:0)
        # A standby does not reconcile another live owner's work.
        standby.process_start=False
        self.standbys[profile]=standby

    def tick(self):
        for profile,e in self.engines.items():
            if profile not in self.standbys:
                e.tick(); continue
            e.acquire()
            other=self.standbys[profile]
            if other.acquire():
                self.lock_violations.append(self.t)
                self.trace.append({'t':self.t,'event':'dual_lock'})
                other.release()
            # Real generation before the pending-list read. Then freeze that
            # list, run the owner's normal tick, and claim using the stale row.
            if not self.stopped:
                with e.store.atomic(): e.times()
            pending=other.store.list_occurrences(state='pending')
            e.tick()
            for stale in pending:
                current=other.store.get_occurrence(stale.wake_id,stale.dedupe_key)
                if current.state=='pending': continue  # owner was gated
                claimed=other.store.claim(stale.wake_id,stale.dedupe_key,epoch=other.epoch,pid=0)
                self.trace.append({'t':self.t,'event':'stale_claim','claimed':claimed})
                if claimed: self.claim_violations.append(self.t)
        for injector in self.injectors.values():injector.flush()

    def next_interest(self):
        times=[]
        for e in self.engines.values():
            for w in e.store.list_wakes(status='active'):
                times.extend(x-self.origin for x in (w.next_due_at,w.deadline_at,w.expires_at) if x is not None and x>self.clock.now())
                if w.kind=='owner_silent':
                    s=e.store.get_session(w.session_key)
                    if s.last_human_at is not None:
                        from heartbeat2.schedule import parse_duration
                        after=e.policy.checkin_after_hours*3600 if w.trigger_json.get('builtin') else parse_duration(w.trigger_json['after'])
                        times.append(s.last_human_at+after-self.origin)
            # With enabled built-in checkins, one initial tick creates the durable wake.
            if e.store.list_occurrences(state='pending') or e.store.list_occurrences(state='claimed'):return self.t+15
        for inj in self.injectors.values():times.extend(x['ready'] for x in inj.queue)
        return min((x for x in times if x>self.t),default=float('inf'))

    def event(self,event):
        kind=event['event']; name=event.get('session','owner'); profile=event.get('profile',self.sessions[name]['profile'])
        e=self.engines[profile]
        if kind=='human':self.human(name,event.get('text','Continue.'),drained=event.get('drained',False))
        elif kind=='injector':self.injectors[profile].configure(event['mode'],event.get('delay_s',0))
        elif kind=='restart':
            had_standby=profile in self.standbys
            self.close_profile(profile);self.build(profile)
            if had_standby:
                self.build_standby(profile)
                self.trace.append({'t':self.t,'event':'standby_rebuilt'})
            self.trace.append({'t':self.t,'event':'restart','profile':profile})
        elif kind=='second_poller':
            self.build_standby(profile)
        elif kind=='busy':
            e.acquire()  # This is a live admitted busy window, after crash recovery.
            s=e.store.get_session(self.sessions[name]['key']);e.store.cas_session(s.session_key,{'busy_since':s.busy_since},busy_since=self.clock.now() if event['on'] else None,busy_epoch=e.epoch if event['on'] else None)
        elif kind=='compact':
            session=self.sessions[name];old=session['sid'];session['sid']=event.get('tip',old+'-compact');self.tips[old]=session['sid']
        elif kind=='new':
            session=self.sessions[name];old=session['sid'];session['sid']=old+'-new'
            e.intake.on_session_reset(old_session_id=old,new_session_id=session['sid'])
        elif kind=='estop':
            if event['on'] and not self.stopped: self.stop_windows.append([self.t,None])
            elif not event['on'] and self.stopped: self.stop_windows[-1][1]=self.t
            self.stopped=event['on']
        elif kind=='arm':self.arm(event['arm'])
        elif kind=='external_event':
            with self.bound(self.sessions[name]):e.intake.event(event.get('kind','subagent_done'),session_key=self.sessions[name]['key'],task_id=event.get('task_id','task'))
        elif kind=='file':
            path=e.ctx._manager.home_path/event.get('path','build.log');path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('a' if event.get('append',True) else 'w') as f:f.write(event['text'])
        elif kind=='command':
            text=event['text']
            for ref,(p,wid) in self.refs.items():text=text.replace('$'+ref,wid)
            self.command(name,text)
        elif kind=='suspend':pass # Runner advances wall/elapsed with no intervening ticks.
        else:raise ValueError('unknown event: '+kind)
        self.trace.append({'t':self.t,**event})

    def close_profile(self,profile):
        if profile in self.standbys:
            other=self.standbys.pop(profile);other.close();other.store.close()
        e=self.engines.pop(profile);e.close();e.store.close()

    def snapshot(self):
        return {profile:{'wakes':[asdict(w) for w in e.store.list_wakes()],
                         'occurrences':[asdict(o) for o in e.store.list_occurrences()],
                         'sessions':[asdict(s) for s in e.store.list_sessions()]}
                for profile,e in self.engines.items()}

    def close(self):
        for profile in list(self.engines):self.close_profile(profile)


def e_runtime(engine):return SimpleNamespace(get_engine=lambda:engine)
