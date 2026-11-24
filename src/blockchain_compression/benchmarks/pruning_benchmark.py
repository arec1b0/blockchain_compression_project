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

import argparse
import gc
import logging
import time
import tracemalloc
from dataclasses import dataclass

from blockchain_compression.benchmarks._cli import (
    add_common_benchmark_args,
    emit_benchmark_output,
    format_json_payload,
)
from blockchain_compression.chain import Chain
from blockchain_compression.observability import configure_logging, resolve_log_level
from blockchain_compression.pruning import BlockchainPruner

logger = logging.getLogger(__name__)

_DEFAULT_CHAIN_LENGTHS = (100, 1_000, 10_000)
_DEFAULT_MAX_FULL_BLOCKS = 50


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


def run_benchmarks(
    chain_lengths: tuple[int, ...] = _DEFAULT_CHAIN_LENGTHS,
    max_full_blocks: int = _DEFAULT_MAX_FULL_BLOCKS,
) -> list[PruningBenchResult]:
    return [benchmark_pruning(length, max_full_blocks) for length in chain_lengths]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blockchain-compress-bench-pruning",
        description="Benchmark incremental blockchain pruning latency and memory savings.",
    )
    parser.add_argument(
        "--chain-lengths",
        nargs="+",
        type=int,
        default=list(_DEFAULT_CHAIN_LENGTHS),
        help="Chain block counts to construct and prune (default: 100 1000 10000).",
    )
    parser.add_argument(
        "--max-full-blocks",
        type=int,
        default=_DEFAULT_MAX_FULL_BLOCKS,
        help="Number of recent full block bodies retained during pruning (default: 50).",
    )
    add_common_benchmark_args(parser)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.max_full_blocks < 1:
        parser.error("--max-full-blocks must be at least 1")
    if any(length < 1 for length in args.chain_lengths):
        parser.error("--chain-lengths values must be at least 1")
    try:
        resolved_level = resolve_log_level(args.log_level)
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))
    configure_logging(resolved_level)

    using_defaults = (
        args.chain_lengths == list(_DEFAULT_CHAIN_LENGTHS)
        and args.max_full_blocks == _DEFAULT_MAX_FULL_BLOCKS
    )
    results = (
        run_benchmarks()
        if using_defaults
        else run_benchmarks(
            chain_lengths=tuple(args.chain_lengths),
            max_full_blocks=args.max_full_blocks,
        )
    )

    if args.json:
        payload: dict[str, object] = {
            "benchmark": "pruning",
            "parameters": {
                "chain_lengths": args.chain_lengths,
                "max_full_blocks": args.max_full_blocks,
            },
            "results": [
                {
                    "chain_length": r.chain_length,
                    "prune_seconds": r.prune_seconds,
                    "memory_before_bytes": r.memory_before_bytes,
                    "memory_after_bytes": r.memory_after_bytes,
                    "memory_freed_bytes": r.memory_freed_bytes,
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
        f"{'chain length':>13} {'prune ms':>10} {'mem before KB':>14} "
        f"{'mem after KB':>13} {'freed KB':>10}"
    )
    lines = [header, "-" * len(header)]
    for result in results:
        lines.append(
            f"{result.chain_length:13d} {result.prune_seconds * 1000:10.3f} "
            f"{result.memory_before_bytes / 1000:14.1f} "
            f"{result.memory_after_bytes / 1000:13.1f} "
            f"{result.memory_freed_bytes / 1000:10.1f}"
        )
    emit_benchmark_output(lines, as_json=False, output_path=args.output, target_logger=logger)


if __name__ == "__main__":
    main()
