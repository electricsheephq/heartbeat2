"""Own time arms only in direct chats, also with dm_only off (0.4.1)."""
from dataclasses import replace
import hashlib

from heartbeat2.model import Session
from test_engine import engine, session, KEY

GROUP = 'agent:telegram:group:team'


def own_id(key):
    return 'owntime_' + hashlib.sha256(key.encode()).hexdigest()[:16]


def group_chat(e):
    e.store.upsert_session(Session(GROUP, 'telegram', 'group', 'team', 'gsid', e.clock.now(), e.clock.now(), 'episode'))


def test_own_time_never_arms_in_a_group_chat(engine):
    e = engine
    e.policy = replace(e.policy, dm_only=False, own_time_hours=6)
    group_chat(e)
    e.tick()
    assert e.store.get_wake(own_id(KEY)) is not None and e.store.get_wake(own_id(KEY)).status == 'active'
    assert e.store.get_wake(own_id(GROUP)) is None


def test_a_group_own_time_wake_from_before_is_cancelled(engine):
    e = engine
    e.policy = replace(e.policy, dm_only=False, own_time_hours=6)
    group_chat(e)
    from heartbeat2.model import Wake
    old = Wake(GROUP, 'This is your own time.', 'self', id=own_id(GROUP), platform='telegram', chat_type='group',
               chat_id='team', armed_session_id='gsid', creator='user', title='Own time',
               trigger_json={'builtin': True, 'own_time': True, 'hours': 6, 'floor': 3300},
               base_interval_s=21600, eff_interval_s=21600, pacing_min_s=3300, pacing_max_s=43200,
               next_due_at=e.clock.now()+21600)
    e.store.create_wake(old)
    e.tick()
    w = e.store.get_wake(own_id(GROUP))
    assert (w.status, w.status_reason) == ('cancelled', 'not_dm')
    assert e.store.get_wake(own_id(KEY)).status == 'active'
    e.tick()
    assert e.store.get_wake(own_id(GROUP)).status == 'cancelled'  # Never reactivated.
