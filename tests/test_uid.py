"""UID accounting, adoption and the amended uid-less fallback (#16)."""
from collections import Counter
import copy
import hashlib
import random

import pytest

from heartbeat2 import intake
from test_engine import KEY, engine, session
from test_u11c import hermes_origin, humans


def owner(body='same', uid=None, mid=None):
    row = {'role': 'user', 'content': hermes_origin(body, 'signal', mid), 'display_kind': 'steer'}
    if uid is not None:
        row['message_uid'] = uid
    return row


def anchor(e, rows, turn='turn'):
    session(e, platform='signal')
    history = rows + [{'role': 'user', 'content': 'internal wake', 'display_kind': 'internal_notification'}]
    e.intake.pre_llm_call(session_id='sid', turn_id=turn, platform='signal',
                          user_message='internal wake', conversation_history=history)
    return history


def api(e, rows, turn='turn'):
    e.intake.pre_api_request(session_id='sid', turn_id=turn, conversation_history=rows)


def test_pure_helpers():
    row = owner('first', 'u', '1')
    row['content'] += '\n\n' + owner('second')['content']
    parts = intake.blocks(row)
    first, second = row['content'].split('\n\n' + intake.ORIGIN)
    assert parts == [('1', hashlib.sha256((first + '\n\n').encode()).hexdigest()[:16]),
                     (None, hashlib.sha256((intake.ORIGIN + second).encode()).hexdigest()[:16])]
    assert intake.uid(row) == 'u'
    for value in [None, '', 3, [], {}]:
        assert intake.uid({'message_uid': value}) is None
    for value in [None, 'u', ['u', 3]]:
        assert intake.absorbed({'_absorbed_message_uids': value}) == []
    assert intake.absorbed({'_absorbed_message_uids': ['a', 'b']}) == ['a', 'b']
    assert intake.blocks(owner('bad').__class__(content=intake.ORIGIN + '\nno JSON\nbody')) == []


@pytest.mark.parametrize('case', ['same-after-drop', 'merged-known', 'merge-before-flush', 'legacy-rewrite'])
def test_identity_branches(engine, case):
    e = engine
    a, b = owner('same', 'a'), owner('other', 'b')
    if case == 'legacy-rewrite':
        a.pop('message_uid')
    rows = anchor(e, [a, b] if case == 'merged-known' else [a])
    if case == 'same-after-drop':
        api(e, [owner('same')])
        assert humans(e) == 1
    elif case == 'merged-known':
        a['content'] += '\n\n' + b['content']
        a['_absorbed_message_uids'] = ['b']
        api(e, [a])
        assert humans(e) == 0
    elif case == 'merge-before-flush':
        fresh = owner('new')
        api(e, rows + [fresh])
        assert humans(e) == 1
        # The block fingerprint includes the literal text through row end;
        # appending this block preserves its own bytes.
        a['content'] += '\n\n' + fresh['content']
        api(e, [a])
        assert humans(e) == 1
    else:
        a['content'] += '\n\ninternal wake'
        a['_absorbed_message_uids'] = ['wake']
        api(e, [a])
        assert humans(e) == 0


def test_issued_occupancy_and_tracked_adoption(engine):
    e = engine
    rows = anchor(e, [])
    first = owner()
    api(e, rows + [first])
    assert humans(e) == 1
    state = e.intake.anchors['sid'][1]
    fp = next(iter(intake.origin_rows([first])))[0]
    assert state.occupancy[fp] == state.issued[fp] == 1
    assert state.tracked[id(first)][0] is first
    first['message_uid'] = 'first'
    second = owner()
    api(e, rows + [first, second])
    assert humans(e) == 2
    assert state.occupancy[fp] == 1 and state.issued[fp] == 2
    assert id(first) not in state.tracked
    assert state.unadopted == Counter({intake.blocks(second)[0][1]: 1})
    second['message_uid'] = 'second'
    api(e, rows + [first, second])
    assert humans(e) == 2 and state.occupancy[fp] == 0 and state.issued[fp] == 2
    assert not +state.unadopted


def test_tracked_growth_moves_occupancy_and_never_writes_rows(engine):
    e = engine
    first = owner('old')
    anchor(e, [first])
    state = e.intake.anchors['sid'][1]
    old_fp = next(iter(intake.origin_rows([first])))[0]
    first['content'] += '\n\n' + owner('new', mid='new')['content']
    before = copy.deepcopy(first)
    api(e, [first])
    assert first == before and humans(e) == 1
    new_fp = next(iter(intake.origin_rows([first])))[0]
    assert state.occupancy[old_fp] == 0 and state.occupancy[new_fp] == 1
    assert state.tracked[id(first)][1:] == (2, new_fp)
    api(e, [first])
    assert humans(e) == 1


def test_adoption_of_rebuilt_row_consumes_pending(engine):
    e = engine
    row = owner()
    anchor(e, [row])
    rebuilt = dict(row, message_uid='new-uid')
    api(e, [rebuilt])
    state = e.intake.anchors['sid'][1]
    fp = next(iter(intake.origin_rows([row])))[0]
    assert humans(e) == 0 and state.occupancy[fp] == 0 and not +state.unadopted
    assert state.issued[fp] == 1


def test_next_turn_replay_and_filtering(engine):
    e = engine
    row = owner('new', 'u')
    rows = anchor(e, [])
    api(e, rows + [row])
    assert humans(e) == 1
    anchor(e, [copy.deepcopy(row)], 'next')
    api(e, [copy.deepcopy(row)], 'next')
    assert humans(e) == 1
    for fields in [{'role': 'assistant'}, {'_compressed_summary': True},
                   {'content': intake.COMPACTION_MARKERS[0] + row['content']}]:
        api(e, [dict(owner('excluded', 'excluded'), **fields)], 'next')
    assert humans(e) == 1


def old_oracle(histories):
    """v0.1.8 pre_api counting copy: max fingerprint multiplicity, row-scoped id-less key."""
    baseline = Counter(fp for fp, _ in intake.origin_rows(histories[0]))
    seen = set()
    for history in histories[1:]:
        rows = list(intake.origin_rows(history))
        current = Counter(fp for fp, _ in rows)
        new = current - baseline
        texts = dict(rows)
        for fp, n in new.items():
            for ordinal in range(baseline[fp] + 1, baseline[fp] + n + 1):
                for mid in intake.origin_ids(texts[fp]):
                    seen.add(('mid', mid) if mid else ('row', fp, ordinal))
        baseline.update(new)
    return len(seen)


@pytest.mark.parametrize('with_uids', [False, True])
def test_rewrite_free_differential_500_cases(engine, with_uids):
    rng = random.Random(1601)
    e = engine
    for case in range(500):
        # Stable, append-only dicts; unique text keeps UID and fingerprint identities comparable.
        history = [owner(f'anchor-{case}-{n}', f'a-{case}-{n}' if with_uids else None)
                   for n in range(rng.randrange(4))]
        turn = f'diff-{with_uids}-{case}'
        anchor(e, history, turn)
        histories = [list(history)]
        before = humans(e)
        for step in range(rng.randrange(1, 6)):
            for n in range(rng.randrange(4)):
                history.append(owner(f'fresh-{case}-{step}-{n}',
                                     f'u-{case}-{step}-{n}' if with_uids else None,
                                     f'm-{case}-{step}-{n}' if rng.random() < .3 else None))
            histories.append(list(history))
            api(e, history, turn)
            api(e, history, turn)
        assert humans(e) - before == old_oracle(histories), (with_uids, case)


def test_quoted_origin_header_is_not_a_block_boundary():
    quoted = 'first line\n' + intake.ORIGIN + '\nquoted without JSON\n'
    one, two = owner(quoted + 'suffix one'), owner(quoted + 'suffix two')
    assert len(intake.blocks(one)) == 1
    assert intake.blocks(one)[0][1] != intake.blocks(two)[0][1]


def test_blocks_appended_to_a_tracked_uidless_row_each_count(engine):
    e = engine
    a = owner('base')
    anchor(e, [a])
    before = humans(e)
    a['content'] += '\n\n' + owner('steer one')['content'] + '\n\n' + owner('steer two')['content']
    api(e, [a])
    assert humans(e) == before + 2


def test_turn_end_and_reset_release_the_anchor(engine):
    e = engine
    anchor(e, [owner('base')])
    e.intake.on_session_end(session_id='sid', turn_id='other-turn', completed=True)
    assert 'sid' in e.intake.anchors  # another turn's end leaves this turn's anchor alone
    e.intake.on_session_end(session_id='sid', turn_id='turn', completed=True)
    assert 'sid' not in e.intake.anchors
    anchor(e, [owner('base')], turn='turn2')
    e.intake.on_session_reset(old_session_id='sid', new_session_id='sid-new')
    assert 'sid' not in e.intake.anchors and 'sid-new' not in e.intake.anchors


def test_stop_without_a_turn_id_keeps_the_anchor_for_the_unwinding_turn(engine):
    e = engine
    anchor(e, [owner('base')])
    e.intake.agent_loop_stopped(session_key=KEY)               # gateway /stop, before the worker unwinds
    e.intake.on_session_end(session_id='sid', interrupted=True)  # TUI teardown: no turn id
    before = humans(e)
    api(e, [owner('base'), owner('accepted steer')])           # the worker's last prepared request
    assert humans(e) == before + 1
    e.intake.on_session_end(session_id='sid', turn_id='turn', interrupted=True)  # the finalizer names the turn
    assert 'sid' not in e.intake.anchors


def test_new_uid_row_repeating_a_present_legacy_row_counts(engine):
    """T4: the legacy uid-less row is still there, so the uid row is a new message, not its adoption."""
    e = engine
    legacy = owner('same')
    anchor(e, [legacy])
    before = humans(e)
    api(e, [legacy, owner('same', 'n1')])
    assert humans(e) == before + 1


@pytest.mark.parametrize('adopt', [False, True], ids=['rewritten-uidless', 'rewritten-then-adopted'])
def test_rewrite_retires_the_original_fingerprint(engine, adopt):
    """T5: after a tracked row is rewritten, a later message matching its old text is new."""
    e = engine
    legacy = owner('old request')
    anchor(e, [legacy])
    legacy['content'] += '\n\ninternal wake text'  # Hermes merges the wake row in place
    if adopt:
        legacy['message_uid'] = 'l1'
    api(e, [legacy])
    before = humans(e)
    api(e, [legacy, owner('old request', 'm1')])
    assert humans(e) == before + 1
