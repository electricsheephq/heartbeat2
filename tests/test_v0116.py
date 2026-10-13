"""Natural schedules, actionable refusals, and recurring receipt holds."""
from dataclasses import replace
from datetime import datetime
from itertools import product
from pathlib import Path
import math
import re
from types import SimpleNamespace

import pytest

from heartbeat2.model import KINDS
from heartbeat2.schedule import parse_at, parse_duration, next_every
from test_engine import engine, session, KEY, arm, start
from test_v0113 import owner_turn
from test_v0114 import command, create, local, night, receipt


def ts(text):
    return datetime.fromisoformat(text).timestamp()


@pytest.mark.parametrize('units,seconds', [
    ('s sec secs second seconds', 1), ('m min mins minute minutes', 60),
    ('h hr hrs hour hours', 3600), ('d day days', 86400),
    ('w wk wks week weeks', 604800),
])
def test_duration_aliases(units, seconds):
    for unit, space, prefix in product(units.split(), ['', ' '], ['', 'in ', '+']):
        assert parse_duration(prefix+'2'+space+unit.upper()) == 2*seconds


@pytest.mark.parametrize('text', ['1 hour and 30 minutes', '1h, 30m',
    '1 hour 30 minutes', 'in 1 HR AND 30 MINS', '+1hour30minutes'])
def test_duration_parts(text):
    assert parse_duration(text) == 5400


def legacy_duration(text):
    """Frozen pre-v0.1.16 string parser, for compatibility properties."""
    text = re.sub(r'^(?:in\s+|\+)', '', text.strip(), flags=re.I)
    result, pos = 0.0, 0
    for match in re.finditer(r'(\d+(?:\.\d+)?)\s*([smhdw])', text, re.I):
        if text[pos:match.start()].strip(): raise ValueError
        result += float(match[1])*dict(s=1, m=60, h=3600, d=86400, w=604800)[match[2].lower()]
        pos = match.end()
    if not pos or text[pos:].strip() or not math.isfinite(result) or result <= 0:
        raise ValueError
    return result


def test_all_legacy_duration_samples_keep_values():
    corpus = ['20m', 'in 2h', '+1d 2h 3m 4s', '1.5h', '10m', '1h30m',
              '+90s', '2d', '1w', '0h1m', '1h0m']
    for number, unit, prefix, space in product(['1', '01', '1.5', '10'], 'smhdwSMHDW',
                                             ['', 'in ', 'IN\t', '+'], ['', ' ', '\t']):
        corpus += [prefix+number+space+unit, prefix+number+space+unit+'2s']
    for text in corpus:
        assert parse_duration(text) == legacy_duration(text), text
    assert parse_duration(60) == 60


@pytest.mark.parametrize('value', ['', '0 seconds', '-1h', '10', '1mo', '1 month',
    'nan hours', 'inf', 'junk 2h', '1h junk', '1h and', 'and 1h', '1h,,2m',
    True, None, float('inf'), float('nan'), 'x'*90])
def test_duration_refusal_names_input(value):
    with pytest.raises(ValueError) as error:
        parse_duration(value)
    assert str(error.value) == (f"invalid duration '{str(value)[:60]}': "
        'use a number and unit such as 10m, 2 hours or 1h30m')


@pytest.mark.parametrize('text', ['22:22', 'today 22:22', '9:05', 'today 9:05'])
@pytest.mark.parametrize('hour', [8, 23])
def test_clock_is_next_occurrence_in_chat_timezone(text, hour):
    now = ts(f'2025-10-12T{hour:02}:00:00+09:00')
    clock = text.removeprefix('today ').zfill(5)
    day = 12 if clock > f'{hour:02}:00' else 13
    assert parse_at(text, now=now, timezone='Asia/Tokyo') == ts(f'2025-10-{day}T{clock}:00+09:00')


def test_clock_equality_dst_and_existing_at_forms():
    now = ts('2025-10-12T12:00:00+00:00')
    assert parse_at('12:00', now=now, timezone='UTC') == now+86400
    for text, expected in [('tomorrow 09:00', now+21*3600), ('in 2h', now+7200),
        ('+20m', now+1200), ('2025-10-13T09:00:00', now+21*3600),
        ('2025-10-13T09:00:00Z', now+21*3600)]:
        assert parse_at(text, now=now, timezone='UTC') == expected
    assert parse_at('02:30', now=ts('2025-03-09T01:00:00-05:00'),
                    timezone='America/New_York') == ts('2025-03-09T03:00:00-04:00')
    assert parse_at('01:30', now=ts('2025-11-02T00:00:00-04:00'),
                    timezone='America/New_York') == ts('2025-11-02T01:30:00-04:00')


@pytest.mark.parametrize('timezone,value', [('Missing/Zone','22:22'), ('/invalid','22:22'),
                                         ('UTC','not a time'), ('UTC','x'*90)])
def test_time_error_is_ours(timezone, value):
    with pytest.raises(ValueError) as error:
        parse_at(value, now=0, timezone=timezone)
    assert str(error.value) == (f"invalid time '{value[:60]}': "
        'use HH:MM, tomorrow HH:MM, in 10m, or an ISO date-time')


@pytest.mark.parametrize('trigger,extra,field,seconds', [
    ({'kind':'every','every':'2 hours'}, {}, 'base_interval_s', 7200),
    ({'kind':'owner_silent','after':'2 hours'}, {}, 'after', '2 hours'),
    ({'kind':'command','command':['git','status'],'check_every':'2 minutes'}, {}, 'check_every', 120),
    ({'kind':'at','at':'in 10 minutes'}, {'until':{'expires':'in 2 hours'}}, 'expires_at', 7200),
    ({'kind':'kanban_completed','deadline':'in 2 hours'}, {}, 'deadline_at', 7200),
])
def test_duration_consumers_through_tool(engine, trigger, extra, field, seconds):
    result = create(engine, trigger, **extra)
    assert result.get('ok'), result
    w = result['wake']
    expected = engine.clock.now()+seconds if field.endswith('_at') else seconds
    assert w.get(field, w['trigger_json'].get(field)) == expected
    if trigger['kind'] == 'every':
        assert w['next_due_at'] == next_every(engine.clock.now(), '2 hours')


@pytest.mark.parametrize('text,seconds', [('in 10minutes', 600), ('in 10 minutes', 600),
    ('in 1 hour and 30 minutes', 5400), ('22:22', None)])
def test_command_add_natural_time(engine, command, text, seconds):
    response = command('add '+text+' Check the report.')
    assert not response.startswith('Error:'), response
    w, = engine.store.list_wakes(KEY)
    assert w.note == 'Check the report.'
    assert w.next_due_at == (engine.clock.now()+seconds if seconds else
                            parse_at(text, now=engine.clock.now(), timezone='UTC'))


def test_round3_clock_tool(engine):
    result = create(engine, {'kind':'at','at':'22:22'})
    assert result.get('ok'), result
    assert result['wake']['next_due_at'] == parse_at('22:22', now=engine.clock.now(), timezone='UTC')


def kinds_detail(kind):
    return (f"unknown trigger kind '{kind}'; valid kinds: {', '.join(sorted(KINDS))}. "
            'A one-time reminder is {"kind": "at", "at": "in 10m"}.')


@pytest.mark.parametrize('action', ['create', 'update', 'all'])
def test_unknown_kind_detail_on_all_tool_paths(engine, action, monkeypatch):
    if action == 'create':
        result = create(engine, {'kind':'after','after':'10m'})
    else:
        w = arm(engine)
        if action == 'all':  # Exercise the aggregate gate path (update/all is refused earlier).
            monkeypatch.setattr(type(engine.policy), 'mutation_gate', lambda self, *a, **kw:
                self.creation_gate(session_key=KEY, platform='telegram', chat_type='dm',
                                   allow_gateway_injection=True, kind='after'))
        result = engine.tool.handle({'action':'update' if action == 'update' else 'resume',
            'id':w.id if action == 'update' else 'all', 'trigger':{'kind':'after','after':'10m'}}, session_id='sid')
        if action == 'all': result = result['results'][0]
    assert result['error'] == 'unknown_kind'
    assert result['detail'] == kinds_detail('after')


@pytest.mark.parametrize('path', ['owner', 'invalid', 'missing', 'cas'])
def test_not_in_flight_detail(engine, path, monkeypatch):
    args = {'action':'respond','outcome':'done','notify':True,'summary':'Done.'}
    if path == 'owner':
        owner_turn(engine)
        result = engine.tool.handle(args, session_id='sid')
    elif path in {'invalid', 'missing'}:
        result = engine.tool.respond(args, SimpleNamespace(invalid=path == 'invalid',
            wake_id='missing', dedupe_key='missing', attempt=1))
    else:
        arm(engine)
        engine.tick()
        start(engine)
        monkeypatch.setattr(engine.store, 'update_occurrence', lambda *a, **kw: False)
        result = engine.tool.handle(args, session_id='sid')
    assert result == {'error':'not_in_flight','detail':
        'respond is only for answering a wake turn; in an ordinary conversation, '
        'just reply to the owner (use create/update/cancel to manage wakes).'}


@pytest.mark.parametrize('kind,field,values', [
    ('at', 'at', ['bad', '25:00', '22:99', 'today nope', 'tomorrow 99:00',
                  '2025-99-01', 'in months', 'in 0 seconds', '+-1h', '2025-01-01T99:00']),
    ('every', 'every', ['bad', '10', '1mo', '1 month', '-1h', '0m',
                       '1h junk', 'NaN', 'inf', '1h and']),
])
def test_ten_malformed_values_through_handle(engine, kind, field, values):
    for value in values:
        result = create(engine, {'kind':kind, field:value})
        assert result.get('error') == 'invalid_request', result
        assert result['detail'].startswith(('invalid time ', 'invalid duration ')), result
    assert not engine.store.list_wakes(KEY)


@pytest.mark.parametrize('args', [
    {'trigger':[['bad']]}, {'until':['bad']}, {'note':5},
    {'trigger':{'kind':'url','url':'https://example.com:bad'}},
    {'trigger':{'kind':'url','url':'https://[bad'}},
])
def test_other_malformed_inputs_do_not_leak_python_errors(engine, args):
    result = create(engine, **args)
    assert result.get('error') == 'invalid_request', result
    assert result['detail'].startswith(('invalid request', 'invalid URL')), result


@pytest.mark.parametrize('kind', ['every', 'cron', 'self'])
@pytest.mark.parametrize('hold', ['spacing', 'quiet', 'neither', 'both'])
def test_recurring_receipt_names_holds(engine, kind, hold):
    now = night(engine, 21 if hold in {'quiet','both'} else 8)
    engine.policy = replace(engine.policy, wake_spacing_minutes=180 if hold in {'spacing','both'} else 10)
    session(engine, last_wake_turn_at=now if hold in {'spacing','both'} else now-3600)
    result = create(engine, {'kind':kind, **({'cron':'0 22 * * *' if hold in {'quiet','both'} else '0 9 * * *'}
                    if kind == 'cron' else {'every':'1h'})})
    assert result.get('ok'), result
    text = receipt(result)
    assert ('Wake spacing holds' in text) is (hold in {'spacing','both'})
    assert ('Quiet hours skip its ticks' in text) is (hold in {'quiet','both'})
    assert 'Quiet hours hold' not in text  # recurring ticks in quiet hours are skipped, not held
    if hold in {'spacing','both'}:
        assert f'Wake spacing holds it until {local(engine, now+3*3600)}.' in text
    if hold in {'quiet','both'}:
        assert f'Quiet hours skip its ticks until {local(engine, now+11*3600)}.' in text
    if hold == 'both': assert text.index('Wake spacing holds') < text.index('Quiet hours skip')


def test_version_changelog_and_skill():
    root = Path(__file__).resolve().parents[1]
    assert 'version: "0.1.16"' in (root/'plugin.yaml').read_text()
    changelog = (root/'CHANGELOG.md').read_text()
    assert changelog.count('## 0.1.16 — 2026-10-14') == 1
    section = changelog.split('## 0.1.16 — 2026-10-14')[1].split('## 0.1.15')[0]
    assert '#72' in section and '#76' in section and 'round-3' in section
    skill = (root/'skills/heartbeat2-usage/SKILL.md').read_text()
    assert all(x in skill for x in ['HH:MM', 'tomorrow HH:MM', 'in 10 minutes', 'ISO', 'kind: at'])
    assert 'set `owner_asked: true` on the `at` create' in skill
