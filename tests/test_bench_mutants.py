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
    result=subprocess.run([sys.executable,'-B','-c',driver],cwd=tmp_path,capture_output=True,text=True,timeout=12)
    assert result.returncode==0,result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope='module')
def control(tmp_path_factory):
    return scratch_run(tmp_path_factory.mktemp('u6-control'),[])


@pytest.mark.parametrize('mutant',MUTANTS)
def test_R6_E1_E2_E3_mutant_gate(tmp_path,mutant,control):
    before=time.monotonic()
    driver=DRIVER.replace('for s in load_scenarios()', 'for s in load_scenarios() if s["id"] in '+repr(SCENARIOS[mutant]))
    failures=scratch_run(tmp_path,MUTANTS[mutant],driver)
    introduced={(k,f) for k,rows in failures.items() for f in rows}-{(k,f) for k,rows in control.items() for f in rows}
    assert introduced,mutant+' survived'
    print(mutant,sorted(introduced))
    assert time.monotonic()-before<2, 'nine mutants must stay below 15 s in total'


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
