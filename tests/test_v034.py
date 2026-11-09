"""0.3.4: a running wake turn shows as a message in progress, not a sent one; summaries are asked for in plain words."""
from types import SimpleNamespace

from heartbeat2 import commands
from heartbeat2.desk import render_desk
from test_engine import KEY, arm, end, engine, pending, start


def _desk(e):
    return render_desk(e.store, e.store.get_session(KEY), clock=e.clock,
                       timezone=e.policy.resolved_timezone(), policy=e.policy)


def _status(e, monkeypatch):
    monkeypatch.setattr(commands, 'session_env', lambda name: KEY if name == 'HERMES_SESSION_KEY' else '')
    return commands.handle('status', SimpleNamespace(get_engine=lambda: e))


def _role(e):
    return arm(e, 'every', role=True, title='Notes', trigger_json={'kind': 'every', 'every': '2m'})


def test_a_running_turn_is_in_progress_on_the_desk_status_and_its_own_wake_text(engine, monkeypatch):
    e = engine
    role = _role(e)
    e.ledger(role, delivered=2)
    assert 'messages today 2/6 ·' in _desk(e)
    e.dispatch(pending(e, role))
    text = e.ctx.calls[-1][0]
    assert 'messages today 2/6 (+1 in progress) ·' in text  # the desk inside the turn's own wake text
    assert 'wake messages today: 2 of 6' in text            # agrees with the line the agent already had
    start(e)
    assert 'messages today 2/6 (+1 in progress) ·' in _desk(e)
    assert '· messages 2/6 (+1 in progress) ·' in _status(e, monkeypatch)
    end(e, 'progress', True, summary='Drafted the notes.')
    out = _desk(e)
    assert 'messages today 3/6 ·' in out and 'in progress' not in out
    assert '· messages 3/6 ·' in _status(e, monkeypatch)


def test_the_turn_that_takes_the_last_message_is_not_told_its_message_is_held(engine):
    e = engine
    role = _role(e)
    e.ledger(role, delivered=5)
    e.dispatch(pending(e, role))
    text = e.ctx.calls[-1][0]
    assert 'messages today 5/6 (+1 in progress) ·' in text
    assert 'Messages held now' not in text and 'Messages are held right now' not in text
    start(e)
    # The owner's own desk during that turn: any other wake's message would be held now.
    out = _desk(e)
    assert 'messages today 5/6 (+1 in progress) ·' in out and "Messages held now: today's message limit" in out
    end(e, 'progress', True, summary='Drafted the notes.')
    out = _desk(e)
    assert 'messages today 6/6 ·' in out and "Messages held now: today's message limit" in out


def test_a_turn_whose_messages_are_held_holds_no_message_slot(engine):
    e = engine
    role = _role(e)
    e.ledger(role, delivered=2)
    e.store.set_meta('quiet:' + KEY, '{"range":"00:00-23:59"}')
    e.dispatch(pending(e, role))
    text = e.ctx.calls[-1][0]
    assert 'Messages are held right now (quiet hours)' in text
    assert 'messages today 2/6 ·' in text and 'in progress' not in text


def test_the_contract_asks_for_a_plain_sentence_summary(engine):
    e = engine
    e.dispatch(pending(e, _role(e)))
    assert 'summary of what you did, written as a plain sentence, since the owner sees role summaries on the desk' in e.ctx.calls[-1][0]


def test_a_turn_held_by_the_message_limit_still_sees_the_limit_on_its_desk(engine):
    e = engine
    role = _role(e)
    e.ledger(role, delivered=6)
    e.dispatch(pending(e, role))
    text = e.ctx.calls[-1][0]
    assert "Messages are held right now (today's message limit)" in text
    assert "messages today 6/6 ·" in text and 'in progress' not in text
    assert "Messages held now: today's message limit" in text
