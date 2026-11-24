"""Merkle-proof growth benchmark: proof length and latency vs. transaction count.

Confirms the O(log N) scaling the tree's balanced-binary design implies: proof
length should grow logarithmically, not linearly, with the number of leaves.

Run with ``blockchain-compress-bench-merkle`` or
``python -m blockchain_compression.benchmarks.merkle_benchmark``.
"""

import argparse
import logging
import math
from dataclasses import dataclass

from blockchain_compression.benchmarks._cli import (
    add_common_benchmark_args,
    emit_benchmark_output,
    format_json_payload,
)
from blockchain_compression.benchmarks.timing import best_time
from blockchain_compression.merkle import MerkleTree
from blockchain_compression.observability import configure_logging, resolve_log_level

logger = logging.getLogger(__name__)

_DEFAULT_SIZES = (10, 100, 1_000, 10_000, 100_000)
_DEFAULT_REPEATS = 3


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
        get_proof_seconds=best_time(lambda: tree.get_proof(probe_index), repeats),
        verify_seconds=best_time(
            lambda: MerkleTree.verify_proof(transactions[probe_index], proof, root), repeats
        ),
    )


def run_benchmarks(
    sizes: tuple[int, ...] = _DEFAULT_SIZES, repeats: int = _DEFAULT_REPEATS
) -> list[MerkleBenchResult]:
    return [benchmark_size(size, repeats) for size in sizes]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blockchain-compress-bench-merkle",
        description="Benchmark Merkle proof generation and verification scaling.",
    )
    parser.add_argument(
        "--sizes",
        nargs="+",
        type=int,
        default=list(_DEFAULT_SIZES),
        help="Transaction counts per tree (default: 10 100 1000 10000 100000).",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=_DEFAULT_REPEATS,
        help="Timing repetitions per tree size (default: 3).",
    )
    add_common_benchmark_args(parser)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    if any(size < 1 for size in args.sizes):
        parser.error("--sizes values must be at least 1")
    try:
        resolved_level = resolve_log_level(args.log_level)
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))
    configure_logging(resolved_level)

    using_defaults = args.sizes == list(_DEFAULT_SIZES) and args.repeats == _DEFAULT_REPEATS
    results = (
        run_benchmarks()
        if using_defaults
        else run_benchmarks(sizes=tuple(args.sizes), repeats=args.repeats)
    )

    if args.json:
        payload: dict[str, object] = {
            "benchmark": "merkle",
            "parameters": {
                "sizes": args.sizes,
                "repeats": args.repeats,
            },
            "results": [
                {
                    "num_transactions": r.num_transactions,
                    "proof_length": r.proof_length,
                    "log2_bound": r.log2_bound,
                    "get_proof_seconds": r.get_proof_seconds,
                    "verify_seconds": r.verify_seconds,
                }
                for r in results
            ],
        }
        emit_benchmark_output(
            format_json_payload(payload),
            as_json=True,
            output_path=args.output,
            target_logger=logger,
        )
        return

    header = (
        f"{'transactions':>13} {'proof len':>10} {'log2 bound':>11} "
        f"{'get_proof ms':>13} {'verify ms':>10}"
    )
    lines = [header, "-" * len(header)]
    for result in results:
        lines.append(
            f"{result.num_transactions:13d} {result.proof_length:10d} {result.log2_bound:11d} "
            f"{result.get_proof_seconds * 1000:13.4f} {result.verify_seconds * 1000:10.4f}"
        )
    emit_benchmark_output(lines, as_json=False, output_path=args.output, target_logger=logger)


if __name__ == "__main__":
    main()
