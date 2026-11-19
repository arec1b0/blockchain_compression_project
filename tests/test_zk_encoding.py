# test_zk_encoding.py

import hashlib
import json

import pytest

from blockchain_compression.canonical import canonical_json
from blockchain_compression.zk_proof.encoding import _DOMAIN, hash_to_scalar
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


def test_canonical_json_rejects_bytes_and_prevents_string_collision():
    # Previously, {"tx_id": b"1"} and {"tx_id": "b'1'"} both serialized to
    # '{"tx_id":"b\'1\'"}'. Now raw bytes must raise TypeError.
    with pytest.raises(TypeError):
        canonical_json({"tx_id": b"1"})
    with pytest.raises(TypeError):
        hash_to_scalar({"tx_id": b"1"})
    assert canonical_json({"tx_id": "b'1'"}) == '{"tx_id":"b\'1\'"}'


def test_canonical_json_rejects_unsupported_objects():
    with pytest.raises(TypeError):
        canonical_json({"obj": object()})
    with pytest.raises(TypeError):
        canonical_json({"set": {1, 2}})


@pytest.mark.parametrize("bad_float", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_and_hash_to_scalar_reject_non_finite_floats(bad_float):
    with pytest.raises(ValueError):
        canonical_json({"x": bad_float})
    with pytest.raises(ValueError):
        hash_to_scalar({"x": bad_float})


def test_canonical_json_is_valid_rfc8259_and_deterministic():
    payload = {"z": [1, 2, {"b": True, "a": None}], "a": "hello"}
    encoded = canonical_json(payload)
    assert encoded == '{"a":"hello","z":[1,2,{"a":null,"b":true}]}'
    assert json.loads(encoded) == payload


def test_hash_to_scalar_includes_versioned_domain_prefix():
    data = {"tx_id": "tx-1", "account": "Alice", "amount": 100}
    raw_bytes = canonical_json(data).encode("utf-8")
    unprefixed = int.from_bytes(hashlib.sha256(raw_bytes).digest(), "big") % Q
    expected = int.from_bytes(hashlib.sha256(_DOMAIN + raw_bytes).digest(), "big") % Q
    assert _DOMAIN == b"blockchain-compression-project/tx-scalar/v1"
    assert hash_to_scalar(data) == expected
    assert hash_to_scalar(data) != unprefixed
