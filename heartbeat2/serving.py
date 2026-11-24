"""Does this process's gateway serve a plugin manager's profile?

Hermes publishes its gateway injector onto every plugin manager the process
knows, including profiles a boot preflight loaded but this gateway does not
serve, so the injector alone does not prove ownership. The injector's owner
(the gateway runner) answers with the rule Hermes itself uses to route plugin
injections. That rule lists the profiles on disk, so a multiplex host must also
have admitted the home: it skips a profile that still runs its own gateway. The
served set changes at runtime, so callers ask on every step.
"""
import logging
import os

LOG=logging.getLogger('heartbeat2')
SERVED,BACKEND,FOREIGN,UNKNOWN,NO_GATEWAY='served','backend','foreign','unknown','no_gateway'
REASONS={FOREIGN:'this gateway does not serve this profile',
         UNKNOWN:'cannot tell whether this gateway serves this profile',
         NO_GATEWAY:'no gateway in this process'}
_WARNED=set()


def pid_alive(pid):
    if not pid or pid <= 0: return False
    try: os.kill(pid,0)
    except ProcessLookupError: return False
    except PermissionError: pass  # The process exists even if we cannot signal it.
    return True


def _create_time(psutil, pid):
    try: return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied): return None


def backend_epoch(nonce):
    pid=os.getpid()
    try: import psutil
    except ImportError: created=None
    else: created=_create_time(psutil,pid)
    return f'{pid}:{created if created is not None else ""}:{nonce}'


def backend_epoch_alive(epoch, *, pid=None):
    owner,_,identity=(epoch or '').partition(':')
    if not owner.isdecimal(): return False
    owner=int(owner)
    if (pid is not None and owner!=pid) or not pid_alive(owner): return False
    # Fall back to PID-only detection when Hermes does not provide psutil.
    try: import psutil
    except ImportError: return True
    created,sep,_=identity.partition(':')
    if not sep or not created: return False
    return created==str(_create_time(psutil,owner))


def state(manager):
    """SERVED, BACKEND, FOREIGN, UNKNOWN (treat as foreign) or NO_GATEWAY."""
    if manager is None or getattr(manager,'_cli_ref',None) is not None:
        return NO_GATEWAY
    if not getattr(manager,'has_gateway_message_injector',False):
        return BACKEND if getattr(manager,'has_tui_message_injector',False) else NO_GATEWAY
    injector=getattr(manager,'_gateway_message_injector',())  # Read once: shutdown clears it to None.
    if injector is None: return NO_GATEWAY
    try:
        owner=injector[0]
        if owner._served_profile_for_home(manager.home_path) is None: return FOREIGN
        if getattr(getattr(owner,'config',None),'multiplex_profiles',False):
            homes=owner._served_profile_homes
            if homes is None: return FOREIGN
            from hermes_constants import hermes_home_key
            key=hermes_home_key(manager.home_path)
            if not any(hermes_home_key(home)==key for home in tuple(homes.values())): return FOREIGN
        return SERVED
    except Exception:
        key=str(getattr(manager,'scope_key',''))
        if key not in _WARNED:
            _WARNED.add(key)
            LOG.warning('heartbeat2 cannot tell whether this gateway serves %s; staying inactive',key,exc_info=True)
        return UNKNOWN


def foreign(manager):
    """A gateway runs here but does not (provably) serve this profile."""
    return state(manager) in (FOREIGN,UNKNOWN)
