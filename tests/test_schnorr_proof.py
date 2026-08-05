# test_schnorr_proof.py

import dataclasses

import pytest

from blockchain_compression.observability import MetricsRegistry
from blockchain_compression.zk_proof.pedersen import Commitment, PedersenCommitment
from blockchain_compression.zk_proof.schnorr import SchnorrProof, generate_proof, verify_proof


def test_schnorr_valid_proof_verifies():
    pc = PedersenCommitment()
    commitment, blinding = pc.commit(42)
    proof = generate_proof(message=42, blinding=blinding, commitment=commitment)

    assert verify_proof(proof, commitment)


def test_verify_proof_does_not_need_message_or_blinding():
    # The actual zero-knowledge property: verification only ever takes the
    # commitment (plus an optional metrics sink) - there is no parameter
    # through which verify_proof could even accept a message or blinding.
    import inspect

    params = list(inspect.signature(verify_proof).parameters)
    assert params == ["proof", "commitment", "metrics"]


def test_schnorr_proof_object_has_no_message_or_blinding_fields():
    pc = PedersenCommitment()
    commitment, blinding = pc.commit(42)
    proof = generate_proof(message=42, blinding=blinding, commitment=commitment)

    field_names = {f.name for f in dataclasses.fields(proof)}
    assert field_names == {"t", "s1", "s2"}


def test_schnorr_rejects_tampered_s1():
    pc = PedersenCommitment()
    commitment, blinding = pc.commit(42)
    proof = generate_proof(message=42, blinding=blinding, commitment=commitment)

    tampered = dataclasses.replace(proof, s1=proof.s1 + 1)
    assert not verify_proof(tampered, commitment)


def test_schnorr_rejects_tampered_s2():
    pc = PedersenCommitment()
    commitment, blinding = pc.commit(42)
    proof = generate_proof(message=42, blinding=blinding, commitment=commitment)

    tampered = dataclasses.replace(proof, s2=proof.s2 + 1)
    assert not verify_proof(tampered, commitment)


def test_schnorr_rejects_tampered_t():
    pc = PedersenCommitment()
    commitment, blinding = pc.commit(42)
    proof = generate_proof(message=42, blinding=blinding, commitment=commitment)

    tampered = dataclasses.replace(proof, t=(proof.t + 1) % (2**2048))
    assert not verify_proof(tampered, commitment)


def test_schnorr_proof_not_bound_to_different_commitment():
    pc = PedersenCommitment()
    commitment, blinding = pc.commit(42)
    other_commitment, _ = pc.commit(43)
    proof = generate_proof(message=42, blinding=blinding, commitment=commitment)

    assert not verify_proof(proof, other_commitment)


def test_schnorr_forged_opening_does_not_verify():
    # A cheating prover who doesn't know the real opening tries a made-up one.
    pc = PedersenCommitment()
    commitment, _real_blinding = pc.commit(42)

    forged = generate_proof(message=99, blinding=12345, commitment=commitment)

    assert not verify_proof(forged, commitment)


def test_two_proofs_same_opening_produce_different_t():
    # Nonce-freshness proxy: reusing a nonce across proofs would invalidate the
    # security proof, so k1/k2 (and therefore t) must differ every call.
    pc = PedersenCommitment()
    commitment, blinding = pc.commit(42)

    proof1 = generate_proof(message=42, blinding=blinding, commitment=commitment)
    proof2 = generate_proof(message=42, blinding=blinding, commitment=commitment)

    assert proof1.t != proof2.t


def test_generate_proof_rejects_out_of_range_inputs():
    pc = PedersenCommitment()
    commitment, blinding = pc.commit(42)
    with pytest.raises(ValueError):
        generate_proof(message=-1, blinding=blinding, commitment=commitment)


def test_generate_proof_type_validation():
    with pytest.raises(TypeError):
        generate_proof(message=1, blinding=1, commitment="not a commitment")


def test_verify_proof_type_validation():
    commitment = Commitment(value=1)
    with pytest.raises(TypeError):
        verify_proof("not a proof", commitment)
    with pytest.raises(TypeError):
        verify_proof(SchnorrProof(t=1, s1=1, s2=1), "not a commitment")


def test_generate_and_verify_proof_record_latency_histograms():
    metrics = MetricsRegistry()
    pc = PedersenCommitment()
    commitment, blinding = pc.commit(42)

    proof = generate_proof(message=42, blinding=blinding, commitment=commitment, metrics=metrics)
    verify_proof(proof, commitment, metrics=metrics)

    assert metrics._histogram_totals[("proof_generation_seconds", ())] == 1
    assert metrics._histogram_totals[("proof_verification_seconds", ())] == 1


def test_verify_proof_rejects_out_of_range_commitment_without_raising():
    from blockchain_compression.zk_proof.groups import P

    bad_commitment = Commitment(value=P)  # out of [1, P-1]
    proof = SchnorrProof(t=1, s1=1, s2=1)
    assert not verify_proof(proof, bad_commitment)
