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

LOG=logging.getLogger('heartbeat2')
SERVED,FOREIGN,UNKNOWN,NO_GATEWAY='served','foreign','unknown','no_gateway'
REASONS={FOREIGN:'this gateway does not serve this profile',
         UNKNOWN:'cannot tell whether this gateway serves this profile',
         NO_GATEWAY:'no gateway in this process'}
_WARNED=set()


def state(manager):
    """SERVED, FOREIGN, UNKNOWN (treat as foreign) or NO_GATEWAY."""
    if manager is None or getattr(manager,'_cli_ref',None) is not None or not getattr(manager,'has_gateway_message_injector',False):
        return NO_GATEWAY
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
            if not any(hermes_home_key(home)==key for home in homes.values()): return FOREIGN
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
