"""Block: a header hash-linked to its predecessor, committing to a transaction body.

The header hash covers only ``index``/``prev_hash``/``merkle_root``/``timestamp`` -
never the compressed body - so a block's identity survives its body being pruned
away. A *present* body's integrity is a separate check (:meth:`Block.verify_body`),
which re-derives the Merkle root from the (decompressed) transactions and compares
it against the header's ``merkle_root`` - the actual commitment to body contents.
"""

import hashlib
import json
import time
from dataclasses import dataclass

from blockchain_compression.canonical import canonical_json
from blockchain_compression.compression.block_compressor import BlockCompressor
from blockchain_compression.merkle.merkle_tree import MerkleTree

_compressor = BlockCompressor()


@dataclass
class Block:
    """One block in a :class:`~blockchain_compression.chain.chain.Chain`.

    Not frozen (pruning mutates it), but the only mutation path is
    :meth:`prune` - ``is_pruned`` is a derived invariant, never set
    independently of ``compressed_body``/``transactions``.
    """

    index: int
    prev_hash: str
    timestamp: float
    merkle_root: str | None
    hash: str
    transactions: list | None
    compressed_body: bytes | None
    is_pruned: bool = False

    @staticmethod
    def compute_hash(index: int, prev_hash: str, merkle_root: str | None, timestamp: float) -> str:
        """SHA-256 over the canonical header fields only - the one place this
        concatenation happens; both :meth:`create` and ``Chain.validate_chain``
        call it, so they can never drift apart."""
        header = canonical_json([index, prev_hash, merkle_root, timestamp])
        return hashlib.sha256(header.encode("utf-8")).hexdigest()

    @classmethod
    def create(
        cls, index: int, prev_hash: str, transactions: list, timestamp: float | None = None
    ) -> "Block":
        """Build a fresh block from raw transactions.

        The Merkle root and the compressed body are built from the *same*
        canonical per-transaction JSON strings, so a decompressed body is
        always guaranteed to reproduce ``merkle_root`` (verified later by
        :meth:`verify_body`).
        """
        if not isinstance(transactions, list):
            raise TypeError(f"transactions must be a list, got {type(transactions).__name__}")
        if timestamp is None:
            timestamp = time.time()
        tx_strings = [canonical_json(tx) for tx in transactions]
        merkle_root = MerkleTree(tx_strings).get_root()
        compressed_body = _compressor.compress_block(canonical_json(transactions).encode("utf-8"))
        block_hash = cls.compute_hash(index, prev_hash, merkle_root, timestamp)
        return cls(
            index=index,
            prev_hash=prev_hash,
            timestamp=timestamp,
            merkle_root=merkle_root,
            hash=block_hash,
            transactions=list(transactions),
            compressed_body=compressed_body,
            is_pruned=False,
        )

    @classmethod
    def from_persisted(
        cls,
        *,
        index: int,
        prev_hash: str,
        merkle_root: str | None,
        hash: str,
        timestamp: float,
        compressed_body: bytes | None,
        is_pruned: bool,
    ) -> "Block":
        """Reconstruct a block from trusted storage *without* recomputing its
        hash - a pruned row has no transactions left to recompute one from.

        A decompression/parse failure on a present body is swallowed here
        (``transactions`` stays ``None``) rather than raised: that state is
        exactly what :meth:`verify_body` exists to report as corruption,
        without ``ChainStore.load_chain`` crashing outright on bad data.
        """
        transactions = None
        if not is_pruned and compressed_body is not None:
            try:
                raw = _compressor.decompress_block(compressed_body)
                transactions = json.loads(raw.decode("utf-8"))
            except ValueError:
                transactions = None
        return cls(
            index=index,
            prev_hash=prev_hash,
            timestamp=timestamp,
            merkle_root=merkle_root,
            hash=hash,
            transactions=transactions,
            compressed_body=compressed_body,
            is_pruned=is_pruned,
        )

    def prune(self) -> None:
        """Drop the body; keep header fields so hash-linkage stays verifiable."""
        self.transactions = None
        self.compressed_body = None
        self.is_pruned = True

    def verify_body(self) -> bool:
        """Check the present body actually matches the header's commitment.

        Returns ``True`` trivially for a pruned block (nothing to check).
        Returns ``False`` if the body is missing without being marked pruned
        (corruption), or if a present body's content doesn't reproduce
        ``merkle_root`` (tampering) - never raises on bad data.
        """
        if self.is_pruned:
            return True
        if self.compressed_body is None or self.transactions is None:
            return False
        try:
            raw = _compressor.decompress_block(self.compressed_body)
        except ValueError:
            return False
        if raw != canonical_json(self.transactions).encode("utf-8"):
            return False
        tx_strings = [canonical_json(tx) for tx in self.transactions]
        return MerkleTree(tx_strings).get_root() == self.merkle_root
