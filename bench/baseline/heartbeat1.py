"""Naive heartbeat-1.0 emulator built from documented failure reports; not current
OpenClaw (which retired HEARTBEAT.md in 2026.8.1). These are never OpenClaw measurements.

This intentionally simple counterfactual represents the specified failure-prone
configuration, not a reproduction of upstream implementation or all versions:
* fixed 30-minute interval/checklist: https://github.com/openclaw/openclaw/issues/106339
  and https://github.com/openclaw/openclaw/issues/81186
* volatile timer lost at restart/long sleep: https://github.com/openclaw/openclaw/issues/78187 and https://github.com/openclaw/openclaw/issues/62294
* no quiet/cap/busy gates: https://github.com/openclaw/openclaw/issues/39536 and https://github.com/openclaw/openclaw/issues/64016
* case-insensitive text sentinel: https://github.com/openclaw/openclaw/issues/17646 and https://github.com/openclaw/openclaw/issues/143787
* last-active-chat destination: https://github.com/openclaw/openclaw/issues/24638 and https://github.com/openclaw/openclaw/issues/25871
* no dedupe: https://github.com/openclaw/openclaw/issues/59703 and https://github.com/openclaw/openclaw/issues/159691
* failed-send retries without a cap: https://github.com/openclaw/openclaw/issues/85743
"""
from bench.sim.model import ScriptedModel

LABEL='naive heartbeat-1.0 emulator built from documented failure reports; not current OpenClaw (which retired HEARTBEAT.md in 2026.8.1)'


class Heartbeat1:
    def __init__(self,gateway,scenario):
        self.gateway,self.scenario=gateway,scenario
        self.interval=scenario.get('baseline_interval_s',1800)
        if self.interval<=0:raise ValueError('baseline interval must be positive')
        self.next_due=self.interval
        self.checklist={item['ref']:dict(item,status='active',fires=0) for item in scenario['heartbeat_checklist']}
        self.model=ScriptedModel(scenario['model_policy'])
        self.last_active=list(gateway.sessions)[-1]
        self.retries=[];self.delayed=[]
        self.deliveries=[];self.turns=[];self.cards=[];self.pause_fires={}

    def event(self,event):
        kind=event['event']
        if kind in {'restart','suspend'}:
            self.next_due=None
            self.retries=[];self.delayed=[]
        if kind=='human':
            self.last_active=event.get('session','owner')
            if self.next_due is None:self.next_due=self.gateway.t+self.interval
        if kind=='arm':
            ref=event['arm']['ref']
            self.checklist[ref]={'ref':ref,'session':event['arm'].get('session','owner'),'note':event['arm'].get('args',{}).get('note','Check task'),'status':'active','fires':0}

    def fire(self,ref,occurrence,due,retry=False):
        task=self.checklist[ref];owner=self.gateway.sessions[task.get('session','owner')]
        destination=self.gateway.sessions[self.last_active]
        step=self.model.verdict(ref)
        task['fires']+=1
        self.turns.append({'t':self.gateway.t,'inject_t':self.gateway.t,'due_t':due,'session_key':owner['key'],
                           'wake_id':'b_'+ref,'ref':ref,'occurrence':occurrence,'valid':True,'outcome':step['outcome']})
        raw=self.model.raw(step)
        # Loose case/whitespace normalization of text rather than structured notify.
        text='' if raw.strip().casefold()=='heartbeat_ok' else raw
        injector=self.gateway.injectors[owner['profile']]
        item={'t':self.gateway.t,'session_key':destination['key'],'owner_session_key':owner['key'],
              'text':text,'wake_id':'b_'+ref,'ref':ref,'occurrence':occurrence,
              'profile':destination['profile'],'owner_profile':owner['profile'],'prompted':False,'unprompted':True,'terminal_before':task['status']=='done'}
        if injector.mode in {'return_false','drop','never_start'}:
            self.retries.append((ref,occurrence,due))
        elif injector.mode=='delay':self.delayed.append((self.gateway.t+injector.delay_s,item))
        elif text:self.deliveries.append(item)
        if step['outcome']=='done':task['status']='done'

    def tick(self):
        retry=self.retries;self.retries=[]
        for ref,occ,due in retry:self.fire(ref,occ,due,True)
        if self.next_due is not None and self.gateway.t>=self.next_due:
            due=self.next_due;self.next_due=self.gateway.t+self.interval
            for ref,task in self.checklist.items():
                if task['status']=='active':self.fire(ref,f'b:{due}',due)
        pending=[]
        for ready,item in self.delayed:
            if ready<=self.gateway.t and item['text']:
                self.deliveries.append({**item,'t':self.gateway.t})
            else:pending.append((ready,item))
        self.delayed=pending

    def next_interest(self):
        if self.retries:return self.gateway.t+15
        return min([x[0] for x in self.delayed]+([self.next_due] if self.next_due is not None else [])+[float('inf')])

    def snapshot(self):
        return {'baseline':{'wakes':list(self.checklist.values()),'occurrences':[], 'sessions':[]}}
