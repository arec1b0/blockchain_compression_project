"""Chain: an ordered, hash-linked sequence of blocks with an attached StateDelta.

Every ``add_block`` call both appends a new :class:`Block` and applies its
transactions to the chain's :class:`~blockchain_compression.compression.state_delta.StateDelta`,
so block structure and account state can never drift apart.
"""

from blockchain_compression.canonical import canonical_json
from blockchain_compression.chain.block import Block
from blockchain_compression.compression.state_delta import StateDelta
from blockchain_compression.observability.metrics import MetricsRegistry


class Chain:
    """A hash-linked chain of blocks, genesis-first."""

    GENESIS_PREV_HASH = "0" * 64

    def __init__(self, state: StateDelta | None = None, metrics: MetricsRegistry | None = None):
        self._state = state if state is not None else StateDelta()
        self._metrics = metrics
        genesis = Block.create(index=0, prev_hash=self.GENESIS_PREV_HASH, transactions=[])
        self.blocks: list[Block] = [genesis]

    @property
    def state(self) -> StateDelta:
        """The chain's cumulative account state."""
        return self._state

    def add_block(self, transactions: list) -> Block:
        """Validate every transaction, apply them all to state, then build and
        append the new block.

        Every transaction is validated up front, before any of them are
        applied - a malformed transaction later in the batch can't leave
        state partially mutated with no corresponding block.
        """
        block, staged_state = self._prepare_block(transactions)
        self._commit_prepared_block(block, staged_state, transactions)
        return block

    def _prepare_block(self, transactions: list) -> tuple[Block, StateDelta]:
        """Build a block and its resulting state without mutating this chain."""
        if not isinstance(transactions, list):
            raise TypeError(f"transactions must be a list, got {type(transactions).__name__}")
        for tx in transactions:
            StateDelta.validate_transaction(tx)

        staged_state = StateDelta.from_snapshot(
            self._state.get_current_state(), self._state.get_applied_tx_ids()
        )
        for tx in transactions:
            staged_state.apply_transaction(tx)

        previous = self.blocks[-1]
        block = Block.create(
            index=previous.index + 1,
            prev_hash=previous.hash,
            transactions=transactions,
        )
        return block, staged_state

    def _commit_prepared_block(
        self, block: Block, staged_state: StateDelta, transactions: list
    ) -> None:
        """Apply a prepared block and state; used by ChainStore after durable commit."""
        self._state._replace_with(staged_state)
        self.blocks.append(block)
        if self._metrics is not None:
            self._metrics.increment_counter(
                "blocks_added_total", help_text="Total blocks appended to the chain"
            )
            if block.compressed_body:
                raw_size = len(canonical_json(transactions).encode("utf-8"))
                ratio = raw_size / len(block.compressed_body)
                self._metrics.set_gauge(
                    "compression_ratio",
                    ratio,
                    labels={"block_index": str(block.index)},
                    help_text="Raw / compressed body size for a block",
                )

    def validate_chain(self, check_bodies: bool = True) -> bool:
        """Walk the chain checking index continuity, ``prev_hash`` linkage,
        header-hash recomputation, and (optionally) present-body integrity.

        Index continuity matters once blocks round-trip through storage,
        where row order isn't otherwise guaranteed without an explicit
        ``ORDER BY``.
        """
        for i, block in enumerate(self.blocks):
            if block.index != i:
                return False
            expected_prev_hash = self.GENESIS_PREV_HASH if i == 0 else self.blocks[i - 1].hash
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

    def get_block(self, index: int) -> Block:
        """Return the block at ``index``. Raises ``IndexError`` if out of range."""
        if not 0 <= index < len(self.blocks):
            raise IndexError(f"index {index} out of range for chain of length {len(self.blocks)}")
        return self.blocks[index]

    def __len__(self) -> int:
        return len(self.blocks)

    def __iter__(self):
        return iter(self.blocks)
