"""U5 confirmed regressions: real local I/O, real commands/intake, no remote network."""
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from conftest import FakeCtx
from test_engine import engine, arm, KEY, start, end, session
from test_commands import runtime
from heartbeat2 import commands, governor, probes, registration
from heartbeat2.model import Wake
from heartbeat2.policy import Policy, INJECTION_CONFIG

_CONNECT = socket.socket.connect
_DNS = socket.getaddrinfo
_CREATE = socket.create_connection

@pytest.fixture
def drip(monkeypatch):
    # Explicit exception: loopback fixture only; remote DNS/connections still refused.
    def connect(sock, address):
        assert address[0] == '127.0.0.1'
        return _CONNECT(sock, address)
    def dns(host, *args):
        assert host == '127.0.0.1'
        return _DNS(host, *args)
    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setattr(socket, 'getaddrinfo', dns)
    monkeypatch.setattr(socket, 'create_connection', _CREATE)
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args): pass
        def do_GET(self):
            self.send_response(200)
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            try:
                for _ in range(100):  # 5 s drip: far beyond the 0.25 s deadline
                    self.wfile.write(b'2\r\n:\n\r\n'); self.wfile.flush()
                    time.sleep(.05)
                self.wfile.write(b'0\r\n\r\n'); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(probes, 'URL_DEADLINE_S', .25, raising=False)
    yield f'http://127.0.0.1:{server.server_port}/'
    server.shutdown(); server.server_close(); thread.join(1)


def test_R3_R1_url_total_deadline(drip):
    before = time.monotonic()
    result = probes.check(Wake(KEY, 'observe', 'url', trigger_json={'url':drip}))
    assert result.get('error') == 'TimeoutError', result
    assert time.monotonic() - before < 2.5  # deadline .25 s; the drip lasts 5 s


def test_R3_R1_slow_url_cannot_block_due_at(engine, drip):
    e = engine
    arm(e, 'url', trigger_json={'url':drip})
    w = arm(e)
    before = time.monotonic(); e.tick()
    assert time.monotonic() - before < 2.5  # deadline .25 s; the drip lasts 5 s
    assert len(e.ctx.calls) == 1 and w.id in e.ctx.calls[0][0]


@pytest.mark.parametrize('flag', ['-w','-p','-wc','-vw','-wRowner/repo','-vp'])
def test_R3_R2_bundled_browser_flags(flag):
    assert not probes.command_allowed(['gh','pr','view','1',flag], Policy().probe_commands)
    assert probes.command_allowed(['gh','pr','view','1','--repo=owner/repo'], Policy().probe_commands)


def observer(tmp_path, source):
    path = tmp_path/'observer'
    path.write_text('#!'+sys.executable+'\n'+source)
    path.chmod(0o700)
    w = Wake(KEY,'observe','command',trigger_json={'command':[str(path)],'fire_on':'success'})
    return w, Policy(probe_commands=str(path))


def test_R3_R2_no_tty_and_prompt_environment(tmp_path):
    w, p = observer(tmp_path, "import os,json\nprint(json.dumps({'sid':os.getsid(0),'pid':os.getpid(),'git':os.getenv('GIT_TERMINAL_PROMPT'),'gh':os.getenv('GH_PROMPT_DISABLED'),'gcm':os.getenv('GCM_INTERACTIVE')}))\n")
    result = probes.check(w, policy=p, home=tmp_path)
    data = json.loads(result['message'])
    assert data['sid'] == data['pid']
    assert (data['git'],data['gh'],data['gcm']) == ('0','1','never')


def test_R3_R2_timeout_kills_descendants(tmp_path, monkeypatch):
    marker = tmp_path/'survivor'
    w, p = observer(tmp_path, "import os,time\nif os.fork()==0:\n time.sleep(.7)\n open("+repr(str(marker))+",'w').write('alive')\n os._exit(0)\ntime.sleep(1.3)\n")
    monkeypatch.setattr(probes,'COMMAND_DEADLINE_S',.2,raising=False)
    result = probes.check(w,policy=p,home=tmp_path)
    time.sleep(.8)
    assert result.get('error') == 'TimeoutExpired', result
    assert not marker.exists()


def test_R3_R2_capture_bounded_during_execution(tmp_path):
    w, p = observer(tmp_path,"import os,time\nos.write(1,b'x'*(300*1024))\ntime.sleep(5)\n")
    before=time.monotonic()
    result=probes.check(w,policy=p,home=tmp_path)
    assert result.get('error')=='ValueError'
    assert time.monotonic()-before < 2.5  # overflow kill; the child sleeps 5 s


@pytest.mark.parametrize('rule,argv,allow', [
    ('browser',['gh','pr','view','1','-w'],'gh pr view'),
    ('verbs',['gh','run','rerun','1'],'gh run'),
    ('prefix',['git','log','-ofile'],'git log'),
    ('prefix',['git','log','-cfoo=bar'],'git log'),
    ('prefix',['git','log','-Pcat'],'git log'),
])
def test_R3_R3_unique_deny_branch(rule,argv,allow):
    assert not probes.command_allowed(argv,allow)


def test_R3_R3_streaming_cap_read_budget(monkeypatch):
    counts=[]
    class Response(io.BytesIO):
        def read(self,n=-1):
            data=super().read(n); counts.append(len(data)); return data
        def read1(self,n=-1): return self.read(n)
    monkeypatch.setattr(probes.urllib.request,'build_opener',lambda *a:SimpleNamespace(open=lambda *a,**k:Response(b'x'*(2*1024*1024))))
    with pytest.raises(ValueError): probes._get_url('http://127.0.0.1/fixture')
    assert sum(counts)<=probes.MAX_BODY+1


def test_R3_R4_missing_file_first_content(engine,tmp_path):
    p=tmp_path/'later.log'
    w=arm(engine,'file',trigger_json={'path':str(p),'pattern':'FAILED'})
    engine.tick()
    for _ in range(6): engine.clock.advance(61); engine.tick()
    current=engine.store.get_wake(w.id)
    assert current.status=='active' and current.error_streak==0
    assert current.probe_state_json['offset']==0
    p.write_text('FAILED first content\n'); engine.clock.advance(61); engine.tick()
    assert len(engine.ctx.calls)==1


def test_R3_R4_missing_file_deadline(engine,tmp_path):
    w=arm(engine,'file',trigger_json={'path':str(tmp_path/'missing')},deadline_at=engine.clock.now()+400)
    for _ in range(8): engine.tick(); engine.clock.advance(61)
    assert len(engine.ctx.calls)==1
    assert engine.store.get_wake(w.id).error_streak==0


@pytest.mark.parametrize('body', [b'ok\n'*(900*1024)+b'FAILED end\n',b'x'*(300*1024)+b'\nFAILED end\n'], ids=['catchup-2.7MB','overlong-300KB'])
def test_R3_R4_catchup_and_overlong_lines(tmp_path,body):
    path=tmp_path/'log'; path.write_bytes(b'old\n')
    w=Wake(KEY,'observe','file',trigger_json={'path':str(path),'pattern':'FAILED'})
    w=replace(w,probe_state_json=probes.check(w)['state'])
    path.write_bytes(b'old\n'+body)
    result=probes.check(w)
    assert result['fire'] and result['state']['offset']==path.stat().st_size
    if body.startswith(b'x'): assert result['state']['skipped_overlong_lines']==1


def test_R3_R4_budget_and_skip_across_checks(tmp_path):
    path=tmp_path/'log'; path.write_bytes(b'')
    w=Wake(KEY,'observe','file',trigger_json={'path':str(path),'pattern':'FAILED'})
    w=replace(w,probe_state_json=probes.check(w)['state'])
    path.write_bytes(b'x'*(5*1024*1024)+b'\nFAILED end\n')
    first=probes.check(w)
    assert first['state']['offset']==4*1024*1024
    assert first['state']['skip_to_newline']
    second=probes.check(replace(w,probe_state_json=first['state']))
    assert second['fire'] and second['state']['offset']==path.stat().st_size


def test_R3_R5_target_update_resets_state(engine,tmp_path):
    a,b=tmp_path/'a',tmp_path/'b'; a.write_text('old\n'); b.write_text('FAILED history\n')
    w=arm(engine,'file',trigger_json={'path':str(a),'pattern':'FAILED'})
    engine.tick()
    before=engine.store.get_wake(w.id).probe_state_json
    assert engine.tool.handle({'action':'update','id':w.id,'note':'new note'})['ok']
    assert engine.store.get_wake(w.id).probe_state_json==before
    assert engine.tool.handle({'action':'update','id':w.id,'trigger':{'kind':'file','path':str(b),'pattern':'FAILED'}})['ok']
    assert engine.store.get_wake(w.id).probe_state_json is None
    engine.tick(); assert not engine.ctx.calls
    with b.open('a') as f:f.write('FAILED fresh\n')
    engine.clock.advance(61); engine.tick()
    assert len(engine.ctx.calls)==1


@pytest.mark.parametrize('fire',[False,True])
def test_R3_R5_successful_probe_clears_error_streak(engine,fire):
    w=arm(engine,'file',trigger_json={'path':'unused'},error_streak=4)
    engine.probe_runner=lambda w:{'fire':fire,'state':{'fingerprint':'ok'},'identity':'fresh'}
    engine.tick()
    assert engine.store.get_wake(w.id).error_streak==0


@pytest.mark.parametrize('hook',['pre_command','post_gateway_admission','pre_gateway_dispatch','pre_llm_call'])
def test_R3_R6_pulse_is_never_human_reply(engine,hook):
    e=engine
    w=arm(e,cancel_on_reply=True,next_due_at=e.clock.now()+1000)
    session(e,unanswered_count=2)
    before=e.store.get_session(KEY)
    e.clock.advance(60)
    if hook=='pre_command':e.intake.pre_command(surface='gateway',command='pulse',session_key=KEY,platform='telegram')
    elif hook=='post_gateway_admission':e.intake.post_gateway_admission(session_key=KEY,platform='telegram',source={'chat_type':'dm','chat_id':'unit'},text='/pulse status',message_id='cmd')
    elif hook=='pre_gateway_dispatch':e.intake.pre_gateway_dispatch(event=SimpleNamespace(internal=False,text='/pulse status',message_id='cmd',source=SimpleNamespace(platform=SimpleNamespace(value='telegram'),chat_type='dm',chat_id='unit',is_bot=False)))
    else:e.intake.pre_llm_call(session_id='sid',turn_id='cmd',user_message='/pulse status',platform='telegram')
    after=e.store.get_session(KEY)
    assert e.store.get_wake(w.id).status=='active'
    assert (after.last_human_at,after.episode_id,after.unanswered_count)==(before.last_human_at,before.episode_id,2)


def test_R3_R6_command_never_credits_engagement(engine):
    w=arm(engine); engine.tick(); start(engine); end(engine,'progress',True)
    engine.clock.advance(60)
    engine.intake.pre_command(surface='gateway',command='pulse',session_key=KEY,platform='telegram')
    assert engine.store.get_wake(w.id).engaged==0
    assert engine.store.get_session(KEY).unanswered_count==1


@pytest.mark.parametrize('ctype',['dm','group',''])
def test_R3_R6_first_command_bound_identity(runtime,monkeypatch,ctype):
    e=runtime.get_engine(); e.ctx.allowed=False
    bound={'HERMES_SESSION_KEY':'agent:telegram:'+ctype+':fresh','HERMES_SESSION_PLATFORM':'telegram','HERMES_SESSION_CHAT_TYPE':ctype,'HERMES_SESSION_CHAT_ID':'fresh'}
    monkeypatch.setattr(commands,'session_env',lambda n:bound.get(n,''))
    out=commands.handle('status',runtime)
    if ctype=='dm':
        assert INJECTION_CONFIG in out
        assert e.store.get_session(bound['HERMES_SESSION_KEY']).last_human_at is None
    else:assert 'DM' in out


@pytest.mark.parametrize('answer',['keep','slow','resume'])
def test_R3_M1_owner_card_history(runtime,monkeypatch,answer):
    e=runtime.get_engine()
    w=arm(e,'every',status='paused',status_reason='governor:noop',trigger_json={'every':'1h'},noop_streak=6,ignored_streak=2,error_streak=3,eff_interval_s=14400)
    e.store.set_meta('card:'+w.id,'noop'); e.store.set_meta('ratio_since:'+w.id,'12')
    called=[]; original=governor.owner_answer
    def track(engine,wake,answer):called.append(answer);return original(engine,wake,answer)
    monkeypatch.setattr(governor,'owner_answer',track)
    out=commands.handle(f'{answer} {w.id}',runtime); assert not out.startswith('Error'),out
    now=e.store.get_wake(w.id)
    assert called==([answer] if answer in {'keep','slow'} else [])
    assert (now.noop_streak,now.ignored_streak,now.error_streak)==((0,0,0) if answer=='keep' else (6,2,3))
    assert now.eff_interval_s==({'keep':3600,'slow':28800,'resume':14400}[answer])
    assert e.store.get_meta('ratio_since:'+w.id)==(str(e.clock.now()) if answer=='keep' else '12')


@pytest.mark.parametrize('answer',['keep','slow'])
def test_R3_M1_card_answers_refuse_plain_pause(runtime,answer):
    w=arm(runtime.get_engine(),'every',status='paused',status_reason='command',trigger_json={'every':'1h'})
    assert 'keep/slow answer a governor card; use /pulse resume' in commands.handle(f'{answer} {w.id}',runtime)


@pytest.mark.parametrize('action,state',[('pause','proposed'),('resume','proposed'),('pause','paused'),('resume','active'),('watch','proposed')])
def test_R3_R7_transitions_are_explicit(runtime,action,state):
    e=runtime.get_engine(); w=arm(e,'every',status=state,trigger_json={'every':'1h'})
    assert commands.handle(f'{action} {w.id}',runtime).startswith('Error')
    assert e.store.get_wake(w.id).status==state


def test_R3_R7_all_reports_skips_and_preserves_proposal(runtime):
    e=runtime.get_engine(); a=arm(e,'every',trigger_json={'every':'1h'}); p=arm(e,'every',status='proposed',trigger_json={'every':'1h'})
    g=arm(e,'every',status='paused',status_reason='governor:noop',trigger_json={'every':'1h'})
    out=commands.handle('pause all for 1h',runtime)
    assert p.id in out and g.id in out and 'skipped' in out
    assert e.store.get_wake(p.id).status=='proposed'
    e.clock.advance(3601); commands.resume_timed(e)
    assert e.store.get_wake(a.id).status=='active'
    out=commands.handle('resume all',runtime)
    assert not out.startswith('Error') and 'skipped' in out
    assert e.store.get_wake(p.id).status=='proposed' and e.store.get_wake(g.id).status=='active'
    assert not commands.handle(f'ok {p.id}',runtime).startswith('Error')


def test_R3_R7_ok_preserves_history(runtime):
    e=runtime.get_engine();w=arm(e,'every',status='proposed',trigger_json={'every':'1h'},noop_streak=2,error_streak=1,eff_interval_s=14400)
    commands.handle(f'ok {w.id}',runtime)
    now=e.store.get_wake(w.id)
    assert (now.status,now.noop_streak,now.error_streak,now.eff_interval_s)==('active',2,1,14400)


@pytest.mark.parametrize('anchor',['9:00','09:00'])
def test_R3_R8_anchor_kept(runtime,anchor):
    out=commands.handle(f'add every 1d at {anchor} Check release',runtime)
    assert not out.startswith('Error'),out
    w=runtime.get_engine().store.get_wake(re.search(r'w_[a-z2-7]{8}',out)[0])
    assert w.trigger_json['at']=='09:00' and w.note=='Check release'


@pytest.mark.parametrize('text',['add every 1d at 09:00','add every 1d at 9:00','add in 2h','add when silent 24h'])
def test_R3_R8_note_required(runtime,text):
    assert commands.handle(text,runtime).startswith('Error')
    assert not runtime.get_engine().store.list_wakes()


def test_R3_R9_governor_card(runtime):
    e=runtime.get_engine(); w=arm(e,'every',status='paused',status_reason='governor:noop')
    e.store.set_meta('card:'+w.id,'noop')
    e.intake.post_gateway_admission(session_key=KEY,platform='telegram',source={'chat_type':'dm','chat_id':'unit'},message_id='human')
    context=e.intake.pre_llm_call(session_id='sid',turn_id='human',user_message='hello',platform='telegram')['context']
    assert f'/pulse watch {w.id}' in context


def test_R3_R9_event_docs():
    root=Path(__file__).resolve().parents[1]
    assert 'selected by `trigger.kind`' in (root/'README.md').read_text()


def test_R3_R9_file_skill():
    root=Path(__file__).resolve().parents[1]
    skill=(root/'skills/heartbeat2-usage/SKILL.md').read_text()
    assert 'relative paths resolve against the profile home' in skill
    for block in re.findall(r'```json\n(.*?)\n```',skill,re.S):
        trigger=json.loads(block).get('trigger',{})
        if trigger.get('kind')=='file':assert Path(trigger['path']).is_absolute()


def test_R3_R10_recording_context_registers_everything(fake_ctx):
    registration.register(fake_ctx)
    assert set(fake_ctx.tools)=={'pulse'}
    assert set(fake_ctx.hooks)==set(registration.HOOKS)
    assert set(fake_ctx.commands)=={'pulse'}
    assert set(fake_ctx.skills)=={'heartbeat2-usage'}


def test_R3_R11_slow_probes_alive_no_false_notice(engine):
    e=engine; wake=arm(e,'command',trigger_json={'command':['git','status']})
    observed=[]
    def slow(w):
        e.clock.advance(25)
        observed.append(e.status()['state'])
        return {'fire':False}
    e.probe_runner=slow; e.tick(); e.clock.advance(5); e.tick()
    assert observed==['alive']
    assert e.store.get_wake(wake.id).error_streak==0
    assert not e.store.get_meta('notice:'+KEY)
    # An actual overdue wake after an idle gap still produces a late notice.
    arm(e,next_due_at=e.clock.now()+10);e.clock.advance(30);e.tick()
    assert 'late' in e.store.get_meta('notice:'+KEY)


def test_R3_R10_required_hermes_fails_not_skips(monkeypatch,tmp_path,clock):
    from integration import test_real_loader as loader
    monkeypatch.setitem(sys.modules,'hermes_cli.plugins',None)
    monkeypatch.setenv('HEARTBEAT2_REQUIRE_HERMES','1')
    try:
        loader.test_real_loader_worker_registration_and_same_session_silence(tmp_path,monkeypatch,clock)
    except pytest.fail.Exception: pass
    except pytest.skip.Exception: pytest.fail('required Hermes was skipped')
    else: pytest.fail('missing required Hermes did not fail')
    assert "HEARTBEAT2_REQUIRE_HERMES: '1'" in (Path(__file__).resolve().parents[1]/'.github/workflows/ci.yml').read_text()


def test_R3_B1_startup_preserves_future_recurring_deadline(engine):
    w=arm(engine,'every',next_due_at=engine.clock.now()+3600,trigger_json={'every':'1h'})
    engine.tick()
    assert not engine.ctx.calls
    assert engine.store.get_wake(w.id).next_due_at==w.next_due_at


def test_R3_B1_restart_preserves_future_recurring_deadline(engine):
    from heartbeat2.poller import Poller
    e=engine;e.acquire()
    w=arm(e,'every',next_due_at=e.clock.now()+3600,trigger_json={'every':'1h'})
    e.close();e.clock.advance(60)
    new=Poller(e.ctx,e.store,e.policy,e.clock,session_key_fn=lambda:KEY,paused=lambda:False,jitter=lambda:0)
    try:
        new.tick()
        assert not e.ctx.calls
        assert e.store.get_wake(w.id).next_due_at==w.next_due_at
    finally:new.close()
