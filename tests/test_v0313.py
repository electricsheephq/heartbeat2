"""Display-only regressions: desk, wake headers, history and dry-run previews."""
from dataclasses import replace

import pytest

from heartbeat2.model import EVENT_KINDS, PROBE_KINDS
from heartbeat2.wake_text import render_wake
from test_engine import engine, arm, pending, session, KEY
from test_v039 import command, snapshot
from test_wake_text import records


@pytest.mark.parametrize('kind,expected', [
    ('every', '1 wake, 0 watchers'), ('reaction', '0 wakes, 1 watcher'),
])
def test_desk_other_singular(engine, command, kind, expected):
    arm(engine, kind)
    assert '- Other: '+expected+' ·' in command('desk')
    arm(engine, kind)
    assert ('2 wakes, 0 watchers' if kind == 'every' else '0 wakes, 2 watchers') in command('desk')


@pytest.mark.parametrize('kind,label', [('owner_silent', 'check-in'), ('every', 'wake')])
@pytest.mark.parametrize('title', ['', ' \t '])
def test_empty_title_header_and_labels(clock, kind, label, title):
    w, o, s = records(clock)
    w = replace(w, kind=kind, title=title)
    header = render_wake(w, o, s, clock=clock).splitlines()[0]
    assert header == f'⟦pulse o:{o.id} a:2⟧ [HeartBeat 2.0 · {label} {w.id} · armed by you 1h ago]'
    for watcher in PROBE_KINDS | EVENT_KINDS:
        text = render_wake(replace(w, kind=watcher, title='Build'), o, s, clock=clock)
        assert f'· watcher {w.id} · Build ·' in text.splitlines()[0]
    text = render_wake(replace(w, kind='owner_silent', role=True), o, s, clock=clock)
    assert f'· role {w.id} · armed by' in text.splitlines()[0]


@pytest.mark.parametrize('held,expected', [
    ('held_unanswered', 'was held: waiting for your reply'),
    ('held_daily_cap', 'was held: waiting for daily capacity'),
    ('held_paused', 'was held: wake paused'),
    *[(prefix+code, 'message held: '+label)
      for prefix in ('message_held:', 'reported:')
      for code, label in [('quiet_hours', 'quiet hours'), ('daily_message_cap', 'daily message cap'),
                          ('unanswered', 'unanswered mute'), ('future_code', 'future code')]],
])
def test_why_past_hold(engine, command, held, expected):
    w = arm(engine)
    o = pending(engine, w)
    assert engine.store.update_occurrence(w.id, o.dedupe_key, 'pending', error=held, held_reason=held)
    assert engine.store.claim(w.id, o.dedupe_key, epoch=engine.epoch, pid=1)
    assert engine.store.start(w.id, o.dedupe_key, attempt=1)
    assert engine.store.cas_occurrence(w.id, o.dedupe_key, 'started', 'done', attempt=1, outcome='progress')
    row = command('why '+w.id).splitlines()[-1]
    assert ' · done · progress · ' in row and expected in row
    if held.startswith('held_'):
        duplicate = pending(engine, w, 't:still-held')
        assert engine.store.update_occurrence(w.id, duplicate.dedupe_key, 'pending', error=held, held_reason=held)
        row = command('why '+w.id).splitlines()[-1]
        assert row.count(expected.removeprefix('was ')) == 1 and 'was held:' not in row


def test_list_clips_filter_but_why_keeps_it(engine, command):
    value = 'm'*200
    w = arm(engine, 'reaction', trigger_json={'message_id': value})
    row = command('list')
    filters = row.split('reaction', 1)[1].split(' · ', 1)[0]
    assert len(filters) <= 60 and filters.endswith('…')
    assert 'Filters: on message '+value in command('why '+w.id)


def test_preview_pending_hold_matches_render(engine, command):
    w = arm(engine, 'every')
    o = pending(engine, w)
    assert engine.store.update_occurrence(w.id, o.dedupe_key, 'pending', error='busy')
    o = engine.store.get_occurrence(w.id, o.dedupe_key)
    before = snapshot(engine)
    preview = command('test '+w.id)
    rendered = engine.render_occurrence(w, replace(o, attempt=1), engine.store.get_session(KEY), held_by=o.error)
    why = 'Why: every (held by a busy chat)'
    assert why in preview and why in rendered
    assert snapshot(engine) == before and not engine.ctx.calls


def test_preview_user_silence_is_conditional(engine, command):
    session(engine, last_human_at=engine.clock.now()-60)
    w = arm(engine, 'owner_silent', creator='user', trigger_json={'after': '24h'})
    before = snapshot(engine)
    verdict = command('test '+w.id).splitlines()[1]
    assert verdict == 'Would run once the owner has been quiet long enough: yes'
    assert snapshot(engine) == before and not engine.ctx.calls
    engine.policy = replace(engine.policy, owner_silent_checkin=True)
    builtin = arm(engine, 'owner_silent', trigger_json={'builtin': True})
    assert command('test '+builtin.id).splitlines()[1].startswith(
        'Would run now: no — the owner has not been quiet long enough')
    pending(engine, w, 's:episode')
    assert command('test '+w.id).splitlines()[1] == 'Would run now: yes'
