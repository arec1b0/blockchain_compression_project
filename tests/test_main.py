# test_main.py

import json
import logging

import pytest

from blockchain_compression import main as main_mod
from blockchain_compression.chain import Chain
from blockchain_compression.observability import (
    LOG_LEVEL_ENV_VAR,
    MetricsRegistry,
    configure_logging,
    resolve_log_level,
)
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


def test_main_log_level_env_var_and_json_logging(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(main_mod, "get_metrics_registry", MetricsRegistry)

    # WARNING suppresses INFO banner lines while completing cleanly.
    caplog.clear()
    main_mod.main(["--log-level", "warning"])
    assert "Chain length (incl. genesis):" not in caplog.text

    # Environment variable sets default level; CLI flag takes precedence over env var.
    monkeypatch.setenv(LOG_LEVEL_ENV_VAR, "WARNING")
    assert resolve_log_level() == "WARNING"
    assert resolve_log_level("debug") == "DEBUG"

    caplog.clear()
    main_mod.main([])
    assert "Chain length (incl. genesis):" not in caplog.text

    caplog.clear()
    main_mod.main(["--log-level", "DEBUG", "--log-format", "json"])
    records = [json.loads(line) for line in caplog.text.strip().splitlines()]
    assert any(r["level"] == "DEBUG" and "CLI options:" in r["message"] for r in records)
    assert any(
        r["level"] == "INFO" and r["logger"] == "blockchain_compression.main" for r in records
    )

    # Restore plain text formatter on active handlers when run with default text format.
    caplog.clear()
    main_mod.main(["--log-level", "INFO"])
    assert "Chain length (incl. genesis): 4" in caplog.text

    # Invalid CLI or env log level fails with actionable error.
    with pytest.raises(SystemExit) as bad_cli_lvl:
        main_mod.main(["--log-level", "TRACE"])
    assert bad_cli_lvl.value.code == 2

    monkeypatch.setenv(LOG_LEVEL_ENV_VAR, "BOGUS")
    with pytest.raises(SystemExit) as bad_env_lvl:
        main_mod.main([])
    assert bad_env_lvl.value.code == 2
    monkeypatch.delenv(LOG_LEVEL_ENV_VAR)

    with pytest.raises(SystemExit) as bad_fmt:
        main_mod.main(["--log-format", "xml"])
    assert bad_fmt.value.code == 2

    with pytest.raises(TypeError, match="string"):
        resolve_log_level(123)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="log_format"):
        configure_logging("INFO", log_format=123)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid --log-format"):
        configure_logging("INFO", log_format="yaml")


def test_main_verify_integrity_flag_and_logging_handler_creation(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(main_mod, "get_metrics_registry", MetricsRegistry)
    db_path = tmp_path / "verify_cli.db"

    with caplog.at_level(logging.INFO):
        main_mod.main(["--db-path", str(db_path), "--verify-integrity"])

    assert "Integrity check passed: blocks=9, schema_version=2" in caplog.text
    assert "idx_applied_tx_ids_block_idx" in caplog.text

    # Exercise StreamHandler creation when no root handlers exist.
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    root.handlers.clear()
    try:
        assert configure_logging("WARNING", log_format="json") == logging.WARNING
        assert len(root.handlers) == 1
        record = logging.LogRecord("demo", logging.WARNING, __file__, 1, "boom", (), None)
        parsed = json.loads(root.handlers[0].format(record))
        assert parsed == {"level": "WARNING", "logger": "demo", "message": "boom"}
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)
