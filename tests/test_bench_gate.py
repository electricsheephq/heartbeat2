"""The same CLI gate used by the report, with a deliberately broken transform."""
import json
from pathlib import Path
import pytest
from bench.run import main
from heartbeat2.intake import Intake


def test_bench_gate(tmp_path):
    assert main(['--gate','--out',str(tmp_path)])==0
    result=json.loads(next(tmp_path.glob('*.json')).read_text())
    assert len(result['scenarios'])==34
    assert result['runtime_s']<30
    assert not result['failures']


def test_bench_gate_rejects_raw_transform(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr(Intake,'transform_llm_output',lambda self,response_text='',**kw:response_text)
    assert main(['--scenario','sentinel-17646','--gate','--out',str(tmp_path)])!=0
    result=json.loads(next(tmp_path.glob('*.json')).read_text())
    assert result['scenarios'][0]['heartbeat2']['metrics']['sentinel_leaks']>0
    assert 'sentinel_leaks' in capsys.readouterr().out


def test_bench_drained_human_finalizes_after_its_output(tmp_path,monkeypatch):
    from bench.run import load_scenarios
    from bench.sim.runner import simulate
    calls=[]
    transform=Intake.transform_llm_output
    ending=Intake.on_session_end
    starting=Intake.pre_llm_call
    def track_start(self,**kw):
        if kw.get('user_message')=='Any update?':
            calls.append(('human-start',kw.get('turn_id'),kw.get('user_message')))
        return starting(self,**kw)
    def track_transform(self,**kw):
        calls.append(('transform',kw.get('turn_id'),kw.get('response_text')))
        return transform(self,**kw)
    def track_end(self,**kw):
        calls.append(('end',kw.get('turn_id'),kw.get('completed')))
        return ending(self,**kw)
    monkeypatch.setattr(Intake,'pre_llm_call',track_start)
    monkeypatch.setattr(Intake,'transform_llm_output',track_transform)
    monkeypatch.setattr(Intake,'on_session_end',track_end)
    result=simulate(load_scenarios('hermes-drained')[0],tmp_path)
    raw=next(x for x in calls if x[0]=='transform' and x[2]=='Here is the update you requested.')
    human=next(x for x in calls if x[0]=='human-start')
    assert raw[1]!=human[1]
    before=calls[:calls.index(raw)]
    assert not any(x[0]=='end' and x[1]==raw[1] and x[2] is True for x in before)
    assert any(x[0]=='end' and x[1]==raw[1] and x[2] is True for x in calls[calls.index(raw)+1:])
    assert calls.index(raw)<next(i for i,x in enumerate(calls) if x[0]=='end' and x[1]==raw[1] and x[2] is True)<calls.index(human)
    answer=next(x for x in calls if x[0]=='transform' and x[1]==human[1])
    assert calls.index(human)<calls.index(answer)
    assert any(x[0]=='end' and x[1]==human[1] and x[2] is True for x in calls[calls.index(answer)+1:])
    assert len(result['deliveries'])==1
    assert result['deliveries'][0]['prompted']
