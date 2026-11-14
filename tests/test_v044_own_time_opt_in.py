"""Own time in groups and topics is opt-in per chat (#177): each topic carries its own full context, so own time runs
only where the owner sends /pulse owntime on. Direct chats keep it by default."""
from dataclasses import replace
from types import SimpleNamespace

from heartbeat2 import commands
from heartbeat2.model import Wake
from test_engine import engine, KEY  # noqa: F401
from test_commands import runtime  # noqa: F401
from test_v043_own_time_groups import GROUP, own_id, group_chat


def _in(monkeypatch, key):
    monkeypatch.setattr(commands, 'session_env', lambda name: key if name == 'HERMES_SESSION_KEY' else '', raising=False)


def _on(e):
    e.policy = replace(e.policy, dm_only=False, own_time_hours=6)


def test_a_group_gets_no_own_time_until_the_owner_turns_it_on(runtime, monkeypatch):
    e = runtime.get_engine()
    _on(e)
    group_chat(e, on=False)
    e.tick()
    assert e.store.get_wake(own_id(KEY)).status == 'active'  # a direct chat has it by default
    assert e.store.get_wake(own_id(GROUP)) is None
    _in(monkeypatch, GROUP)
    assert commands.handle('owntime', runtime) == 'Own time: off in this group or topic until you turn it on (/pulse owntime on runs it here)'
    assert 'on in this chat, about every 6h' in commands.handle('owntime on', runtime)
    e.tick()
    assert e.store.get_wake(own_id(GROUP)).status == 'active'
    assert 'Own time: on in this chat' in commands.handle('status', runtime)


def test_only_the_topic_turned_on_gets_own_time(engine):
    e = engine
    _on(e)
    topics = [GROUP + ':' + str(n) for n in (1, 2, 3)]
    for key in topics: group_chat(e, key, on=False)
    e.store.set_meta('own_time_on:' + topics[1], '1')
    e.tick()
    assert [e.store.get_wake(own_id(key)) is not None for key in topics] == [False, True, False]


def test_own_time_already_armed_in_a_group_stops_until_turned_on(runtime, monkeypatch):
    # 0.3.15 and 0.4.2 armed own time in every group and topic; after upgrading it runs only where the owner turns it on.
    e = runtime.get_engine()
    _on(e)
    group_chat(e, on=False)
    e.store.create_wake(Wake(GROUP, 'This is your own time.', 'self', id=own_id(GROUP), platform='telegram', chat_type='group',
                             chat_id='team', armed_session_id='gsid', creator='user', title='Own time',
                             trigger_json={'builtin': True, 'own_time': True, 'hours': 6, 'floor': 3300},
                             base_interval_s=21600, eff_interval_s=21600, pacing_min_s=3300, pacing_max_s=43200,
                             next_due_at=e.clock.now()))
    e.tick()
    w = e.store.get_wake(own_id(GROUP))
    assert (w.status, w.status_reason) == ('cancelled', 'not_opted_in')
    assert not [c for c in e.ctx.calls if c[1] == GROUP]  # its due turn never went out
    assert all(o.state not in {'pending', 'claimed', 'started'} for o in e.store.list_occurrences(own_id(GROUP)))
    _in(monkeypatch, GROUP)
    commands.handle('owntime on', runtime)
    e.tick()
    w = e.store.get_wake(own_id(GROUP))
    assert (w.status, w.status_reason) == ('active', '') and w.next_due_at == e.clock.now() + 21600


def test_off_then_on_again_in_a_group(runtime, monkeypatch):
    e = runtime.get_engine()
    _on(e)
    group_chat(e, on=False)
    _in(monkeypatch, GROUP)
    commands.handle('owntime on', runtime)
    e.tick()
    commands.handle('owntime off', runtime)
    assert e.store.get_wake(own_id(GROUP)).status_reason == 'chat_off'
    e.tick()
    assert e.store.get_wake(own_id(GROUP)).status == 'cancelled'
    commands.handle('owntime on', runtime)
    e.tick()
    assert e.store.get_wake(own_id(GROUP)).status == 'active'


def test_dispatch_refuses_own_time_in_a_group_not_turned_on(engine):
    e = engine
    _on(e)
    group_chat(e)
    e.tick()
    e.store.set_meta('own_time_on:' + GROUP, '')  # turned on, armed, then the flag is gone (another poller, say)
    w = e.store.get_wake(own_id(GROUP))
    occurrence = SimpleNamespace(state='pending', held_since=None, error='', dedupe_key='t:1')
    assert e.gate(w, occurrence, e.store.get_session(GROUP))[0] == 'not_opted_in'
    assert e.own_time_refusal('dm', KEY) == ''  # a direct chat needs no opt-in
