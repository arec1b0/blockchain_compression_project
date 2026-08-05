# test_zk_encoding.py

import pytest

from blockchain_compression.zk_proof.encoding import hash_to_scalar
from blockchain_compression.zk_proof.groups import Q


def test_hash_to_scalar_in_range():
    scalar = hash_to_scalar({"tx_id": "tx-1", "account": "Alice", "amount": 100})
    assert 0 <= scalar < Q


def test_hash_to_scalar_is_deterministic():
    data = {"tx_id": "tx-1", "account": "Alice", "amount": 100}
    assert hash_to_scalar(data) == hash_to_scalar(data)


def test_hash_to_scalar_is_key_order_independent():
    assert hash_to_scalar({"a": 1, "b": 2}) == hash_to_scalar({"b": 2, "a": 1})


def test_hash_to_scalar_differs_for_different_data():
    assert hash_to_scalar({"amount": 100}) != hash_to_scalar({"amount": 101})


def test_hash_to_scalar_type_validation():
    with pytest.raises(TypeError):
        hash_to_scalar("not a dict")
