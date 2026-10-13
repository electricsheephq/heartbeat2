"""Numeric settings must survive real ticks."""
from dataclasses import fields
import json
import os

import pytest

from heartbeat2.model import KINDS, PROBE_KINDS, EVENT_KINDS
from heartbeat2.policy import Policy
from heartbeat2.registration import Runtime
from test_engine import engine, arm, session, pending, start, end, KEY
from test_v014a import bind
from test_engine import GatewayCtx


NUMERIC_FIELDS = tuple(f.name for f in fields(Policy)
                       if type(getattr(Policy(), f.name)) in (int, float))
# The widest value per field runs by default; HEARTBEAT2_FUZZ_FULL=1 adds the rest.
FUZZ_VALUES = (10**6, 10**9, 10**12, 10**18) if os.environ.get('HEARTBEAT2_FUZZ_FULL') else (10**18,)
BOUNDS = {'ledger_days': 36500, 'min_interval_minutes': 525600, 'agent_ungated_floor_minutes': 525600, 'noop_backoff_factor': 100, 'noop_max_backoff_hours': 8760, 'probe_floor_seconds': 604800, 'max_lifetime_days': 3650, 'checkin_after_hours': 8760}


def arm_kind(e, kind):
    trigger = {'cron': '* * * * *'} if kind == 'cron' else {}
    if kind == 'every_anchor':
        return arm(e, 'every', trigger_json={'at': '00:00'},
                   base_interval_s=86400, eff_interval_s=86400)
    if kind == 'owner_silent':
        session(e, last_human_at=e.clock.now() - 48*3600)
        trigger = {'after': 3600}
    w = arm(e, kind, trigger_json=trigger)
    if kind in PROBE_KINDS:
        e.probe_runner = lambda _: {'fire': True, 'identity': 'changed', 'message': 'changed'}
    if kind in EVENT_KINDS:
        e.store.add_event(kind, 'event', {}, session_key=KEY)
    return w


def exercise_tick(e, kind):
    # Tick's ignored-message settlement exercises the maximum exponent and
    # schedule/bind consumers before dispatch, using a separate completed wake.
    old = arm(e, 'cron' if kind == 'cron' else 'every',
              trigger_json={'cron': '* * * * *'} if kind == 'cron' else {},
              next_due_at=e.clock.now())
    e.tick()
    start(e)
    end(e, outcome='progress', notify=True)
    e.store.cas_wake(old.id, 'active', noop_streak=34, next_due_at=e.clock.now() + 48*3600)
    e.clock.advance(24*3600 + 1)
    e.ctx.calls.clear()
    session(e, last_wake_turn_at=None)
    w = arm_kind(e, kind)
    e.ctx.settings.update(e.fuzz_setting)
    # Refresh before gc, even though the previous lifecycle already ticked.
    e.store.set_meta('last_gc_at', '')
    e.tick()
    errors = {'tick': e.store.get_meta('last_error')}
    errors.update({x.id: e.store.get_meta('wake_error:' + x.id)
                   for x in e.store.list_wakes()})
    return w, {k: v for k, v in errors.items() if v}


@pytest.mark.parametrize('key', NUMERIC_FIELDS)
@pytest.mark.parametrize('value', FUZZ_VALUES)
@pytest.mark.parametrize('kind', sorted(KINDS) + ['every_anchor'])
def test_numeric_setting_tick_fuzz(tmp_path, clock, key, value, kind):
    rt = Runtime(GatewayCtx(tmp_path, settings={'quiet_hours': '', 'timezone': 'UTC'}), clock=clock)
    try:
        e = bind(rt)
        e.fuzz_setting = {key: value}
        w, errors = exercise_tick(e, kind)
        assert not errors, (key, value, kind, errors)
        assert len(e.ctx.calls) == 1, (key, value, kind, e.store.list_occurrences(w.id))
    finally:
        rt.close()


@pytest.mark.parametrize('key,maximum', BOUNDS.items())
@pytest.mark.parametrize('startup', [False, True])
def test_bound_notice_and_due_wake(tmp_path, clock, key, maximum, startup):
    value = maximum + 1
    with pytest.raises(ValueError, match=key + '.*maximum'):
        Policy.from_settings({key: value}, clock=clock)
    rt = Runtime(GatewayCtx(tmp_path, settings={'quiet_hours': '', 'timezone': 'UTC'}), clock=clock)
    try:
        expected = getattr(Policy(), key)
        if not startup:
            expected += 1
            rt.ctx.settings[key] = expected
            rt.policy()
        rt.ctx.settings[key] = value
        policy = rt.policy()
        assert getattr(policy, key) == expected
        notice, = json.loads(rt.settings_error)
        assert notice['key'] == key and notice['effective'] == expected
        assert 'maximum' in notice['reason']
        e = bind(rt)
        arm(e)
        e.tick()
        assert len(e.ctx.calls) == 1 and not e.store.get_meta('last_error')
    finally:
        rt.close()


@pytest.mark.parametrize('key,maximum', BOUNDS.items())
@pytest.mark.parametrize('kind', sorted(KINDS) + ['every_anchor'])
def test_maximum_setting_survives_tick(tmp_path, clock, key, maximum, kind):
    rt = Runtime(GatewayCtx(tmp_path, settings={'quiet_hours': '', 'timezone': 'UTC'}), clock=clock)
    try:
        e = bind(rt)
        e.fuzz_setting = {key: maximum}
        _, errors = exercise_tick(e, kind)
        assert not errors
        assert getattr(e.policy, key) == maximum
        assert not rt.settings_error
        assert len(e.ctx.calls) == 1
    finally:
        rt.close()
