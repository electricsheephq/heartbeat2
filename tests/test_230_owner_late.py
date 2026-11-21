"""#230: an owner's own one-time reminder that comes due during downtime is delivered late, or its turn reports the miss."""
import pytest

from heartbeat2.commands import _item
from test_engine import engine, session, KEY, arm
from test_v0113 import owner_turn, finish
from test_v0114 import command, create

HOUR = 3600


def owner_reminder(e, command, path):
    if path == 'command':  # /pulse add: the owner's own wake
        result = command('add in 2h check in for the flight')
        assert not result.startswith('Error:'), result
        w, = e.store.list_wakes(KEY)
        return w
    owner_turn(e)  # asked for in chat: an agent wake with owner proof
    result = create(e, {'kind':'at', 'at':'in 2 hours'}, owner_asked=True)
    assert result.get('ok'), result
    return e.store.get_wake(result['wake']['id'])


@pytest.mark.parametrize('path', ['command', 'asked'])
@pytest.mark.parametrize('late,expect', [(10*60, None), (8*HOUR, 'late'), (30*HOUR, 'missed')])
def test_owner_reminder_after_downtime(engine, command, path, late, expect):
    e = engine
    w = owner_reminder(e, command, path)
    finish(e)
    e.clock.advance(w.next_due_at - e.clock.now() + late)  # HeartBeat or the gateway was down across the due time
    e.tick()
    assert len(e.ctx.calls) == 1, 'the owner reminder gets exactly one turn'
    text = e.ctx.calls[-1][0]
    assert ('deliver it now and say it is late' in text) == (expect == 'late')
    assert ('Tell the owner in one line that this reminder was missed' in text) == (expect == 'missed')
    assert ('Do the work the note describes' in text) == (expect != 'missed')
    assert e.store.list_occurrences(w.id)[0].state == 'claimed'
    assert 'missed' not in (e.store.get_meta('notice:'+KEY) or '')


def test_agent_one_shot_still_missed_with_a_named_notice(engine):
    e = engine
    w = arm(e, next_due_at=e.clock.now() - 7*HOUR, title='Look at the build')
    e.tick()
    assert e.store.list_occurrences(w.id)[0].state == 'missed'
    assert not e.ctx.calls
    notice = e.store.get_meta('notice:'+KEY)
    assert w.id in notice and 'Look at the build' in notice and 'too late after downtime' in notice
    assert 'missed: too late after HeartBeat or the gateway was down' in _item(e, e.store.get_wake(w.id))


def test_longer_catch_up_hours_widens_the_owner_window(engine, command, monkeypatch):
    e = engine
    from dataclasses import replace
    e.policy = replace(e.policy, catch_up_hours=48)
    assert e.policy.late_window_s() == 48*HOUR
    w = owner_reminder(e, command, 'command')
    finish(e)
    e.clock.advance(w.next_due_at - e.clock.now() + 30*HOUR)
    e.tick()
    assert 'deliver it now and say it is late' in e.ctx.calls[-1][0]


@pytest.mark.parametrize('outage,expect', [(8*HOUR, 'late'), (40*HOUR, 'missed')])
def test_owner_reminder_keeps_its_turn_when_expiry_passes_in_the_outage(engine, command, outage, expect):
    """Review: /pulse add in 335h expires an hour after its due time, so an outage across both expired it silently."""
    e = engine
    result = command('add in 335h renew the passport')
    assert not result.startswith('Error:'), result
    w, = e.store.list_wakes(KEY)
    assert w.expires_at is not None and w.expires_at < w.next_due_at + outage
    finish(e)
    e.clock.advance(w.next_due_at - e.clock.now() + outage)
    e.tick()
    assert len(e.ctx.calls) == 1, 'past its expiry, the owner reminder still gets its one turn'
    text = e.ctx.calls[-1][0]
    assert ('say it is late' in text) == (expect == 'late') and ('it was missed' in text) == (expect == 'missed')
    e.tick()  # the claimed turn is not cancelled as expired on the next tick
    assert e.store.list_occurrences(w.id)[0].state == 'claimed'
    assert e.store.get_wake(w.id).status == 'active'


def test_agent_one_shot_past_expiry_still_expires(engine):
    e = engine
    w = arm(e, next_due_at=e.clock.now() - 2*HOUR, expires_at=e.clock.now() - HOUR)
    e.tick()
    assert e.store.get_wake(w.id).status == 'expired' and not e.ctx.calls


def test_a_deliberate_pause_is_not_downtime(engine, command):
    """Review: an on-time reminder held 25 h by a chat pause must not say it was missed."""
    e = engine
    w = owner_reminder(e, command, 'command')
    finish(e)
    session(e, paused_until=w.next_due_at + 25*HOUR)
    e.clock.advance(w.next_due_at - e.clock.now())
    e.tick()                       # on time: the occurrence is made, then held by the pause
    assert not e.ctx.calls and e.store.list_occurrences(w.id)
    e.clock.advance(25*HOUR + 60)
    e.store.set_meta('last_tick_at', str(e.clock.now() - 5))  # the gateway kept ticking through the pause
    session(e, paused_until=None)
    e.tick()
    text = e.ctx.calls[-1][0]
    assert 'it was missed' not in text and 'say it is late' not in text


def test_a_turn_refunded_after_a_long_crash_reports_the_miss(engine, command):
    """Review: claimed on time, then the gateway was down 30 h; the restart refund keeps created_at at the due time."""
    e = engine
    w = owner_reminder(e, command, 'command')
    finish(e)
    e.clock.advance(w.next_due_at - e.clock.now())
    e.tick()
    o, = e.store.list_occurrences(w.id)
    assert o.state == 'claimed' and len(e.ctx.calls) == 1
    assert 'Do the work the note describes' in e.ctx.calls[-1][0]
    e.clock.advance(30*HOUR)       # crash before the turn started
    e.process_start = True
    e.reconcile()
    e.tick()
    text = e.ctx.calls[-1][0]
    assert 'Tell the owner in one line that this reminder was missed' in text
    assert 'Do the work the note describes' not in text


def test_a_turn_queued_before_an_outage_counts_it(engine, command):
    """Codex connector: queued 8 h late (late), then HeartBeat switched off for 30 h before the turn ran."""
    e = engine
    from dataclasses import replace
    w = owner_reminder(e, command, 'command')
    finish(e)
    e.ctx.accept = False                             # the gateway refuses the injection, so the late reminder stays queued
    e.clock.advance(w.next_due_at - e.clock.now() + 8*HOUR)
    e.tick()
    assert e.store.list_occurrences(w.id)[0].state == 'pending'
    e.ctx.accept = True
    e.policy = replace(e.policy, enabled=False)      # HeartBeat switched off; the gateway keeps ticking, but it does nothing
    e.tick()
    e.clock.advance(30*HOUR)
    e.store.set_meta('last_tick_at', str(e.clock.now() - 5))  # continuous ticks: no gap, so only the off stretch counts
    e._last_tick_mono = e.clock.monotonic() - 5
    e.tick()
    assert e.store.get_meta('off_since') and not e.store.get_meta('outages')
    e.policy = replace(e.policy, enabled=True)
    e.tick()
    text = e.ctx.calls[-1][0]
    assert 'Tell the owner in one line that this reminder was missed' in text


def test_overlapping_outages_count_once(engine):
    e = engine
    now = e.clock.now()
    e.record_outage(now - 10*HOUR, now - 4*HOUR)   # the gateway was down...
    e.record_outage(now - 8*HOUR, now - 2*HOUR)    # ...while HeartBeat was also switched off
    e.record_outage(now - 30*HOUR, now - 20*HOUR)  # before the turn was queued: not counted
    assert e.downtime_since(now - 12*HOUR) == 8*HOUR
    assert e.downtime_since(now - 6*HOUR, before=60) == 60 + 4*HOUR


def test_a_wall_clock_step_is_not_downtime(engine, command):
    """Round 3: a 30 h forward wall-clock step with 5 s really elapsed must not turn an on-time reminder report-only."""
    e = engine
    w = owner_reminder(e, command, 'command')
    finish(e)
    e.ctx.accept = False
    e.clock.advance(w.next_due_at - e.clock.now())
    e.tick()                                          # queued on time; the injection is refused, so it waits
    assert e.store.list_occurrences(w.id)[0].state == 'pending'
    e.ctx.accept = True
    e.clock.wall += 30*HOUR                           # wall clock steps; monotonic doesn't
    e.clock.advance(5)
    e.tick()
    text = e.ctx.calls[-1][0]
    assert 'Do the work the note describes' in text and 'this reminder was missed' not in text
    assert not e.store.get_meta('outages')


def test_report_only_turn_has_no_silent_settled_exit(engine, command):
    e = engine
    w = owner_reminder(e, command, 'command')
    finish(e)
    e.clock.advance(w.next_due_at - e.clock.now() + 30*HOUR)
    e.tick()
    text = e.ctx.calls[-1][0]
    assert 'this reminder was missed' in text and 'end it silently' not in text
