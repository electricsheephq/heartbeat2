"""Import-light registration; profile-bound work starts only in the bootstrap.

One daemon per immutable manager scope, plus a cross-process flock. Hooks in
non-gateway processes may still write inbox events; they never run the poller.
"""
from contextlib import contextmanager
from pathlib import Path
import logging
import threading

LOG=logging.getLogger('heartbeat2')
_RUNTIMES={}
_GUARD=threading.RLock()
HOOKS=('post_gateway_admission','pre_command','gateway_platform_event','pre_llm_call',
       'post_api_request','transform_llm_output','on_session_end','agent_loop_stopped',
       'on_session_reset','kanban_task_completed','kanban_task_blocked',
       'on_kanban_worker_exited','subagent_stop','api_request_error')
EVENTS={'kanban_task_completed':'kanban_completed','kanban_task_blocked':'kanban_blocked',
        'on_kanban_worker_exited':'kanban_worker_exited','subagent_stop':'subagent_done',
        'api_request_error':'api_error'}


@contextmanager
def home_scope(home):
    reset=token=None
    try:
        from hermes_constants import set_hermes_home_override,reset_hermes_home_override
        token=set_hermes_home_override(home)
        reset=reset_hermes_home_override
    except Exception:
        pass
    try: yield
    finally:
        if reset is not None: reset(token)


class Runtime:
    def __init__(self,ctx,*,clock=None,bootstrap_interval=2,tick_interval=5):
        from .clock import SystemClock
        self.ctx=ctx
        self.home=Path(ctx._manager.home_path)
        self.scope=ctx._manager.scope_key
        self.clock=clock or SystemClock()
        self.bootstrap_interval,self.tick_interval=bootstrap_interval,tick_interval
        self._engine=None
        self._engine_lock=threading.RLock()
        self._wake=threading.Event()
        self._stop=threading.Event()
        self._thread=None
        self._threads=[]
        self.closed=False

    def policy(self):
        from dataclasses import fields
        from .policy import Policy
        with home_scope(self.home):
            settings={f.name:self.ctx.get_config(f.name,getattr(Policy(),f.name))
                      for f in fields(Policy) if f.name not in {'clock','profile_timezone'}}
            profile=''
            try:
                from hermes_time import get_timezone_name
                profile=get_timezone_name()
            except Exception: pass
            return Policy.from_settings(settings,clock=self.clock,profile_timezone=profile)

    def get_engine(self):
        with self._engine_lock:
            if self.closed: raise RuntimeError('heartbeat2 runtime unloaded')
            if self._engine is None:
                from .store import Store
                from .poller import Poller
                with home_scope(self.home):
                    store=Store(self.home/'plugin-data/heartbeat2/pulse.db',self.clock)
                    try:
                        self._engine=Poller(self.ctx,store,self.policy(),self.clock)
                    except BaseException:
                        store.close()
                        raise
                self._engine.refresh_policy=self.policy
                self._engine.restart=self.restart
                self._engine.signal=self._wake.set
            return self._engine

    def hook(self,name,**kwargs):
        try:
            with home_scope(self.home):
                intake=self.get_engine().intake
                if name in EVENTS: return intake.event(EVENTS[name],**kwargs)
                return getattr(intake,name)(**kwargs)
        except Exception:
            LOG.warning('heartbeat2 hook adapter %s failed',name,exc_info=True)
            return None

    def tool(self,args,**kwargs):
        try:
            with home_scope(self.home): return self.get_engine().tool.handle(args,**kwargs)
        except Exception:
            LOG.warning('heartbeat2 tool adapter failed',exc_info=True)
            return {'error':'engine_unavailable'}

    def command(self,raw_args):
        with home_scope(self.home):
            from .commands import handle
            return handle(raw_args,self)

    def start(self):
        with self._engine_lock:
            if self.closed or self._thread and self._thread.is_alive(): return
            stop=threading.Event()
            self._stop=stop
            self._thread=threading.Thread(target=self._bootstrap,args=(stop,),name='heartbeat2-bootstrap',daemon=True)
            self._threads.append(self._thread)
            self._thread.start()

    def restart(self):
        # Cooperative replacement: old thread retains its own lock until it
        # exits; the successor cannot claim while that lock is held.
        with self._engine_lock:
            if self.closed: return
            self._stop.set()
            self._wake.set()
            self._thread=None
            self.start()

    def _bootstrap(self,stop):
        runner=None
        next_tick=self.clock.monotonic()
        try:
            with home_scope(self.home):
                while not stop.is_set():
                    manager=self.ctx._manager
                    if getattr(manager,'_cli_ref',None) is not None or not getattr(manager,'has_gateway_message_injector',False):
                        stop.wait(self.bootstrap_interval)
                        continue
                    if runner is None:
                        from .poller import Poller
                        e=self.get_engine()
                        runner=Poller(self.ctx,e.store,e.policy,self.clock)
                        runner.refresh_policy=self.policy
                    if not runner.acquire():
                        stop.wait(30)
                        continue
                    remaining=next_tick-self.clock.monotonic()
                    if remaining>0:
                        self._wake.wait(min(remaining,self.bootstrap_interval))
                        self._wake.clear()
                        continue
                    runner.tick()
                    next_tick=self.clock.monotonic()+self.tick_interval
        except Exception:
            LOG.warning('heartbeat2 bootstrap stopped',exc_info=True)
        finally:
            if runner: runner.close()

    def close(self):
        with self._engine_lock:
            if self.closed: return
            self.closed=True
            self._stop.set()
            self._wake.set()
        for thread in self._threads:
            if thread is not threading.current_thread(): thread.join(timeout=.5)
        # Do not close a connection under a still-running old tick.
        if self._engine and all(not t.is_alive() for t in self._threads):
            self._engine.close()
            self._engine.store.close()
        with _GUARD:
            if _RUNTIMES.get(self.scope) is self: _RUNTIMES.pop(self.scope,None)


def register(ctx,**options):
    # No SQLite opens, config reads or probes here; the skill registrar may stat its path.
    if getattr(ctx,'_manager',None) is None: return None
    from .tool import SCHEMA,DESCRIPTION
    scope=ctx._manager.scope_key
    with _GUARD:
        runtime=_RUNTIMES.get(scope)
        if runtime is not None and not runtime.closed: return runtime
        runtime=Runtime(ctx,**options)
        _RUNTIMES[scope]=runtime
        try:
            ctx.register_tool(name='pulse',toolset='heartbeat2',schema=SCHEMA,
                              handler=runtime.tool,description=DESCRIPTION)
            for name in HOOKS:
                def callback(_name=name,**kwargs):
                    return runtime.hook(_name,**kwargs)
                ctx.register_hook(name,callback)
            ctx.register_command(name='pulse',handler=runtime.command,
                                 description='HeartBeat 2.0 wakes and watchers',args_hint='<action>')
            skill=getattr(ctx,'register_skill',None)
            if skill:
                skill('heartbeat2-usage',Path(__file__).resolve().parents[1]/'skills/heartbeat2-usage/SKILL.md',
                      'Close named open loops with bounded wakes and deterministic watchers.')
            cleanup=getattr(ctx,'on_unload',None)
            if cleanup: cleanup(runtime.close)
            runtime.start()
            return runtime
        except BaseException:
            runtime.close()
            raise
