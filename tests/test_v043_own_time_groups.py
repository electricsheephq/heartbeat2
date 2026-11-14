"""Own time runs wherever the agent may write, groups and topics included when dm_only is off, and the owner switches it
per chat with /pulse owntime (#175; reverts 0.4.1's direct-chat-only rule)."""
from dataclasses import replace
import hashlib

from heartbeat2 import commands
from heartbeat2.model import Session, Wake
from test_engine import engine, KEY  # noqa: F401
from test_commands import runtime  # noqa: F401

GROUP = 'agent:telegram:group:team'


def own_id(key):
    return 'owntime_' + hashlib.sha256(key.encode()).hexdigest()[:16]


def group_chat(e, key=GROUP):
    e.store.upsert_session(Session(key, 'telegram', 'group', 'team', 'gsid', e.clock.now(), e.clock.now(), 'episode'))


def cancelled_by_041(e):
    e.store.create_wake(Wake(GROUP, 'This is your own time.', 'self', id=own_id(GROUP), platform='telegram', chat_type='group',
                             chat_id='team', armed_session_id='gsid', creator='user', title='Own time', status='cancelled',
                             status_reason='not_dm', trigger_json={'builtin': True, 'own_time': True, 'hours': 6, 'floor': 3300},
                             base_interval_s=21600, eff_interval_s=21600, pacing_min_s=3300, pacing_max_s=43200))


def test_own_time_arms_in_a_group_when_dm_only_is_off(engine):
    e = engine
    e.policy = replace(e.policy, dm_only=False, own_time_hours=6)
    group_chat(e)
    e.tick()
    assert e.store.get_wake(own_id(KEY)).status == 'active'
    assert e.store.get_wake(own_id(GROUP)).status == 'active'


def test_with_dm_only_on_a_group_gets_no_own_time(engine):
    e = engine
    e.policy = replace(e.policy, dm_only=False, own_time_hours=6)
    group_chat(e)
    e.tick()
    e.policy = replace(e.policy, dm_only=True)
    e.tick()
    w = e.store.get_wake(own_id(GROUP))
    assert (w.status, w.status_reason) == ('cancelled', 'dm_only')
    assert e.store.get_wake(own_id(KEY)).status == 'active'
    e.policy = replace(e.policy, dm_only=False)
    e.tick()
    assert e.store.get_wake(own_id(GROUP)).status == 'active'


def test_own_time_cancelled_by_0_4_1_comes_back(engine):
    e = engine
    e.policy = replace(e.policy, dm_only=False, own_time_hours=6)
    group_chat(e)
    cancelled_by_041(e)
    e.tick()
    w = e.store.get_wake(own_id(GROUP))
    assert (w.status, w.status_reason) == ('active', '') and w.next_due_at == e.clock.now() + 21600


def test_the_owner_switches_own_time_per_chat(runtime):
    e = runtime.get_engine()
    e.policy = replace(e.policy, own_time_hours=6)
    e.tick()
    assert e.store.get_wake(own_id(KEY)).status == 'active'
    assert commands.handle('owntime', runtime) == 'Own time: on in this chat, about every 6h (/pulse owntime off turns it off here)'
    assert 'off in this chat' in commands.handle('owntime off', runtime)
    e.tick()
    w = e.store.get_wake(own_id(KEY))
    assert (w.status, w.status_reason) == ('cancelled', 'chat_off')
    e.tick()
    assert e.store.get_wake(own_id(KEY)).status == 'cancelled'  # stays off
    assert 'Own time: off in this chat' in commands.handle('status', runtime)
    assert 'on in this chat' in commands.handle('owntime on', runtime)
    e.tick()
    assert e.store.get_wake(own_id(KEY)).status == 'active'
    assert 'Own time: on in this chat' in commands.handle('status', runtime)


def test_one_topic_off_leaves_the_others_on(engine):
    e = engine
    e.policy = replace(e.policy, dm_only=False, own_time_hours=6)
    topics = [GROUP + ':' + str(n) for n in (1, 2)]
    for key in topics: group_chat(e, key)
    e.tick()
    e.store.set_meta('own_time_off:' + topics[0], '1')
    e.tick()
    assert e.store.get_wake(own_id(topics[0])).status_reason == 'chat_off'
    assert e.store.get_wake(own_id(topics[1])).status == 'active'


def test_status_and_help_show_own_time(runtime):
    e = runtime.get_engine()
    assert 'Own time:' not in commands.handle('status', runtime)  # own_time_hours 0: no line
    e.policy = replace(e.policy, own_time_hours=12)
    assert 'Own time: on in this chat, about every 12h' in commands.handle('status', runtime)
    assert 'owntime on|off' in commands.HELP
    e.policy = replace(e.policy, own_time_hours=0)
    assert commands.handle('owntime', runtime) == 'Own time: off for this profile (own_time_hours is 0 in settings)'
