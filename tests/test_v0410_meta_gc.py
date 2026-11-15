"""#54: gc removes per-occurrence meta keys together with their occurrences, and nothing else."""
import pytest

from test_store import seed, store  # noqa: F401  (fixture)

# Every meta key the plugin writes as '<prefix><occurrence id>'. A new per-occurrence key belongs here and in the store's list.
PREFIXES = ('accepted:', 'engaged:', 'governed:', 'ignored:', 'inject_failures:', 'model_refunded:',
            'not_before:', 'provider_error:', 'recovered:', 'report_shown:', 'spend:')


def missed(store, wake, n):
    key = f"t:{n}"
    store.add_occurrence(wake.id, key)
    store.cas_occurrence(wake.id, key, "pending", "missed")
    return store.get_occurrence(wake.id, key)


@pytest.mark.parametrize("prefix", PREFIXES)
def test_gc_prunes_markers_of_occurrences_it_removed(store, clock, prefix):
    wake = seed(store)
    old, kept = missed(store, wake, 1000), missed(store, wake, 1001)
    store.set_meta(prefix + old.id, "1")
    store.set_meta(prefix + kept.id, "1")
    store.set_meta(prefix + wake.id + "/gone:1.5", "1")
    clock.advance(3 * 86400)
    # seed() leaves one live occurrence, so two keep exactly the newest terminal one.
    store.gc(ledger_days=30, keep_occurrences=2)
    assert store.get_occurrence(wake.id, old.dedupe_key) is None and store.get_occurrence(wake.id, kept.dedupe_key)
    assert store.get_meta(prefix + old.id) is None
    assert store.get_meta(prefix + wake.id + "/gone:1.5") is None
    assert store.get_meta(prefix + kept.id) == "1"


def test_gc_keeps_every_other_meta_key(store, clock):
    wake = seed(store)
    gone = wake.id + "/gone:1.5"
    others = {
        # Replay guards and per-wake, per-session and per-day keys are never occurrence markers.
        "human:abc": "1", f"nonhuman:{wake.session_key}:42": "1", "card:" + wake.id: "noop",
        "resume_after:" + wake.id: "1", "owner_report:sid": "{}", "quiet:" + wake.session_key: "{}",
        # '_' is a LIKE wildcard: these look like prefixed keys to a LIKE match, not to an exact one.
        "notXbefore:" + gone: "1", "reportXshown:" + gone: "1", "model-refunded:" + gone: "1",
        # A prefixed key with no occurrence id after it isn't a marker.
        "accepted:no-slash": "1", "governed:": "1",
    }
    for key, value in others.items():
        store.set_meta(key, value)
    clock.advance(3 * 86400)
    store.gc(ledger_days=30)
    for key, value in others.items():
        assert store.get_meta(key) == value, key


def test_store_prunes_exactly_the_listed_prefixes():
    from heartbeat2.store import OCCURRENCE_META_PREFIXES
    assert set(OCCURRENCE_META_PREFIXES) == set(PREFIXES)


def test_gc_prunes_child_counters_with_their_occurrence(store, clock):
    """children:<occurrence id>:<attempt> counts what a wake turn armed; it goes when gc removes the turn's row."""
    wake = seed(store)
    old, kept = missed(store, wake, 1000), missed(store, wake, 1001)
    live_key = "e:msg/42:7"  # A dedupe key with ':' and '/' in it.
    store.add_occurrence(wake.id, live_key)
    store.set_meta(f"children:{old.id}:1", "2")
    store.set_meta(f"children:{kept.id}:3", "1")
    store.set_meta(f"children:{wake.id}/{live_key}:2", "1")
    malformed = {f"children:{wake.id}/gone": "1", f"children:{wake.id}/gone:x": "1", "children:no-slash:1": "1"}
    for key, value in malformed.items():
        store.set_meta(key, value)
    clock.advance(3 * 86400)
    store.gc(ledger_days=30, keep_occurrences=3)
    assert store.get_occurrence(wake.id, old.dedupe_key) is None
    assert store.get_meta(f"children:{old.id}:1") is None
    assert store.get_meta(f"children:{kept.id}:3") == "1"
    assert store.get_meta(f"children:{wake.id}/{live_key}:2") == "1"
    for key, value in malformed.items():
        assert store.get_meta(key) == value, key


def test_gc_keeps_child_counter_of_a_recently_claimed_turn(store, clock):
    """A turn's row is kept for two days after its claim, so its counter outlives the turn even past the history limit."""
    wake = seed(store)
    claimed = missed(store, wake, 1000)
    store.connection.execute("UPDATE occurrences SET claimed_at=? WHERE wake_id=? AND dedupe_key=?",
                             (clock.now() + 86400, wake.id, claimed.dedupe_key))
    missed(store, wake, 1001)
    store.set_meta(f"children:{claimed.id}:1", "1")
    clock.advance(1.5 * 86400)
    store.gc(ledger_days=30, keep_occurrences=2)
    assert store.get_occurrence(wake.id, claimed.dedupe_key) is not None
    assert store.get_meta(f"children:{claimed.id}:1") == "1"
