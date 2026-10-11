"""Deterministic policies, no LLM/network: structured response via the real tool."""
class ScriptedModel:
    def __init__(self, policy):
        self.policy, self.counts = policy, {}

    def verdict(self, ref, valid=True):
        count = self.counts.get(ref, 0)
        steps = self.policy.get(ref, self.policy.get('default', [{'outcome':'no_change','notify':False,'summary':'Nothing changed.'}]))
        if not steps: raise ValueError('model policy must be nonempty')
        step = dict(steps[min(count, len(steps)-1)])
        if valid: self.counts[ref] = count+1
        step.setdefault('outcome','no_change')
        step.setdefault('notify',False)
        step.setdefault('summary','Nothing changed.')
        return step

    def respond(self, engine, sid, step):
        args = {k:v for k,v in step.items() if k in {'outcome','notify','summary','next_check','scratch'}}
        return engine.tool.handle({'action':'respond',**args},session_id=sid)

    @staticmethod
    def raw(step):
        return step.get('raw', step['summary'] if step['notify'] else 'HEARTBEAT_OK')
