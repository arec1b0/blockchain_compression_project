"""Blockchain storage pruning: retain only the most recent blocks.

Pruning assumes historical blocks can be safely discarded once processed by
the consensus layer; validation of incoming blocks happens elsewhere.
"""

import logging

logger = logging.getLogger(__name__)


class BlockchainPruner:
    """Keeps at most ``max_blocks`` recent blocks, discarding the oldest."""

    def __init__(self, max_blocks: int):
        if isinstance(max_blocks, bool) or not isinstance(max_blocks, int):
            raise TypeError(f"max_blocks must be an integer, got {type(max_blocks).__name__}")
        if max_blocks < 1:
            raise ValueError("max_blocks must be at least 1")
        self.max_blocks = max_blocks
        self.blocks = []

    def add_block(self, block: dict) -> None:
        """Append ``block`` and prune the oldest blocks if over the limit."""
        if not isinstance(block, dict):
            raise TypeError(f"block must be a dict, got {type(block).__name__}")
        self.blocks.append(block)
        if len(self.blocks) > self.max_blocks:
            self.prune_blocks()

    def prune_blocks(self) -> None:
        """Drop the oldest blocks so at most ``max_blocks`` remain."""
        pruned = len(self.blocks) - self.max_blocks
        if pruned > 0:
            self.blocks = self.blocks[-self.max_blocks:]
            logger.info("Pruned %d old block(s); keeping the last %d.", pruned, self.max_blocks)

    def get_blocks(self) -> list:
        """Return the currently retained blocks, oldest first."""
        return self.blocks
