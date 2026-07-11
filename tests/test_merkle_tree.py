# test_merkle_tree.py

import pytest

from blockchain_compression.merkle import MerkleTree


def test_merkle_tree_construction():
    tree = MerkleTree(["tx1", "tx2", "tx3", "tx4"])
    assert tree.get_root() is not None


def test_merkle_tree_root_hash():
    tree = MerkleTree(["tx1", "tx2", "tx3", "tx4"])

    h1 = MerkleTree.hash_leaf("tx1")
    h2 = MerkleTree.hash_leaf("tx2")
    h3 = MerkleTree.hash_leaf("tx3")
    h4 = MerkleTree.hash_leaf("tx4")
    h12 = MerkleTree.hash_node(h1, h2)
    h34 = MerkleTree.hash_node(h3, h4)
    expected_root = MerkleTree.hash_node(h12, h34)

    assert tree.get_root() == expected_root


def test_leaf_and_node_hashes_are_domain_separated():
    # Hashing the same material as a leaf and as a node must give different
    # digests, so internal nodes can never be reinterpreted as leaves.
    left = MerkleTree.hash_leaf("a")
    right = MerkleTree.hash_leaf("b")
    assert MerkleTree.hash_node(left, right) != MerkleTree.hash_leaf(left + right)


def test_empty_tree_regression():
    # Regression: MerkleTree([]) used to raise IndexError in get_root().
    tree = MerkleTree([])
    assert tree.get_root() is None
    with pytest.raises(IndexError):
        tree.get_proof(0)
    assert not MerkleTree.verify_proof("tx", [], tree.get_root())


def test_single_transaction_tree():
    tree = MerkleTree(["only-tx"])
    assert tree.get_root() == MerkleTree.hash_leaf("only-tx")
    assert tree.get_proof(0) == []
    assert MerkleTree.verify_proof("only-tx", [], tree.get_root())


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 8, 9])
def test_proofs_verify_for_every_leaf(count):
    transactions = [f"tx{i}" for i in range(count)]
    tree = MerkleTree(transactions)
    root = tree.get_root()
    for i, tx in enumerate(transactions):
        proof = tree.get_proof(i)
        assert MerkleTree.verify_proof(tx, proof, root), f"proof failed for leaf {i} of {count}"


def test_proof_rejects_tampered_transaction():
    tree = MerkleTree(["tx1", "tx2", "tx3"])
    proof = tree.get_proof(1)
    assert not MerkleTree.verify_proof("tx2-tampered", proof, tree.get_root())


def test_proof_rejects_wrong_root():
    tree = MerkleTree(["tx1", "tx2"])
    proof = tree.get_proof(0)
    assert not MerkleTree.verify_proof("tx1", proof, MerkleTree.hash_leaf("bogus"))


def test_odd_leaf_count_has_no_duplicate_ambiguity():
    # With promotion of unpaired nodes, [a, b, c] and [a, b, c, c] must not
    # produce the same root (the classic duplicate-last-hash collision).
    assert MerkleTree(["a", "b", "c"]).get_root() != MerkleTree(["a", "b", "c", "c"]).get_root()


def test_invalid_constructor_inputs():
    with pytest.raises(TypeError):
        MerkleTree("not-a-list")
    with pytest.raises(TypeError):
        MerkleTree([b"bytes-tx"])


def test_get_proof_invalid_index():
    tree = MerkleTree(["tx1", "tx2"])
    with pytest.raises(IndexError):
        tree.get_proof(5)
    with pytest.raises(IndexError):
        tree.get_proof(-1)
    with pytest.raises(TypeError):
        tree.get_proof("0")


def test_malformed_proof_step_raises():
    tree = MerkleTree(["tx1", "tx2"])
    root = tree.get_root()
    with pytest.raises(ValueError):
        MerkleTree.verify_proof("tx1", [{"hash": root}], root)
    with pytest.raises(ValueError):
        MerkleTree.verify_proof("tx1", [{"hash": root, "position": "up"}], root)
