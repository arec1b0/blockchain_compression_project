# test_pruning_benchmark.py

import json

import pytest

from blockchain_compression.benchmarks import pruning_benchmark as prune_bench
from blockchain_compression.benchmarks.pruning_benchmark import _build_chain, benchmark_pruning
from blockchain_compression.pruning import BlockchainPruner


def test_retained_full_block_count_is_bounded_independent_of_chain_length():
    # The "load_resistance" property: once chain length exceeds max_full_blocks,
    # the number of blocks kept full stays pinned at max_full_blocks - it must
    # not grow as the chain grows. Correctness, not timing, so no CI flakiness.
    max_full_blocks = 50
    for chain_length in (60, 600):
        chain = _build_chain(chain_length)
        BlockchainPruner(chain, max_full_blocks=max_full_blocks).prune()

        retained = sum(1 for block in chain if not block.is_pruned)
        assert retained == max_full_blocks
        assert chain.validate_chain()


def test_benchmark_pruning_smoke():
    result = benchmark_pruning(chain_length=100, max_full_blocks=10)

    assert result.chain_length == 100
    assert result.prune_seconds > 0
    assert result.memory_before_bytes > 0
    assert result.memory_freed_bytes > 0  # pruning must actually free traced memory


def test_pruning_benchmark_run_and_main_fast(monkeypatch, caplog):
    monkeypatch.setattr(
        prune_bench,
        "run_benchmarks",
        lambda chain_lengths=(15, 25), max_full_blocks=5: [
            benchmark_pruning(length, max_full_blocks) for length in chain_lengths
        ],
    )
    with caplog.at_level("INFO"):
        prune_bench.main([])

    assert "chain length" in caplog.text
    assert "freed KB" in caplog.text


def test_pruning_benchmark_cli_json_parameters_and_errors(tmp_path, capsys):
    prune_bench.main(["--chain-lengths", "12", "20", "--max-full-blocks", "4", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["benchmark"] == "pruning"
    assert payload["parameters"] == {"chain_lengths": [12, 20], "max_full_blocks": 4}
    assert [r["chain_length"] for r in payload["results"]] == [12, 20]
    assert all(r["memory_freed_bytes"] > 0 for r in payload["results"])

    out_file = tmp_path / "pruning.txt"
    prune_bench.main(
        [
            "--chain-lengths",
            "10",
            "--max-full-blocks",
            "3",
            "--output",
            str(out_file),
            "--log-level",
            "WARNING",
        ]
    )
    assert "chain length" in out_file.read_text(encoding="utf-8")

    for bad_args in (
        ["--chain-lengths", "0"],
        ["--max-full-blocks", "0"],
        ["--log-level", "BAD"],
    ):
        with pytest.raises(SystemExit) as exc:
            prune_bench.main(bad_args)
        assert exc.value.code == 2
