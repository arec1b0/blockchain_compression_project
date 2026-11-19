# test_pedersen_commitment.py

import dataclasses

import pytest

from blockchain_compression.zk_proof.encoding import FIELD_HEX_LENGTH, GROUP_ID
from blockchain_compression.zk_proof.groups import G, H, P, Q
from blockchain_compression.zk_proof.pedersen import (
    COMMITMENT_FORMAT_VERSION,
    COMMITMENT_WIRE_PREFIX,
    Commitment,
    PedersenCommitment,
)


def test_group_parameters_land_in_order_q_subgroup():
    # Safety net for the hand-transcribed RFC 3526 constant: a transcription
    # error would very likely break at least one of these.
    assert P.bit_length() == 2048
    assert pow(G, Q, P) == 1
    assert pow(H, Q, P) == 1
    assert H not in (0, 1)
    assert G != H


def test_commit_verify_roundtrip():
    pc = PedersenCommitment()
    commitment, blinding = pc.commit(42)
    assert pc.verify_opening(commitment, 42, blinding)


def test_verify_rejects_wrong_message():
    pc = PedersenCommitment()
    commitment, blinding = pc.commit(42)
    assert not pc.verify_opening(commitment, 43, blinding)


def test_verify_rejects_wrong_blinding():
    pc = PedersenCommitment()
    commitment, blinding = pc.commit(42)
    assert not pc.verify_opening(commitment, 42, blinding + 1)


def test_hiding_same_message_different_blinding_differs():
    pc = PedersenCommitment()
    commitment1, _ = pc.commit(7)
    commitment2, _ = pc.commit(7)
    assert commitment1 != commitment2


def test_explicit_blinding_is_honored():
    pc = PedersenCommitment()
    commitment, blinding = pc.commit(5, blinding=123)
    assert blinding == 123
    assert pc.verify_opening(commitment, 5, 123)


def test_message_out_of_range_raises():
    pc = PedersenCommitment()
    with pytest.raises(ValueError):
        pc.commit(-1)
    with pytest.raises(ValueError):
        pc.commit(Q)
    with pytest.raises(ValueError):
        pc.commit(True)  # bool must not silently pass as an int


def test_blinding_out_of_range_raises():
    pc = PedersenCommitment()
    with pytest.raises(ValueError):
        pc.commit(5, blinding=0)
    with pytest.raises(ValueError):
        pc.commit(5, blinding=-1)
    with pytest.raises(ValueError):
        pc.commit(5, blinding=Q)
    with pytest.raises(ValueError):
        pc.commit(5, blinding=True)
    with pytest.raises(ValueError):
        pc.commit(5, blinding=False)


def test_verify_opening_type_validation():
    pc = PedersenCommitment()
    commitment, blinding = pc.commit(42)
    with pytest.raises(TypeError):
        pc.verify_opening("not a commitment", 42, 1)
    with pytest.raises(TypeError):
        pc.verify_opening(Commitment(value=True), 42, blinding)
    with pytest.raises(TypeError):
        pc.verify_opening(commitment, True, blinding)
    with pytest.raises(TypeError):
        pc.verify_opening(commitment, 42, False)
    with pytest.raises(TypeError):
        pc.verify_opening(commitment, "42", blinding)
    with pytest.raises(TypeError):
        pc.verify_opening(commitment, 42, "1")


def test_verify_opening_rejects_out_of_range_without_raising():
    pc = PedersenCommitment()
    commitment, blinding = pc.commit(42)
    assert not pc.verify_opening(commitment, -1, blinding)
    assert not pc.verify_opening(commitment, Q, blinding)
    assert not pc.verify_opening(commitment, 42, 0)
    assert not pc.verify_opening(commitment, 42, Q)
    assert not pc.verify_opening(Commitment(value=1), 0, 1)
    assert not pc.verify_opening(Commitment(value=P), 42, blinding)


def test_commitment_is_frozen():
    commitment = Commitment(value=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        commitment.value = 2


def test_commitment_dict_and_bytes_roundtrip_and_determinism():
    pc = PedersenCommitment()
    commitment, _ = pc.commit(42)

    encoded_dict = commitment.to_dict()
    assert encoded_dict["version"] == COMMITMENT_FORMAT_VERSION
    assert encoded_dict["group"] == GROUP_ID
    assert isinstance(encoded_dict["value"], str)
    assert len(encoded_dict["value"]) == FIELD_HEX_LENGTH
    assert encoded_dict == commitment.to_dict()
    assert Commitment.from_dict(encoded_dict) == commitment

    encoded_bytes = commitment.to_bytes()
    assert encoded_bytes.startswith(COMMITMENT_WIRE_PREFIX)
    assert len(encoded_bytes) == len(COMMITMENT_WIRE_PREFIX) + FIELD_HEX_LENGTH // 2
    assert encoded_bytes == commitment.to_bytes()
    assert Commitment.from_bytes(encoded_bytes) == commitment


def test_commitment_serialization_rejects_malformed_and_invalid_inputs():
    pc = PedersenCommitment()
    commitment, _ = pc.commit(42)
    valid_dict = commitment.to_dict()
    valid_bytes = commitment.to_bytes()

    with pytest.raises(TypeError):
        Commitment.from_dict("not-a-dict")
    with pytest.raises(TypeError):
        Commitment.from_bytes("not-bytes")
    with pytest.raises(TypeError):
        Commitment(value=True).to_dict()
    with pytest.raises(ValueError):
        Commitment(value=P - 1).to_dict()
    with pytest.raises(ValueError):
        Commitment(value=1).to_bytes()

    for bad_hex in (
        valid_dict["value"].upper(),
        "0x" + valid_dict["value"],
        valid_dict["value"][:-2],
        valid_dict["value"] + "00",
        "z" * FIELD_HEX_LENGTH,
        " " + valid_dict["value"][1:],
        f"{P - 1:0{FIELD_HEX_LENGTH}x}",
        f"{P:0{FIELD_HEX_LENGTH}x}",
        f"{1:0{FIELD_HEX_LENGTH}x}",
    ):
        with pytest.raises(ValueError):
            Commitment.from_dict({**valid_dict, "value": bad_hex})

    with pytest.raises(ValueError):
        Commitment.from_dict({**valid_dict, "version": 2})
    with pytest.raises(ValueError):
        Commitment.from_dict({**valid_dict, "version": True})
    with pytest.raises(ValueError):
        Commitment.from_dict({**valid_dict, "group": "other-group"})
    with pytest.raises(ValueError):
        Commitment.from_dict({"version": 1, "value": valid_dict["value"]})
    with pytest.raises(TypeError):
        Commitment.from_dict({**valid_dict, "value": 123})

    with pytest.raises(ValueError):
        Commitment.from_bytes(valid_bytes[:-1])
    with pytest.raises(ValueError):
        Commitment.from_bytes(valid_bytes + b"\x00")
    with pytest.raises(ValueError):
        Commitment.from_bytes(b"BADHDR\x01" + valid_bytes[len(COMMITMENT_WIRE_PREFIX) :])
    with pytest.raises(ValueError):
        Commitment.from_bytes(
            COMMITMENT_WIRE_PREFIX + (P - 1).to_bytes(FIELD_HEX_LENGTH // 2, "big")
        )
