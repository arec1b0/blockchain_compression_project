"""Merkle tree with domain separation and inclusion proofs.

Leaf hashes and internal-node hashes use distinct one-byte prefixes
(``0x00`` for leaves, ``0x01`` for nodes), so an internal node can never be
reinterpreted as a leaf (second-preimage hardening, as in RFC 6962).
Unpaired nodes on odd-width levels are promoted to the next level unchanged
rather than duplicated, which avoids the root-collision ambiguity of the
duplicate-last-hash scheme (CVE-2012-2459).
"""

import hashlib

LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"


class MerkleTree:
    """Merkle tree over a list of transaction strings.

    Provides the root hash as a commitment to the full transaction set, and
    per-transaction inclusion proofs via :meth:`get_proof` /
    :meth:`verify_proof`.
    """

    def __init__(self, transactions: list):
        if not isinstance(transactions, list):
            raise TypeError("transactions must be a list of strings")
        for tx in transactions:
            if not isinstance(tx, str):
                raise TypeError(f"each transaction must be a string, got {type(tx).__name__}")
        self.transactions = list(transactions)
        self.levels = self._build_levels(self.transactions)

    @staticmethod
    def hash_leaf(data: str) -> str:
        """Hash a transaction as a leaf node (0x00-prefixed SHA-256 hex digest)."""
        return hashlib.sha256(LEAF_PREFIX + data.encode("utf-8")).hexdigest()

    @staticmethod
    def hash_node(left: str, right: str) -> str:
        """Hash two child hex digests as an internal node (0x01-prefixed SHA-256)."""
        return hashlib.sha256(
            NODE_PREFIX + bytes.fromhex(left) + bytes.fromhex(right)
        ).hexdigest()

    @classmethod
    def _build_levels(cls, transactions: list) -> list:
        """Build the tree bottom-up; returns a list of levels, leaves first."""
        if not transactions:
            return []
        levels = [[cls.hash_leaf(tx) for tx in transactions]]
        while len(levels[-1]) > 1:
            current = levels[-1]
            next_level = []
            for i in range(0, len(current), 2):
                if i + 1 < len(current):
                    next_level.append(cls.hash_node(current[i], current[i + 1]))
                else:
                    # Unpaired node: promote unchanged instead of hashing with itself.
                    next_level.append(current[i])
            levels.append(next_level)
        return levels

    def get_root(self):
        """Return the root hash, or ``None`` for an empty tree."""
        return self.levels[-1][0] if self.levels else None

    def get_proof(self, index: int) -> list:
        """Return the inclusion proof for the transaction at ``index``.

        The proof is a list of ``{"hash": <hex digest>, "position": "left" | "right"}``
        steps, where ``position`` is the side of the sibling hash relative to
        the path being verified. Levels where the node was promoted without a
        sibling contribute no step.
        """
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("index must be an integer")
        if not 0 <= index < len(self.transactions):
            raise IndexError(
                f"index {index} out of range for {len(self.transactions)} transaction(s)"
            )
        proof = []
        for level in self.levels[:-1]:
            if index % 2 == 0:
                if index + 1 < len(level):
                    proof.append({"hash": level[index + 1], "position": "right"})
                # Otherwise the node was promoted unchanged: no sibling here.
            else:
                proof.append({"hash": level[index - 1], "position": "left"})
            index //= 2
        return proof

    @classmethod
    def verify_proof(cls, transaction: str, proof: list, root: str) -> bool:
        """Check that ``transaction`` is committed under ``root`` via ``proof``."""
        if not isinstance(transaction, str):
            raise TypeError("transaction must be a string")
        if not isinstance(proof, list):
            raise TypeError("proof must be a list of proof steps")
        if root is None:
            return False
        current = cls.hash_leaf(transaction)
        for step in proof:
            if not isinstance(step, dict) or "hash" not in step or "position" not in step:
                raise ValueError("each proof step must be a dict with 'hash' and 'position'")
            if step["position"] == "right":
                current = cls.hash_node(current, step["hash"])
            elif step["position"] == "left":
                current = cls.hash_node(step["hash"], current)
            else:
                raise ValueError("proof step position must be 'left' or 'right'")
        return current == root
