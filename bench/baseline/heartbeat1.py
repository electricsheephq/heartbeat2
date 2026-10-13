"""Naive heartbeat-1.0 emulator built from documented failure reports; not current
OpenClaw (which retired HEARTBEAT.md in 2026.8.1). These are never OpenClaw measurements.

This intentionally simple counterfactual represents the specified failure-prone
configuration, not a reproduction of upstream implementation or all versions:
* fixed 30-minute interval/checklist: https://github.com/openclaw/openclaw/issues/106339
  and https://github.com/openclaw/openclaw/issues/81186
* volatile timer lost at restart: https://github.com/openclaw/openclaw/issues/78187
* no quiet/cap/busy gates: https://github.com/openclaw/openclaw/issues/39536 and https://github.com/openclaw/openclaw/issues/64016
* case-insensitive text sentinel: https://github.com/openclaw/openclaw/issues/17646 and https://github.com/openclaw/openclaw/issues/143787
* last-active-chat destination: https://github.com/openclaw/openclaw/issues/24638 and https://github.com/openclaw/openclaw/issues/25871
* timer re-armed on restart, racy double-fire and no dedupe: https://github.com/openclaw/openclaw/issues/59703 and https://github.com/openclaw/openclaw/issues/159691
* failed-send retries without a cap: https://github.com/openclaw/openclaw/issues/85743
"""
from pathlib import Path
import re
from heartbeat2.schedule import parse_at, parse_duration
from bench.sim.model import ScriptedModel

LABEL='naive heartbeat-1.0 emulator built from documented failure reports; not current OpenClaw (which retired HEARTBEAT.md in 2026.8.1)'


class Heartbeat1:
    def __init__(self,gateway,scenario):
        self.gateway,self.scenario=gateway,scenario
        self.interval=scenario.get('baseline_interval_s',1800)
        if self.interval<=0:raise ValueError('baseline interval must be positive')
        self.next_due=self.interval; self.pollers=1; self.compacted=False
        self.checklist={item['ref']:dict(item,status='active',fires=0) for item in scenario['heartbeat_checklist']}
        self.arms={item['ref']:item for item in scenario['arms']}
        self.model=ScriptedModel(scenario['model_policy'])
        self.last_active=list(gateway.sessions)[-1]
        self.retries=[];self.delayed=[];self.delivered=set();self.events=set()
        self.deliveries=[];self.turns=[];self.cards=[];self.pause_fires={};self.message_attempts=[]

    def event(self,event):
        kind=event['event']
        if kind=='second_poller': self.pollers=2
        if kind=='compact': self.compacted=True
        if kind=='suspend':
            # Keep overdue timer slots and replay on resume: sleep-then-burst
            # https://github.com/openclaw/openclaw/issues/62294
            pass
        if kind=='restart':
            # Two independently rearmed timers race the same checklist snapshot.
            # https://github.com/openclaw/openclaw/issues/59703
            self.next_due=self.gateway.t+self.interval if self.pollers>1 else None
            self.retries=[];self.delayed=[]
        if kind=='external_event': self.events.add(event.get('kind','subagent_done'))
        if kind=='human':
            self.last_active=event.get('session','owner')
            if self.next_due is None:self.next_due=self.gateway.t+self.interval
        if kind=='arm':
            item=event['arm'];ref=item['ref'];self.arms[ref]=item
            self.checklist[ref]={'ref':ref,'session':item.get('session','owner'),'note':item.get('args',{}).get('note','Check task'),'status':'active','fires':0,'armed_t':self.gateway.t}

    def ready(self,ref):
        """Shared task readiness: a completion script cannot fire before its task.

        This is a model's checklist observation, not an HB2 deterministic gate.
        The naive polling still costs a model turn when the task is not ready.
        """
        arm=self.arms.get(ref,{})
        args=arm.get('args',{}); trigger=args.get('trigger',{})
        kind=trigger.get('kind');now=self.gateway.t
        if kind=='at':
            due=parse_at(trigger['at'],now=self.gateway.origin+self.checklist[ref].get('armed_t',0),timezone='UTC')-self.gateway.origin
            return now>=due
        if kind in {'every','self'}: return now>=parse_duration(trigger.get('every',trigger.get('after',self.interval)))
        if kind=='file':
            owner=self.gateway.sessions[arm.get('session','owner')]
            home=self.gateway.contexts[owner['profile']]._manager.home_path
            path=Path(trigger['path'].replace('$HOME',str(home)))
            return path.exists() and bool(re.search(trigger.get('pattern','.'),path.read_text()))
        if kind in {'subagent_done','api_error'}: return kind in self.events
        return True

    def fire(self,ref,occurrence,due):
        task=self.checklist[ref];owner=self.gateway.sessions[task.get('session','owner')]
        destination=self.gateway.sessions[self.last_active]
        ready=self.ready(ref)
        step=self.model.verdict(ref) if ready else {'outcome':'no_change','notify':False,'summary':'Nothing changed.'}
        if self.compacted: step={'outcome':'unreported','notify':False,'summary':'Stopped after compaction.','raw':'HEARTBEAT_OK'}
        task['fires']+=1
        self.turns.append({'t':self.gateway.t,'inject_t':self.gateway.t,'due_t':due,'session_key':owner['key'],
                           'wake_id':'b_'+ref,'ref':ref,'occurrence':occurrence,'valid':True,'outcome':step['outcome'],
                           'lateness_s':max(0,self.gateway.t-due),'deferred_s':0,
                           'tokens':self.gateway.tokens.turn((owner['profile'],owner['key']),self.gateway.t)})
        raw=self.model.raw(step)
        text='' if raw.strip().casefold()=='heartbeat_ok' else raw
        identity=(ref,occurrence)
        item={'t':self.gateway.t,'session_key':destination['key'],'owner_session_key':owner['key'],
              'text':text,'wake_id':'b_'+ref,'ref':ref,'occurrence':occurrence,
              'profile':destination['profile'],'owner_profile':owner['profile'],'prompted':False,'wake_message':True,
              'terminal_before':identity in self.delivered}
        self.send(item,step['outcome'])
        if step.get('drained_human'):
            # Same real drain order as HB2: finish the wake before the owner turn.
            before=len(self.gateway.deliveries)
            self.gateway.human(task.get('session','owner'),step['drained_human'],drained=True,
                response_text=step.get('human_raw','Acknowledged.'),
                reply={'wake_id':'b_'+ref,'ref':ref,'occurrence':occurrence,'owner_session_key':owner['key']})
            answers=self.gateway.deliveries[before:]
            self.deliveries.extend(answers)
            self.message_attempts.extend({**answer,'retry':False,'accepted':True} for answer in answers)

    def send(self,item,outcome,*,retry=False):
        """Uncapped resends of the same model output, without another turn.

        https://github.com/openclaw/openclaw/issues/85743
        Only return_false is an observable failure; accepted/lost sends cannot
        trigger retries. Attempts and resends are messages, never model wakes.
        """
        ref=item['ref']; identity=(ref,item['occurrence'])
        task=self.checklist[ref]
        injector=self.gateway.injectors[item['owner_profile']]
        text=item['text']; sent=False
        item={**item,'t':self.gateway.t,'terminal_before':identity in self.delivered}
        if text:
            self.message_attempts.append({**item,'retry':retry,'accepted':injector.mode!='return_false'})
        if not text: sent=True
        elif injector.mode=='return_false': self.retries.append((item,outcome))
        elif injector.mode in {'drop','never_start'}: sent=True
        elif injector.mode=='delay': self.delayed.append((self.gateway.t+injector.delay_s,item,outcome))
        elif text:
            self.deliveries.append(item); self.delivered.add(identity); sent=True
        if sent and outcome=='done': task['status']='done'

    def tick(self):
        if self.gateway.t>=self.scenario.get('baseline_stop_after_s',float('inf')):
            # Volatile scheduler stops after extended uptime (#59710).
            # https://github.com/openclaw/openclaw/issues/59710
            self.next_due=None;return
        retry=self.retries;self.retries=[]
        for item,outcome in retry:self.send(item,outcome,retry=True)
        if self.next_due is not None and self.gateway.t>=self.next_due:
            # Freeze candidates before either timer runs: a racy double-fire,
            # rather than reading done after timer one and hiding #59703.
            due=self.next_due
            while due<=self.gateway.t:
                active=[ref for ref,task in self.checklist.items() if task['status']=='active']
                for _ in range(self.pollers):
                    for ref in active:self.fire(ref,f'b:{due}',due)
                due+=self.interval
            self.next_due=due
        pending=[]
        for ready,item,outcome in self.delayed:
            if ready<=self.gateway.t and item['text']:
                identity=(item['ref'],item['occurrence'])
                self.deliveries.append({**item,'t':self.gateway.t,'terminal_before':identity in self.delivered})
                self.delivered.add(identity)
                if outcome=='done':self.checklist[item['ref']]['status']='done'
            else:pending.append((ready,item,outcome))
        self.delayed=pending

    def next_interest(self):
        if self.retries:return self.gateway.t+15
        return min([x[0] for x in self.delayed]+([self.next_due] if self.next_due is not None else [])+[float('inf')])

    def snapshot(self):
        return {'baseline':{'wakes':list(self.checklist.values()),'occurrences':[], 'sessions':[]}}
