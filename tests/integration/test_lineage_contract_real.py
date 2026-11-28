"""Verify HeartBeat's chat routing against the pinned Hermes SQLite lineage."""
import os

import pytest

from test_backend_wakes import bind, hosts
from test_rotating_compaction import arm, K0, K1, K2


def test_pinned_hermes_public_lineage(hosts, tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    try:
        from hermes_state import SessionDB
    except ModuleNotFoundError as exc:
        if exc.name != 'hermes_state':
            raise
        if os.environ.get('HEARTBEAT2_REQUIRE_HERMES') == '1':
            pytest.fail('HEARTBEAT2_REQUIRE_HERMES=1 but hermes_state is not importable')
        pytest.skip('Hermes state module unavailable')
    if not callable(getattr(SessionDB, 'get_compression_tip', None)):
        pytest.skip('Hermes has no compression-lineage API')

    _, e = hosts
    branch, branch_tip, reset, stale = 'branch', 'branch-tip', 'reset', 'stale-sibling'
    path = tmp_path / 'state.db'
    with SessionDB(db_path=path) as db:
        db.create_session(K0, 'desktop')
        bind(monkeypatch, e, K0)
        root_wake = e.store.get_wake(arm(e))
        db.create_session(stale, 'desktop', parent_session_id=K0)
        db.end_session(stale, 'session_end')
        db.end_session(K0, 'compression')
        db.create_session(K1, 'desktop', parent_session_id=K0)
        db.create_session(branch, 'desktop', parent_session_id=K1, model_config={'_branched_from': K1})
        db.create_session(reset, 'desktop', parent_session_id=K1, model_config={'_reset_from': K1})
        bind(monkeypatch, e, branch)
        branch_wake = e.store.get_wake(arm(e))
        db.end_session(K1, 'compression')
        db.create_session(K2, 'desktop', parent_session_id=K1)
        db.end_session(branch, 'compression')
        db.create_session(branch_tip, 'desktop', parent_session_id=branch)

    with SessionDB(db_path=path, read_only=True) as db:
        assert db.get_compression_tip(K0) == K2
        assert db.get_compression_tip(branch) == branch_tip
        assert db.get_session(branch_tip)['parent_session_id'] == branch

    assert e.chat_key(K1, 'desktop') == e.chat_key(K2, 'desktop') == K0
    assert e.chat_key(branch_tip, 'desktop') == branch
    assert e.chat_key(reset, 'desktop') == reset
    assert e.chat_key(stale, 'desktop') == stale
    assert e.deliver_key(root_wake, e.store.get_session(K0)) == K2
    assert e.deliver_key(branch_wake, e.store.get_session(branch)) == branch_tip
