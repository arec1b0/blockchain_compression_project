# test_zk_proofs.py

import pytest

from blockchain_compression.zk_snark import ZKSnark


def test_generate_proof():
    zk = ZKSnark()
    data = {"transaction": "tx1", "amount": 100}

    proof = zk.generate_proof(data)
    assert proof["proof"] == ZKSnark.PROOF_TAG
    assert "data_hash" in proof


def test_verify_proof():
    zk = ZKSnark()
    data = {"transaction": "tx1", "amount": 100}

    proof = zk.generate_proof(data)
    assert zk.verify_proof(proof, data)


def test_verify_rejects_tampered_data():
    zk = ZKSnark()
    proof = zk.generate_proof({"transaction": "tx1", "amount": 100})
    assert not zk.verify_proof(proof, {"transaction": "tx1", "amount": 999})


def test_verify_rejects_malformed_proof():
    zk = ZKSnark()
    data = {"transaction": "tx1", "amount": 100}
    assert not zk.verify_proof({}, data)
    assert not zk.verify_proof({"proof": ZKSnark.PROOF_TAG, "data_hash": 123}, data)


def test_hash_is_key_order_independent():
    assert ZKSnark.hash_data({"a": 1, "b": 2}) == ZKSnark.hash_data({"b": 2, "a": 1})


def test_input_type_validation():
    zk = ZKSnark()
    with pytest.raises(TypeError):
        zk.generate_proof("not a dict")
    with pytest.raises(TypeError):
        zk.verify_proof("not a dict", {})
    with pytest.raises(TypeError):
        zk.verify_proof({}, "not a dict")
