# test_state_delta.py

import pytest

from blockchain_compression.compression import StateDelta


def _tx(tx_id, account, amount):
    return {"tx_id": tx_id, "account": account, "amount": amount}


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

    hydrated = StateDelta.from_snapshot(
        original.get_current_state(), original.get_applied_tx_ids()
    )

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
    # Nothing was applied by the failed calls.
    assert state_delta.get_current_state() == {}
