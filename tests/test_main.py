# test_main.py

import pytest

from blockchain_compression import main as main_mod
from blockchain_compression.chain import Chain
from blockchain_compression.observability import MetricsRegistry
from blockchain_compression.persistence import ChainStore


def test_main_default_cli_end_to_end(monkeypatch, caplog):
    fresh_metrics = MetricsRegistry()
    monkeypatch.setattr(main_mod, "get_metrics_registry", lambda: fresh_metrics)

    with caplog.at_level("INFO"):
        main_mod.main([])

    assert "Chain length (incl. genesis): 4" in caplog.text
    assert "Account state: {'Alice': 80, 'Bob': 70, 'Carol': 75}" in caplog.text
    assert "Proof verifies against the tree's own root: True" in caplog.text
    assert "Chain length after growth: 9" in caplog.text
    assert "Blocks with a full body: [7, 8]" in caplog.text
    assert "Reloaded state: {'Alice': 80, 'Bob': 95, 'Carol': 75}" in caplog.text
    assert "verify_proof(proof, commitment) after dict round-trip (no plaintext): True" in (
        caplog.text
    )
    assert "blocks_added_total 8.0" in caplog.text
    assert "last_block_compression_ratio" in caplog.text
    assert fresh_metrics.get_counter("blocks_added_total") == 8.0
    assert fresh_metrics.get_counter("blocks_pruned_total") == 7.0


def test_main_with_explicit_db_path_and_retention(tmp_path, monkeypatch, caplog):
    fresh_metrics = MetricsRegistry()
    monkeypatch.setattr(main_mod, "get_metrics_registry", lambda: fresh_metrics)
    db_path = tmp_path / "nested" / "cli_chain.db"

    with caplog.at_level("INFO"):
        main_mod.main(["--db-path", str(db_path), "--max-full-blocks", "4"])

    assert db_path.exists()
    assert "Blocks with a full body: [5, 6, 7, 8]" in caplog.text

    with ChainStore(db_path) as store:
        reloaded = store.load_chain()
    assert len(reloaded) == 9
    assert reloaded.validate_chain() is True
    assert [b.index for b in reloaded if not b.is_pruned] == [5, 6, 7, 8]
    assert reloaded.state.get_current_state() == {"Alice": 80, "Bob": 95, "Carol": 75}


def test_main_argument_parsing_and_error_paths(capsys):
    with pytest.raises(SystemExit) as help_exc:
        main_mod.main(["--help"])
    assert help_exc.value.code == 0
    help_out = capsys.readouterr().out
    assert "--max-full-blocks" in help_out
    assert "--db-path" in help_out

    with pytest.raises(SystemExit) as bad_val_exc:
        main_mod.main(["--max-full-blocks", "0"])
    assert bad_val_exc.value.code == 2
    assert "at least 1" in capsys.readouterr().err

    with pytest.raises(SystemExit) as unknown_exc:
        main_mod.main(["--no-such-option"])
    assert unknown_exc.value.code == 2


def test_sync_block_missing_account_raises(tmp_path):
    db_path = tmp_path / "sync_err.db"
    chain = Chain()
    blk = chain.add_block([{"tx_id": "tx-1", "account": "Alice", "amount": 10}])

    with ChainStore(db_path) as store:
        with pytest.raises(KeyError, match="Bob"):
            main_mod._sync_block(
                store,
                chain,
                blk,
                [{"tx_id": "tx-x", "account": "Bob", "amount": 1}],
            )
