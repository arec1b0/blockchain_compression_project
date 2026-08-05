"""Merkle-proof growth benchmark: proof length and latency vs. transaction count.

Confirms the O(log N) scaling the tree's balanced-binary design implies: proof
length should grow logarithmically, not linearly, with the number of leaves.

Run with ``blockchain-compress-bench-merkle`` or
``python -m blockchain_compression.benchmarks.merkle_benchmark``.
"""

import logging
import math
import time
from dataclasses import dataclass

from blockchain_compression.merkle import MerkleTree

logger = logging.getLogger(__name__)


@dataclass
class MerkleBenchResult:
    num_transactions: int
    proof_length: int
    get_proof_seconds: float
    verify_seconds: float

    @property
    def log2_bound(self) -> int:
        """Worst-case proof length for a balanced binary tree: ceil(log2(N))."""
        if self.num_transactions <= 1:
            return 0
        return math.ceil(math.log2(self.num_transactions))


def _best_time(func, repeats: int) -> float:
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        func()
        best = min(best, time.perf_counter() - start)
    return best


def benchmark_size(num_transactions: int, repeats: int = 3) -> MerkleBenchResult:
    """Benchmark proof generation/verification for a tree of ``num_transactions`` leaves."""
    transactions = [f"tx-{i}" for i in range(num_transactions)]
    tree = MerkleTree(transactions)
    root = tree.get_root()
    probe_index = num_transactions // 2

    proof = tree.get_proof(probe_index)
    if not MerkleTree.verify_proof(transactions[probe_index], proof, root):
        raise RuntimeError(f"proof did not verify for size {num_transactions}")

    return MerkleBenchResult(
        num_transactions=num_transactions,
        proof_length=len(proof),
        get_proof_seconds=_best_time(lambda: tree.get_proof(probe_index), repeats),
        verify_seconds=_best_time(
            lambda: MerkleTree.verify_proof(transactions[probe_index], proof, root), repeats
        ),
    )


def run_benchmarks(sizes=(10, 100, 1_000, 10_000, 100_000), repeats: int = 3) -> list:
    return [benchmark_size(size, repeats) for size in sizes]


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    header = (
        f"{'transactions':>13} {'proof len':>10} {'log2 bound':>11} "
        f"{'get_proof ms':>13} {'verify ms':>10}"
    )
    logger.info(header)
    logger.info("-" * len(header))
    for result in run_benchmarks():
        logger.info(
            "%13d %10d %11d %13.4f %10.4f",
            result.num_transactions,
            result.proof_length,
            result.log2_bound,
            result.get_proof_seconds * 1000,
            result.verify_seconds * 1000,
        )


if __name__ == "__main__":
    main()
