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
from collections.abc import Iterable
from dataclasses import dataclass

from blockchain_compression.canonical import canonical_json
from blockchain_compression.compression.block_compressor import BlockCompressor
from blockchain_compression.merkle.merkle_tree import MerkleTree
from blockchain_compression.observability.metrics import MetricsRegistry
from blockchain_compression.zk_proof.attachment import ZKAttachment

_compressor = BlockCompressor()


def _serialize_transactions(transactions: list) -> tuple[list[str], str]:
    """Serialize each transaction once cleanly and assemble a canonical JSON array."""
    tx_strings = [canonical_json(tx) for tx in transactions]
    return tx_strings, "[" + ",".join(tx_strings) + "]"


def _normalize_zk_attachments(
    zk_attachments: Iterable[ZKAttachment] | None,
) -> tuple[ZKAttachment, ...]:
    if zk_attachments is None:
        return ()
    if isinstance(zk_attachments, (str, bytes, bytearray, dict)):
        raise TypeError("zk_attachments must be an iterable of ZKAttachment instances")
    normalized = tuple(zk_attachments)
    for attachment in normalized:
        if not isinstance(attachment, ZKAttachment):
            raise TypeError(
                f"each attachment must be a ZKAttachment, got {type(attachment).__name__}"
            )
        attachment.to_dict()
    return normalized


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
    zk_attachments: tuple[ZKAttachment, ...] = ()

    @staticmethod
    def compute_hash(index: int, prev_hash: str, merkle_root: str | None, timestamp: float) -> str:
        """SHA-256 over the canonical header fields only - the one place this
        concatenation happens; both :meth:`create` and ``Chain.validate_chain``
        call it, so they can never drift apart."""
        header = canonical_json([index, prev_hash, merkle_root, timestamp])
        return hashlib.sha256(header.encode("utf-8")).hexdigest()

    @classmethod
    def _create_with_raw_size(
        cls,
        index: int,
        prev_hash: str,
        transactions: list,
        timestamp: float | None = None,
        zk_attachments: Iterable[ZKAttachment] | None = None,
        compressor: BlockCompressor | None = None,
    ) -> tuple["Block", int]:
        if not isinstance(transactions, list):
            raise TypeError(f"transactions must be a list, got {type(transactions).__name__}")
        if compressor is not None and not isinstance(compressor, BlockCompressor):
            raise TypeError(
                f"compressor must be a BlockCompressor, got {type(compressor).__name__}"
            )
        normalized_attachments = _normalize_zk_attachments(zk_attachments)
        if timestamp is None:
            timestamp = time.time()
        tx_strings, body_json = _serialize_transactions(transactions)
        merkle_root = MerkleTree(tx_strings).get_root()
        raw_bytes = body_json.encode("utf-8")
        active_compressor = _compressor if compressor is None else compressor
        compressed_body = active_compressor.compress_block(raw_bytes)
        block_hash = cls.compute_hash(index, prev_hash, merkle_root, timestamp)
        copied_transactions = json.loads(body_json)
        block = cls(
            index=index,
            prev_hash=prev_hash,
            timestamp=timestamp,
            merkle_root=merkle_root,
            hash=block_hash,
            transactions=copied_transactions,
            compressed_body=compressed_body,
            is_pruned=False,
            zk_attachments=normalized_attachments,
        )
        return block, len(raw_bytes)

    @classmethod
    def create(
        cls,
        index: int,
        prev_hash: str,
        transactions: list,
        timestamp: float | None = None,
        zk_attachments: Iterable[ZKAttachment] | None = None,
        compressor: BlockCompressor | None = None,
    ) -> "Block":
        """Build a fresh block from raw transactions.

        The Merkle root and the compressed body are built from the *same*
        canonical per-transaction JSON strings (assembled directly via
        ``"[" + ",".join(tx_strings) + "]"`` without re-serializing the full
        list), so a decompressed body is always guaranteed to reproduce
        ``merkle_root`` (verified later by :meth:`verify_body`). Transactions
        are deep-copied through canonical JSON round-tripping so caller
        mutations cannot desynchronize the block body. Optional
        ``compressor`` configures preset/framing behavior; optional
        ``zk_attachments`` are structurally validated without altering the
        block header hash.
        """
        block, _ = cls._create_with_raw_size(
            index=index,
            prev_hash=prev_hash,
            transactions=transactions,
            timestamp=timestamp,
            zk_attachments=zk_attachments,
            compressor=compressor,
        )
        return block

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
        max_decompressed_bytes: int | None = None,
        zk_attachments: Iterable[ZKAttachment] | None = None,
    ) -> "Block":
        """Reconstruct a block from trusted storage *without* recomputing its
        hash - a pruned row has no transactions left to recompute one from.

        A decompression/parse/shape failure on a present body is swallowed here
        (``transactions`` stays ``None``) rather than raised: that state is
        exactly what :meth:`verify_body` exists to report as corruption,
        without ``ChainStore.load_chain`` crashing outright on bad data.
        """
        normalized_attachments = _normalize_zk_attachments(zk_attachments)
        transactions = None
        if not is_pruned and compressed_body is not None:
            compressor = (
                _compressor
                if max_decompressed_bytes is None
                else BlockCompressor(max_decompressed_bytes=max_decompressed_bytes)
            )
            try:
                raw = compressor.decompress_block(compressed_body)
                decoded = json.loads(raw.decode("utf-8"))
                transactions = decoded if isinstance(decoded, list) else None
            except (ValueError, TypeError):
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
            zk_attachments=normalized_attachments,
        )

    def verify_zk_attachments(self, metrics: MetricsRegistry | None = None) -> bool:
        """Explicitly verify all optional :class:`ZKAttachment` items on this block.

        This is opt-in and separate from :meth:`verify_body` and
        ``Chain.validate_chain()``. Returns ``False`` if any attachment lacks a
        proof or fails Schnorr verification.
        """
        return all(attachment.verify(metrics=metrics) for attachment in self.zk_attachments)

    def prune(self) -> None:
        """Drop the body; keep header fields so hash-linkage stays verifiable."""
        self.transactions = None
        self.compressed_body = None
        self.is_pruned = True

    def verify_body(self, max_decompressed_bytes: int | None = None) -> bool:
        """Check the present body actually matches the header's commitment.

        Returns ``True`` for a properly pruned block (both ``compressed_body``
        and ``transactions`` are ``None``). Returns ``False`` if a pruned
        block still carries body/transaction data, if an unpruned block's body
        is missing, non-list, or exceeds ``max_decompressed_bytes``, or if a
        present body's content doesn't reproduce ``merkle_root`` (tampering) -
        never raises on bad data.
        """
        if self.is_pruned:
            return self.compressed_body is None and self.transactions is None
        if self.compressed_body is None or not isinstance(self.transactions, list):
            return False
        try:
            compressor = (
                _compressor
                if max_decompressed_bytes is None
                else BlockCompressor(max_decompressed_bytes=max_decompressed_bytes)
            )
            raw = compressor.decompress_block(self.compressed_body)
            tx_strings, expected_json = _serialize_transactions(self.transactions)
            if raw != expected_json.encode("utf-8"):
                return False
        except (ValueError, TypeError):
            return False
        return MerkleTree(tx_strings).get_root() == self.merkle_root
