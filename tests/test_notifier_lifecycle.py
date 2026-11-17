"""Review regressions: unload accounting/serialization and midnight reservations."""
from dataclasses import replace
from datetime import datetime, timedelta
import json
import threading

from test_engine import engine, KEY
from test_notifier import held, offer, settle
from heartbeat2.registration import Runtime, register


def test_inflight_unload_records_success_before_reload(engine,monkeypatch):
    monkeypatch.setattr(Runtime,'start',lambda s:None)
    rt=register(engine.ctx,clock=engine.clock)
    rt._engine=engine
    calls,r,_=held(engine,monkeypatch)
    entered=threading.Event(); release=threading.Event()
    closing=threading.Event(); successor_ready=threading.Event()
    def send(*a):
        calls.append(a); entered.set(); assert release.wait(3)
        return {'ok':True,'status':'queued'}
    offer(r,2,send=send)
    engine.clock.advance(600); engine.notifications.tick(engine)
    assert entered.wait(1)
    close=engine.notifications.close
    def tracked_close(): closing.set(); close()
    monkeypatch.setattr(engine.notifications,'close',tracked_close)
    closer=threading.Thread(target=rt.close)
    successors=[]
    def reload():
        successors.append(register(engine.ctx,clock=engine.clock)); successor_ready.set()
    successor=threading.Thread(target=reload)
    try:
        closer.start(); assert closing.wait(1)
        successor.start()
        assert not successor_ready.wait(.05), 'replacement admitted before old send settled'
        release.set(); closer.join(2); successor.join(2)
        assert not closer.is_alive() and not successor.is_alive()
        new=successors[0].get_engine()
        data=json.loads(new.store.get_meta('notify_episode:'+KEY))
        assert 'queued' in data and new.daily(KEY)['delivered']==1
        events=new.store.connection.execute("SELECT count(*) FROM events WHERE kind='notify'").fetchone()[0]
        assert events==1 and len(calls)==1
    finally:
        release.set(); closer.join(3)
        if successor.ident is not None: successor.join(3)
        for runtime in successors: runtime.close()


def test_inflight_notice_reserves_completion_day_across_midnight(engine,monkeypatch):
    _,r,_=held(engine,monkeypatch)
    engine.policy=replace(engine.policy,max_messages_per_day=1)
    tz=engine.policy.resolved_timezone()
    midnight=(datetime.fromtimestamp(engine.clock.now(),tz).replace(hour=0,minute=0,second=0,microsecond=0)
              +timedelta(days=1)).timestamp()
    engine.clock.advance(midnight-engine.clock.now()-1)
    entered=threading.Event(); release=threading.Event()
    def send(*a):
        entered.set(); assert release.wait(3)
        return {'ok':True}
    offer(r,2,send=send)
    try:
        engine.notifications.tick(engine); assert entered.wait(1)
        engine.clock.advance(2)
        assert engine.daily(KEY)['delivered']==1, 'in-flight reservation lost at midnight'
    finally:
        release.set(); settle(engine)
    assert engine.daily(KEY)['delivered']==1
    assert engine.store.get_session(KEY).unanswered_count==0


def test_cancel_then_fresh_hold_before_next_tick_starts_new_episode(engine,monkeypatch):
    from test_engine import arm,pending
    calls,_,rows=held(engine,monkeypatch)
    engine.clock.advance(600);engine.notifications.tick(engine);settle(engine)
    w,_=rows[0]
    engine.cancel_occurrences(w.id,'cancelled')
    assert engine.store.get_meta('notify_episode:'+KEY) is None
    w=arm(engine,platform='desktop');engine.dispatch(pending(engine,w),cache_line='')
    engine.clock.advance(600);engine.notifications.tick(engine);settle(engine)
    assert len(calls)==2


def test_five_second_send_does_not_block_tick(engine,monkeypatch):
    import time
    _,r,_=held(engine,monkeypatch)
    entered=threading.Event()
    def send(*a):
        entered.set();time.sleep(5)
        return {'ok':True}
    offer(r,2,send=send)
    engine.clock.advance(600)
    start=time.monotonic();engine.tick()
    assert time.monotonic()-start < 1 and entered.wait(1)
    start=time.monotonic();engine.tick()
    assert time.monotonic()-start < 1
    assert engine.notifications.idle.wait(7)
