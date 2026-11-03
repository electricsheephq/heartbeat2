"""#113: only the gateway that serves a profile opens its store, holds its lock or delivers.

Hermes loads every profile's plugins at gateway boot and stamps its injector on
all of them, so a manager can carry an injector for a home this gateway never serves.
"""
import fcntl
import json
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from heartbeat2 import serving
from heartbeat2.model import Session
from heartbeat2.registration import Runtime, register
from heartbeat2.store import Store
from heartbeat2.poller import Poller
from test_engine import GatewayCtx, KEY, arm, engine, pending, session  # noqa: F401 (fixture)
from test_v014a import bind

SETTINGS = {'quiet_hours': '', 'timezone': 'UTC'}


def until(predicate, timeout=2):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if predicate(): return True
        time.sleep(.01)
    return predicate()


def locked(home):
    path = home/'plugin-data/heartbeat2/poller.lock'
    if not path.exists(): return False
    with open(path, 'a+') as f:
        try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


def unload(ctx):
    for cleanup in ctx.unloads: cleanup()


def boot(ctx, clock):
    return register(ctx, clock=clock, bootstrap_interval=.01, tick_interval=.01)


def test_foreign_home_opens_nothing(tmp_path, clock):
    ctx = GatewayCtx(tmp_path, settings=SETTINGS)
    ctx.runner.served = False
    rt = boot(ctx, clock)
    try:
        time.sleep(.2)
        assert rt._thread.is_alive()
        assert rt.hook('pre_llm_call', session_id='sid', turn_id='turn', platform='telegram') is None
        assert rt.hook('kanban_task_completed', task_id='t1') is None
        assert json.loads(rt.tool({'action': 'list'}))['error'] == 'engine_unavailable'
        assert 'inactive here' in rt.command('status')
        assert rt.status()['reason'] == serving.REASONS[serving.FOREIGN]
        assert rt._engine is None and rt._runner is None
        assert not (tmp_path/'plugin-data').exists()
        assert ctx.calls == []
    finally:
        unload(ctx)


def test_served_home_starts_when_injector_is_published(tmp_path, clock):
    ctx = GatewayCtx(tmp_path, settings=SETTINGS)
    ctx._manager.has_gateway_message_injector = False
    rt = boot(ctx, clock)
    try:
        time.sleep(.1)
        assert not (tmp_path/'plugin-data').exists()
        ctx._manager.has_gateway_message_injector = True
        assert until(lambda: locked(tmp_path))
        assert rt.status() is None
    finally:
        unload(ctx)


def test_unserved_mid_run_releases_lock_and_holds_due_wakes(tmp_path, clock):
    ctx = GatewayCtx(tmp_path, settings=SETTINGS)
    rt = boot(ctx, clock)
    store = None
    try:
        assert until(lambda: locked(tmp_path))
        ctx.runner.served = False
        assert until(lambda: not locked(tmp_path))
        store = Store(tmp_path/'plugin-data/heartbeat2/pulse.db', clock)
        store.upsert_session(Session(KEY, 'telegram', 'dm', 'unit', 'sid', clock.now(), clock.now(), 'episode'))
        arm(SimpleNamespace(store=store, clock=clock))
        clock.advance(10)
        time.sleep(.2)
        assert ctx.calls == [] and not locked(tmp_path)
        assert rt.status()['reason'] == serving.REASONS[serving.FOREIGN]
        ctx.runner.served = True
        clock.advance(10)
        assert ctx.injected.wait(timeout=2)
        clock.advance(10)
        time.sleep(.2)
        assert len(ctx.calls) == 1
    finally:
        unload(ctx)
        if store: store.close()


def test_unknown_served_check_fails_closed_and_warns_once(tmp_path, clock, caplog):
    ctx = GatewayCtx(tmp_path, settings=SETTINGS)
    ctx._manager._gateway_message_injector = (object(), None)
    with caplog.at_level(logging.WARNING, logger='heartbeat2'):
        rt = boot(ctx, clock)
        try:
            time.sleep(.2)
            assert rt.hook('pre_llm_call', session_id='sid', turn_id='turn', platform='telegram') is None
            assert rt.status()['reason'] == serving.REASONS[serving.UNKNOWN]
            assert serving.REASONS[serving.UNKNOWN] in rt.command('status')
            assert not (tmp_path/'plugin-data').exists()
        finally:
            unload(ctx)
    warned = [r for r in caplog.records if 'cannot tell whether this gateway serves' in r.getMessage()]
    assert len(warned) == 1


def test_dispatch_rechecks_before_claim(engine):
    e = engine
    w = arm(e)
    assert e.acquire()
    o = pending(e, w)
    e.ctx.runner.served = False
    e.dispatch(o)
    assert e.ctx.calls == []
    assert e.store.get_occurrence(w.id, o.dedupe_key).state == 'pending'
    e.ctx.runner.served = True
    e.dispatch(e.store.get_occurrence(w.id, o.dedupe_key))
    assert len(e.ctx.calls) == 1


def test_no_gateway_process_still_records_hook_events(tmp_path, clock):
    # CLI, TUI and kanban worker processes run this profile's own turns: they record, never deliver.
    ctx = GatewayCtx(tmp_path, cli=True, settings=SETTINGS)
    rt = boot(ctx, clock)
    try:
        rt.hook('kanban_task_completed', task_id='t1', board='main')
        assert [x['kind'] for x in rt.get_engine().store.pending_events()] == ['kanban_completed']
        assert not locked(tmp_path) and ctx.calls == []
    finally:
        unload(ctx)


class Host:
    """A multiplex host: Hermes's rule lists the profiles on disk; the host admits only some."""
    def __init__(self, candidates, admitted):
        self.config = SimpleNamespace(multiplex_profiles=True)
        self.candidates = {Path(h).resolve(): name for name, h in candidates.items()}
        self._served_profile_homes = admitted
    def _served_profile_for_home(self, home):
        return self.candidates.get(Path(home).resolve())


def manager(home, owner, **changes):
    fields = dict(home_path=Path(home), scope_key=str(home), _cli_ref=None,
                  has_gateway_message_injector=True, _gateway_message_injector=(owner, None))
    fields.update(changes)
    return SimpleNamespace(**fields)


@pytest.fixture
def home_key(monkeypatch):
    monkeypatch.setitem(sys.modules, 'hermes_constants', SimpleNamespace(hermes_home_key=lambda p: str(Path(p).resolve())))


def test_state_follows_hermes_routing_and_multiplex_admission(tmp_path, home_key):
    a, b = tmp_path/'a', tmp_path/'b'
    a.mkdir(); b.mkdir()
    (tmp_path/'alias').symlink_to(a)
    def answer(value): return SimpleNamespace(config=SimpleNamespace(multiplex_profiles=False), _served_profile_for_home=lambda h: value)
    def boom(h): raise RuntimeError('renamed upstream')
    host = Host({'default': a, 'b': b}, {'default': tmp_path/'alias'})
    cases = [
        (manager(a, answer('x')), serving.SERVED),
        (manager(a, answer('')), serving.SERVED),       # Hermes refuses only on None
        (manager(a, answer(None)), serving.FOREIGN),
        (manager(a, SimpleNamespace(_served_profile_for_home=boom)), serving.UNKNOWN),
        (manager(a, object()), serving.UNKNOWN),
        (manager(a, answer('x'), has_gateway_message_injector=False), serving.NO_GATEWAY),
        (manager(a, answer('x'), _cli_ref=object()), serving.NO_GATEWAY),
        (None, serving.NO_GATEWAY),
        (manager(a, answer('x'), _gateway_message_injector=None), serving.NO_GATEWAY),  # cleared at shutdown
        (SimpleNamespace(home_path=a, scope_key='renamed', _cli_ref=None, has_gateway_message_injector=True), serving.UNKNOWN),
        (manager(a, host), serving.SERVED),             # admitted, under another spelling of the path
        (manager(b, host), serving.FOREIGN),            # on disk but not admitted (it runs its own gateway)
        (manager(tmp_path/'c', host), serving.FOREIGN),
        (manager(a, Host({'default': a}, None)), serving.FOREIGN),  # admission not recorded yet
    ]
    assert [serving.state(m) for m, _ in cases] == [want for _, want in cases]


def test_unserved_dispatch_leaves_the_wake_untouched(engine):
    e = engine
    w = arm(e, expires_at=e.clock.now()+60)
    o = pending(e, w)
    e.clock.advance(120)  # expired: the serving gateway would settle it
    e.ctx.runner.served = False
    e.dispatch(o)
    assert e.store.get_occurrence(w.id, o.dedupe_key) == o
    e.ctx.runner.served = True
    e.dispatch(o)
    assert e.store.get_occurrence(w.id, o.dedupe_key).state != 'pending'


def test_home_lost_after_claim_gives_the_claim_back(engine, monkeypatch):
    import heartbeat2.wake_text as wake_text
    e = engine
    w = arm(e)
    assert e.acquire()
    o = pending(e, w)
    e.store.set_meta('provider_health', json.dumps({'reason': 'provider_unavailable', 'until': e.clock.now()-1}))
    render = wake_text.render_wake
    def lose(*args, **kwargs):
        assert json.loads(e.store.get_meta('provider_health')).get('trial')  # the claim did set a trial
        e.ctx.runner.served = False
        return render(*args, **kwargs)
    monkeypatch.setattr(wake_text, 'render_wake', lose)
    e.dispatch(o)
    after = e.store.get_occurrence(w.id, o.dedupe_key)
    assert e.ctx.calls == []
    assert after.state == 'pending' and after.error == o.error and after.claimed_at is None and after.epoch is None
    assert not e.store.get_meta('inject_failures:'+after.id) and not e.store.get_meta('accepted:'+after.id)
    assert 'trial' not in json.loads(e.store.get_meta('provider_health'))
    e.ctx.runner.served = True
    monkeypatch.setattr(wake_text, 'render_wake', render)
    e.dispatch(after)
    assert len(e.ctx.calls) == 1


def test_home_lost_during_probes_stops_the_tick(engine, monkeypatch):
    e = engine
    arm(e)
    probes = e.probes
    def lose(): e.ctx.runner.served = False
    monkeypatch.setattr(e, 'probes', lose)
    e.tick()
    assert e.ctx.calls == [] and e.store.list_occurrences() == []
    e.ctx.runner.served = True
    monkeypatch.setattr(e, 'probes', probes)
    e.tick()
    assert len(e.ctx.calls) == 1


def test_reacquire_after_another_holder_recovers_its_turns(engine):
    e = engine
    w = arm(e)
    o = pending(e, w)
    assert e.acquire()
    e.release()  # this gateway stopped serving the home
    other_store = Store(e.store.path, e.clock)
    other = Poller(e.ctx, other_store, e.policy, e.clock, paused=lambda: False)
    try:
        assert other.acquire()  # the profile's own gateway took over, then crashed mid-turn
        other_store.set_meta('epoch', 'epoch-of-another-process')
        assert other_store.claim(w.id, o.dedupe_key, epoch=other.epoch, pid=1)
        session(e, busy_since=e.clock.now(), busy_epoch=other.epoch)
    finally:
        other.close(); other_store.close()
    assert e.acquire()  # served again: recover the crashed holder's turns as a takeover does
    after = e.store.get_occurrence(w.id, o.dedupe_key)
    assert after.state == 'pending' and after.error == 'restart_refund'
    assert e.store.get_session(KEY).busy_since is None


def test_turn_in_flight_when_the_home_is_lost_still_settles(tmp_path, clock):
    # The gateway served the home and sent the wake; the profile's own turn must still settle here.
    rt = Runtime(GatewayCtx(tmp_path, settings=SETTINGS), clock=clock)
    try:
        e = bind(rt)
        assert rt.inactive() == ''  # this gateway serves the home
        arm(e)
        e.tick()
        text = e.ctx.calls[-1][0]
        e.ctx.runner.served = False
        rt.hook('pre_llm_call', session_id='sid', turn_id='turn', user_message=text, platform='telegram')
        rt.hook('post_api_request', session_id='sid', turn_id='turn', usage={})
        assert json.loads(rt.tool(dict(action='respond', outcome='no_change', notify=False, summary='same'), session_id='sid'))['ok']
        # A silent verdict stays silent: the raw text never reaches the chat.
        assert rt.hook('transform_llm_output', response_text='Chatty output', session_id='sid', turn_id='turn') == '[SILENT]'
        rt.hook('on_session_end', session_id='sid', turn_id='turn', completed=True, failed=False, interrupted=False)
        o, = e.store.list_occurrences()
        assert o.state == 'done'
        assert e.store.get_session(KEY).busy_since is None
        assert len(e.ctx.calls) == 1
    finally:
        rt.close()


def test_late_end_after_unserve_keeps_the_successors_turn_busy(tmp_path, clock):
    rt = Runtime(GatewayCtx(tmp_path, settings=SETTINGS), clock=clock)
    try:
        e = bind(rt)
        assert rt.inactive() == ''
        arm(e)
        e.tick()
        rt.hook('pre_llm_call', session_id='sid', turn_id='turn', user_message=e.ctx.calls[-1][0], platform='telegram')
        e.ctx.runner.served = False
        # The profile's own gateway (another process) now runs a turn in the same chat.
        session(e, busy_since=e.clock.now(), busy_epoch='successor-engine')
        rt.hook('on_session_end', session_id='sid', turn_id='turn', completed=True, failed=False, interrupted=False)
        s = e.store.get_session(KEY)
        assert (s.busy_since, s.busy_epoch) == (e.clock.now(), 'successor-engine')
    finally:
        rt.close()


def test_engine_opened_before_the_injector_does_not_count_as_served(tmp_path, clock):
    ctx = GatewayCtx(tmp_path, settings=SETTINGS)
    ctx._manager.has_gateway_message_injector = False  # gateway still starting
    rt = Runtime(ctx, clock=clock)
    try:
        rt.hook('kanban_task_completed', task_id='t1', board='main')  # records, as before
        assert rt._engine is not None
        ctx._manager.has_gateway_message_injector = True
        ctx.runner.served = False
        assert rt.hook('kanban_task_completed', task_id='t2', board='main') is None
        assert len(rt.get_engine().store.pending_events()) == 1
    finally:
        rt.close()
