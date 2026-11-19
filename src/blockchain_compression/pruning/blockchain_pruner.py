"""Blockchain storage pruning: retain full bodies for only the most recent blocks.

Operates over a real hash-linked :class:`~blockchain_compression.chain.chain.Chain`,
not a disposable list. Pruning drops a block's body (transactions and compressed
bytes) but keeps its header fields (``index``/``prev_hash``/``merkle_root``/``hash``/
``timestamp``), so ``Chain.validate_chain()`` still succeeds afterwards - hash
linkage never depended on the body being present. This mirrors how real "pruned
node" clients operate: history is discarded, but the chain of custody back to
genesis remains verifiable.
"""

import logging

from blockchain_compression.chain.chain import Chain
from blockchain_compression.observability.metrics import MetricsRegistry

logger = logging.getLogger(__name__)


class BlockchainPruner:
    """Keeps full bodies for the most recent ``max_full_blocks`` blocks of a
    ``Chain``, pruning (header-only) every older block incrementally."""

    def __init__(self, chain: Chain, max_full_blocks: int, metrics: MetricsRegistry | None = None):
        if not isinstance(chain, Chain):
            raise TypeError(f"chain must be a Chain, got {type(chain).__name__}")
        if isinstance(max_full_blocks, bool) or not isinstance(max_full_blocks, int):
            raise TypeError(
                f"max_full_blocks must be an integer, got {type(max_full_blocks).__name__}"
            )
        if max_full_blocks < 1:
            raise ValueError("max_full_blocks must be at least 1")
        self.chain = chain
        self.max_full_blocks = max_full_blocks
        self._metrics = metrics
        self._next_unpruned_idx: int = 0

    @property
    def next_unpruned_index(self) -> int:
        """Lower bound on the next block index that has not yet been pruned."""
        return self._next_unpruned_idx

    def reset_cursor(self) -> None:
        """Reset the incremental pruning scan cursor (e.g. after rollback)."""
        self._next_unpruned_idx = 0

    def _advance_pruned_prefix(self, limit: int) -> None:
        while (
            self._next_unpruned_idx < limit
            and self.chain.get_block(self._next_unpruned_idx).is_pruned
        ):
            self._next_unpruned_idx += 1

    def eligible_block_indices(self) -> list[int]:
        """Return indices of unpruned blocks older than the last ``max_full_blocks``."""
        # Clamp at 0: a plain `len(chain) - max_full_blocks` goes negative when
        # the chain is shorter than the retention window, and slicing with a
        # negative index means something else entirely (`blocks[:-2]` keeps
        # all-but-the-last-2, not "keep everything").
        cutoff = max(0, len(self.chain) - self.max_full_blocks)
        self._advance_pruned_prefix(cutoff)
        if self._next_unpruned_idx >= cutoff:
            return []
        return [
            idx
            for idx in range(self._next_unpruned_idx, cutoff)
            if not self.chain.get_block(idx).is_pruned
        ]

    def prune(self) -> list[int]:
        """Prune every not-yet-pruned block older than the last
        ``max_full_blocks``. Returns the list of newly-pruned block indices
        (empty if none qualified)."""
        cutoff = max(0, len(self.chain) - self.max_full_blocks)
        pruned_indices = self.eligible_block_indices()
        for index in pruned_indices:
            self.chain.get_block(index).prune()
        if cutoff > self._next_unpruned_idx:
            self._next_unpruned_idx = cutoff
        if pruned_indices:
            logger.info(
                "Pruned %d block body(ies); keeping the last %d full.",
                len(pruned_indices),
                self.max_full_blocks,
            )
            if self._metrics is not None:
                self._metrics.increment_counter(
                    "pruning_events_total",
                    help_text="Number of prune() calls that pruned something",
                )
                self._metrics.increment_counter(
                    "blocks_pruned_total",
                    value=len(pruned_indices),
                    help_text="Total blocks whose bodies have been pruned",
                )
        return pruned_indices
