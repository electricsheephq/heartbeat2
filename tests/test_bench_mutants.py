"""U5 reviewers' seam mutations, applied only to isolated temporary copies.

A broken gate driver/import is an error, never a caught mutant. The driver uses
exactly the scenario evaluator behind bench.run --gate, without requiring git.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

ROOT=Path(__file__).resolve().parents[1]
CAS=('heartbeat2/store.py',"WHERE wake_id=? AND dedupe_key=? AND state='pending'\"\"\",\n                (epoch","WHERE wake_id=? AND dedupe_key=?\"\"\",\n                (epoch")
FLOCK=('heartbeat2/poller.py','            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)','            pass')
MUTANTS={
    'governor_pause_off':[('heartbeat2/governor.py','    if rule: pause(engine,w,rule)','    if False: pause(engine,w,rule)')],
    'caps_off':[('heartbeat2/poller.py','        if cap and not (owner_class(w) and one_shot(w)):','        if False:')],
    'mute_class_regress':[('heartbeat2/policy.py','unanswered_count >= self.max_unanswered and not owner_class(wake)', 'unanswered_count >= self.max_unanswered and not is_owner(wake)')],
    'dm_only_off':[('heartbeat2/policy.py','        if self.dm_only and chat_type != "dm":','        if False:')],
    'replay_slots':[('heartbeat2/poller.py',"                        s.add_occurrence(w.id,due.dedupe_key,state=due.state)\n", "                        s.add_occurrence(w.id,due.dedupe_key,state=due.state)\n                    if w.kind in RECURRING_KINDS and due.state=='pending':\n                        k=due.scheduled_for+w.eff_interval_s\n                        while k<=now:\n                            s.add_occurrence(w.id,f't:{k:.17g}'); k+=w.eff_interval_s\n")],
    'claim_cas_off':[CAS], 'flock_off':[FLOCK], 'flock_and_claim_off':[FLOCK,CAS],
    'fencing_off':[('heartbeat2/intake.py',"if f.invalid or not o or o.state!='started' or o.attempt!=f.attempt: return '[SILENT]'", "if f.invalid or not o or o.state!='started' or o.attempt!=f.attempt: return None")],
    'estop_off':[('heartbeat2/poller.py','if not self.policy.enabled or self.paused(): return','if not self.policy.enabled: return'),('heartbeat2/poller.py','if self.paused() or not self.policy.enabled: return','if not self.policy.enabled: return'),('heartbeat2/intake.py','not session.enabled or e.paused() or','not session.enabled or')],
    'transform_raw':[('heartbeat2/intake.py',"        if not f or turn_id and f.turn_id and f.turn_id!=turn_id: return None\n        with s.atomic():\n            o=s.get_occurrence(f.wake_id,f.dedupe_key)\n            if f.owner_replied", "        return None\n        with s.atomic():\n            o=s.get_occurrence(f.wake_id,f.dedupe_key)\n            if f.owner_replied")],
    'cancel_on_reply_off':[('heartbeat2/intake.py',"if w.cancel_on_reply and w.status in {'active','paused','proposed'}:", 'if False:')],
    'silenced_human':[('heartbeat2/intake.py',"        if not f or turn_id and f.turn_id and f.turn_id!=turn_id: return None\n        with s.atomic():\n            o=s.get_occurrence(f.wake_id,f.dedupe_key)\n            if f.owner_replied", "        if not f or turn_id and f.turn_id and f.turn_id!=turn_id: return '[SILENT]'\n        with s.atomic():\n            o=s.get_occurrence(f.wake_id,f.dedupe_key)\n            if f.owner_replied")],
}
SCENARIOS={'replay_slots':['sleep-62294','hermes-scale-zero'],'claim_cas_off':['duplicates-59703'],
           'flock_off':['duplicates-59703'],'flock_and_claim_off':['duplicates-59703'],
           'fencing_off':['duplicates-59703'],'estop_off':['estop-i19'],
           'transform_raw':['sentinel-17646'],'cancel_on_reply_off':['hermes-drained'],'silenced_human':['hermes-drained']}
SCENARIOS.update(governor_pause_off=['ignored-ladder'], caps_off=['caps-owner-bypass'],
                 mute_class_regress=['mute-classes'], dm_only_off=['dm-only'])
CONTROL_SCENARIOS = sorted({scenario for mutant in MUTANTS for scenario in SCENARIOS[mutant]})
GATE_ORDER_MUTANT=[('heartbeat2/poller.py',
    '        from .commands import quiet_until\n        quiet=quiet_until(self,s.session_key,urgent=w.urgent)',
    '        mute=self.policy.unanswered_gate(w,s.unanswered_count)\n        if mute: return mute,False,None\n        from .commands import quiet_until\n        quiet=quiet_until(self,s.session_key,urgent=w.urgent)')]
GATE_ORDER_DRIVER='''import json,tempfile
from bench.sim.gateway import FakeGateway
scenario={'settings':{},'arms':[],'model_policy':{},'timeline':[]}
with tempfile.TemporaryDirectory() as home:
    gateway=FakeGateway(scenario,home)
    try:
        assert 'quiet' in gateway.command('owner','quiet for 3m').lower()
        gateway.arm({'ref':'task','args':{'note':'Check the task.','trigger':{'kind':'at','at':'in 1m'}}})
        engine=gateway.engine_for('owner')
        w,=engine.store.list_wakes()
        session=engine.store.get_session(w.session_key)
        assert engine.store.cas_session(w.session_key,{'unanswered_count':session.unanswered_count},
                                        unanswered_count=engine.policy.max_unanswered)
        gateway.clock.advance(60)
        gateway.tick()
        occurrence,=engine.store.list_occurrences(w.id)
        failures=[] if occurrence.state=='pending' and occurrence.error=='quiet_hours' else ['quiet window dropped: '+occurrence.state+':'+occurrence.error]
        print(json.dumps({'quiet-window':failures} if failures else {}))
    finally:
        gateway.close()
'''
DRIVER='''import json,tempfile
from pathlib import Path
from bench.run import load_scenarios
from bench.sim.runner import simulate
with tempfile.TemporaryDirectory() as home:
    failures={s['id']:simulate(s,Path(home)/s['id'])['failures'] for s in load_scenarios()}
print(json.dumps({k:v for k,v in failures.items() if v}))
'''


def scratch_run(tmp_path,mutations,driver=DRIVER):
    for name in ('heartbeat2','bench'):
        shutil.copytree(ROOT/name,tmp_path/name,ignore=shutil.ignore_patterns('__pycache__','results'))
    for path,old,new in mutations:
        p=tmp_path/path; text=p.read_text()
        assert text.count(old)>=1,(path,old)
        p.write_text(text.replace(old,new))
    # A hang guard, not a speed bar: the selected control measured ~5.01 s locally;
    # the full control previously exceeded 12 s on a slow shared CI runner.
    result=subprocess.run([sys.executable,'-B','-c',driver],cwd=tmp_path,capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope='module')
def control(tmp_path_factory):
    driver=DRIVER.replace('for s in load_scenarios()',
                          'for s in load_scenarios() if s["id"] in '+repr(CONTROL_SCENARIOS))
    return scratch_run(tmp_path_factory.mktemp('u6-control'),[],driver)


def test_control_covers_every_mutant():
    for mutant in MUTANTS:
        assert set(CONTROL_SCENARIOS) >= set(SCENARIOS[mutant]), mutant


@pytest.fixture(scope='module', autouse=True)
def mutant_budget():
    before=time.monotonic()
    yield
    elapsed=time.monotonic()-before
    print(f'mutant module total: {elapsed:.3f}s')
    assert elapsed<60, 'all mutants must stay below 60 s in total'


@pytest.mark.parametrize('mutant',MUTANTS)
def test_R6_E1_E2_E3_mutant_gate(tmp_path,mutant,control):
    before=time.monotonic()
    driver=DRIVER.replace('for s in load_scenarios()', 'for s in load_scenarios() if s["id"] in '+repr(SCENARIOS[mutant]))
    failures=scratch_run(tmp_path,MUTANTS[mutant],driver)
    introduced={(k,f) for k,rows in failures.items() for f in rows}-{(k,f) for k,rows in control.items() for f in rows}
    assert introduced,mutant+' survived'
    print(mutant,sorted(introduced),f'{time.monotonic()-before:.3f}s')


@pytest.mark.parametrize('mutant', ['shell','timeout'])
def test_R6_E15_command_contract_mutants(tmp_path,mutant):
    mutation=('heartbeat2/probes.py','shell=False','shell=True') if mutant=='shell' else ('heartbeat2/probes.py','COMMAND_DEADLINE_S = 30.0','COMMAND_DEADLINE_S = 3000.0')
    driver='''import json,sys,tempfile
from heartbeat2 import probes
with tempfile.TemporaryDirectory() as home:
    rc,body=probes._run_command([sys.executable,'-c','import sys,json;print(json.dumps(sys.argv[1:]))','a b'],home)
print(json.dumps(probes.COMMAND_DEADLINE_S==30 and rc==0 and body.strip()==b'["a b"]'))
'''
    assert scratch_run(tmp_path,[mutation],driver) is False


def test_R7_L6_gate_order_unanswered_first(tmp_path):
    control=scratch_run(tmp_path/'control',[],GATE_ORDER_DRIVER)
    assert not control,control
    failures=scratch_run(tmp_path/'mutant',GATE_ORDER_MUTANT,GATE_ORDER_DRIVER)
    assert failures,'gate_order_unanswered_first survived'
    print('gate_order_unanswered_first',failures)
