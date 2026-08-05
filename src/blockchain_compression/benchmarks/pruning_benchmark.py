"""Pruning-under-load benchmark: prune latency and memory as chain length grows.

Demonstrates the "load_resistance" property: pruning cost should stay cheap and
roughly independent of total chain length, not degrade as history accumulates.

Uses ``tracemalloc`` rather than ``sys.getsizeof`` to measure memory:
``sys.getsizeof`` only reports an object's own shallow size (e.g. a list's
pointer array), not what it references, so it would report ~no change
regardless of whether pruning actually freed anything. Tracing must also start
*before* the chain is built - allocations made before ``tracemalloc.start()``
are invisible to it, so freeing them later would not show up as freed memory
either.

Run with ``blockchain-compress-bench-pruning`` or
``python -m blockchain_compression.benchmarks.pruning_benchmark``.
"""

import gc
import logging
import time
import tracemalloc
from dataclasses import dataclass

from blockchain_compression.chain import Chain
from blockchain_compression.pruning import BlockchainPruner

logger = logging.getLogger(__name__)


@dataclass
class PruningBenchResult:
    chain_length: int
    prune_seconds: float
    memory_before_bytes: int
    memory_after_bytes: int

    @property
    def memory_freed_bytes(self) -> int:
        return self.memory_before_bytes - self.memory_after_bytes


def _build_chain(length: int) -> Chain:
    chain = Chain()
    for i in range(length):
        chain.add_block([{"tx_id": f"tx-{i}", "account": "Alice", "amount": 1}])
    return chain


def benchmark_pruning(chain_length: int, max_full_blocks: int = 50) -> PruningBenchResult:
    """Build a chain of ``chain_length`` blocks, prune it, and measure latency
    plus traced-memory before/after. Tracing starts before the chain is built
    so the chain's own allocations are inside the tracked window."""
    gc.collect()
    tracemalloc.start()
    try:
        chain = _build_chain(chain_length)
        pruner = BlockchainPruner(chain, max_full_blocks=max_full_blocks)
        gc.collect()
        before, _ = tracemalloc.get_traced_memory()

        start = time.perf_counter()
        pruner.prune()
        elapsed = time.perf_counter() - start

        gc.collect()
        after, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    return PruningBenchResult(
        chain_length=chain_length,
        prune_seconds=elapsed,
        memory_before_bytes=before,
        memory_after_bytes=after,
    )


def run_benchmarks(chain_lengths=(100, 1_000, 10_000), max_full_blocks: int = 50) -> list:
    return [benchmark_pruning(length, max_full_blocks) for length in chain_lengths]


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    header = (
        f"{'chain length':>13} {'prune ms':>10} {'mem before KB':>14} "
        f"{'mem after KB':>13} {'freed KB':>10}"
    )
    logger.info(header)
    logger.info("-" * len(header))
    for result in run_benchmarks():
        logger.info(
            "%13d %10.3f %14.1f %13.1f %10.1f",
            result.chain_length,
            result.prune_seconds * 1000,
            result.memory_before_bytes / 1000,
            result.memory_after_bytes / 1000,
            result.memory_freed_bytes / 1000,
        )


if __name__ == "__main__":
    main()
