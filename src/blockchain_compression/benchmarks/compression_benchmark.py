"""LZMA compression benchmark over synthetic blockchain workloads.

Measures compressed size, compression ratio, and compress/decompress
throughput across datasets with different entropy profiles and LZMA presets.

Run with ``blockchain-compress-bench`` or
``python -m blockchain_compression.benchmarks.compression_benchmark``.
"""

import json
import logging
import random
import time
from dataclasses import dataclass

from blockchain_compression.compression import BlockCompressor

logger = logging.getLogger(__name__)

_SEED = 1337


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
    rng = random.Random(_SEED)
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
    return random.Random(_SEED).randbytes(size)


DATASETS = {
    "synthetic-blocks": synthetic_block_data,
    "repetitive": repetitive_data,
    "random": random_data,
}


def _best_time(func, repeats: int) -> float:
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        func()
        best = min(best, time.perf_counter() - start)
    return best


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
        compress_seconds=_best_time(lambda: compressor.compress_block(data), repeats),
        decompress_seconds=_best_time(lambda: compressor.decompress_block(compressed), repeats),
    )


def run_benchmarks(presets=(1, 6, 9), repeats: int = 3) -> list:
    results = []
    for name, make_data in DATASETS.items():
        data = make_data()
        for preset in presets:
            results.append(benchmark_dataset(name, data, preset, repeats))
    return results


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    header = (
        f"{'dataset':<18} {'preset':>6} {'orig KB':>9} {'comp KB':>9} "
        f"{'ratio':>7} {'comp MB/s':>10} {'decomp MB/s':>12}"
    )
    logger.info(header)
    logger.info("-" * len(header))
    for result in run_benchmarks():
        logger.info(
            "%-18s %6d %9.1f %9.1f %7.2f %10.2f %12.2f",
            result.dataset,
            result.preset,
            result.original_size / 1000,
            result.compressed_size / 1000,
            result.ratio,
            result.compress_mb_per_s,
            result.decompress_mb_per_s,
        )


if __name__ == "__main__":
    main()
