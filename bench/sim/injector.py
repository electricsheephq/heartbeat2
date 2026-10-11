"""Scheduling acceptance is distinct from model start and delivery."""
import re
from heartbeat2.intake import marker

class FakeInjector:
    def __init__(self, gateway, profile):
        self.gateway, self.profile = gateway, profile
        self.mode = 'ok'
        self.delay_s = 0
        self.attempts = []
        self.queue = []
        self.unstarted = []

    def configure(self, mode, delay_s=0):
        match = re.fullmatch(r'delay\((\d+(?:\.\d+)?)\)', mode)
        self.mode = 'delay' if match else mode
        self.delay_s = float(match[1]) if match else delay_s
        if self.mode not in {'ok','drop','return_false','never_start','delay'}:raise ValueError(mode)

    def inject(self, text, session_key):
        parsed = marker(text)
        if not parsed: raise ValueError('engine injected a message without its marker')
        wid, occurrence, attempt = parsed
        now = self.gateway.t
        row = {'t':now,'session_key':session_key,'wake_id':wid,'occurrence':occurrence,'attempt':attempt,'mode':self.mode}
        self.attempts.append(row)
        if self.mode == 'return_false': return False
        if self.mode == 'drop': return True
        item = {**row,'text':text,'ready':now+self.delay_s if self.mode=='delay' else now}
        if self.mode == 'never_start': self.unstarted.append(item)
        else: self.queue.append(item)
        return True

    def flush(self):
        ready = [x for x in self.queue if x['ready'] <= self.gateway.t]
        self.queue = [x for x in self.queue if x['ready'] > self.gateway.t]
        for item in ready: self.gateway.wake_turn(self.profile, item)
