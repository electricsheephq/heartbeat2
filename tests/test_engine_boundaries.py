"""Required attempt, accounting, lifecycle and permission boundaries."""
from dataclasses import replace
from test_engine import engine,arm,session,start,end,KEY
from heartbeat2.model import Session


def test_stale_marker_cannot_supersede_new_attempt(engine):
    e=engine
    w=arm(e)
    e.tick()
    old=e.ctx.calls[-1][0]
    o=e.store.list_occurrences(w.id)[0]
    e.store.refund(w.id,o.dedupe_key,attempt=1)
    e.store.claim(w.id,o.dedupe_key,epoch=e.epoch,pid=1)
    start(e,message=old)
    assert e.store.get_occurrence(w.id,o.dedupe_key).state=='claimed'
    assert e.intake.transform_llm_output(response_text='stale',session_id='sid')=='[SILENT]'


def test_unreported_chatty_output_is_silent_by_default(engine):
    e=engine
    arm(e);e.tick();start(e)
    assert e.intake.transform_llm_output(response_text='I have a long unsolicited update.',session_id='sid')=='[SILENT]'


def test_interrupted_model_turn_consumes_daily_cap(engine):
    e=engine
    e.policy=replace(e.policy,max_model_wakes_per_day=1)
    arm(e);e.tick();start(e)
    e.intake.post_api_request(session_id='sid',usage={'input_tokens':100,'output_tokens':20})
    e.intake.on_session_end(session_id='sid',failed=True,completed=False)
    e.clock.advance(601)
    w=arm(e);e.tick()
    assert len(e.ctx.calls)==1
    assert e.store.list_occurrences(w.id)[0].error=='daily_model_cap'
    assert e.store.get_wake(e.store.list_occurrences()[0].wake_id).tokens_in==100


def test_new_after_human_compaction_orphans_unfired_wake(engine):
    e=engine
    w=arm(e,next_due_at=e.clock.now()+7200)
    e.compression_tip=lambda sid:'compressed'
    e.intake.pre_llm_call(session_id='compressed',user_message='human')
    e.intake.on_session_end(session_id='compressed',completed=True)
    e.intake.on_session_reset(old_session_id='compressed',new_session_id='new')
    assert e.store.get_wake(w.id).status=='orphaned'


def test_invalid_respond_does_not_stage_scratch(engine):
    e=engine
    w=arm(e)
    e.tick();start(e)
    result=e.tool.handle({'action':'respond','notify':False,'outcome':'no_change','scratch':'must not persist','next_check':'1m'},session_id='sid')
    assert result['error']=='invalid_request'
    end(e)
    assert e.store.get_wake(w.id).scratch==''


def test_successful_silent_progress_is_zero_value_even_without_transform(engine):
    e=engine
    w=arm(e,'every')
    e.tick();start(e)
    e.tool.handle({'action':'respond','notify':False,'outcome':'progress','summary':'internal'},session_id='sid')
    e.intake.on_session_end(session_id='sid',completed=True)
    assert e.store.get_wake(w.id).noop_streak==1


def test_unknown_parent_event_never_uses_foreign_ambient_session(engine):
    e=engine
    e.intake.event('subagent_done',parent_session_id='foreign-parent',child_summary='completed')
    assert e.store.pending_events()==[]


def test_changed_user_floor_applies_to_existing_recurring_wake(engine):
    e=engine
    w=arm(e,'every')
    e.tick();start(e);end(e,'progress',True)
    e.policy=replace(e.policy,min_interval_minutes=120)
    e.clock.advance(3600);e.tick()
    assert e.store.get_wake(w.id).eff_interval_s>=7200
    assert e.store.get_wake(w.id).next_due_at>=e.clock.now()+7200


def test_paused_after_claim_cannot_deliver(engine):
    e=engine
    w=arm(e);e.tick();start(e)
    e.tool.handle({'action':'respond','notify':True,'outcome':'progress','summary':'update'},session_id='sid')
    e.store.cas_wake(w.id,'active',status='paused')
    assert e.intake.transform_llm_output(response_text='update',session_id='sid')=='[SILENT]'


def test_cron_governor_skips_exact_slots_and_babysit_three_noops(engine):
    e=engine
    e.policy=replace(e.policy,noop_slowdown_after=1)
    w=arm(e,'cron',trigger_json={'cron':'0 * * * *'})
    e.tick();start(e);end(e)
    skips=[o for o in e.store.list_occurrences(w.id) if o.state=='skipped' and o.error=='governor']
    assert len(skips)==1
    e.store.cas_wake(w.id,'active',status='paused')
    session(e,last_wake_turn_at=None)
    e.policy=replace(e.policy,noop_slowdown_after=3)
    b=arm(e,'every',babysit=True,base_interval_s=900,eff_interval_s=900)
    for _ in range(3):
        e.tick();start(e);end(e)
        e.clock.advance(e.store.get_wake(b.id).eff_interval_s)
    assert e.store.get_wake(b.id).status=='done'


def test_restart_does_not_reset_three_accepted_attempt_budget(engine):
    from heartbeat2.poller import Poller
    e=engine
    w=arm(e)
    for _ in range(3):
        e.tick()
        # Restart before the ten-minute acknowledgement gate runs.
        e.close()
        e=Poller(e.ctx,e.store,e.policy,e.clock,paused=lambda:False,jitter=lambda:0)
        e.clock.advance(601)
    try:
        e.tick()
        assert len(e.ctx.calls)==3
        assert e.store.list_occurrences(w.id)[0].state=='dead'
    finally:
        e.close()


def test_human_activity_resets_existing_rearm_chain_depth(engine):
    e=engine
    w=arm(e,depth=2)
    e.clock.advance(1)
    e.intake.post_gateway_admission(session_key=KEY,platform='telegram',source={'chat_type':'dm','chat_id':'unit'},message_id='new-human',text='continue')
    assert e.store.get_wake(w.id).depth==0
