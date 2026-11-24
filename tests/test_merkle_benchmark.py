# test_merkle_benchmark.py

import json

import pytest

from blockchain_compression.benchmarks import merkle_benchmark as merkle_bench
from blockchain_compression.benchmarks.merkle_benchmark import benchmark_size


def test_proof_length_within_log2_bound():
    # Structural, deterministic assertion - no timing involved, so no CI flakiness.
    for size in (1, 2, 3, 5, 10, 50, 200):
        result = benchmark_size(size, repeats=1)
        assert result.proof_length <= result.log2_bound


def test_benchmark_result_fields_are_positive():
    result = benchmark_size(64, repeats=1)
    assert result.num_transactions == 64
    assert result.proof_length > 0
    assert result.get_proof_seconds > 0
    assert result.verify_seconds > 0


def test_single_transaction_has_empty_proof():
    result = benchmark_size(1, repeats=1)
    assert result.proof_length == 0
    assert result.log2_bound == 0


def test_merkle_benchmark_run_and_main_fast(monkeypatch, caplog):
    monkeypatch.setattr(
        merkle_bench,
        "run_benchmarks",
        lambda sizes=(4, 8), repeats=1: [benchmark_size(s, repeats) for s in sizes],
    )
    with caplog.at_level("INFO"):
        merkle_bench.main([])

    assert "transactions" in caplog.text
    assert "get_proof ms" in caplog.text


def test_merkle_benchmark_cli_json_parameters_and_errors(tmp_path, capsys):
    out_file = tmp_path / "merkle.json"
    merkle_bench.main(["--sizes", "4", "16", "--repeats", "1", "--json", "--output", str(out_file)])
    assert capsys.readouterr().out == ""
    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["benchmark"] == "merkle"
    assert payload["parameters"] == {"sizes": [4, 16], "repeats": 1}
    assert [r["num_transactions"] for r in payload["results"]] == [4, 16]
    assert all(r["proof_length"] <= r["log2_bound"] for r in payload["results"])

    merkle_bench.main(["--sizes", "8", "--repeats", "1", "--json"])
    stdout_payload = json.loads(capsys.readouterr().out)
    assert stdout_payload["results"][0]["num_transactions"] == 8

    for bad_args in (["--sizes", "0"], ["--repeats", "0"], ["--log-level", "NOPE"]):
        with pytest.raises(SystemExit) as exc:
            merkle_bench.main(bad_args)
        assert exc.value.code == 2
