"""Chain: an ordered, hash-linked sequence of blocks with an attached StateDelta.

Every ``add_block`` call both appends a new :class:`Block` and applies its
transactions to the chain's :class:`~blockchain_compression.compression.state_delta.StateDelta`,
so block structure and account state can never drift apart.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from blockchain_compression.chain.block import Block
from blockchain_compression.compression.block_compressor import BlockCompressor
from blockchain_compression.compression.state_delta import BlockDelta, StateDelta
from blockchain_compression.observability.metrics import (
    COMPRESSION_RATIO_BUCKETS,
    MetricsRegistry,
)
from blockchain_compression.zk_proof.attachment import ZKAttachment


@dataclass(frozen=True)
class PreparedBlock:
    """A staged block and batch delta ready to be committed after persistence."""

    block: Block
    delta: BlockDelta
    raw_body_bytes: int


class Chain:
    """A hash-linked chain of blocks, genesis-first."""

    GENESIS_PREV_HASH = "0" * 64

    def __init__(
        self,
        state: StateDelta | None = None,
        metrics: MetricsRegistry | None = None,
        compressor: BlockCompressor | None = None,
    ):
        if state is not None and not isinstance(state, StateDelta):
            raise TypeError(f"state must be a StateDelta, got {type(state).__name__}")
        if compressor is not None and not isinstance(compressor, BlockCompressor):
            raise TypeError(
                f"compressor must be a BlockCompressor, got {type(compressor).__name__}"
            )
        self._state = state if state is not None else StateDelta()
        self._metrics = metrics
        self._compressor = compressor
        genesis = Block.create(
            index=0,
            prev_hash=self.GENESIS_PREV_HASH,
            transactions=[],
            compressor=self._compressor,
        )
        self._blocks: list[Block] = [genesis]

    @classmethod
    def from_persisted(
        cls,
        blocks: list[Block] | tuple[Block, ...],
        state: StateDelta,
        metrics: MetricsRegistry | None = None,
        compressor: BlockCompressor | None = None,
    ) -> "Chain":
        """Reconstruct a chain from persisted blocks and state without building
        or compressing a throwaway genesis block."""
        if not isinstance(blocks, (list, tuple)) or not blocks:
            raise ValueError("blocks must be a non-empty sequence of Block instances")
        if not all(isinstance(block, Block) for block in blocks):
            raise TypeError("all elements in blocks must be Block instances")
        if not isinstance(state, StateDelta):
            raise TypeError(f"state must be a StateDelta, got {type(state).__name__}")
        if compressor is not None and not isinstance(compressor, BlockCompressor):
            raise TypeError(
                f"compressor must be a BlockCompressor, got {type(compressor).__name__}"
            )
        instance = cls.__new__(cls)
        instance._state = state
        instance._metrics = metrics
        instance._compressor = compressor
        instance._blocks = list(blocks)
        return instance

    _from_persisted = from_persisted

    @property
    def compressor(self) -> BlockCompressor | None:
        """Optional :class:`BlockCompressor` configured for new blocks on this chain."""
        return self._compressor

    @property
    def blocks(self) -> tuple[Block, ...]:
        """Read-only sequence of blocks in the chain, genesis-first."""
        return tuple(self._blocks)

    @property
    def state(self) -> StateDelta:
        """The chain's cumulative account state.

        Exposed for inspection (e.g. ``get_current_state()`` and
        ``get_applied_tx_ids()``). Direct mutation of this object outside
        :meth:`add_block` is unsupported; use :meth:`get_state_snapshot` when an
        isolated mutable copy is needed.
        """
        return self._state

    def get_state_snapshot(self) -> StateDelta:
        """Return an independent copy of the chain's current :class:`StateDelta`."""
        return self._state.copy()

    def add_block(
        self,
        transactions: list,
        zk_attachments: Iterable[ZKAttachment] | None = None,
    ) -> Block:
        """Validate every transaction, apply them all to state, then build and
        append the new block.

        Every transaction is validated up front (including duplicate and replay
        checks) before any state mutation or block creation. Optional
        ``zk_attachments`` are stored without changing mandatory chain rules.
        """
        prepared = self.prepare_block(transactions, zk_attachments=zk_attachments)
        return self.commit_block(prepared)

    def prepare_block(
        self,
        transactions: list,
        zk_attachments: Iterable[ZKAttachment] | None = None,
    ) -> PreparedBlock:
        """Build a prospective block and batch :class:`BlockDelta` in
        ``O(len(transactions))`` without mutating or copying full chain state.

        Used together with :meth:`commit_block` by coordinated persistence layers
        (such as :class:`~blockchain_compression.persistence.chain_store.ChainStore`)
        so storage commits can happen before the in-memory chain advances.
        """
        delta = self._state.stage_batch(transactions)
        previous = self._blocks[-1]
        block, raw_body_bytes = Block._create_with_raw_size(
            index=previous.index + 1,
            prev_hash=previous.hash,
            transactions=transactions,
            zk_attachments=zk_attachments,
            compressor=self._compressor,
        )
        return PreparedBlock(
            block=block,
            delta=delta,
            raw_body_bytes=raw_body_bytes,
        )

    def commit_block(self, prepared: PreparedBlock) -> Block:
        """Commit a :class:`PreparedBlock` produced by :meth:`prepare_block`."""
        if not isinstance(prepared, PreparedBlock):
            raise TypeError(f"prepared must be a PreparedBlock, got {type(prepared).__name__}")
        tip = self._blocks[-1]
        if prepared.block.index != tip.index + 1 or prepared.block.prev_hash != tip.hash:
            raise ValueError("chain advanced since block was prepared; cannot commit stale block")

        # Stage-time prepare_block already verified replay protection against
        # both RAM and any backing lookup; skip re-querying the backing store
        # here because coordinated stores (e.g. ChainStore) persist delta.tx_ids
        # right before calling commit_block.
        self._state.commit_delta(prepared.delta, check_backing_lookup=False)
        self._blocks.append(prepared.block)
        if self._metrics is not None:
            self._metrics.increment_counter(
                "blocks_added_total", help_text="Total blocks appended to the chain"
            )
            if prepared.block.compressed_body is not None:
                compressed_bytes = len(prepared.block.compressed_body)
                self._metrics.increment_counter(
                    "block_raw_bytes_total",
                    value=prepared.raw_body_bytes,
                    help_text="Cumulative raw serialized block body bytes",
                )
                self._metrics.increment_counter(
                    "block_compressed_bytes_total",
                    value=compressed_bytes,
                    help_text="Cumulative stored block body bytes",
                )
                if compressed_bytes > 0:
                    ratio = prepared.raw_body_bytes / compressed_bytes
                    self._metrics.set_gauge(
                        "last_block_compression_ratio",
                        ratio,
                        help_text=(
                            "Raw / stored body size ratio for the most recently committed block"
                        ),
                    )
                    self._metrics.observe_histogram(
                        "block_compression_ratio",
                        ratio,
                        buckets=COMPRESSION_RATIO_BUCKETS,
                        help_text="Distribution of raw / stored body size ratios across blocks",
                    )
        return prepared.block

    def validate_chain(self, check_bodies: bool = True) -> bool:
        """Walk the chain checking index continuity, ``prev_hash`` linkage,
        header-hash recomputation, and (optionally) present-body integrity.

        Index continuity matters once blocks round-trip through storage,
        where row order isn't otherwise guaranteed without an explicit
        ``ORDER BY``.
        """
        for i, block in enumerate(self._blocks):
            if block.index != i:
                return False
            expected_prev_hash = self.GENESIS_PREV_HASH if i == 0 else self._blocks[i - 1].hash
            if block.prev_hash != expected_prev_hash:
                return False
            recomputed = Block.compute_hash(
                block.index, block.prev_hash, block.merkle_root, block.timestamp
            )
            if recomputed != block.hash:
                return False
            if check_bodies and not block.verify_body():
                return False
        return True

    def verify_zk_attachments(self, metrics: MetricsRegistry | None = None) -> bool:
        """Explicitly verify all optional :class:`ZKAttachment` items across the chain.

        This is opt-in and separate from :meth:`validate_chain`.
        """
        return all(block.verify_zk_attachments(metrics=metrics) for block in self._blocks)

    def get_block(self, index: int) -> Block:
        """Return the block at ``index``. Raises ``IndexError`` if out of range."""
        if not 0 <= index < len(self._blocks):
            raise IndexError(f"index {index} out of range for chain of length {len(self._blocks)}")
        return self._blocks[index]

    def __len__(self) -> int:
        return len(self._blocks)

    def __iter__(self) -> Iterator[Block]:
        return iter(self._blocks)
