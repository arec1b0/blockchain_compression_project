"""End-to-end performance and scalability benchmarks for Steps 4.1–4.5.

Measures:
- Old double-pass vs new single-pass block JSON serialization.
- Block creation, LZMA presets (0, 1, 6, 9), raw vs LZMA framing, and bounded
  decompression checks across small (1 tx), medium (50 txs), and large (500 txs)
  blocks.
- In-memory ``Chain.add_block`` append latency and traced memory at increasing
  heights.
- Incremental pruning cost at increasing heights and idempotent no-op pruning.
- SQLite ``ChainStore`` append throughput, pruning, incremental vs full
  compaction on disk, reload/validation time, and bounded vs unbounded
  ``applied_tx_ids`` RAM usage.
- Zero-knowledge ``generate_proof`` and ``verify_proof`` throughput and active
  modular-exponentiation backend.

Run with ``python -m blockchain_compression.benchmarks.scalability_benchmark``.
"""

import argparse
import gc
import hashlib
import logging
import platform
import sqlite3
import sys
import tempfile
import time
import tracemalloc
from dataclasses import dataclass
from pathlib import Path

from blockchain_compression.benchmarks._cli import (
    add_common_benchmark_args,
    emit_benchmark_output,
    format_json_payload,
)
from blockchain_compression.canonical import canonical_json
from blockchain_compression.chain import Block, Chain
from blockchain_compression.compression import FRAME_RAW, BlockCompressor
from blockchain_compression.observability import configure_logging, resolve_log_level
from blockchain_compression.persistence import ChainStore
from blockchain_compression.pruning import BlockchainPruner
from blockchain_compression.zk_proof import (
    PedersenCommitment,
    generate_proof,
    verify_proof,
)
from blockchain_compression.zk_proof.groups import ZK_MODEXP_BACKEND

logger = logging.getLogger(__name__)

_DEFAULT_REPEATS = 50
_DEFAULT_COMPRESSION_TX_COUNTS = (1, 50, 500)
_DEFAULT_CHAIN_HEIGHTS = (100, 1000, 3000)
_DEFAULT_SAMPLE_BLOCKS = 50
_DEFAULT_PRUNE_HEIGHTS = (100, 1000, 3000)
_DEFAULT_NOOP_HEIGHT = 1200
_DEFAULT_NOOP_CALLS = 500
_DEFAULT_SQLITE_BLOCKS = 300
_DEFAULT_OVERFLOW_BLOCKS = 40
_DEFAULT_OVERFLOW_TXS_PER_BLOCK = 100
_DEFAULT_CACHE_PROBE_TXS = 2000
_DEFAULT_CACHE_LIMIT = 100
_DEFAULT_ZK_REPEATS = 5


@dataclass(frozen=True)
class SerializationBenchResult:
    label: str
    num_txs: int
    raw_bytes: int
    old_ms: float
    new_ms: float


@dataclass(frozen=True)
class BlockCompressionBenchResult:
    label: str
    num_txs: int
    preset: int
    min_compress_bytes: int
    frame: str
    raw_bytes: int
    stored_bytes: int
    ratio: float
    create_ms: float
    decompress_ms: float
    bomb_cap_rejected: bool


@dataclass(frozen=True)
class ChainAppendBenchResult:
    height: int
    avg_ms_per_block: float
    traced_mem_kib: float


@dataclass(frozen=True)
class IncrementalPruneBenchResult:
    height_growth_ms: list[tuple[int, float]]
    noop_500_us: float


@dataclass(frozen=True)
class SQLiteStoreBenchResult:
    num_blocks: int
    append_seconds: float
    blocks_per_sec: float
    prune_ms: float
    size_before_bytes: int
    size_after_prune_bytes: int
    size_after_inc_compact_bytes: int
    size_after_full_compact_bytes: int
    reload_validate_ms: float
    overflow_before_bytes: int
    overflow_after_prune_bytes: int
    overflow_after_compact_bytes: int
    overflow_freelist_after_prune: int
    unbounded_mem_kib: float
    bounded_mem_kib: float
    bounded_cached_ids: int
    bounded_replay_rejected: bool


@dataclass(frozen=True)
class ZKBenchResult:
    backend: str
    repeats: int
    gen_mean_ms: float
    gen_min_ms: float
    gen_max_ms: float
    ver_mean_ms: float
    ver_min_ms: float
    ver_max_ms: float


def make_batch(prefix: str, count: int) -> list[dict]:
    """Build deterministic transaction batches matching the pre-4.x baseline shape."""
    return [
        {"tx_id": f"{prefix}-{i}", "account": f"acct-{i % 50}", "amount": i + 1}
        for i in range(count)
    ]


def benchmark_serialization(
    sizes: tuple[tuple[str, int], ...] = (
        ("small(1tx)", 1),
        ("medium(50tx)", 50),
        ("large(500tx)", 500),
    ),
    repeats: int = 50,
) -> list[SerializationBenchResult]:
    """Compare double ``canonical_json`` vs single-pass array assembly."""
    results: list[SerializationBenchResult] = []
    for label, n_tx in sizes:
        batch = make_batch(f"ser-{label}", n_tx)
        old_json = canonical_json(batch)
        new_json = "[" + ",".join(canonical_json(tx) for tx in batch) + "]"
        if old_json != new_json:
            raise RuntimeError(f"canonical JSON mismatch for {label}")

        n_reps = repeats if n_tx <= 50 else max(1, min(repeats, 10))
        # Warm up both paths once.
        _ = [canonical_json(tx) for tx in batch], canonical_json(batch)
        _ = "[" + ",".join(canonical_json(tx) for tx in batch) + "]"

        t0 = time.perf_counter()
        for _ in range(n_reps):
            _ = [canonical_json(tx) for tx in batch]
            _ = canonical_json(batch)
        old_ms = (time.perf_counter() - t0) / n_reps * 1000.0

        t0 = time.perf_counter()
        for _ in range(n_reps):
            tx_strings = [canonical_json(tx) for tx in batch]
            _ = "[" + ",".join(tx_strings) + "]"
        new_ms = (time.perf_counter() - t0) / n_reps * 1000.0

        results.append(
            SerializationBenchResult(
                label=label,
                num_txs=n_tx,
                raw_bytes=len(new_json.encode("utf-8")),
                old_ms=old_ms,
                new_ms=new_ms,
            )
        )
    return results


def benchmark_block_compression(
    sizes: tuple[tuple[str, int, int], ...] = (
        ("small(1tx)", 1, 50),
        ("medium(50tx)", 50, 50),
        ("large(500tx)", 500, 10),
    ),
    configs: tuple[tuple[int, int], ...] = (
        (1, 256),  # New balanced default (size-aware framing)
        (0, 256),
        (6, 256),
        (9, 256),
        (6, 0),  # Legacy forced-LZMA preset 6
    ),
) -> list[BlockCompressionBenchResult]:
    """Compare ``Block.create`` latency, stored size, framing, and safety."""
    bomb_payload = b'["' + (b"x" * 2_000) + b'"]'
    results: list[BlockCompressionBenchResult] = []
    for label, n_tx, reps in sizes:
        batch = make_batch(f"b-{label}", n_tx)
        raw_bytes = len(canonical_json(batch).encode("utf-8"))
        for preset, threshold in configs:
            comp = BlockCompressor(preset=preset, min_compress_bytes=threshold)
            # 1 warm-up before timed repetitions.
            warm = Block.create(index=1, prev_hash="0" * 64, transactions=batch, compressor=comp)
            if warm.compressed_body is None:
                raise RuntimeError("expected compressed_body on newly created block")

            t0 = time.perf_counter()
            blk = warm
            for _ in range(reps):
                blk = Block.create(index=1, prev_hash="0" * 64, transactions=batch, compressor=comp)
            create_ms = (time.perf_counter() - t0) / reps * 1000.0

            stored = blk.compressed_body
            if stored is None:
                raise RuntimeError("expected compressed_body on newly created block")
            t0 = time.perf_counter()
            for _ in range(reps):
                decoded = comp.decompress_block(stored)
            decompress_ms = (time.perf_counter() - t0) / reps * 1000.0
            if len(decoded) != raw_bytes:
                raise RuntimeError("decompressed byte length mismatch")

            bomb = BlockCompressor(preset=0, min_compress_bytes=threshold).compress_block(
                bomb_payload
            )
            capped = BlockCompressor(max_decompressed_bytes=128)
            bomb_rejected = False
            try:
                capped.decompress_block(bomb)
            except ValueError:
                bomb_rejected = True
            if not bomb_rejected:
                raise RuntimeError("decompression cap failed to reject oversized payload")

            frame = "raw(0x00)" if stored[0] == FRAME_RAW else "lzma(0x01)"
            results.append(
                BlockCompressionBenchResult(
                    label=label,
                    num_txs=n_tx,
                    preset=preset,
                    min_compress_bytes=threshold,
                    frame=frame,
                    raw_bytes=raw_bytes,
                    stored_bytes=len(stored),
                    ratio=raw_bytes / len(stored),
                    create_ms=create_ms,
                    decompress_ms=decompress_ms,
                    bomb_cap_rejected=bomb_rejected,
                )
            )
    return results


def benchmark_chain_append(
    heights: tuple[int, ...] = (100, 1000, 3000),
    sample_blocks: int = 50,
) -> list[ChainAppendBenchResult]:
    """Measure per-block append latency and live traced RAM across heights."""
    gc.collect()
    tracemalloc.start()
    results: list[ChainAppendBenchResult] = []
    try:
        chain = Chain()
        for target in heights:
            while len(chain) - 1 < target - sample_blocks:
                idx = len(chain)
                chain.add_block([{"tx_id": f"m-{idx}", "account": f"a-{idx % 200}", "amount": 1}])
            t0 = time.perf_counter()
            for _ in range(sample_blocks):
                idx = len(chain)
                chain.add_block([{"tx_id": f"m-{idx}", "account": f"a-{idx % 200}", "amount": 1}])
            avg_ms = (time.perf_counter() - t0) / sample_blocks * 1000.0
            cur_mem, _ = tracemalloc.get_traced_memory()
            results.append(
                ChainAppendBenchResult(
                    height=target,
                    avg_ms_per_block=avg_ms,
                    traced_mem_kib=cur_mem / 1024.0,
                )
            )
    finally:
        tracemalloc.stop()
    return results


def benchmark_incremental_pruning(
    noop_height: int = 1200,
    noop_calls: int = 500,
    growth_heights: tuple[int, ...] = (100, 1000, 3000),
) -> IncrementalPruneBenchResult:
    """Measure incremental growth pruning and repeated idempotent ``prune()`` calls."""
    chain = Chain()
    pruner = BlockchainPruner(chain, max_full_blocks=50)
    growth_ms: list[tuple[int, float]] = []
    for target in growth_heights:
        while len(chain) - 1 < target:
            idx = len(chain)
            chain.add_block([{"tx_id": f"pg-{idx}", "account": "A", "amount": 1}])
        t0 = time.perf_counter()
        pruner.prune()
        growth_ms.append((target, (time.perf_counter() - t0) * 1000.0))

    noop_chain = Chain()
    noop_pruner = BlockchainPruner(noop_chain, max_full_blocks=50)
    for i in range(noop_height):
        noop_chain.add_block([{"tx_id": f"pn-{i}", "account": "A", "amount": 1}])
    noop_pruner.prune()
    t0 = time.perf_counter()
    for _ in range(noop_calls):
        noop_pruner.prune()
    noop_us = (time.perf_counter() - t0) / noop_calls * 1e6

    return IncrementalPruneBenchResult(height_growth_ms=growth_ms, noop_500_us=noop_us)


def _measure_replay_cache_mem(num_txs: int, max_cached: int | None) -> tuple[float, int, bool]:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "cache_mem.db"
        gc.collect()
        tracemalloc.start()
        try:
            with ChainStore(db_path, max_cached_tx_ids=max_cached) as store:
                chain = Chain()
                for i in range(num_txs):
                    store.add_block(chain, [{"tx_id": f"c-{i}", "account": "A", "amount": 1}])
                cached_count = chain.state.cached_applied_tx_count
                replay_rejected = chain.state.is_applied("c-0")
                cur_mem, _ = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    return cur_mem / 1024.0, cached_count, replay_rejected


def benchmark_sqlite_store(
    num_blocks: int = 300,
    overflow_blocks: int = 40,
    overflow_txs_per_block: int = 100,
    cache_probe_txs: int = 2000,
    cache_limit: int = 100,
) -> SQLiteStoreBenchResult:
    """Measure SQLite WAL append throughput, pruning, compaction, reload, and RAM."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "bench.db"
        with ChainStore(db_path) as store:
            chain = Chain()
            t0 = time.perf_counter()
            for i in range(num_blocks):
                store.add_block(
                    chain,
                    [
                        {
                            "tx_id": f"db-{i}",
                            "account": f"u-{i % 50}",
                            "amount": 1,
                            "pad": f"data-{i}-" + ("x" * 200),
                        }
                    ],
                )
            append_s = time.perf_counter() - t0
            store.compact()
            size_before = db_path.stat().st_size

            pruner = BlockchainPruner(chain, max_full_blocks=min(10, max(1, num_blocks // 3)))
            t_prune0 = time.perf_counter()
            store.prune_chain(pruner)
            prune_ms = (time.perf_counter() - t_prune0) * 1000.0
            size_after_prune = db_path.stat().st_size

            store.compact()
            size_after_inc = db_path.stat().st_size
            store.compact(full=True)
            size_after_full = db_path.stat().st_size

        with ChainStore(db_path) as store:
            t_load0 = time.perf_counter()
            reloaded = store.load_chain(validate=True)
            reload_ms = (time.perf_counter() - t_load0) * 1000.0
            if len(reloaded) != num_blocks + 1:
                raise RuntimeError("reloaded chain length mismatch")

        overflow_db = Path(tmp) / "overflow.db"
        with ChainStore(overflow_db) as store:
            ov_chain = Chain()
            for i in range(overflow_blocks):
                batch = [
                    {
                        "tx_id": f"ov-{i}-{j}",
                        "account": f"a-{j % 8}",
                        "amount": 1,
                        "memo": hashlib.sha256(f"{i}-{j}".encode()).hexdigest(),
                    }
                    for j in range(overflow_txs_per_block)
                ]
                store.add_block(ov_chain, batch)
            store.compact()
            ov_before = overflow_db.stat().st_size
            ov_pruner = BlockchainPruner(ov_chain, max_full_blocks=2)
            store.prune_chain(ov_pruner)
            ov_freelist = int(store.get_pragma("freelist_count"))
            ov_after_prune = overflow_db.stat().st_size
            store.compact()
            ov_after_compact = overflow_db.stat().st_size

    unbounded_kib, _, _ = _measure_replay_cache_mem(cache_probe_txs, None)
    bounded_kib, bounded_cached, replay_ok = _measure_replay_cache_mem(cache_probe_txs, cache_limit)

    return SQLiteStoreBenchResult(
        num_blocks=num_blocks,
        append_seconds=append_s,
        blocks_per_sec=num_blocks / append_s,
        prune_ms=prune_ms,
        size_before_bytes=size_before,
        size_after_prune_bytes=size_after_prune,
        size_after_inc_compact_bytes=size_after_inc,
        size_after_full_compact_bytes=size_after_full,
        reload_validate_ms=reload_ms,
        overflow_before_bytes=ov_before,
        overflow_after_prune_bytes=ov_after_prune,
        overflow_after_compact_bytes=ov_after_compact,
        overflow_freelist_after_prune=ov_freelist,
        unbounded_mem_kib=unbounded_kib,
        bounded_mem_kib=bounded_kib,
        bounded_cached_ids=bounded_cached,
        bounded_replay_rejected=replay_ok,
    )


def benchmark_zk(repeats: int = 5) -> ZKBenchResult:
    """Measure ZK proof generation and verification latency after 1 warm-up."""
    pc = PedersenCommitment()
    commitment, blinding = pc.commit(42)
    warm_proof = generate_proof(42, blinding, commitment)
    if not verify_proof(warm_proof, commitment):
        raise RuntimeError("warm-up proof failed verification")

    gen_times: list[float] = []
    ver_times: list[float] = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        proof = generate_proof(42, blinding, commitment)
        gen_times.append((time.perf_counter() - t0) * 1000.0)

        t0 = time.perf_counter()
        if not verify_proof(proof, commitment):
            raise RuntimeError("benchmark proof failed verification")
        ver_times.append((time.perf_counter() - t0) * 1000.0)

    return ZKBenchResult(
        backend=ZK_MODEXP_BACKEND,
        repeats=repeats,
        gen_mean_ms=sum(gen_times) / repeats,
        gen_min_ms=min(gen_times),
        gen_max_ms=max(gen_times),
        ver_mean_ms=sum(ver_times) / repeats,
        ver_min_ms=min(ver_times),
        ver_max_ms=max(ver_times),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blockchain-compress-scalability-bench",
        description="Run the end-to-end performance and scalability benchmark suite.",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=_DEFAULT_REPEATS,
        help="Timing repetitions for serialization (and default for compression).",
    )
    parser.add_argument(
        "--compression-tx-counts",
        nargs="+",
        type=int,
        default=list(_DEFAULT_COMPRESSION_TX_COUNTS),
        help="Transaction counts per block for serialization and compression benchmarks.",
    )
    parser.add_argument(
        "--compression-repeats",
        type=int,
        default=None,
        help="Override repetition count for block compression benchmarks.",
    )
    parser.add_argument(
        "--chain-heights",
        nargs="+",
        type=int,
        default=list(_DEFAULT_CHAIN_HEIGHTS),
        help="Strictly ascending chain heights to sample append latency and RAM.",
    )
    parser.add_argument(
        "--sample-blocks",
        type=int,
        default=_DEFAULT_SAMPLE_BLOCKS,
        help="Number of recent blocks timed at each height in --chain-heights.",
    )
    parser.add_argument(
        "--prune-heights",
        nargs="+",
        type=int,
        default=list(_DEFAULT_PRUNE_HEIGHTS),
        help="Strictly ascending chain heights to measure incremental pruning.",
    )
    parser.add_argument(
        "--noop-height",
        type=int,
        default=_DEFAULT_NOOP_HEIGHT,
        help="Chain height for idempotent no-op prune() microbenchmark.",
    )
    parser.add_argument(
        "--noop-calls",
        type=int,
        default=_DEFAULT_NOOP_CALLS,
        help="Number of consecutive no-op prune() calls to average.",
    )
    parser.add_argument(
        "--sqlite-blocks",
        type=int,
        default=_DEFAULT_SQLITE_BLOCKS,
        help="Number of single-tx blocks persisted in the SQLite ChainStore benchmark.",
    )
    parser.add_argument(
        "--overflow-blocks",
        type=int,
        default=_DEFAULT_OVERFLOW_BLOCKS,
        help="Number of large multi-tx blocks used to benchmark freelist/incremental compaction.",
    )
    parser.add_argument(
        "--overflow-txs-per-block",
        type=int,
        default=_DEFAULT_OVERFLOW_TXS_PER_BLOCK,
        help="Transactions per overflow block in the SQLite compaction benchmark.",
    )
    parser.add_argument(
        "--cache-probe-txs",
        type=int,
        default=_DEFAULT_CACHE_PROBE_TXS,
        help="Transaction count used to compare unbounded vs bounded replay-cache RAM.",
    )
    parser.add_argument(
        "--cache-limit",
        type=int,
        default=_DEFAULT_CACHE_LIMIT,
        help="Bounded applied_tx_ids cache limit (>= 0) for the SQLite benchmark.",
    )
    parser.add_argument(
        "--zk-repeats",
        type=int,
        default=_DEFAULT_ZK_REPEATS,
        help="Repetitions for ZK proof generation and verification timing.",
    )
    add_common_benchmark_args(parser)
    return parser


def _size_label(n_tx: int) -> str:
    if n_tx == 1:
        return "small(1tx)"
    if n_tx == 50:
        return "medium(50tx)"
    if n_tx == 500:
        return "large(500tx)"
    return f"batch({n_tx}tx)"


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    positive_int_flags = [
        ("--repeats", args.repeats),
        ("--sample-blocks", args.sample_blocks),
        ("--noop-height", args.noop_height),
        ("--noop-calls", args.noop_calls),
        ("--sqlite-blocks", args.sqlite_blocks),
        ("--overflow-blocks", args.overflow_blocks),
        ("--overflow-txs-per-block", args.overflow_txs_per_block),
        ("--cache-probe-txs", args.cache_probe_txs),
        ("--zk-repeats", args.zk_repeats),
    ]
    for flag, val in positive_int_flags:
        if val < 1:
            parser.error(f"{flag} must be at least 1")
    if args.compression_repeats is not None and args.compression_repeats < 1:
        parser.error("--compression-repeats must be at least 1")
    if args.cache_limit < 0:
        parser.error("--cache-limit must be at least 0")
    if any(n < 1 for n in args.compression_tx_counts):
        parser.error("--compression-tx-counts values must be at least 1")
    if any(h < args.sample_blocks for h in args.chain_heights):
        parser.error("--chain-heights values must be >= --sample-blocks")
    if any(a >= b for a, b in zip(args.chain_heights, args.chain_heights[1:], strict=False)):
        parser.error("--chain-heights values must be strictly ascending")
    if any(h < 1 for h in args.prune_heights):
        parser.error("--prune-heights values must be at least 1")
    if any(a >= b for a, b in zip(args.prune_heights, args.prune_heights[1:], strict=False)):
        parser.error("--prune-heights values must be strictly ascending")


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    try:
        resolved_level = resolve_log_level(args.log_level)
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))
    configure_logging(resolved_level)

    if args.repeats == _DEFAULT_REPEATS and args.compression_tx_counts == list(
        _DEFAULT_COMPRESSION_TX_COUNTS
    ):
        ser_results = benchmark_serialization()
    else:
        ser_sizes = tuple((_size_label(n), n) for n in args.compression_tx_counts)
        ser_results = benchmark_serialization(sizes=ser_sizes, repeats=args.repeats)

    if (
        args.compression_tx_counts == list(_DEFAULT_COMPRESSION_TX_COUNTS)
        and args.compression_repeats is None
        and args.repeats == _DEFAULT_REPEATS
    ):
        comp_results = benchmark_block_compression()
    else:
        comp_reps = (
            args.compression_repeats if args.compression_repeats is not None else args.repeats
        )
        comp_sizes = tuple((_size_label(n), n, comp_reps) for n in args.compression_tx_counts)
        comp_results = benchmark_block_compression(sizes=comp_sizes)

    if (
        args.chain_heights == list(_DEFAULT_CHAIN_HEIGHTS)
        and args.sample_blocks == _DEFAULT_SAMPLE_BLOCKS
    ):
        append_results = benchmark_chain_append()
    else:
        append_results = benchmark_chain_append(
            heights=tuple(args.chain_heights),
            sample_blocks=args.sample_blocks,
        )

    if (
        args.prune_heights == list(_DEFAULT_PRUNE_HEIGHTS)
        and args.noop_height == _DEFAULT_NOOP_HEIGHT
        and args.noop_calls == _DEFAULT_NOOP_CALLS
    ):
        pr = benchmark_incremental_pruning()
    else:
        pr = benchmark_incremental_pruning(
            noop_height=args.noop_height,
            noop_calls=args.noop_calls,
            growth_heights=tuple(args.prune_heights),
        )

    if (
        args.sqlite_blocks == _DEFAULT_SQLITE_BLOCKS
        and args.overflow_blocks == _DEFAULT_OVERFLOW_BLOCKS
        and args.overflow_txs_per_block == _DEFAULT_OVERFLOW_TXS_PER_BLOCK
        and args.cache_probe_txs == _DEFAULT_CACHE_PROBE_TXS
        and args.cache_limit == _DEFAULT_CACHE_LIMIT
    ):
        sr = benchmark_sqlite_store()
    else:
        sr = benchmark_sqlite_store(
            num_blocks=args.sqlite_blocks,
            overflow_blocks=args.overflow_blocks,
            overflow_txs_per_block=args.overflow_txs_per_block,
            cache_probe_txs=args.cache_probe_txs,
            cache_limit=args.cache_limit,
        )

    zr = benchmark_zk() if args.zk_repeats == _DEFAULT_ZK_REPEATS else benchmark_zk(args.zk_repeats)

    py_ver = sys.version.split()[0]
    sqlite_ver = sqlite3.sqlite_version
    os_name = platform.platform()
    cpu_name = platform.processor()

    if args.json:
        payload: dict[str, object] = {
            "benchmark": "scalability",
            "environment": {
                "python": py_ver,
                "sqlite": sqlite_ver,
                "os": os_name,
                "cpu": cpu_name,
            },
            "parameters": {
                "repeats": args.repeats,
                "compression_tx_counts": args.compression_tx_counts,
                "compression_repeats": args.compression_repeats,
                "chain_heights": args.chain_heights,
                "sample_blocks": args.sample_blocks,
                "prune_heights": args.prune_heights,
                "noop_height": args.noop_height,
                "noop_calls": args.noop_calls,
                "sqlite_blocks": args.sqlite_blocks,
                "overflow_blocks": args.overflow_blocks,
                "overflow_txs_per_block": args.overflow_txs_per_block,
                "cache_probe_txs": args.cache_probe_txs,
                "cache_limit": args.cache_limit,
                "zk_repeats": args.zk_repeats,
            },
            "results": {
                "serialization": [
                    {
                        "label": s.label,
                        "num_txs": s.num_txs,
                        "raw_bytes": s.raw_bytes,
                        "old_ms": s.old_ms,
                        "new_ms": s.new_ms,
                        "speedup": s.old_ms / max(s.new_ms, 1e-9),
                    }
                    for s in ser_results
                ],
                "block_compression": [
                    {
                        "label": c.label,
                        "num_txs": c.num_txs,
                        "preset": c.preset,
                        "min_compress_bytes": c.min_compress_bytes,
                        "frame": c.frame,
                        "raw_bytes": c.raw_bytes,
                        "stored_bytes": c.stored_bytes,
                        "ratio": c.ratio,
                        "create_ms": c.create_ms,
                        "decompress_ms": c.decompress_ms,
                        "bomb_cap_rejected": c.bomb_cap_rejected,
                    }
                    for c in comp_results
                ],
                "chain_append": [
                    {
                        "height": a.height,
                        "avg_ms_per_block": a.avg_ms_per_block,
                        "traced_mem_kib": a.traced_mem_kib,
                    }
                    for a in append_results
                ],
                "incremental_pruning": {
                    "height_growth_ms": [{"height": h, "ms": ms} for h, ms in pr.height_growth_ms],
                    "noop_500_us": pr.noop_500_us,
                    "noop_us_per_call": pr.noop_500_us,
                },
                "sqlite_store": {
                    "num_blocks": sr.num_blocks,
                    "append_seconds": sr.append_seconds,
                    "blocks_per_sec": sr.blocks_per_sec,
                    "prune_ms": sr.prune_ms,
                    "size_before_bytes": sr.size_before_bytes,
                    "size_after_prune_bytes": sr.size_after_prune_bytes,
                    "size_after_inc_compact_bytes": sr.size_after_inc_compact_bytes,
                    "size_after_full_compact_bytes": sr.size_after_full_compact_bytes,
                    "reload_validate_ms": sr.reload_validate_ms,
                    "overflow_before_bytes": sr.overflow_before_bytes,
                    "overflow_after_prune_bytes": sr.overflow_after_prune_bytes,
                    "overflow_after_compact_bytes": sr.overflow_after_compact_bytes,
                    "overflow_freelist_after_prune": sr.overflow_freelist_after_prune,
                    "unbounded_mem_kib": sr.unbounded_mem_kib,
                    "bounded_mem_kib": sr.bounded_mem_kib,
                    "bounded_cached_ids": sr.bounded_cached_ids,
                    "bounded_replay_rejected": sr.bounded_replay_rejected,
                },
                "zk": {
                    "backend": zr.backend,
                    "repeats": zr.repeats,
                    "gen_mean_ms": zr.gen_mean_ms,
                    "gen_min_ms": zr.gen_min_ms,
                    "gen_max_ms": zr.gen_max_ms,
                    "ver_mean_ms": zr.ver_mean_ms,
                    "ver_min_ms": zr.ver_min_ms,
                    "ver_max_ms": zr.ver_max_ms,
                },
            },
        }
        emit_benchmark_output(
            format_json_payload(payload),
            as_json=True,
            output_path=args.output,
            target_logger=logger,
        )
        return

    lines = [
        f"ENV: Python {py_ver}, SQLite {sqlite_ver}, OS {os_name}, CPU {cpu_name}",
        "",
        "=== 1. JSON Serialization (Double vs Single-Pass) ===",
    ]
    for ser in ser_results:
        lines.append(
            f"{ser.label:<14} raw={ser.raw_bytes:6d}B | old={ser.old_ms:.4f}ms "
            f"new={ser.new_ms:.4f}ms (speedup={ser.old_ms / max(ser.new_ms, 1e-9):.2f}x)"
        )

    lines.extend(["", "=== 2. Block Compression & Framing ==="])
    for comp in comp_results:
        lines.append(
            f"{comp.label:<13} preset={comp.preset:d} thresh={comp.min_compress_bytes:3d} "
            f"frame={comp.frame:<10} raw={comp.raw_bytes:6d}B stored={comp.stored_bytes:6d}B "
            f"ratio={comp.ratio:5.2f}x create={comp.create_ms:7.3f}ms "
            f"decomp={comp.decompress_ms:6.3f}ms cap_ok={comp.bomb_cap_rejected}"
        )

    lines.extend(["", "=== 3. In-Memory Chain.add_block Scaling ==="])
    for app in append_results:
        lines.append(
            f"height={app.height:4d}: {app.avg_ms_per_block:.3f} ms/block | "
            f"traced_mem={app.traced_mem_kib:.1f} KiB"
        )

    lines.extend(["", "=== 4. Incremental Pruning ==="])
    for height, ms in pr.height_growth_ms:
        lines.append(f"growth_prune @height={height:4d}: {ms:.3f} ms")
    lines.append(
        f"{args.noop_calls}x idempotent prune() @height={args.noop_height}: "
        f"{pr.noop_500_us:.2f} us/call"
    )

    lines.extend(
        [
            "",
            "=== 5. SQLite ChainStore & Bounded Replay Cache ===",
            f"{sr.num_blocks} add_block: {sr.append_seconds:.3f}s "
            f"({sr.blocks_per_sec:.1f} blk/s) | "
            f"prune={sr.prune_ms:.2f}ms | reload+validate={sr.reload_validate_ms:.2f}ms",
            f"small inline DB size: before={sr.size_before_bytes}B "
            f"after_prune={sr.size_after_prune_bytes}B "
            f"after_inc={sr.size_after_inc_compact_bytes}B "
            f"after_full={sr.size_after_full_compact_bytes}B",
            f"overflow DB compaction: before={sr.overflow_before_bytes}B "
            f"after_prune={sr.overflow_after_prune_bytes}B "
            f"(freelist={sr.overflow_freelist_after_prune}) "
            f"after_compact={sr.overflow_after_compact_bytes}B",
            f"{args.cache_probe_txs}-tx replay cache RAM: "
            f"unbounded={sr.unbounded_mem_kib:.1f} KiB vs "
            f"bounded({sr.bounded_cached_ids})={sr.bounded_mem_kib:.1f} KiB "
            f"(cached={sr.bounded_cached_ids}, c-0_rejected={sr.bounded_replay_rejected})",
            "",
            "=== 6. ZK Throughput ===",
            f"backend={zr.backend} reps={zr.repeats} | "
            f"generate={zr.gen_mean_ms:.2f}ms (min={zr.gen_min_ms:.2f}, max={zr.gen_max_ms:.2f}, "
            f"{1000.0 / zr.gen_mean_ms:.1f} ops/s) | "
            f"verify={zr.ver_mean_ms:.2f}ms (min={zr.ver_min_ms:.2f}, max={zr.ver_max_ms:.2f}, "
            f"{1000.0 / zr.ver_mean_ms:.1f} ops/s)",
        ]
    )
    emit_benchmark_output(lines, as_json=False, output_path=args.output, target_logger=logger)


if __name__ == "__main__":
    main()
