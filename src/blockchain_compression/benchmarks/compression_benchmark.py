"""LZMA compression benchmark over synthetic blockchain workloads.

Measures compressed size, compression ratio, and compress/decompress
throughput across datasets with different entropy profiles and LZMA presets.

Run with ``blockchain-compress-bench`` or
``python -m blockchain_compression.benchmarks.compression_benchmark``.
"""

import argparse
import json
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass

from blockchain_compression.benchmarks._cli import (
    add_common_benchmark_args,
    emit_benchmark_output,
    format_json_payload,
)
from blockchain_compression.benchmarks.timing import best_time
from blockchain_compression.compression import BlockCompressor
from blockchain_compression.observability import configure_logging, resolve_log_level

logger = logging.getLogger(__name__)

_SEED = 1337
_DEFAULT_DATASETS = ("synthetic-blocks", "repetitive", "random")
_DEFAULT_PRESETS = (1, 6, 9)
_DEFAULT_REPEATS = 3
_DEFAULT_NUM_BLOCKS = 200
_DEFAULT_TXS_PER_BLOCK = 50
_DEFAULT_DATA_SIZE = 1_000_000


@dataclass
class BenchmarkResult:
    dataset: str
    preset: int
    original_size: int
    compressed_size: int
    compress_seconds: float
    decompress_seconds: float

    @property
    def ratio(self) -> float:
        """Compression ratio (original / compressed); higher is better."""
        return self.original_size / self.compressed_size

    @property
    def compress_mb_per_s(self) -> float:
        return self.original_size / self.compress_seconds / 1_000_000

    @property
    def decompress_mb_per_s(self) -> float:
        return self.original_size / self.decompress_seconds / 1_000_000


def synthetic_block_data(num_blocks: int = 200, txs_per_block: int = 50) -> bytes:
    """JSON-serialized blocks with pseudo-random transactions (deterministic seed)."""
    rng = random.Random(_SEED)  # noqa: S311  # non-cryptographic synthetic benchmark data
    blocks = []
    for number in range(num_blocks):
        transactions = [
            {
                "tx_id": f"tx-{number}-{i}",
                "from": f"0x{rng.getrandbits(160):040x}",
                "to": f"0x{rng.getrandbits(160):040x}",
                "amount": rng.randint(1, 10_000_000),
            }
            for i in range(txs_per_block)
        ]
        blocks.append({"block_number": number, "transactions": transactions})
    return json.dumps(blocks).encode("utf-8")


def repetitive_data(size: int = 1_000_000) -> bytes:
    """Highly redundant data: best case for compression."""
    template = b'{"block_number": 1, "data": "Block 1 data"}'
    return (template * (size // len(template) + 1))[:size]


def random_data(size: int = 1_000_000) -> bytes:
    """Incompressible high-entropy data: worst case for compression."""
    return random.Random(_SEED).randbytes(size)  # noqa: S311  # non-cryptographic benchmark data


DATASETS: dict[str, Callable[[], bytes]] = {
    "synthetic-blocks": synthetic_block_data,
    "repetitive": repetitive_data,
    "random": random_data,
}


def benchmark_dataset(name: str, data: bytes, preset: int, repeats: int = 3) -> BenchmarkResult:
    """Benchmark one dataset at one LZMA preset, verifying the round trip."""
    compressor = BlockCompressor(preset=preset)
    compressed = compressor.compress_block(data)
    if compressor.decompress_block(compressed) != data:
        raise RuntimeError(f"round-trip mismatch for dataset {name!r} at preset {preset}")
    return BenchmarkResult(
        dataset=name,
        preset=preset,
        original_size=len(data),
        compressed_size=len(compressed),
        compress_seconds=best_time(lambda: compressor.compress_block(data), repeats),
        decompress_seconds=best_time(lambda: compressor.decompress_block(compressed), repeats),
    )


def run_benchmarks(
    presets: tuple[int, ...] = _DEFAULT_PRESETS,
    repeats: int = _DEFAULT_REPEATS,
    datasets: dict[str, Callable[[], bytes]] | None = None,
) -> list[BenchmarkResult]:
    selected_datasets = DATASETS if datasets is None else datasets
    results = []
    for name, make_data in selected_datasets.items():
        data = make_data()
        for preset in presets:
            results.append(benchmark_dataset(name, data, preset, repeats))
    return results


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blockchain-compress-bench",
        description="Benchmark framed LZMA compression ratio and throughput across datasets.",
    )
    parser.add_argument(
        "--datasets",
        nargs="+",
        choices=list(_DEFAULT_DATASETS),
        default=list(_DEFAULT_DATASETS),
        help="Dataset profiles to benchmark (default: synthetic-blocks repetitive random).",
    )
    parser.add_argument(
        "--presets",
        nargs="+",
        type=int,
        default=list(_DEFAULT_PRESETS),
        help="LZMA presets 0..9 to evaluate (default: 1 6 9).",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=_DEFAULT_REPEATS,
        help="Timing repetitions per measurement (default: 3).",
    )
    parser.add_argument(
        "--num-blocks",
        type=int,
        default=_DEFAULT_NUM_BLOCKS,
        help="Block count for synthetic-blocks dataset (default: 200).",
    )
    parser.add_argument(
        "--txs-per-block",
        type=int,
        default=_DEFAULT_TXS_PER_BLOCK,
        help="Transactions per synthetic block (default: 50).",
    )
    parser.add_argument(
        "--data-size",
        type=int,
        default=_DEFAULT_DATA_SIZE,
        help="Payload size in bytes for repetitive and random datasets (default: 1000000).",
    )
    add_common_benchmark_args(parser)
    return parser


def _build_dataset_factories(
    datasets: list[str], num_blocks: int, txs_per_block: int, data_size: int
) -> dict[str, Callable[[], bytes]]:
    def _make_synthetic() -> bytes:
        return synthetic_block_data(num_blocks=num_blocks, txs_per_block=txs_per_block)

    def _make_repetitive() -> bytes:
        return repetitive_data(size=data_size)

    def _make_random() -> bytes:
        return random_data(size=data_size)

    builders: dict[str, Callable[[], bytes]] = {
        "synthetic-blocks": _make_synthetic,
        "repetitive": _make_repetitive,
        "random": _make_random,
    }
    return {name: builders[name] for name in datasets}


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    if args.num_blocks < 1:
        parser.error("--num-blocks must be at least 1")
    if args.txs_per_block < 1:
        parser.error("--txs-per-block must be at least 1")
    if args.data_size < 1:
        parser.error("--data-size must be at least 1")
    if any(not 0 <= preset <= 9 for preset in args.presets):
        parser.error("--presets values must be integers between 0 and 9")
    try:
        resolved_level = resolve_log_level(args.log_level)
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))
    configure_logging(resolved_level)

    using_defaults = (
        args.datasets == list(_DEFAULT_DATASETS)
        and args.presets == list(_DEFAULT_PRESETS)
        and args.repeats == _DEFAULT_REPEATS
        and args.num_blocks == _DEFAULT_NUM_BLOCKS
        and args.txs_per_block == _DEFAULT_TXS_PER_BLOCK
        and args.data_size == _DEFAULT_DATA_SIZE
    )
    if using_defaults:
        results = run_benchmarks()
    else:
        selected = _build_dataset_factories(
            args.datasets, args.num_blocks, args.txs_per_block, args.data_size
        )
        results = run_benchmarks(
            presets=tuple(args.presets),
            repeats=args.repeats,
            datasets=selected,
        )

    if args.json:
        payload: dict[str, object] = {
            "benchmark": "compression",
            "parameters": {
                "datasets": args.datasets,
                "presets": args.presets,
                "repeats": args.repeats,
                "num_blocks": args.num_blocks,
                "txs_per_block": args.txs_per_block,
                "data_size": args.data_size,
            },
            "results": [
                {
                    "dataset": r.dataset,
                    "preset": r.preset,
                    "original_size": r.original_size,
                    "compressed_size": r.compressed_size,
                    "ratio": r.ratio,
                    "compress_seconds": r.compress_seconds,
                    "decompress_seconds": r.decompress_seconds,
                    "compress_mb_per_s": r.compress_mb_per_s,
                    "decompress_mb_per_s": r.decompress_mb_per_s,
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
        f"{'dataset':<18} {'preset':>6} {'orig KB':>9} {'comp KB':>9} "
        f"{'ratio':>7} {'comp MB/s':>10} {'decomp MB/s':>12}"
    )
    lines = [header, "-" * len(header)]
    for result in results:
        lines.append(
            f"{result.dataset:<18} {result.preset:6d} {result.original_size / 1000:9.1f} "
            f"{result.compressed_size / 1000:9.1f} {result.ratio:7.2f} "
            f"{result.compress_mb_per_s:10.2f} {result.decompress_mb_per_s:12.2f}"
        )
    emit_benchmark_output(lines, as_json=False, output_path=args.output, target_logger=logger)


if __name__ == "__main__":
    main()
