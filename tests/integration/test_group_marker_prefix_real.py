"""The real Hermes sender-prefix wire contract authorizes group wakes (#98)."""
from dataclasses import replace
import importlib
import os
from types import SimpleNamespace

import pytest

from heartbeat2.intake import marker
from test_group_marker import claim, group
from test_engine import engine


def test_real_sender_prefix_authorizes_tagged_group_wake(group):
    if os.environ.get('HEARTBEAT2_REQUIRE_HERMES') != '1':
        pytest.skip('set HEARTBEAT2_REQUIRE_HERMES for pinned wire acceptance')
    inbound = importlib.import_module('gateway.run_inbound')
    session = importlib.import_module('gateway.session')
    prefix = getattr(inbound.GatewayInboundMixin, '_prefix_inbound_sender_context', None)
    if prefix is None:
        pytest.skip('sender-prefix method is absent at this Hermes pin')
    runner = SimpleNamespace(config=SimpleNamespace(group_sessions_per_user=True,
                                                    thread_sessions_per_user=False))
    source = session.SessionSource(platform=inbound.Platform.TELEGRAM, chat_type='group',
        chat_id='unit', thread_id='topic', user_name='Owner ] Name')
    event = SimpleNamespace(channel_context=None)
    e = group
    _, o, text = claim(e)
    prefixed = prefix(runner, event, source, text)
    assert prefixed == '[Owner ] Name] '+text
    assert marker(prefixed, secret=e.store.get_meta('marker_secret')) == (o.wake_id, o.dedupe_key, o.attempt)
    assert prefix(runner, event, replace(source, chat_type='dm'), text) == text
