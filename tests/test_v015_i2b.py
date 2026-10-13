"""Owner text that quotes a compaction marker is still owner text; carriers and replays are not."""
import pytest

from heartbeat2.intake import ORIGIN, origin_rows, row_text
from test_engine import arm, engine, session
from test_u11c import hermes_origin, humans, steer_row


# agent/context_compressor.py at the three supported Hermes pins.
END = '--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---'
HEADER = '[PRIOR CONTEXT — for reference only; not a new message]'
DELIMITER = '[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]'
REPLAY = ('[STILL IN PROGRESS — this is the active request, restated after the compaction boundary '
          'because it was not finished yet. Continue it; do not start over.]')


def merged(row, force, parts=False, summary='Earlier work was completed.'):
    old = row['content']
    summary = '[CONTEXT COMPACTION — REFERENCE ONLY]\n' + summary
    prefix, suffix = ((summary + '\n\n' + END + '\n\n', '') if force else
                      (HEADER + '\n', '\n\n' + DELIMITER + '\n\n' + summary + '\n\n' + END))
    content = prefix + old + suffix
    if parts:
        content = [{'type': 'text', 'text': prefix}, {'type': 'text', 'text': old}]
        if suffix:
            content.append({'type': 'text', 'text': suffix})
    return {**row, 'content': content, '_compressed_summary': True}


def api(e, history):
    e.intake.pre_api_request(session_id='sid', turn_id='wake', conversation_history=history)


def begin_wake(e, history, platform):
    session(e, platform=platform)
    w = arm(e, 'every', platform=platform)
    e.tick()
    row = {'role': 'user', 'content': e.ctx.calls[-1][0], 'display_kind': 'internal_notification'}
    history = history + [row]
    e.intake.pre_llm_call(session_id='sid', turn_id='wake', user_message=row['content'],
                          conversation_history=history, platform=platform)
    return w, row, history


def silent_reply(e):
    assert e.tool.handle({'action': 'respond', 'outcome': 'no_change', 'notify': False}, session_id='sid')['ok']
    return e.intake.transform_llm_output(session_id='sid', turn_id='wake', response_text='Chatty wake text')


@pytest.mark.parametrize('platform,mid', [('telegram', '501'), ('signal', None)])
@pytest.mark.parametrize('wrapped', [False, True], ids=['origin', 'steer'])
@pytest.mark.parametrize('quote', ['', '--- END OF CONTEXT SUMMARY', END, HEADER, DELIMITER, REPLAY],
                         ids=['control', 'end-prefix', 'end-full', 'prior-header', 'delimiter', 'replay-header'])
def test_marker_quoting_owner_steer_counts_and_folds(engine, platform, mid, wrapped, quote):
    e = engine
    _, row, history = begin_wake(e, [], platform)
    text = hermes_origin('Here is the log: ' + quote + ' — why?', platform, mid)
    owner = steer_row(text) if wrapped else {'role': 'user', 'content': text}
    for _ in range(2):
        api(e, history + [owner])
    assert humans(e) == 1
    assert e.intake.flights['sid'].owner_replied
    assert silent_reply(e) is None, 'direct owner answer was silenced'


@pytest.mark.parametrize('force', [False, True])
def test_same_turn_merge_and_unwrap_stays_silent(engine, force):
    e = engine
    owner = steer_row(hermes_origin('Already carried.', 'signal'))
    _, row, _ = begin_wake(e, [owner], 'signal')
    api(e, [merged(owner, force), row])
    api(e, [owner, row])
    assert humans(e) == 0
    assert not e.intake.flights['sid'].owner_replied
    assert silent_reply(e) == '[SILENT]'


@pytest.mark.parametrize('metadata', [False, True])
@pytest.mark.parametrize('prefix', ['[CONTEXT COMPACTION — REFERENCE ONLY]', '[CONTEXT SUMMARY]:'])
def test_summary_body_quoting_origin_is_never_fingerprinted(metadata, prefix):
    text = prefix + '\n' + hermes_origin('Quoted owner.', 'signal') + '\n\n' + END
    assert list(origin_rows([{'role': 'user', 'content': text, '_compressed_summary': metadata}])) == []


@pytest.mark.parametrize('force', [None, False, True], ids=['standalone', 'delimiter', 'force-leading'])
def test_replay_copy_never_counts_twice(engine, force):
    e = engine
    _, row, history = begin_wake(e, [], 'signal')
    owner = steer_row(hermes_origin('Current owner steer.', 'signal'))
    api(e, history + [owner])
    assert humans(e) == 1
    replay = {'role': 'user', 'content': REPLAY + '\n' + owner['content']}
    if force is not None:
        replay = merged(replay, force)
    api(e, history + [owner, replay])
    assert humans(e) == 1
    assert list(origin_rows([replay])) == []


def test_replay_header_wins_over_quoted_end_marker():
    quoted = hermes_origin('Quoted owner log.', 'signal')
    replay = {'role': 'user', 'content': REPLAY + '\n' + END + '\n' + quoted}
    assert list(origin_rows([replay])) == []
