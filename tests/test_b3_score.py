from copy import deepcopy
from datetime import date, datetime
import json
import pytest
from bench.soak.schedule import build, digest
from bench.soak.score import score, attribute, matching


def corpus():
    s = build(date(2026, 10, 15), 'Asia/Tokyo')
    log, wakes, occurrences, verdicts = [], [], [], []
    session = dict(session_key='chat', current_session_id='s', enabled=1, unanswered_count=0)
    mid = 0
    def snap(): return deepcopy(dict(wakes=wakes, occurrences=occurrences, sessions=[session]))
    def emit(kind, e=None, at=None, **kw):
        log.append(dict(t=datetime.fromtimestamp(at or 1).astimezone().isoformat(), kind=kind, episode=e, **deepcopy(kw)))
    emit('declaration', start=s['start'], tz=s['tz'], schedule_sha256=s['sha256'], seed=s['seed'], frozen=dict(allow_gateway_injection=True,hermes_version='1.0',hermes_commit='a'*40,resolved_timezone='Asia/Tokyo'))
    emit('baseline_setup', session=session, effective_quiet='22:00-08:00', starting_transcript=[], transcript_sha256=digest([]))
    for e in s['episodes']:
        eid, k = e['id'], e['kind']; start = datetime.fromisoformat(e['sends'][0]['at']).timestamp()
        emit('episode_start', eid, start, schedule=e, session=session, frozen={}, gateway_starts=[], snapshot=snap())
        if k == 'W':
            emit('file_write', eid, start, mode='wb', path='fixtures/'+e['job']+'.status', bytes='RUNNING\n')
            flip = datetime.fromisoformat(e['flip']).timestamp()
            emit('file_write', eid, flip, mode='ab', key='flip', path='fixtures/'+e['job']+'.status', bytes='DONE\n')
            emit('baseline', eid, start+300, key=eid+'-0', probe_state_json={'size': 8}, first_probe_at=start+295)
        current = []
        for n, send in enumerate(e['sends']):
            at = datetime.fromisoformat(send['at']).timestamp(); mid += 1; before = snap()
            w = None
            if k != 'N':
                due = datetime.fromisoformat(e['due'][n]).timestamp() if e['due'] else start+3600 if k == 'S' else flip+300
                term = e['tasks'][n]['term'] if e['tasks'] else 'DONE' if k == 'W' else 'check'
                w = dict(id=eid+f'-{n}', created_at=at, kind='file' if k == 'W' else 'every' if k == 'S' else 'at', status='active', status_reason='', note=term, owner_requested=1,
                         next_due_at=due, cancel_on_reply=k.startswith('C'), deadline_at=None, eff_interval_s=3600, max_fires=None, expires_at=None,
                         trigger_json={'path': 'fixtures/'+e.get('job', '')+'.status', 'pattern': '^DONE$', 'check_every': 300}, session_key='chat')
                wakes.append(w); current.append(w)
            ps = [{'args': {'action': 'create'}, 'result': {'ok': True, 'wake': deepcopy(w), 'receipt': 'Fires once.'}}] if w else []
            owner = dict(role='user', timestamp=at, content=send['text'])
            message = dict(id=mid, date=send['at'], edit_date=None, out=True, text=send['text'])
            emit('send', eid, at, key=f'request:{n}', purpose='request', message=message, before=before)
            emit('turn', eid, at+10, key=f'request:{n}', purpose='request', message_id=mid, owner=owner, rows=[dict(role='assistant', timestamp=at+1, content='Set.' if w else 'Okay.', tool_calls=None)], final_text='Set.' if w else 'Okay.', pulse_results=ps, snapshot=snap(), before=before)
            verdicts.append(dict(id=f'a-{mid}', useful=True, fabrication=False, narration=False, overpromise=False, why=''))
            mid += 1
            emit('dm', eid, at+30, message=dict(id=mid, date=datetime.fromtimestamp(at+1).astimezone().isoformat(), edit_date=None, out=False, text='Set.' if w else 'Okay.'))
            if not w: continue
            gate_at = flip-30 if k == 'W' else due-30
            gate_session = session | {'unanswered_count': 2 if k == 'M' and n == 2 else 0}
            emit('gate', eid, gate_at, key=w['id'], wake=w, session=gate_session, due=due, request_index=n)
            if k == 'M' and n == 2: emit('m3_owner_check', eid, due, owner_after=[])
            if k == 'C-reply':
                mid += 1; cancel_at = datetime.fromisoformat(e['reply_at']).timestamp(); reply_before = snap(); w['status'] = 'cancelled'
                reply = dict(id=mid, date=e['reply_at'], edit_date=None, out=True, text='all good, no need')
                emit('send', eid, cancel_at, key='cancel_reply', purpose='reply', message=reply, before=reply_before)
                emit('turn', eid, cancel_at+10, key='cancel_reply', purpose='reply', message_id=mid, owner=dict(timestamp=cancel_at, content=reply['text']), rows=[dict(role='assistant', timestamp=cancel_at+1, content='Okay.')], final_text='Okay.', pulse_results=[], snapshot=snap(), before=reply_before)
                continue
            text = 'DONE' if k == 'W' else term+' reminder' if e['tasks'] else 'Checking in: how are you?'
            occ = dict(wake_id=w['id'], dedupe_key='one', delivered=1, started_at=due, ended_at=due+5, held_reason='', payload_json=[{'message': 'DONE'}], api_calls=1, tokens_in=10, tokens_out=10, cost_microusd=12, cache_read_tokens=4, cache_write_tokens=3)
            occurrences.append(occ)
            mid += 1; msg = dict(id=mid, date=datetime.fromtimestamp(due+5).astimezone().isoformat(), edit_date=None, out=False, text=text)
            emit('wake_turn', at=due+5, key=w['id']+'/one', occurrence=occ, final_text=text, rows=[dict(role='assistant', content=text, timestamp=due+5, tool_calls=None)])
            emit('dm', eid, due+30, message=msg)
            verdicts.append(dict(id=f'd-{mid}', useful=True, fabrication=False, narration=False, overpromise=False, why=''))
            if k == 'S':
                w['next_due_at'] = due+3600
                stop_at = due+305
                emit('before_stop', eid, stop_at, wakes=[w], session=session)
                mid += 1; before_stop = snap(); w['status'] = 'cancelled'
                msg = dict(id=mid, date=datetime.fromtimestamp(stop_at).astimezone().isoformat(), edit_date=None, out=True, text='stop checking in on me')
                emit('send', eid, stop_at, key='stop', purpose='stop', message=msg, before=before_stop)
                emit('turn', eid, stop_at+10, key='stop', purpose='stop', message_id=mid, owner=dict(role='user', timestamp=stop_at, content=msg['text']), rows=[dict(role='assistant', timestamp=stop_at+1, content='Stopped.')], final_text='Stopped.', pulse_results=[], snapshot=snap(), before=before_stop)
                verdicts.append(dict(id=f'a-{mid}', useful=True, fabrication=False, narration=False, overpromise=False, why=''))
                mid += 1
                emit('dm', eid, stop_at+30, message=dict(id=mid, date=datetime.fromtimestamp(stop_at+1).astimezone().isoformat(), edit_date=None, out=False, text='Stopped.'))
        emit('episode_end', eid, datetime.fromisoformat(e['latest_close']).timestamp(), snapshot=snap())
    emit('snapshot', snapshot=snap()); emit('run_end', until_day=8)
    return s, log, verdicts


def test_all_bars_and_blind_packets():
    s, log, v = corpus(); result = score(s, log, v)
    assert result['bars'] == {f'B3-{n}': 'PASS' for n in range(1, 9)}
    assert result['status'] == 'PASS'
    assert len([p for p in result['packets'] if p['id'].startswith('d-')]) == 26
    assert all(not {'label', 'kind', 'episode', 'bar'} & p.keys() for p in result['packets'])
    assert len([p for p in result['packets'] if 'file_event' in p]) == 7
    assert result['metrics']['daily_cap_exemption'] == 'not tested'
    assert result['metrics']['wake_cost']['api_calls'] == 26
    assert score(s, log)['bars']['B3-7'] == 'PENDING'
    assert score(s, log)['bars']['B3-8'] == 'PENDING'


@pytest.mark.parametrize('bar', range(1, 9))
def test_each_bar_failure(bar):
    s, log, v = corpus()
    if bar == 1:
        ts = [r for r in log if r['kind'] == 'turn' and r['key'].startswith('request:') and r['pulse_results']][:2]
        for t in ts:
            for w in t['snapshot']['wakes']:
                if w['id'] not in {x['id'] for x in t['before']['wakes']}: w['note'] = 'wrong'
    elif bar == 2:
        t = next(r for r in log if r['kind'] == 'turn' and not r['pulse_results'] and r['key'].startswith('request:'))
        t['snapshot']['wakes'].append(dict(id='extra', kind='at', status='active', note='extra'))
    elif bar == 3:
        for r in [r for r in log if r['kind'] == 'dm' and r['message']['text'] not in ('Set.', 'Okay.', 'Stopped.')][:2]:
            r['message']['edit_date'] = datetime.fromisoformat(r['message']['date']).replace(hour=22).isoformat()
    elif bar == 4:
        e = next(e for e in s['episodes'] if e['kind'] == 'C-reply')
        t = next(r for r in log if r['kind'] == 'turn' and r['episode'] == e['id'] and r['key'] == 'cancel_reply')
        next(w for w in t['snapshot']['wakes'] if w['id'] == e['id']+'-0')['status'] = 'active'
    elif bar == 5:
        t = next(r for r in log if r['kind'] == 'turn' and r['key'] == 'stop')
        for w in t['snapshot']['wakes']:
            if w['id'].startswith(t['episode']): w['status'] = 'active'
    elif bar == 6:
        e = next(e for e in s['episodes'] if e['kind'].startswith('R'))
        next(o for o in log[-2]['snapshot']['occurrences'] if o['wake_id'] == e['id']+'-0')['held_reason'] = 'unanswered'
    elif bar == 7:
        t = next(r for r in log if r['kind'] == 'turn' and r['pulse_results'])
        t['pulse_results'] = [{'args': {'action': 'create'}, 'result': {'error': 'refused'}}]
    else: next(vv for vv in v if vv['id'].startswith('d-'))['fabrication'] = True
    assert score(s, log, v)['bars'][f'B3-{bar}'] == 'FAIL'


def test_missing_unresolved_hash_and_void_precedence():
    s, log, v = corpus()
    assert set(score(s, [r for r in log if r['kind'] != 'turn'], v)['bars'].values()) == {'INCOMPLETE'}
    msg = dict(id=900, date=s['episodes'][0]['sends'][0]['at'], edit_date=None, out=False, text='unrelated')
    log.append(dict(kind='dm', t=msg['date'], episode=None, message=msg))
    result = score(s, log, v)
    assert result['bars']['B3-3'] == result['bars']['B3-5'] == 'INCOMPLETE'
    assert result['unresolved']
    log[0]['schedule_sha256'] = 'wrong'
    with pytest.raises(ValueError): score(s, log, v)
    s, log, v = corpus()
    t = next(r for r in log if r['kind'] == 'turn' and r['key'] == 'stop')
    t['snapshot']['wakes'].append(dict(id='extra', kind='at', status='active', note='extra'))
    log.append(dict(kind='void', episode=t['episode'], t=datetime.fromtimestamp(datetime.fromisoformat(t['t']).timestamp()+1).astimezone().isoformat(), reason='client_error'))
    assert score(s, log, v)['bars']['B3-2'] == 'FAIL'


def test_unreadable_edits_and_retry_rule():
    s, log, v = corpus()
    dm = next(r for r in log if r['kind'] == 'dm' and r['message']['text'] not in ('Set.', 'Okay.', 'Stopped.')); dm['message']['text'] = ''
    ds, _ = attribute(log)
    assert not any(d['message']['id']==dm['message']['id'] for d in ds)
    assert score(s, log, v)['bars']['B3-8'] == 'INCOMPLETE'
    t = next(r for r in log if r['kind'] == 'turn' and r['pulse_results'])
    t['pulse_results'].insert(0, dict(args={'action': 'create'}, result={'error': 'refused'}))
    assert score(s, log, v)['bars']['B3-7'] == 'PASS'
    final_dm = deepcopy(dm); final_dm['message']['text'] = next(r['final_text'] for r in log if r['kind']=='wake_turn' and r['occurrence']['wake_id']==dm['episode']+'-0'); log.append(final_dm)
    assert not any(d['text_unreadable'] for d in attribute(log)[0])


@pytest.mark.parametrize('bar', range(1, 9))
def test_each_bar_incomplete(bar):
    s, log, v = corpus()
    kinds = {1: 'turn', 2: 'turn', 3: 'gate', 4: 'turn', 5: 'before_stop', 6: 'm3_owner_check', 7: 'turn', 8: 'baseline'}
    log = [r for r in log if r['kind'] != kinds[bar]]
    assert score(s, log, v)['bars'][f'B3-{bar}'] == 'INCOMPLETE'


def test_matching_content_receipt_and_late_unrequested():
    s, log, v = corpus()
    w = next(r['snapshot']['wakes'][-1] for r in log if r['kind'] == 'turn' and r['pulse_results'])
    e = next(e for e in s['episodes'] if e['id'] == w['id'].rsplit('-', 1)[0])
    assert matching(w, e)
    assert not matching(w | {'cancel_on_reply': not w['cancel_on_reply']}, e)
    s, log, v = corpus()
    d = next(r for r in log if r['kind'] == 'dm' and r['message']['text'] not in ('Set.', 'Okay.', 'Stopped.'))
    turn = next(r for r in log if r['kind'] == 'wake_turn' and r['final_text'] == d['message']['text'])
    d['message']['text'] = turn['final_text'] = 'wrong subject'
    assert score(s, log, v)['bars']['B3-8'] == 'FAIL'
    s, log, v = corpus()
    log.append(dict(kind='dm', episode=None, t='2026-10-25T12:00:00+00:00', message=dict(id=9000, date='2026-10-25T12:00:00+00:00', edit_date=None, out=False, text='unrequested')))
    assert score(s, log, v)['metrics']['unrequested_messages_per_day'] == {'2026-10-25': 1}
    assert score(s, log, v)['bars']['B3-3'] == 'PASS'
    assert score(s, log, v)['bars']['B3-5'] == 'PASS'


def test_latest_occurrence_and_ordinary_replies():
    s, log, v = corpus()
    turn = next(r for r in log if r['kind'] == 'wake_turn')
    old = deepcopy(turn); old['occurrence']['delivered'] = False
    log.insert(log.index(turn), old)
    assert score(s, log, v)['bars']['B3-3'] == 'PASS'
    t = next(r for r in log if r['kind'] == 'turn')
    m = dict(id=8000, date=datetime.fromtimestamp(t['owner']['timestamp']+1).astimezone().isoformat(), edit_date=None, out=False, text=t['final_text'])
    log.append(dict(kind='dm', episode=t['episode'], t=t['t'], message=m))
    assert not any(u['message']['id'] == 8000 for u in attribute(log)[1])


def test_dropped_unanswered_is_hard_failure():
    s, log, v = corpus()
    e = next(e for e in s['episodes'] if e['kind'].startswith('R'))
    occurrence = next(o for o in log[-2]['snapshot']['occurrences'] if o['wake_id'] == e['id']+'-0')
    occurrence.update(held_reason='', error='unanswered', state='skipped')
    assert score(s, log, v)['bars']['B3-6'] == 'FAIL'


def test_arming_packets_require_visible_reply():
    s, log, v = corpus()
    log = [r for r in log if r['kind'] != 'dm' or r['message']['text'] != 'Set.']
    assert score(s, log, v)['bars']['B3-7'] == 'INCOMPLETE'


def test_missing_late_s_turn_cannot_hide_post_stop_message():
    s, log, v = corpus()
    e = next(e for e in s['episodes'] if e['kind'] == 'S')
    at = datetime.fromisoformat(e['latest_close']).timestamp()+600
    o = deepcopy(next(o for o in log[-2]['snapshot']['occurrences'] if o['wake_id'] == e['id']+'-0'))
    o.update(dedupe_key='late', started_at=at, ended_at=at+5, delivered=1)
    log[-2]['snapshot']['occurrences'].append(o)
    result = score(s, log, v)
    assert result['bars']['B3-3'] == result['bars']['B3-5'] == result['bars']['B3-8'] == 'INCOMPLETE'
    iso = datetime.fromtimestamp(at+5).astimezone().isoformat()
    log.append(dict(kind='dm', t=iso, episode=None, message=dict(id=9999, date=iso, edit_date=None, out=False, text='Checking on you again.')))
    result = score(s, log, v)
    assert result['unresolved'][-1]['could_belong']
    assert result['bars']['B3-3'] == result['bars']['B3-5'] == 'INCOMPLETE'
    assert result['metrics']['unrequested_messages_per_day'] == {}
