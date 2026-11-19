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

from blockchain_compression.canonical import canonical_json
from blockchain_compression.chain import Block, Chain
from blockchain_compression.compression import FRAME_RAW, BlockCompressor
from blockchain_compression.persistence import ChainStore
from blockchain_compression.pruning import BlockchainPruner
from blockchain_compression.zk_proof import (
    PedersenCommitment,
    generate_proof,
    verify_proof,
)
from blockchain_compression.zk_proof.groups import ZK_MODEXP_BACKEND

logger = logging.getLogger(__name__)


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
            ov_freelist = store._conn.execute("PRAGMA freelist_count").fetchone()[0]
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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger.info(
        "ENV: Python %s, SQLite %s, OS %s, CPU %s",
        sys.version.split()[0],
        sqlite3.sqlite_version,
        platform.platform(),
        platform.processor(),
    )

    logger.info("\n=== 1. JSON Serialization (Double vs Single-Pass) ===")
    for ser in benchmark_serialization():
        logger.info(
            "%-14s raw=%6dB | old=%.4fms new=%.4fms (speedup=%.2fx)",
            ser.label,
            ser.raw_bytes,
            ser.old_ms,
            ser.new_ms,
            ser.old_ms / max(ser.new_ms, 1e-9),
        )

    logger.info("\n=== 2. Block Compression & Framing ===")
    for comp in benchmark_block_compression():
        logger.info(
            "%-13s preset=%d thresh=%3d frame=%-10s raw=%6dB stored=%6dB "
            "ratio=%5.2fx create=%7.3fms decomp=%6.3fms cap_ok=%s",
            comp.label,
            comp.preset,
            comp.min_compress_bytes,
            comp.frame,
            comp.raw_bytes,
            comp.stored_bytes,
            comp.ratio,
            comp.create_ms,
            comp.decompress_ms,
            comp.bomb_cap_rejected,
        )

    logger.info("\n=== 3. In-Memory Chain.add_block Scaling ===")
    for app in benchmark_chain_append():
        logger.info(
            "height=%4d: %.3f ms/block | traced_mem=%.1f KiB",
            app.height,
            app.avg_ms_per_block,
            app.traced_mem_kib,
        )

    logger.info("\n=== 4. Incremental Pruning ===")
    pr = benchmark_incremental_pruning()
    for height, ms in pr.height_growth_ms:
        logger.info("growth_prune @height=%4d: %.3f ms", height, ms)
    logger.info("500x idempotent prune() @height=1200: %.2f us/call", pr.noop_500_us)

    logger.info("\n=== 5. SQLite ChainStore & Bounded Replay Cache ===")
    sr = benchmark_sqlite_store()
    logger.info(
        "%d add_block: %.3fs (%.1f blk/s) | prune=%.2fms | reload+validate=%.2fms",
        sr.num_blocks,
        sr.append_seconds,
        sr.blocks_per_sec,
        sr.prune_ms,
        sr.reload_validate_ms,
    )
    logger.info(
        "small inline DB size: before=%dB after_prune=%dB after_inc=%dB after_full=%dB",
        sr.size_before_bytes,
        sr.size_after_prune_bytes,
        sr.size_after_inc_compact_bytes,
        sr.size_after_full_compact_bytes,
    )
    logger.info(
        "overflow DB compaction: before=%dB after_prune=%dB (freelist=%d) after_compact=%dB",
        sr.overflow_before_bytes,
        sr.overflow_after_prune_bytes,
        sr.overflow_freelist_after_prune,
        sr.overflow_after_compact_bytes,
    )
    logger.info(
        "2000-tx replay cache RAM: unbounded=%.1f KiB vs bounded(100)=%.1f KiB "
        "(cached=%d, c-0_rejected=%s)",
        sr.unbounded_mem_kib,
        sr.bounded_mem_kib,
        sr.bounded_cached_ids,
        sr.bounded_replay_rejected,
    )

    logger.info("\n=== 6. ZK Throughput ===")
    zr = benchmark_zk()
    logger.info(
        "backend=%s reps=%d | generate=%.2fms (min=%.2f, max=%.2f, %.1f ops/s) | "
        "verify=%.2fms (min=%.2f, max=%.2f, %.1f ops/s)",
        zr.backend,
        zr.repeats,
        zr.gen_mean_ms,
        zr.gen_min_ms,
        zr.gen_max_ms,
        1000.0 / zr.gen_mean_ms,
        zr.ver_mean_ms,
        zr.ver_min_ms,
        zr.ver_max_ms,
        1000.0 / zr.ver_mean_ms,
    )


if __name__ == "__main__":
    main()
