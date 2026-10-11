"""Deterministic U3 probes: file/process fixtures, fake HTTP and subprocesses."""
from dataclasses import replace
import io
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys

import pytest

from heartbeat2 import probes
from heartbeat2.model import Wake
from heartbeat2.policy import Policy
from test_engine import engine, arm


def wake(kind, state=None, **trigger):
    return Wake('agent:telegram:dm:unit', 'Check task', kind, trigger_json=trigger,
                probe_state_json=state, next_due_at=100)


@pytest.mark.parametrize('argv', [
    ['git', 'status-evil'], ['git', 'status', '--output=out'], ['git', 'log', '-oout'],
    ['git', 'log', '--exec=x'], ['git', 'log', '-c', 'x=y'],
    ['git', 'ls-remote', '--upload-pack=x'], ['gh', 'pr', 'view', '--web'],
    ['git', 'log', '>out'], ['git', 'log', 'x|y'], ['git', 'log', '--ext-diff'],
    ['git', 'log', '--textconv'], ['git', 'log', '--config-env=x'],
])
def test_readonly_full_token_allowlist_rejects_execution_and_writes(argv, monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('unsafe subprocess'))
    assert probes.check(wake('command', command=argv), policy=Policy(), home=tmp_path).get('error')
    assert not Policy().command_allowed(argv)


def test_allowlist_disabled_and_full_prefix(tmp_path):
    assert probes.check(wake('command', command=['git', 'status']),
                        policy=Policy(probe_commands=''), home=tmp_path).get('error')
    assert Policy(probe_commands='git status --short').command_allowed(['git', 'status', '--short'])
    assert not Policy(probe_commands='git status --short').command_allowed(['git', 'status', '--shorter'])


@pytest.mark.parametrize('url', ['file:///tmp/a', 'ftp://example.test', 'http://u:p@example.test', 'https:///missing'])
def test_url_scheme_credentials_fail_before_io(url, monkeypatch):
    monkeypatch.setattr(probes, '_get_url', lambda *a: pytest.fail('unsafe GET'), raising=False)
    assert probes.check(wake('url', url=url)).get('error')


def test_file_baseline_change_regex_appended_lines_rotation_truncation(tmp_path):
    p = tmp_path/'build.log'
    p.write_text('FAILED old\n')
    w = wake('file', path=str(p), pattern='FAILED')
    first = probes.check(w)
    assert not first['fire'] and first['state']['offset'] == p.stat().st_size
    w = replace(w, probe_state_json=first['state'])
    p.write_text('FAILED old\nFAILED partial')
    partial = probes.check(w)
    assert not partial['fire']
    p.write_text('FAILED old\nFAILED partial\n')
    fired = probes.check(replace(w, probe_state_json=partial['state']))
    assert fired['fire'] and 'FAILED partial' in fired['message']
    assert not probes.check(replace(w, probe_state_json=fired['state']))['fire']
    p.write_text('FAILED new\n')
    truncated = probes.check(replace(w, probe_state_json=fired['state']))
    assert truncated['fire']
    p.rename(tmp_path/'old.log')
    p.write_text('FAILED rotated\n')
    assert probes.check(replace(w, probe_state_json=truncated['state']))['fire']
    baseline = probes.check(wake('file', path=str(p)))
    p.write_text('change\n')
    assert probes.check(wake('file', state=baseline['state'], path=str(p)))['fire']


def test_file_and_url_errors_are_not_changes(tmp_path, monkeypatch):
    assert not probes.check(wake('file', path=str(tmp_path/'absent'))).get('error')
    monkeypatch.setattr(probes, '_get_url', lambda url: (_ for _ in ()).throw(OSError()), raising=False)
    result = probes.check(wake('url', url='https://example.test'))
    assert result.get('error') and not result['fire'] and 'state' not in result


def test_url_hash_change_regex_and_size_bound(monkeypatch):
    body = [b'pending']
    monkeypatch.setattr(probes, '_get_url', lambda url: body[0], raising=False)
    w = wake('url', url='https://example.test')
    first = probes.check(w)
    assert not first['fire'] and len(first['state']['fingerprint']) == 64
    body[0] = b'complete'
    second = probes.check(replace(w, probe_state_json=first['state']))
    assert second['fire']
    assert not probes.check(replace(w, probe_state_json=second['state']))['fire']
    assert probes.check(replace(w, trigger_json={**w.trigger_json, 'pattern':'complete'}))['fire']
    body[0] = b'x'*(256*1024+1)
    assert probes.check(w).get('error')


def test_url_transport_get_timeout_no_proxy_cookie_or_auth(monkeypatch):
    captured = []
    class Response(io.BytesIO):
        def read1(self,n): return self.read(n)
    class Opener:
        def open(self, request, timeout):
            captured.append((request, timeout))
            return Response(b'ok')
    monkeypatch.setattr('urllib.request.build_opener', lambda *handlers: (captured.append(handlers) or Opener()))
    result = probes.check(wake('url', url='http://127.0.0.1:1234/fixture'))
    assert not result.get('error')
    request, timeout = captured[-1]
    assert request.get_method() == 'GET' and 0 < timeout <= 10
    assert probes.URL_DEADLINE_S == 10
    assert not any(k.lower() in {'cookie','authorization'} for k in request.headers)
    assert captured[0][0].proxies == {}


def test_process_exit_and_pid_reuse_guard(monkeypatch):
    current = [123.0]
    class NoSuchProcess(Exception): pass
    class Process:
        def __init__(self, pid):
            if current[0] is None: raise NoSuchProcess()
        def create_time(self): return current[0]
    monkeypatch.setitem(sys.modules, 'psutil', SimpleNamespace(Process=Process, NoSuchProcess=NoSuchProcess, ZombieProcess=NoSuchProcess))
    w = wake('process', pid=42)
    first = probes.check(w)
    assert not first['fire'] and first['state']['create_time'] == 123.0
    current[0] = 456.0
    assert probes.check(replace(w, probe_state_json=first['state']))['fire']
    current[0] = None
    assert probes.check(replace(w, probe_state_json=first['state']))['fire']


def test_command_modes_json_state_and_cwd(tmp_path, monkeypatch):
    output = [b'one', 0]
    calls = []
    def run(argv, cwd):
        calls.append((argv, {'cwd':cwd}))
        return output[1],output[0]
    monkeypatch.setattr(probes, '_run_command', run)
    w = wake('command', command=['git','status'], fire_on='change')
    first = probes.check(w, policy=Policy(), home=tmp_path)
    assert not first['fire'] and first['baseline']
    assert not probes.check(replace(w, probe_state_json=first['state']), policy=Policy(), home=tmp_path)['fire']
    assert calls[0][1]['cwd'] == tmp_path
    for mode, code, fire in [('success',0,True),('success',1,False),('failure',1,True),('failure',0,False)]:
        output[1] = code
        assert probes.check(replace(w, trigger_json={**w.trigger_json,'fire_on':mode}), policy=Policy(), home=tmp_path)['fire'] is fire
    output[:] = [json.dumps({'fire':False,'message':'waiting','state':{'a':1}}).encode(),0]
    j = replace(w, trigger_json={**w.trigger_json,'fire_on':'json','cwd':str(tmp_path/'recorded')})
    assert 'state' not in probes.check(j, policy=Policy(), home=tmp_path)
    output[0] = json.dumps({'fire':True,'message':'ready','state':{'a':1}}).encode()
    assert probes.check(j, policy=Policy(), home=tmp_path)['state']['value'] == {'a':1}
    assert calls[-1][1]['cwd'] == tmp_path/'recorded'
    output[0] = json.dumps({'fire':True,'message':'ready','state':'x'*17000}).encode()
    assert probes.check(j, policy=Policy(), home=tmp_path).get('error')
    output[:] = [b'changed',2]
    assert probes.check(w, policy=Policy(), home=tmp_path).get('error')


def test_probe_deadline_and_command_state_only_on_fire(engine):
    e = engine
    w = arm(e, 'command', trigger_json={'command':['git','status']}, deadline_at=e.clock.now())
    e.probe_runner = lambda w: {'fire':False,'state':{'should':'not save'}}
    e.tick()
    assert e.store.get_wake(w.id).probe_state_json is None
    assert e.store.list_occurrences(w.id)[0].payload_json[0]['message'] == 'deadline passed — nothing happened'


@pytest.mark.parametrize('trigger', [
    {'kind':'url','url':'file:///tmp/a'},
    {'kind':'url','url':'http://u:p@example.test'},
    {'kind':'command','command':['git','log','--output=x']},
])
def test_arming_refuses_unsafe_probes(engine, trigger):
    result = engine.tool.handle({'action':'create','note':'Check task','trigger':trigger})
    assert result.get('error') and not engine.store.list_wakes()


def test_process_identity_is_recorded_when_armed(engine, monkeypatch):
    monkeypatch.setitem(sys.modules,'psutil',SimpleNamespace(Process=lambda pid: SimpleNamespace(create_time=lambda:123.0)))
    result = engine.tool.handle({'action':'create','note':'Watch process','trigger':{'kind':'process','pid':42}})
    assert result['wake']['probe_state_json']['create_time'] == 123.0


def test_recorded_command_cwd_is_supported(engine, tmp_path):
    result = engine.tool.handle({'action':'create','note':'Watch repo','trigger':{'kind':'command','command':['git','status'],'cwd':str(tmp_path)}})
    assert result['ok']


@pytest.mark.parametrize('argv', [
    ['git','ls-remote','-uhelper'], ['git','ls-remote','-u','helper'],
])
def test_command_refuses_short_upload_pack_helper(argv, tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess,'run',lambda *a,**k: SimpleNamespace(returncode=0))
    assert not Policy().command_allowed(argv)
    assert probes.check(wake('command',command=argv),policy=Policy(),home=tmp_path).get('error')


def test_url_redirect_scheme_and_credentials_are_checked_before_follow():
    import urllib.request
    request=urllib.request.Request('http://127.0.0.1/fixture')
    for url in ('file:///tmp/test','http://u:p@127.0.0.1/fixture'):
        with pytest.raises(ValueError):
            probes._SafeRedirect().redirect_request(request,None,302,'found',{},url)


def test_command_timeout_is_error_without_state(tmp_path,monkeypatch):
    def timeout(*a,**k): raise subprocess.TimeoutExpired(a[0],30)
    monkeypatch.setattr(probes,'_run_command',timeout)
    result=probes.check(wake('command',command=['git','status']),policy=Policy(),home=tmp_path)
    assert result=={'fire':False,'error':'TimeoutExpired'}
