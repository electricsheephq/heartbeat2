"""All slash-command mutations and reads are scoped to gateway-bound session."""
from dataclasses import replace
from types import SimpleNamespace
import re

import pytest

from heartbeat2 import commands
from heartbeat2.model import FINISHED_WAKE_STATUSES, Session, Wake
from heartbeat2.policy import INJECTION_CONFIG
from test_engine import engine, arm, KEY

# Written out, not read from the model, so removing a status from FINISHED_WAKE_STATUSES fails these tests.
FINISHED = ['cancelled', 'done', 'expired', 'failed', 'orphaned']


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
    for field in ('fires','checks','model wakes','messages','engaged','cache/cost','verdict'):
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


def test_long_notes_shorten_at_a_word_not_mid_word(runtime):
    e = runtime.get_engine()
    checkin = 'Check in gently if there is something helpful to offer. Silence is fine.'
    e.store.create_wake(Wake(KEY, checkin, 'at', platform='telegram', chat_type='dm', chat_id='unit',
                             armed_session_id='sid', next_due_at=e.clock.now(), expires_at=e.clock.now() + 86400))
    out = commands.handle('list', runtime)
    assert 'Check in gently if there is something helpful to offer…' in out and 'Sile' not in out
    assert 'Check in gently if there is something…:' in commands.handle('ledger', runtime)
    added = commands.handle('add in 2h ' + 'Send the quarterly numbers to finance and copy the board. ' * 2, runtime)
    title = e.store.get_wake(re.search(r'w_[a-z2-7]{8}', added)[0]).title
    assert title == 'Send the quarterly numbers to finance and copy the board. Send the quarterly…'
    assert commands._clip('Short note', 60) == 'Short note'
    assert commands._clip('Pay\nsupercalifragilisticexpialidocious-invoice', 20) == 'Pay…'
    assert commands._clip('hello world and more', 13) == 'hello world…'
    assert commands._clip('x' * 100, 10) == 'x' * 9 + '…'


def test_list_default_shows_live_items_and_newest_finished(runtime):
    e = runtime.get_engine()
    assert commands.handle('list', runtime) == 'No wakes or watchers in this chat.'
    finished = []
    for n in range(11):
        w = arm(e, title=f'finished {n}')
        commands.handle(f'cancel {w.id}', runtime)
        finished.append(w)
        e.clock.advance(60)
    out = commands.handle('list', runtime)
    assert finished[0].id not in out and all(w.id in out for w in finished[1:])
    assert out.splitlines()[-1] == '1 older finished item hidden; /pulse list all'
    # Bookkeeping that rewrites an old finished wake (here a session rebind) must not bring it back.
    assert e.store.cas_wake(finished[0].id, 'cancelled', armed_session_id='sid2')
    assert finished[0].id not in commands.handle('list', runtime)
    live = arm(e, title='live', next_due_at=e.clock.now()+86400)
    last = arm(e, title='finished last')
    commands.handle(f'cancel {last.id}', runtime)
    out = commands.handle('list', runtime)
    assert live.id in out and last.id in out
    assert finished[0].id not in out and finished[1].id not in out
    assert out.splitlines()[-1] == '2 older finished items hidden; /pulse list all'
    out = commands.handle('list all', runtime)
    assert all(w.id in out for w in finished+[live, last]) and 'hidden' not in out


def test_list_and_ledger_rows_collapse_note_and_title_whitespace(runtime):
    e = runtime.get_engine()
    note = 'first line\n\nsecond   line'
    w = e.store.create_wake(Wake(KEY, note, 'at', title=''))
    titled = arm(e, title='first title\n\nsecond   title')
    out = commands.handle('list', runtime)
    assert len(out.splitlines()) == 2
    assert 'first line second line' in out and 'first title second title' in out
    ledger = commands.handle('ledger', runtime).splitlines()
    assert len(ledger) == 3 and ledger[0].startswith('Ledger 7d:')
    assert any(row.startswith(w.id+' first line second line:') for row in ledger[1:])
    assert any(row.startswith(titled.id+' first title second title:') for row in ledger[1:])
    assert f'\nNote: {note}\n' in commands.handle(f'why {w.id}', runtime)
    assert e.store.get_wake(w.id).note == note
    assert e.store.get_wake(titled.id).title == titled.title


def test_paused_rows_name_the_command_that_resumes_them(runtime):
    e = runtime.get_engine()
    governed = arm(e, status='paused', status_reason='governor:noop')
    held = arm(e, title='held')
    commands.handle(f'pause {held.id}', runtime)
    timed = arm(e, title='timed')
    commands.handle(f'pause {timed.id} for 3d', runtime)
    rows = {line.split(' ')[0]: line for line in commands.handle('list', runtime).splitlines()}
    assert 'paused · resumes on keep/resume' in rows[governed.id]
    assert 'paused · resumes on resume' in rows[held.id] and 'keep' not in rows[held.id]
    until = commands._time(e, e.clock.now() + 3 * 86400)
    assert f'paused · resumes {until} or on resume' in rows[timed.id] and 'keep' not in rows[timed.id]
    # keep is refused for a command pause, which is why its row no longer offers it.
    assert 'use /pulse resume' in commands.handle(f'keep {held.id}', runtime)


def test_finished_rows_show_no_next_time(runtime):
    e = runtime.get_engine()
    added = commands.handle('add in 2h check the deploy', runtime)
    w = e.store.get_wake(re.search(r'w_[a-z2-7]{8}', added)[0])
    assert ' · next ' in added
    cancelled = commands.handle(f'cancel {w.id}', runtime)
    row = next(line for line in commands.handle('list', runtime).splitlines() if line.startswith(w.id))
    for out in (cancelled, row):
        assert 'cancelled' in out and ' · next ' not in out


def test_finished_statuses_are_pinned():
    assert FINISHED_WAKE_STATUSES == set(FINISHED) and commands.FINISHED_STATUSES == set(FINISHED)


@pytest.mark.parametrize('status', FINISHED)
def test_bulk_commands_skip_finished_items(runtime, status):
    e = runtime.get_engine()
    a = arm(e, status=status)
    b = arm(e)
    out = commands.handle('cancel all', runtime)
    assert a.id not in out and 'item is terminal' not in out
    assert b.id in out and e.store.get_wake(b.id).status == 'cancelled'
    assert commands.handle('pause all', runtime) == 'No matching items in this chat.'
    assert e.store.get_wake(a.id).status == status


@pytest.mark.parametrize('action,start,end', [('pause', 'active', 'paused'), ('resume', 'paused', 'active'),
                                              ('cancel', 'active', 'cancelled')])
@pytest.mark.parametrize('status', FINISHED)
def test_tool_id_all_skips_finished_items(runtime, status, action, start, end):
    e = runtime.get_engine()
    done = arm(e, status=status)
    live = arm(e, status=start)
    result = e.tool.handle({'action': action, 'id': 'all'}, session_id='sid')
    assert result.get('ok'), result
    ids = [item.get('id') or item.get('wake', {}).get('id') for item in result['results']]
    assert ids == [live.id]
    assert e.store.get_wake(live.id).status == end
    assert e.store.get_wake(done.id).status == status
