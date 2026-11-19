# test_scalability_benchmark.py

from blockchain_compression.benchmarks import scalability_benchmark as scale_bench
from blockchain_compression.benchmarks.scalability_benchmark import (
    benchmark_block_compression,
    benchmark_chain_append,
    benchmark_incremental_pruning,
    benchmark_serialization,
    benchmark_sqlite_store,
    benchmark_zk,
)


def test_scalability_benchmark_functions_smoke():
    ser_results = benchmark_serialization(
        sizes=(("small(1tx)", 1), ("medium(10tx)", 10)),
        repeats=2,
    )
    assert len(ser_results) == 2
    assert all(r.old_ms > 0 and r.new_ms > 0 for r in ser_results)

    comp_results = benchmark_block_compression(
        sizes=(("small(1tx)", 1, 2), ("large(40tx)", 40, 2)),
        configs=((1, 256), (6, 0)),
    )
    assert comp_results[0].frame == "raw(0x00)"
    assert comp_results[1].frame == "lzma(0x01)"
    assert comp_results[2].frame == "lzma(0x01)"
    assert all(r.bomb_cap_rejected for r in comp_results)

    chain_results = benchmark_chain_append(heights=(10, 20), sample_blocks=5)
    assert [r.height for r in chain_results] == [10, 20]
    assert all(r.avg_ms_per_block > 0 and r.traced_mem_kib > 0 for r in chain_results)

    prune_results = benchmark_incremental_pruning(
        noop_height=20,
        noop_calls=10,
        growth_heights=(10, 20),
    )
    assert len(prune_results.height_growth_ms) == 2
    assert prune_results.noop_500_us > 0

    sqlite_res = benchmark_sqlite_store(
        num_blocks=15,
        overflow_blocks=8,
        overflow_txs_per_block=100,
        cache_probe_txs=25,
        cache_limit=5,
    )
    assert sqlite_res.num_blocks == 15
    assert sqlite_res.overflow_freelist_after_prune > 0
    assert sqlite_res.overflow_after_compact_bytes < sqlite_res.overflow_before_bytes
    assert sqlite_res.bounded_cached_ids == 5
    assert sqlite_res.bounded_replay_rejected is True

    zk_res = benchmark_zk(repeats=1)
    assert zk_res.backend == "python"
    assert zk_res.gen_mean_ms > 0 and zk_res.ver_mean_ms > 0


def test_scalability_benchmark_main_fast(monkeypatch, caplog):
    monkeypatch.setattr(
        scale_bench,
        "benchmark_serialization",
        lambda: benchmark_serialization(
            sizes=(("small(1tx)", 1), ("medium(10tx)", 10)),
            repeats=1,
        ),
    )
    monkeypatch.setattr(
        scale_bench,
        "benchmark_block_compression",
        lambda: benchmark_block_compression(
            sizes=(("small(1tx)", 1, 1), ("large(40tx)", 40, 1)),
            configs=((1, 256), (6, 0)),
        ),
    )
    monkeypatch.setattr(
        scale_bench,
        "benchmark_chain_append",
        lambda: benchmark_chain_append(heights=(10,), sample_blocks=5),
    )
    monkeypatch.setattr(
        scale_bench,
        "benchmark_incremental_pruning",
        lambda: benchmark_incremental_pruning(
            noop_height=15,
            noop_calls=5,
            growth_heights=(10,),
        ),
    )
    monkeypatch.setattr(
        scale_bench,
        "benchmark_sqlite_store",
        lambda: benchmark_sqlite_store(
            num_blocks=10,
            overflow_blocks=6,
            overflow_txs_per_block=100,
            cache_probe_txs=20,
            cache_limit=5,
        ),
    )
    monkeypatch.setattr(scale_bench, "benchmark_zk", lambda: benchmark_zk(repeats=1))

    with caplog.at_level("INFO"):
        scale_bench.main()

    assert "JSON Serialization" in caplog.text
    assert "Block Compression & Framing" in caplog.text
    assert "SQLite ChainStore & Bounded Replay Cache" in caplog.text
    assert "ZK Throughput" in caplog.text
