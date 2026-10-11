"""All slash-command mutations and reads are scoped to gateway-bound session."""
from dataclasses import replace
from types import SimpleNamespace
import re

import pytest

from heartbeat2 import commands
from heartbeat2.model import Session, Wake
from heartbeat2.policy import INJECTION_CONFIG
from test_engine import engine, arm, KEY


@pytest.fixture
def runtime(engine, monkeypatch):
    monkeypatch.setattr(commands, 'session_env', lambda name: KEY if name=='HERMES_SESSION_KEY' else '', raising=False)
    return SimpleNamespace(get_engine=lambda: engine, home=engine.ctx._manager.home_path, clock=engine.clock)


@pytest.mark.parametrize('action', ['why','pause','resume','cancel','ok','keep','slow','stop','watch'])
def test_commands_reject_foreign_ids_without_read_or_mutation(runtime, action):
    e = runtime.get_engine()
    w = e.store.create_wake(Wake('foreign','SECRET OTHER NOTE','at',status='proposed'))
    result = commands.handle(f'{action} {w.id}', runtime)
    assert 'unknown' in result.lower() and 'SECRET' not in result
    assert e.store.get_wake(w.id) == w


@pytest.mark.parametrize('key', ['', 'default'])
def test_missing_gateway_session_cannot_touch_state(runtime, monkeypatch, key):
    monkeypatch.setattr(commands, 'session_env', lambda name: key)
    assert 'session' in commands.handle('off',runtime).lower()
    assert runtime.get_engine().store.get_session(KEY).enabled


def test_all_reads_mutations_on_off_are_session_scoped(runtime):
    e = runtime.get_engine()
    mine = arm(e, title='mine', trigger_json={'at':'in 1h'})
    other = e.store.create_wake(Wake('foreign','FOREIGN NOTE','at',title='FOREIGN TITLE'))
    for action in ('status','list','ledger 7d'):
        out = commands.handle(action,runtime)
        assert 'FOREIGN' not in out and 'mine' in out
    assert 'paused' in commands.handle('pause all',runtime)
    assert e.store.get_wake(other.id).status == 'active'
    assert 'active' in commands.handle(f'resume {mine.id}',runtime)
    commands.handle('off',runtime)
    assert not e.store.get_session(KEY).enabled
    commands.handle('on',runtime)
    assert e.store.get_session(KEY).enabled
    commands.handle('cancel all',runtime)
    assert e.store.get_wake(mine.id).status == 'cancelled' and e.store.get_wake(other.id).status == 'active'


def test_status_injection_yaml_quiet_ledger_why(runtime):
    e = runtime.get_engine()
    w = arm(e,title='build',reason='CI is pending')
    e.ctx.allowed=False
    out = commands.handle('',runtime)
    assert INJECTION_CONFIG in out and 'not running' in out and 'UTC' in out and 'quiet' in out.lower()
    assert 'CI is pending' in commands.handle(f'why {w.id}',runtime)
    e.ctx.allowed=True
    for field in ('fires','checks','model wakes','messages','engaged','≈tokens','verdict'):
        assert field in commands.handle('ledger 7d',runtime).lower()


def test_user_add_proposal_approval_and_governor_answers(runtime):
    e = runtime.get_engine()
    e.policy = replace(e.policy,agent_can_create='no')
    for text, kind in [('add in 2h Finish report','at'),('add every 1d at 09:00 Check release','every'),('add when silent 24h Check open task','owner_silent')]:
        out = commands.handle(text,runtime)
        assert 'w_' in out
        w = e.store.get_wake(re.search(r'w_[a-z2-7]{8}',out)[0])
        assert w.kind == kind and w.creator == 'user' and w.created_in == 'command'
    w = arm(e, 'every', status='proposed')
    assert 'active' in commands.handle(f'ok {w.id}',runtime)
    assert e.store.get_wake(w.id).status == 'active'
    e.store.cas_wake(w.id,'active',status='paused',status_reason='governor:noop')
    assert 'active' in commands.handle(f'keep {w.id}',runtime)
    old = e.store.get_wake(w.id).eff_interval_s
    e.store.cas_wake(w.id,'active',status='paused',status_reason='governor:noop')
    assert 'active' in commands.handle(f'slow {w.id}',runtime)
    assert e.store.get_wake(w.id).eff_interval_s > old
    assert 'Ask your agent to turn it into a watcher' in commands.handle(f'watch {w.id}',runtime)
    assert e.store.get_wake(w.id).status == 'paused'
    commands.handle(f'stop {w.id}',runtime)
    assert e.store.get_wake(w.id).status == 'cancelled'


def test_quiet_override_timed_pause_and_invalid_input(runtime):
    e = runtime.get_engine()
    w = arm(e, trigger_json={'at':'in 1h'})
    assert '22:00-07:30' in commands.handle('quiet 22:00-07:30',runtime)
    assert 'quiet' in commands.handle('quiet for 3h',runtime).lower()
    e.tick()
    assert not e.ctx.calls
    commands.handle('quiet off',runtime)
    assert 'paused' in commands.handle(f'pause {w.id} for 3d',runtime)
    e.clock.advance(3*86400)
    e.tick()
    assert e.store.get_wake(w.id).status in {'active','done'}
    for invalid in ('quiet nonsense','ledger all','add in 2h','pause all for bad','on foreign'):
        assert 'error' in commands.handle(invalid,runtime).lower()


def test_quiet_off_releases_previously_deferred_one_shot(runtime):
    e=runtime.get_engine()
    w=arm(e)
    commands.handle('quiet for 3h',runtime)
    e.tick()
    assert not e.ctx.calls
    commands.handle('quiet off',runtime)
    e.tick()
    assert len(e.ctx.calls)==1


def test_pause_resume_does_not_reuse_superseded_occurrence(runtime):
    e=runtime.get_engine()
    w=arm(e)
    e.store.add_occurrence(w.id,f't:{w.next_due_at:.17g}')
    commands.handle(f'pause {w.id}',runtime)
    commands.handle(f'resume {w.id}',runtime)
    e.clock.advance(5)
    e.tick()
    assert len(e.ctx.calls)==1
