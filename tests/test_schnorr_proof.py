# test_schnorr_proof.py

import dataclasses
import sys
import types

import pytest

from blockchain_compression.observability import MetricsRegistry
from blockchain_compression.zk_proof.encoding import FIELD_HEX_LENGTH, GROUP_ID
from blockchain_compression.zk_proof.groups import (
    ZK_MODEXP_BACKEND,
    G,
    H,
    P,
    Q,
    resolve_modexp_backend,
)
from blockchain_compression.zk_proof.pedersen import Commitment, PedersenCommitment
from blockchain_compression.zk_proof.schnorr import (
    PROOF_FORMAT_VERSION,
    PROOF_WIRE_PREFIX,
    SchnorrProof,
    _challenge,
    generate_proof,
    verify_proof,
)


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
    with pytest.raises(ValueError):
        generate_proof(message=Q, blinding=blinding, commitment=commitment)
    with pytest.raises(ValueError):
        generate_proof(message=42, blinding=0, commitment=commitment)
    with pytest.raises(ValueError):
        generate_proof(message=42, blinding=Q, commitment=commitment)
    with pytest.raises(ValueError):
        generate_proof(message=42, blinding=blinding, commitment=Commitment(value=P - 1))
    with pytest.raises(ValueError):
        generate_proof(message=42, blinding=blinding, commitment=Commitment(value=1))


def test_generate_proof_type_validation():
    pc = PedersenCommitment()
    commitment, blinding = pc.commit(42)
    with pytest.raises(TypeError):
        generate_proof(message=1, blinding=1, commitment="not a commitment")
    with pytest.raises(TypeError):
        generate_proof(message=True, blinding=blinding, commitment=commitment)
    with pytest.raises(TypeError):
        generate_proof(message=42, blinding=True, commitment=commitment)
    with pytest.raises(TypeError):
        generate_proof(message="42", blinding=blinding, commitment=commitment)
    with pytest.raises(TypeError):
        generate_proof(message=42, blinding=blinding, commitment=Commitment(value=True))


def test_verify_proof_type_validation():
    commitment = Commitment(value=G)
    with pytest.raises(TypeError):
        verify_proof("not a proof", commitment)
    with pytest.raises(TypeError):
        verify_proof(SchnorrProof(t=1, s1=1, s2=1), "not a commitment")
    with pytest.raises(TypeError):
        verify_proof(SchnorrProof(t=1, s1=1, s2=1), Commitment(value=True))
    with pytest.raises(TypeError):
        verify_proof(SchnorrProof(t=True, s1=1, s2=1), commitment)
    with pytest.raises(TypeError):
        verify_proof(SchnorrProof(t=1, s1=True, s2=1), commitment)
    with pytest.raises(TypeError):
        verify_proof(SchnorrProof(t=1, s1=1, s2=False), commitment)
    with pytest.raises(TypeError):
        verify_proof(SchnorrProof(t=1, s1="1", s2=1), commitment)


def test_generate_and_verify_proof_record_latency_histograms():
    metrics = MetricsRegistry()
    pc = PedersenCommitment()
    commitment, blinding = pc.commit(42)

    proof = generate_proof(message=42, blinding=blinding, commitment=commitment, metrics=metrics)
    verify_proof(proof, commitment, metrics=metrics)

    gen_snap = metrics.get_histogram("proof_generation_seconds")
    ver_snap = metrics.get_histogram("proof_verification_seconds")
    assert gen_snap is not None and gen_snap.count == 1
    assert ver_snap is not None and ver_snap.count == 1


def test_verify_proof_rejects_out_of_range_commitment_without_raising():
    bad_commitment = Commitment(value=P)  # out of (1, P)
    proof = SchnorrProof(t=1, s1=1, s2=1)
    assert not verify_proof(proof, bad_commitment)
    assert not verify_proof(proof, Commitment(value=1))
    assert not verify_proof(proof, Commitment(value=0))
    assert not verify_proof(proof, Commitment(value=-1))


def test_verify_proof_rejects_non_subgroup_commitment_forgery():
    # C = P - 1 has order 2 and is not in the order-Q Pedersen subgroup.
    # Even if a cheating prover chooses t = (P - 1) * g^k1 * h^k2 mod P when e
    # is odd so the algebraic equation g^s1 * h^s2 == t * C^e holds mod P,
    # subgroup membership validation must reject it.
    non_subgroup_commitment = Commitment(value=P - 1)
    assert pow(non_subgroup_commitment.value, Q, P) != 1

    for k1 in range(1, 50):
        k2 = k1 + 7
        t = ((P - 1) * pow(G, k1, P) * pow(H, k2, P)) % P
        e = _challenge(non_subgroup_commitment.value, t)
        if e % 2 == 1:
            forged = SchnorrProof(t=t, s1=k1, s2=k2)
            assert not verify_proof(forged, non_subgroup_commitment)
            break
    else:
        pytest.fail("did not find odd challenge in search window")


def test_verify_proof_rejects_out_of_subgroup_t():
    pc = PedersenCommitment()
    commitment, _ = pc.commit(42)
    # P - 1 is in [1, P) but has order 2 (not in order-Q subgroup).
    bad_t_proof = SchnorrProof(t=P - 1, s1=1, s2=1)
    assert not verify_proof(bad_t_proof, commitment)


def test_verify_proof_rejects_invalid_and_oversized_scalar_ranges():
    pc = PedersenCommitment()
    commitment, blinding = pc.commit(42)
    valid_proof = generate_proof(message=42, blinding=blinding, commitment=commitment)

    for bad_s in (-1, -Q, Q, Q + 1, 10**10000):
        assert not verify_proof(dataclasses.replace(valid_proof, s1=bad_s), commitment)
        assert not verify_proof(dataclasses.replace(valid_proof, s2=bad_s), commitment)
    for bad_t in (0, -1, P, P + 1):
        assert not verify_proof(dataclasses.replace(valid_proof, t=bad_t), commitment)


def test_schnorr_proof_dict_and_bytes_roundtrip_and_determinism():
    pc = PedersenCommitment()
    commitment, blinding = pc.commit(42)
    proof = generate_proof(message=42, blinding=blinding, commitment=commitment)

    encoded_dict = proof.to_dict()
    assert encoded_dict["version"] == PROOF_FORMAT_VERSION
    assert encoded_dict["group"] == GROUP_ID
    for field_name in ("t", "s1", "s2"):
        assert isinstance(encoded_dict[field_name], str)
        assert len(encoded_dict[field_name]) == FIELD_HEX_LENGTH
    assert encoded_dict == proof.to_dict()
    restored_from_dict = SchnorrProof.from_dict(encoded_dict)
    assert restored_from_dict == proof
    assert verify_proof(restored_from_dict, commitment)

    encoded_bytes = proof.to_bytes()
    assert encoded_bytes.startswith(PROOF_WIRE_PREFIX)
    assert len(encoded_bytes) == len(PROOF_WIRE_PREFIX) + 3 * (FIELD_HEX_LENGTH // 2)
    assert encoded_bytes == proof.to_bytes()
    restored_from_bytes = SchnorrProof.from_bytes(encoded_bytes)
    assert restored_from_bytes == proof
    assert verify_proof(restored_from_bytes, commitment)


def test_schnorr_proof_serialization_rejects_malformed_and_invalid_inputs():
    pc = PedersenCommitment()
    commitment, blinding = pc.commit(42)
    proof = generate_proof(message=42, blinding=blinding, commitment=commitment)
    valid_dict = proof.to_dict()
    valid_bytes = proof.to_bytes()

    with pytest.raises(TypeError):
        SchnorrProof.from_dict("not-a-dict")
    with pytest.raises(TypeError):
        SchnorrProof.from_bytes("not-bytes")
    with pytest.raises(TypeError):
        SchnorrProof(t=True, s1=1, s2=1).to_dict()
    with pytest.raises(ValueError):
        SchnorrProof(t=P - 1, s1=1, s2=1).to_dict()
    with pytest.raises(ValueError):
        SchnorrProof(t=G, s1=Q, s2=1).to_bytes()

    for bad_hex in (
        valid_dict["t"].upper(),
        "0x" + valid_dict["t"],
        valid_dict["t"][:-2],
        valid_dict["t"] + "00",
        "g" * FIELD_HEX_LENGTH,
        f"{P - 1:0{FIELD_HEX_LENGTH}x}",
        f"{P:0{FIELD_HEX_LENGTH}x}",
    ):
        with pytest.raises(ValueError):
            SchnorrProof.from_dict({**valid_dict, "t": bad_hex})

    for bad_scalar_hex in (
        f"{Q:0{FIELD_HEX_LENGTH}x}",
        valid_dict["s1"][:-2],
        valid_dict["s1"].upper(),
    ):
        with pytest.raises(ValueError):
            SchnorrProof.from_dict({**valid_dict, "s1": bad_scalar_hex})
        with pytest.raises(ValueError):
            SchnorrProof.from_dict({**valid_dict, "s2": bad_scalar_hex})

    with pytest.raises(ValueError):
        SchnorrProof.from_dict({**valid_dict, "version": 2})
    with pytest.raises(ValueError):
        SchnorrProof.from_dict({**valid_dict, "version": True})
    with pytest.raises(ValueError):
        SchnorrProof.from_dict({**valid_dict, "group": "other"})
    with pytest.raises(ValueError):
        SchnorrProof.from_dict({"version": 1, "group": GROUP_ID, "t": valid_dict["t"]})
    with pytest.raises(TypeError):
        SchnorrProof.from_dict({**valid_dict, "s1": 123})

    with pytest.raises(ValueError):
        SchnorrProof.from_bytes(valid_bytes[:-1])
    with pytest.raises(ValueError):
        SchnorrProof.from_bytes(valid_bytes + b"\x00")
    with pytest.raises(ValueError):
        SchnorrProof.from_bytes(b"BADHDR\x01" + valid_bytes[len(PROOF_WIRE_PREFIX) :])
    bad_t_bytes = (
        PROOF_WIRE_PREFIX
        + (P - 1).to_bytes(FIELD_HEX_LENGTH // 2, "big")
        + valid_bytes[len(PROOF_WIRE_PREFIX) + FIELD_HEX_LENGTH // 2 :]
    )
    with pytest.raises(ValueError):
        SchnorrProof.from_bytes(bad_t_bytes)


def test_modexp_backend_resolution_and_optional_gmpy2_fallback(monkeypatch, caplog):
    assert ZK_MODEXP_BACKEND == "python"
    name_py, fn_py = resolve_modexp_backend("python")
    assert name_py == "python"
    assert fn_py is pow

    # Missing optional gmpy2 falls back cleanly without failing.
    monkeypatch.setitem(sys.modules, "gmpy2", None)
    with caplog.at_level("WARNING"):
        name_fb, fn_fb = resolve_modexp_backend("gmpy2")
    assert name_fb == "python"
    assert fn_fb is pow
    assert "falling back" in caplog.text

    # Present gmpy2 delegates cleanly to gmpy2.powmod with identical arithmetic.
    calls: list[tuple[int, int, int]] = []
    fake_gmpy2 = types.ModuleType("gmpy2")

    def _fake_powmod(base: int, exp: int, mod: int) -> int:
        calls.append((base, exp, mod))
        return pow(base, exp, mod)

    fake_gmpy2.powmod = _fake_powmod
    monkeypatch.setitem(sys.modules, "gmpy2", fake_gmpy2)
    name_opt, fn_opt = resolve_modexp_backend("gmpy2")
    assert name_opt == "gmpy2"
    assert fn_opt(G, 12345, P) == pow(G, 12345, P)
    assert calls == [(G, 12345, P)]

    with pytest.raises(ValueError, match="unsupported"):
        resolve_modexp_backend("openssl-unknown")
