# test_zk_attachment.py

import pytest

from blockchain_compression.observability import MetricsRegistry
from blockchain_compression.zk_proof import (
    ATTACHMENT_FORMAT_VERSION,
    Commitment,
    PedersenCommitment,
    ZKAttachment,
    generate_proof,
    hash_to_scalar,
)


def test_zk_attachment_roundtrip_and_opt_in_verification():
    tx = {"tx_id": "tx-1", "account": "Alice", "amount": 100}
    scalar = hash_to_scalar(tx)
    commitment, blinding = PedersenCommitment().commit(scalar)
    proof = generate_proof(message=scalar, blinding=blinding, commitment=commitment)

    attachment = ZKAttachment(commitment=commitment, proof=proof, tx_id="tx-1")
    encoded = attachment.to_dict()
    assert encoded["version"] == ATTACHMENT_FORMAT_VERSION
    assert encoded["tx_id"] == "tx-1"
    assert encoded == attachment.to_dict()

    restored = ZKAttachment.from_dict(encoded)
    assert restored == attachment

    metrics = MetricsRegistry()
    assert restored.verify(metrics=metrics) is True
    snap = metrics.get_histogram("proof_verification_seconds")
    assert snap is not None and snap.count == 1


def test_zk_attachment_without_proof_verifies_false():
    commitment, _ = PedersenCommitment().commit(42)
    attachment = ZKAttachment(commitment=commitment, proof=None, tx_id=None)

    restored = ZKAttachment.from_dict(attachment.to_dict())
    assert restored.proof is None
    assert restored.verify() is False


def test_zk_attachment_validation_rejects_malformed_inputs():
    commitment, blinding = PedersenCommitment().commit(42)
    proof = generate_proof(message=42, blinding=blinding, commitment=commitment)
    valid = ZKAttachment(commitment=commitment, proof=proof, tx_id="tx-1").to_dict()

    with pytest.raises(TypeError):
        ZKAttachment(commitment="not-a-commitment")
    with pytest.raises(TypeError):
        ZKAttachment(commitment=commitment, proof="not-a-proof")
    with pytest.raises(ValueError):
        ZKAttachment(commitment=commitment, tx_id="")
    with pytest.raises(ValueError):
        ZKAttachment(commitment=commitment, version=2)
    with pytest.raises(ValueError):
        ZKAttachment(commitment=commitment, version=True)

    with pytest.raises(TypeError):
        ZKAttachment.from_dict("not-a-dict")
    with pytest.raises(ValueError):
        ZKAttachment.from_dict({"version": 1, "commitment": valid["commitment"]})
    with pytest.raises(ValueError):
        ZKAttachment.from_dict({**valid, "version": 2})
    with pytest.raises(ValueError):
        ZKAttachment.from_dict({**valid, "tx_id": ""})
    with pytest.raises(TypeError):
        ZKAttachment.from_dict({**valid, "commitment": Commitment(value=1)})  # wrong type
    with pytest.raises(ValueError):
        ZKAttachment.from_dict({**valid, "commitment": {**valid["commitment"], "version": 99}})
