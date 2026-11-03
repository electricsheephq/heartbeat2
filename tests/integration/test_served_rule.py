"""#113 seam: heartbeat2's served check against Hermes's own routing rule and home keys."""
import importlib
import os
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from heartbeat2 import serving


def _injection_mixin():
    try: return importlib.import_module('gateway.run_plugin_injection').GatewayPluginInjectionMixin
    except ImportError:
        if os.environ.get('HEARTBEAT2_REQUIRE_HERMES')=='1':
            pytest.fail('HEARTBEAT2_REQUIRE_HERMES=1 but Hermes is not importable')
        pytest.skip('Hermes is not importable in this venv')


def test_served_state_follows_hermes_routing_rule(tmp_path):
    Mixin = _injection_mixin()
    from hermes_constants import get_routing_process_hermes_home
    home = Path(get_routing_process_hermes_home())
    other = home/'profiles'/'hb2served'
    other.mkdir(parents=True, exist_ok=True)
    (other/'config.yaml').write_text('model: test\n')
    (tmp_path/'alias').symlink_to(home)

    class Runner(Mixin):
        def __init__(self, multiplex, admitted=None):
            self.config = NS(multiplex_profiles=multiplex)
            self._primary_profile_name = 'default'
            self._served_profile_homes = admitted
    def state(path, runner):
        return serving.state(NS(home_path=Path(path), scope_key=str(path), _cli_ref=None,
                                has_gateway_message_injector=True, _gateway_message_injector=(runner, None)))

    solo = Runner(False)
    assert state(home, solo) == serving.SERVED
    assert state(tmp_path/'alias', solo) == serving.SERVED
    assert state(other, solo) == serving.FOREIGN
    host = Runner(True, {'default': home})
    assert state(home, host) == serving.SERVED
    assert state(other, host) == serving.FOREIGN  # a profile on disk the host did not admit
    host._served_profile_homes['hb2served'] = other
    assert state(other, host) == serving.SERVED
    assert state(tmp_path/'elsewhere', host) == serving.FOREIGN
