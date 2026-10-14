"""Proposal results read as not set; settled subjects end silently; the report-a-problem skill ships."""
from dataclasses import replace
from pathlib import Path

import yaml

from heartbeat2 import registration
from heartbeat2.model import Occurrence, Session, Wake
from heartbeat2.wake_text import render_wake
from test_engine import engine, KEY, arm
from test_v0114 import create, receipt

ROOT = Path(__file__).resolve().parents[1]
SETTLED = '- If the chat shows the note\'s subject was settled after this wake was armed, end it silently: outcome="done", notify=false.'


def test_proposal_result_is_not_armed_and_its_receipt_leads_with_not_set(engine):
    engine.policy = replace(engine.policy, agent_can_create='ask_first')
    result = create(engine)
    assert result.get('ok') and result['armed'] is False and result['wake']['status'] == 'proposed', result
    text = receipt(result)
    assert text.startswith('Not set yet:') and f'/pulse ok {result["wake"]["id"]}' in text
    assert 'armed:' not in text and 'Fires' not in text


def test_active_create_and_mutations_report_armed(engine):
    result = create(engine)
    assert result.get('ok') and result['armed'] is True, result
    w = arm(engine)
    paused = engine.tool.handle({'action':'pause','id':w.id}, session_id='sid')
    assert paused.get('ok') and paused['armed'] is False, paused
    resumed = engine.tool.handle({'action':'resume','id':w.id}, session_id='sid')
    assert resumed.get('ok') and resumed['armed'] is True, resumed


def test_wake_text_ends_a_settled_subject_silently(clock):
    wake = Wake('s', 'Check the deploy.', 'at', id='w_abcdefgh', created_at=clock.now()-60)
    text = render_wake(wake, Occurrence(wake.id, 't:1', attempt=1), Session('s'), clock=clock)
    lines = text.splitlines()
    assert SETTLED in lines
    assert lines.index(SETTLED) == lines.index(next(l for l in lines if l.startswith('- notify=false'))) + 1


def test_report_problem_skill_is_registered_with_its_description(fake_ctx):
    registration.register(fake_ctx)
    path = ROOT/'skills/heartbeat2-report-problem/SKILL.md'
    assert path.is_file()
    front = yaml.safe_load(path.read_text().split('---')[1])
    assert front == {'name': 'heartbeat2-report-problem', 'description': 'HeartBeat 2.0: report a bug or problem'}
    assert 'heartbeat2-report-problem' in fake_ctx.skills
    body = path.read_text()
    assert 'do not fire a wake or send a DM to reproduce' in body
    assert 'security/advisories/new' in body and 'issues/new?template=bug_report.yml' in body
