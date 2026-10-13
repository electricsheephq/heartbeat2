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
    # The joining blank line is not part of the first block, so it matches the standalone row's block.
    assert parts == [('1', hashlib.sha256(first.encode()).hexdigest()[:16]),
                     (None, hashlib.sha256((intake.ORIGIN + second).encode()).hexdigest()[:16])]
    assert parts[0][1] == intake.blocks(owner('first', 'u', '1'))[0][1]
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
    assert len(state.tracked[id(first)][1]) == 2 and state.tracked[id(first)][2] == new_fp
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


@pytest.mark.parametrize('rewrite', ['internal-wake', 'append-block'])
def test_rewrite_and_new_uid_row_with_the_old_text_in_one_request(engine, rewrite):
    """T4+T5 together: a still-present rewritten row is reconciled before any uid row may adopt it."""
    e = engine
    legacy = owner('old')
    anchor(e, [legacy])
    legacy['content'] += '\n\n' + ('internal wake text' if rewrite == 'internal-wake' else owner('new block')['content'])
    expected = 1 if rewrite == 'internal-wake' else 2
    before = humans(e)
    for _ in range(2):
        api(e, [legacy, owner('old', 'fresh')])
        assert humans(e) == before + expected


PAIR_OPS = ['keep', 'adopt-rebuilt', 'adopt-in-place', 'wake', 'append-twin', 'append-new', 'drop',
            'adopt-in-place-and-append']


def pair_cases():
    for twins in (True, False):
        for op_a in PAIR_OPS:
            for op_b in PAIR_OPS:
                for fresh in ('none', 'uid-twin', 'uid-new', 'uidless-twin', 'uidless-new'):
                    for reverse in (False, True):
                        yield twins, op_a, op_b, fresh, reverse


def run_pair_case(e, twins, op_a, op_b, fresh, reverse):
    """Counts for one case: (expected, [first hook, repeated hook, after every row is stamped]) or None if ambiguous."""
    c = f'{twins}-{op_a}-{op_b}-{fresh}-{reverse}'
    text = {'A': f'{c}-same' if twins else f'{c}-a', 'B': f'{c}-same' if twins else f'{c}-b'}
    rows = {'A': owner(text['A']), 'B': owner(text['B'])}
    turn = f'pairs-{c}'
    anchor(e, list(rows.values()), turn)
    history, expected, introduced = [], 0, []
    for name, op in (('A', op_a), ('B', op_b)):
        row = rows[name]
        if op == 'drop':
            continue
        if op == 'adopt-rebuilt':
            row = dict(row, message_uid=f'{c}-{name}')
        if op in ('adopt-in-place', 'adopt-in-place-and-append'):
            row['message_uid'] = f'{c}-{name}'
        if op == 'wake':
            row['content'] += '\n\ninternal wake text'
        if op == 'append-twin':
            row['content'] += '\n\n' + owner(text['A'])['content']
            expected += 1
            introduced.append(text['A'])
        if op in ('append-new', 'adopt-in-place-and-append'):
            row['content'] += '\n\n' + owner(f'{c}-{name}-new')['content']
            expected += 1
        history.append(row)
    if fresh != 'none':
        body = text['A'] if fresh.endswith('twin') else f'{c}-fresh'
        history.append(owner(body, f'{c}-fresh' if fresh.startswith('uid-') else None))
        expected += 1
        introduced.append(body)
    # Without a uid, a dropped legacy row and a new block with its text are indistinguishable.
    if {text[n] for n, op in (('A', op_a), ('B', op_b)) if op == 'drop'} & set(introduced):
        return None
    if reverse:
        history.reverse()
    before = humans(e)
    observed = []
    for _ in range(2):
        api(e, history, turn)
        observed.append(humans(e) - before)
    for row in history:
        row.setdefault('message_uid', f'{c}-stamped-{id(row)}')
    api(e, history, turn)
    observed.append(humans(e) - before)
    return expected, observed


def test_two_row_operation_pairs_count_each_new_block_once(engine):
    """Every pair of same-request operations on two accounted uid-less rows, plus an optional fresh row:
    each new owner block counts once on the first hook, and never again (repeat hook, later uid stamping)."""
    wrong = []
    checked = 0
    for case in pair_cases():
        result = run_pair_case(engine, *case)
        if result is None:
            continue
        checked += 1
        expected, observed = result
        if observed != [expected] * 3:
            wrong.append((case, expected, observed))
    assert checked > 1000
    assert not wrong, (len(wrong), wrong[:12])


@pytest.mark.parametrize('stamp', [False, True], ids=['uidless', 'stamped'])
@pytest.mark.parametrize('tail,expected', [('fresh', 1), ('third-legacy', 0)])
def test_a_merged_legacy_block_keeps_its_identity_whatever_follows(engine, tail, expected, stamp):
    """Hermes joins merged rows with a blank line; an absorbed block's fingerprint must not depend on what follows it."""
    e = engine
    first, absorbed_row, third = owner('first'), owner('absorbed'), owner('third')
    anchor(e, [first, absorbed_row] + ([third] if tail == 'third-legacy' else []))
    if stamp:
        first['message_uid'] = 'first'
    first['content'] += '\n\n' + absorbed_row['content'] + '\n\n' + (owner('last')['content'] if tail == 'fresh' else third['content'])
    before = humans(e)
    for _ in range(2):
        api(e, [first])
        assert humans(e) == before + expected
    first['message_uid'] = 'first'
    api(e, [first])
    assert humans(e) == before + expected
