"""15-second ticks, with safe idle jumps through the identical tick path."""
import math
from .gateway import FakeGateway
from .metrics import compute


def simulate(scenario,home,baseline=False):
    data=scenario
    if baseline:data={**scenario,'arms':[]}
    gateway=FakeGateway(data,home)
    driver=gateway
    if baseline:
        from bench.baseline.heartbeat1 import Heartbeat1
        driver=Heartbeat1(gateway,scenario)
    timeline=sorted(enumerate(scenario['timeline']),key=lambda pair:(pair[1]['t'],pair[0]))
    events=[x for _,x in timeline]; index=0
    try:
        while True:
            while index<len(events) and events[index]['t']<=gateway.t:
                event=events[index];index+=1
                if event['event']=='suspend':
                    until=event['until']
                    if until<=gateway.t:raise ValueError('suspend must move forward')
                    gateway.clock.advance(until-gateway.t)
                gateway.event(event)
                if baseline:driver.event(event)
            driver.tick()
            if gateway.t>=scenario['duration']:break
            event_t=events[index]['t'] if index<len(events) else float('inf')
            interesting=driver.next_interest()
            target=min(event_t,interesting,scenario['duration'])
            # Keep a 15 s cadence while pending/claimed/retry work exists. An idle
            # jump cannot skip a scheduled due point or scripted event.
            if target<=gateway.t+15:target=min(gateway.t+15,event_t,scenario['duration'])
            else:target=min(math.ceil(target/15)*15,event_t,scenario['duration'])
            if target<=gateway.t:raise ValueError('simulator failed to advance')
            gateway.clock.advance(target-gateway.t)
        snapshot=driver.snapshot()
        if not baseline:
            for profile,p in snapshot.items():
                for w in p['wakes']:w['ref']=gateway.ref_for(profile,w['id'])
        messages=getattr(driver,'message_attempts',None)
        metrics,failures=compute(scenario,driver.deliveries,driver.turns,gateway.humans,snapshot,driver.pause_fires,driver.cards,messages)
        if not baseline:
            if gateway.lock_violations: failures.append('poller_lock_exclusion violated')
            if gateway.claim_violations: failures.append('stale_claim_cas violated')
        return {'metrics':metrics,'failures':failures,'deliveries':driver.deliveries,'turns':driver.turns,
                'message_attempts':messages if messages is not None else driver.deliveries,
                'database':snapshot,'cards':driver.cards,'humans':gateway.humans,
                'injections':{p:i.attempts for p,i in gateway.injectors.items()},'commands':gateway.command_log,'trace':gateway.trace}
    finally:gateway.close()
