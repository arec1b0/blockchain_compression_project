# test_pedersen_commitment.py

import dataclasses

import pytest

from blockchain_compression.zk_proof.groups import G, H, P, Q
from blockchain_compression.zk_proof.pedersen import Commitment, PedersenCommitment


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
        pc.commit(5, blinding=-1)
    with pytest.raises(ValueError):
        pc.commit(5, blinding=Q)


def test_verify_opening_type_validation():
    pc = PedersenCommitment()
    with pytest.raises(TypeError):
        pc.verify_opening("not a commitment", 42, 1)


def test_verify_opening_rejects_out_of_range_without_raising():
    pc = PedersenCommitment()
    commitment, blinding = pc.commit(42)
    assert not pc.verify_opening(commitment, -1, blinding)
    assert not pc.verify_opening(commitment, 42, Q)


def test_commitment_is_frozen():
    commitment = Commitment(value=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        commitment.value = 2
