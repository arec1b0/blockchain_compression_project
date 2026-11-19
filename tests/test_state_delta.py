# test_state_delta.py

from decimal import Decimal

import pytest

from blockchain_compression.compression import BlockDelta, StateDelta, extract_block_delta
from blockchain_compression.compression.state_delta import MAX_BALANCE, MIN_BALANCE


def _tx(tx_id, account, amount):
    return {"tx_id": tx_id, "account": account, "amount": amount}


def test_stage_batch_and_commit_delta_are_atomic():
    state = StateDelta()
    state.apply_transaction(_tx("tx-0", "Alice", 100))
    state.apply_transaction(_tx("tx-untouched", "Carol", 500))

    delta = state.stage_batch([_tx("tx-1", "Alice", -40), _tx("tx-2", "Bob", 25)])
    # Untouched Carol must not appear in staged BlockDelta.
    assert delta == BlockDelta(
        tx_ids=("tx-1", "tx-2"),
        accounts=frozenset({"Alice", "Bob"}),
        updated_accounts={"Alice": 60, "Bob": 25},
    )
    # Staging must not mutate committed state.
    assert state.get_current_state() == {"Alice": 100, "Carol": 500}
    assert not state.is_applied("tx-1")

    # Failed stage_batch (overflow on second tx) leaves state unchanged.
    with pytest.raises(ValueError, match="signed 64-bit"):
        state.stage_batch([_tx("tx-x", "Bob", 10), _tx("tx-y", "Alice", MAX_BALANCE)])
    assert state.get_current_state() == {"Alice": 100, "Carol": 500}
    assert not state.is_applied("tx-x")

    # Committing an invalid delta fails without mutating balances or tx_ids.
    bad_delta = BlockDelta(
        tx_ids=("tx-3", "tx-0"),
        accounts=frozenset({"Bob"}),
        updated_accounts={"Bob": 999},
    )
    with pytest.raises(ValueError, match="already been applied"):
        state.commit_delta(bad_delta)
    assert state.get_current_state() == {"Alice": 100, "Carol": 500}
    assert not state.is_applied("tx-3")

    state.commit_delta(delta)
    assert state.get_current_state() == {"Alice": 60, "Bob": 25, "Carol": 500}
    assert state.is_applied("tx-1") and state.is_applied("tx-2")


def test_bounded_lru_cache_evicts_and_queries_backing_lookup():
    with pytest.raises(ValueError, match="tx_lookup"):
        StateDelta(max_applied_tx_ids=2)
    with pytest.raises(ValueError, match="non-negative integer"):
        StateDelta(max_applied_tx_ids=-1, tx_lookup=lambda _tx_id: False)

    persisted_ids: set[str] = set()
    lookups: list[str] = []

    def _lookup(tx_id: str) -> bool:
        lookups.append(tx_id)
        return tx_id in persisted_ids

    state = StateDelta(max_applied_tx_ids=2, tx_lookup=_lookup)
    for tx_id in ("tx-1", "tx-2"):
        state.apply_transaction(_tx(tx_id, "Alice", 10))
        persisted_ids.add(tx_id)

    # Prior to overflow, unseen IDs return False without hitting _lookup.
    assert state.is_applied("never-seen") is False
    assert lookups == []

    # Promote tx-1 as most-recently-used before adding tx-3 so tx-2 is evicted.
    assert state.is_applied("tx-1") is True
    state.apply_transaction(_tx("tx-3", "Alice", 10))
    persisted_ids.add("tx-3")
    assert state.has_evicted_tx_ids is True
    assert state.cached_applied_tx_count == 2
    assert state.get_applied_tx_ids() == frozenset({"tx-1", "tx-3"})

    # tx-2 was evicted from RAM, so replay protection must fall back to _lookup.
    assert state.is_applied("tx-2") is True
    assert lookups == ["tx-2"]
    assert state.apply_transaction(_tx("tx-2", "Alice", 999)) == {}
    assert state.get_current_state() == {"Alice": 30}

    # Unbounded reversion after overflow must fail closed.
    with pytest.raises(ValueError, match="evictions"):
        state.configure_applied_tx_cache(None)

    # max_applied_tx_ids=0 keeps zero entries in RAM and delegates every lookup.
    zero_cache = StateDelta(max_applied_tx_ids=0, tx_lookup=_lookup)
    zero_cache.apply_transaction(_tx("tx-z", "Bob", 5))
    persisted_ids.add("tx-z")
    assert zero_cache.cached_applied_tx_count == 0
    assert zero_cache.is_applied("tx-z") is True


def test_apply_transaction():
    state_delta = StateDelta()
    assert state_delta.apply_transaction(_tx("tx-1", "Alice", 100)) == {"Alice": 100}
    assert state_delta.apply_transaction(_tx("tx-2", "Bob", 50)) == {"Bob": 50}
    assert state_delta.apply_transaction(_tx("tx-3", "Alice", -30)) == {"Alice": 70}


def test_get_current_state():
    state_delta = StateDelta()
    state_delta.apply_transaction(_tx("tx-1", "Alice", 100))
    state_delta.apply_transaction(_tx("tx-2", "Bob", 50))
    assert state_delta.get_current_state() == {"Alice": 100, "Bob": 50}


def test_get_current_state_returns_copy():
    state_delta = StateDelta()
    state_delta.apply_transaction(_tx("tx-1", "Alice", 100))
    snapshot = state_delta.get_current_state()
    snapshot["Alice"] = 0
    assert state_delta.get_current_state() == {"Alice": 100}


def test_replay_is_idempotent_regression():
    # Regression: replaying the same transaction used to double the balance.
    state_delta = StateDelta()
    tx = _tx("tx-1", "Alice", 100)
    state_delta.apply_transaction(tx)
    replay_delta = state_delta.apply_transaction(tx)
    assert replay_delta == {}
    assert state_delta.get_current_state() == {"Alice": 100}


def test_distinct_tx_ids_with_same_payload_both_apply():
    # Idempotency keys off tx_id, not payload: two separate 100-unit payments count twice.
    state_delta = StateDelta()
    state_delta.apply_transaction(_tx("tx-1", "Alice", 100))
    state_delta.apply_transaction(_tx("tx-2", "Alice", 100))
    assert state_delta.get_current_state() == {"Alice": 200}


def test_get_applied_tx_ids():
    state_delta = StateDelta()
    state_delta.apply_transaction(_tx("tx-1", "Alice", 100))
    state_delta.apply_transaction(_tx("tx-2", "Bob", 50))

    assert state_delta.get_applied_tx_ids() == frozenset({"tx-1", "tx-2"})


def test_from_snapshot_round_trip():
    original = StateDelta()
    original.apply_transaction(_tx("tx-1", "Alice", 100))
    original.apply_transaction(_tx("tx-2", "Bob", 50))

    hydrated = StateDelta.from_snapshot(original.get_current_state(), original.get_applied_tx_ids())

    assert hydrated.get_current_state() == original.get_current_state()
    assert hydrated.get_applied_tx_ids() == original.get_applied_tx_ids()
    # Replaying a tx_id that was part of the snapshot is still correctly a no-op.
    assert hydrated.apply_transaction(_tx("tx-1", "Alice", 999)) == {}
    assert hydrated.get_current_state() == {"Alice": 100, "Bob": 50}


def test_validate_transaction_is_public():
    # Chain.add_block relies on this being callable without an instance.
    StateDelta.validate_transaction(_tx("tx-1", "Alice", 100))
    with pytest.raises(ValueError):
        StateDelta.validate_transaction(_tx("", "Alice", 100))


def test_transaction_validation():
    state_delta = StateDelta()
    with pytest.raises(TypeError):
        state_delta.apply_transaction(["not", "a", "dict"])
    with pytest.raises(ValueError):
        state_delta.apply_transaction({"account": "Alice", "amount": 100})  # missing tx_id
    with pytest.raises(ValueError):
        state_delta.apply_transaction(_tx("", "Alice", 100))
    with pytest.raises(ValueError):
        state_delta.apply_transaction(_tx("tx-1", "", 100))
    with pytest.raises(ValueError):
        state_delta.apply_transaction(_tx("tx-1", "Alice", "100"))
    with pytest.raises(ValueError):
        state_delta.apply_transaction(_tx("tx-1", "Alice", True))
    with pytest.raises(ValueError):
        state_delta.apply_transaction(_tx("tx-1", "Alice", 10.0))
    with pytest.raises(ValueError):
        state_delta.apply_transaction(_tx("tx-1", "Alice", 1.5))
    with pytest.raises(ValueError):
        state_delta.apply_transaction(_tx("tx-1", "Alice", Decimal("10")))
    with pytest.raises(ValueError):
        state_delta.apply_transaction(_tx("tx-1", "Alice", float("nan")))
    with pytest.raises(ValueError):
        state_delta.apply_transaction(_tx("tx-1", "Alice", float("inf")))
    with pytest.raises(ValueError):
        state_delta.apply_transaction(_tx("tx-1", "Alice", MAX_BALANCE + 1))
    with pytest.raises(ValueError):
        state_delta.apply_transaction(_tx("tx-1", "Alice", MIN_BALANCE - 1))
    # Nothing was applied by the failed calls.
    assert state_delta.get_current_state() == {}


def test_cumulative_balance_overflow_is_rejected_without_mutation():
    state_delta = StateDelta()
    state_delta.apply_transaction(_tx("tx-1", "Alice", MAX_BALANCE))
    with pytest.raises(ValueError, match="signed 64-bit integer"):
        state_delta.apply_transaction(_tx("tx-2", "Alice", 1))
    assert state_delta.get_current_state() == {"Alice": MAX_BALANCE}
    assert not state_delta.is_applied("tx-2")


def test_exact_large_integer_base_units():
    state_delta = StateDelta()
    amount = 10**18 + 1
    state_delta.apply_transaction(_tx("tx-wei", "Alice", amount))
    balance = state_delta.get_current_state()["Alice"]
    assert balance == amount
    assert type(balance) is int


def test_from_snapshot_validates_balances_and_tx_ids():
    with pytest.raises(TypeError):
        StateDelta.from_snapshot(["not", "dict"], [])
    with pytest.raises(ValueError):
        StateDelta.from_snapshot({"": 10}, [])
    with pytest.raises(ValueError):
        StateDelta.from_snapshot({"Alice": 10.0}, [])
    with pytest.raises(ValueError):
        StateDelta.from_snapshot({"Alice": MAX_BALANCE + 1}, [])
    with pytest.raises(ValueError):
        StateDelta.from_snapshot({"Alice": 10}, [""])


def test_extract_block_delta_extracts_tx_ids_and_sorted_balances():
    state = StateDelta()
    txs = [
        _tx("tx-2", "Bob", 50),
        _tx("tx-1", "Alice", 100),
        _tx("tx-3", "Bob", -20),
    ]
    for tx in txs:
        state.apply_transaction(tx)
    state.apply_transaction(_tx("tx-0", "Carol", 75))  # untouched by txs

    delta = extract_block_delta(txs, state)
    assert isinstance(delta, BlockDelta)
    assert delta.tx_ids == ("tx-2", "tx-1", "tx-3")
    assert delta.accounts == frozenset({"Alice", "Bob"})
    assert delta.updated_accounts == {"Alice": 100, "Bob": 30}


def test_extract_block_delta_validates_inputs_and_missing_accounts():
    state = StateDelta()
    with pytest.raises(TypeError):
        extract_block_delta("not-a-list", state)
    with pytest.raises(TypeError):
        extract_block_delta([], "not-a-state")
    with pytest.raises(KeyError, match="Alice"):
        extract_block_delta([_tx("tx-1", "Alice", 10)], state)
