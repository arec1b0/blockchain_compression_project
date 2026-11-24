# test_chain_store.py

import contextlib
import hashlib
import json
import sqlite3
import threading

import pytest

from blockchain_compression.chain import Block, Chain
from blockchain_compression.compression import BlockCompressor, StateDelta, extract_block_delta
from blockchain_compression.compression.state_delta import MAX_BALANCE, MIN_BALANCE
from blockchain_compression.observability import MetricsRegistry
from blockchain_compression.persistence import ChainStore, IntegrityReport
from blockchain_compression.persistence.chain_store import _convert_legacy_balance
from blockchain_compression.pruning import BlockchainPruner
from blockchain_compression.zk_proof import (
    PedersenCommitment,
    ZKAttachment,
    generate_proof,
    hash_to_scalar,
)


def _tx(tx_id, account, amount):
    return {"tx_id": tx_id, "account": account, "amount": amount}


def _exec_db(db_path, sql, params=()):
    with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
        with conn:
            conn.execute(sql, params)


def _query_db(db_path, sql, params=()):
    with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()


def _sync_block(store, chain, block, transactions):
    """Test-local helper mirroring how a caller derives store.append_block's
    arguments from a Chain.add_block() call using extract_block_delta."""
    delta = extract_block_delta(transactions, chain.state)
    store.append_block(block, delta.tx_ids, delta.updated_accounts)


def _build_and_persist(store, num_blocks):
    chain = Chain()
    _sync_block(store, chain, chain.get_block(0), [])
    for i in range(num_blocks):
        txs = [_tx(f"tx-{i}", "Alice", 10)]
        block = chain.add_block(txs)
        _sync_block(store, chain, block, txs)
    return chain


def test_round_trip_preserves_chain_and_state(tmp_path):
    db_path = tmp_path / "chain.db"
    with ChainStore(db_path) as store:
        original = _build_and_persist(store, num_blocks=5)

    with ChainStore(db_path) as store:
        reloaded = store.load_chain()

    assert len(reloaded) == len(original)
    assert [b.hash for b in reloaded] == [b.hash for b in original]
    assert reloaded.state.get_current_state() == original.state.get_current_state()
    assert reloaded.state.get_applied_tx_ids() == original.state.get_applied_tx_ids()
    assert reloaded.validate_chain()


def test_reload_after_pruning_still_validates(tmp_path):
    db_path = tmp_path / "chain.db"
    with ChainStore(db_path) as store:
        chain = _build_and_persist(store, num_blocks=6)
        pruned = BlockchainPruner(chain, max_full_blocks=2).prune()
        for index in pruned:
            store.mark_pruned(index)

    assert pruned  # sanity: the test actually exercised pruning

    with ChainStore(db_path) as store:
        reloaded = store.load_chain()

    assert reloaded.validate_chain()
    for index in pruned:
        assert reloaded.get_block(index).is_pruned
        assert reloaded.get_block(index).transactions is None
    # Header fields survive even though the body is gone.
    assert reloaded.get_block(pruned[0]).hash == chain.get_block(pruned[0]).hash


def test_reload_preserves_full_body_for_retained_blocks(tmp_path):
    db_path = tmp_path / "chain.db"
    with ChainStore(db_path) as store:
        chain = _build_and_persist(store, num_blocks=4)
        for index in BlockchainPruner(chain, max_full_blocks=1).prune():
            store.mark_pruned(index)

    with ChainStore(db_path) as store:
        reloaded = store.load_chain()

    last = reloaded.get_block(len(reloaded) - 1)
    assert not last.is_pruned
    assert last.transactions == chain.get_block(len(chain) - 1).transactions
    assert last.verify_body()


def test_empty_store_loads_genesis_only_chain(tmp_path):
    db_path = tmp_path / "chain.db"
    with ChainStore(db_path):
        pass  # just create the schema, persist nothing

    with ChainStore(db_path) as store:
        reloaded = store.load_chain()

    assert len(reloaded) == 1
    assert reloaded.validate_chain()


def test_mark_pruned_missing_index_raises(tmp_path):
    db_path = tmp_path / "chain.db"
    with ChainStore(db_path) as store:
        with pytest.raises(KeyError):
            store.mark_pruned(999)


def test_replay_protection_survives_reload(tmp_path):
    db_path = tmp_path / "chain.db"
    with ChainStore(db_path) as store:
        _build_and_persist(store, num_blocks=1)

    with ChainStore(db_path) as store:
        reloaded = store.load_chain()

    # Replaying an already-applied tx_id after reload is still a no-op.
    assert reloaded.state.apply_transaction(_tx("tx-0", "Alice", 9999)) == {}
    state = reloaded.state.get_current_state()
    assert state == {"Alice": 10}
    assert type(state["Alice"]) is int


def test_append_block_is_atomic_across_tables(tmp_path):
    db_path = tmp_path / "chain.db"
    with ChainStore(db_path) as store:
        chain = Chain()
        genesis = chain.get_block(0)
        block = chain.add_block([_tx("tx-1", "Alice", 10)])

        _exec_db(
            db_path,
            """
            CREATE TRIGGER reject_state BEFORE INSERT ON state_snapshot
            BEGIN SELECT RAISE(ABORT, 'injected state write failure'); END
            """,
        )
        # Failure mid-transaction on state_snapshot must roll back the block row
        # inserted earlier in the same append_block() call.
        with pytest.raises(sqlite3.IntegrityError):
            store.append_block(block, ["tx-1"], {"Alice": 10})

        _exec_db(db_path, "DROP TRIGGER reject_state")
        _sync_block(store, chain, genesis, [])
    rows = _query_db(db_path, "SELECT idx FROM blocks")
    assert [row["idx"] for row in rows] == [0]  # only genesis, block 1 rolled back


def test_append_block_rejects_non_integer_balances(tmp_path):
    db_path = tmp_path / "chain.db"
    with ChainStore(db_path) as store:
        chain = Chain()
        block = chain.add_block([_tx("tx-1", "Alice", 10)])

        for bad_balance in (10.5, 10.0, [10], True, MAX_BALANCE + 1, MIN_BALANCE - 1):
            with pytest.raises(ValueError):
                store.append_block(block, ["tx-1"], {"Alice": bad_balance})

    assert _query_db(db_path, "SELECT COUNT(*) AS n FROM blocks")[0]["n"] == 0


def test_coordinated_add_block_rolls_back_without_mutating_chain(tmp_path):
    db_path = tmp_path / "chain.db"
    with ChainStore(db_path) as store:
        chain = Chain()
        _exec_db(
            db_path,
            """
            CREATE TRIGGER reject_state BEFORE INSERT ON state_snapshot
            BEGIN SELECT RAISE(ABORT, 'injected state write failure'); END
            """,
        )

        with pytest.raises(sqlite3.IntegrityError):
            store.add_block(chain, [_tx("tx-1", "Alice", 10)])

        assert len(chain) == 1
        assert chain.state.get_current_state() == {}
        assert chain.state.get_applied_tx_ids() == frozenset()
    assert _query_db(db_path, "SELECT COUNT(*) AS n FROM blocks")[0]["n"] == 0
    assert _query_db(db_path, "SELECT COUNT(*) AS n FROM applied_tx_ids")[0]["n"] == 0


def test_coordinated_add_and_prune_round_trip(tmp_path):
    db_path = tmp_path / "chain.db"
    with ChainStore(db_path) as store:
        chain = Chain()
        for i in range(3):
            store.add_block(chain, [_tx(f"tx-{i}", "Alice", 10)])

        pruner = BlockchainPruner(chain, max_full_blocks=1)
        pruned = store.prune_chain(pruner)

        assert pruned == [0, 1, 2]
        assert chain.validate_chain()

    with ChainStore(db_path) as store:
        reloaded = store.load_chain()
    assert reloaded.validate_chain()
    state = reloaded.state.get_current_state()
    assert state == {"Alice": 30}
    assert type(state["Alice"]) is int
    assert [block.is_pruned for block in reloaded] == [True, True, True, False]


def test_coordinated_prune_failure_leaves_chain_and_database_unchanged(tmp_path):
    db_path = tmp_path / "chain.db"
    with ChainStore(db_path) as store:
        chain = _build_and_persist(store, num_blocks=2)
        pruner = BlockchainPruner(chain, max_full_blocks=1)
        _exec_db(
            db_path,
            """
            CREATE TRIGGER reject_prune BEFORE UPDATE OF body ON blocks
            WHEN OLD.idx = 0
            BEGIN SELECT RAISE(ABORT, 'injected prune failure'); END
            """,
        )

        with pytest.raises(sqlite3.IntegrityError):
            store.prune_chain(pruner)

        assert all(not block.is_pruned for block in chain)
        assert chain.validate_chain()
    rows = _query_db(db_path, "SELECT is_pruned FROM blocks ORDER BY idx")
    assert [row["is_pruned"] for row in rows] == [0, 0, 0]


def test_context_manager_closes_connection(tmp_path):
    db_path = tmp_path / "chain.db"
    with ChainStore(db_path) as store:
        pass

    with pytest.raises(sqlite3.ProgrammingError):
        store.load_chain()


def test_large_exact_integer_balance_persists_without_precision_loss(tmp_path):
    db_path = tmp_path / "chain.db"
    amount = 10**18 + 1
    with ChainStore(db_path) as store:
        chain = Chain()
        store.add_block(chain, [_tx("tx-wei", "Alice", amount)])

    with ChainStore(db_path) as store:
        reloaded = store.load_chain()

    balance = reloaded.state.get_current_state()["Alice"]
    assert balance == amount
    assert type(balance) is int


def test_coordinated_add_block_rejects_float_and_replay_without_persisting(tmp_path):
    db_path = tmp_path / "chain.db"
    with ChainStore(db_path) as store:
        chain = Chain()
        store.add_block(chain, [_tx("tx-1", "Alice", 100)])

        with pytest.raises(ValueError, match="exact integer"):
            store.add_block(chain, [_tx("tx-2", "Alice", 12.5)])
        with pytest.raises(ValueError, match="already been applied"):
            store.add_block(chain, [_tx("tx-1", "Alice", 999)])

        assert len(chain) == 2
    assert _query_db(db_path, "SELECT COUNT(*) AS n FROM blocks")[0]["n"] == 2
    assert _query_db(db_path, "SELECT COUNT(*) AS n FROM applied_tx_ids")[0]["n"] == 1


def test_append_block_does_not_ignore_duplicate_tx_id_collision(tmp_path):
    db_path = tmp_path / "chain.db"
    with ChainStore(db_path) as store:
        chain = Chain()
        genesis = chain.get_block(0)
        b1 = chain.add_block([_tx("tx-1", "Alice", 10)])
        b2 = chain.add_block([_tx("tx-2", "Alice", 20)])
        store.append_block(genesis, [], {})
        store.append_block(b1, ["tx-1"], {"Alice": 10})

        with pytest.raises(sqlite3.IntegrityError):
            store.append_block(b2, ["tx-1"], {"Alice": 30})

    assert _query_db(db_path, "SELECT COUNT(*) AS n FROM blocks")[0]["n"] == 2


def test_legacy_real_schema_migrates_to_integer_and_reloads(tmp_path):
    db_path = tmp_path / "legacy.db"
    chain = Chain()
    genesis = chain.get_block(0)
    b1 = chain.add_block([_tx("tx-1", "Alice", 80), _tx("tx-2", "Bob", 95)])

    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE blocks (
            idx         INTEGER PRIMARY KEY,
            prev_hash   TEXT NOT NULL,
            merkle_root TEXT,
            hash        TEXT NOT NULL UNIQUE,
            timestamp   REAL NOT NULL,
            body        BLOB,
            is_pruned   INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE state_snapshot (
            account TEXT PRIMARY KEY,
            balance REAL NOT NULL
        );
        CREATE TABLE applied_tx_ids (
            tx_id     TEXT PRIMARY KEY,
            block_idx INTEGER NOT NULL REFERENCES blocks(idx)
        );
        """
    )
    for blk in (genesis, b1):
        conn.execute(
            "INSERT INTO blocks (idx, prev_hash, merkle_root, hash, timestamp, body, is_pruned) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                blk.index,
                blk.prev_hash,
                blk.merkle_root,
                blk.hash,
                blk.timestamp,
                blk.compressed_body,
                0,
            ),
        )
    conn.execute("INSERT INTO state_snapshot (account, balance) VALUES ('Alice', 80.0)")
    conn.execute("INSERT INTO state_snapshot (account, balance) VALUES ('Bob', 95.0)")
    conn.execute("INSERT INTO applied_tx_ids (tx_id, block_idx) VALUES ('tx-1', 1)")
    conn.execute("INSERT INTO applied_tx_ids (tx_id, block_idx) VALUES ('tx-2', 1)")
    conn.commit()
    conn.close()

    with ChainStore(db_path) as store:
        reloaded = store.load_chain()
        state = reloaded.state.get_current_state()
        assert state == {"Alice": 80, "Bob": 95}
        assert all(type(v) is int for v in state.values())

        # Can continue appending exact large integers after migration.
        store.add_block(reloaded, [_tx("tx-3", "Alice", 10**18 + 1)])

    assert _query_db(db_path, "PRAGMA user_version")[0][0] == 2
    with ChainStore(db_path) as store:
        reloaded_again = store.load_chain()
    assert reloaded_again.state.get_current_state()["Alice"] == 80 + 10**18 + 1


def test_legacy_real_schema_rejects_fractional_balance_on_migration(tmp_path):
    db_path = tmp_path / "legacy_bad.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE blocks (
            idx INTEGER PRIMARY KEY, prev_hash TEXT NOT NULL, merkle_root TEXT,
            hash TEXT NOT NULL UNIQUE, timestamp REAL NOT NULL, body BLOB,
            is_pruned INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE state_snapshot (account TEXT PRIMARY KEY, balance REAL NOT NULL);
        CREATE TABLE applied_tx_ids (
            tx_id TEXT PRIMARY KEY,
            block_idx INTEGER NOT NULL REFERENCES blocks(idx)
        );
        INSERT INTO state_snapshot (account, balance) VALUES ('Alice', 12.5);
        """
    )
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="non-integer balance"):
        ChainStore(db_path)

    # Legacy table and version remain unmodified on failed migration.
    conn = sqlite3.connect(str(db_path))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert (
        conn.execute("SELECT balance FROM state_snapshot WHERE account='Alice'").fetchone()[0]
        == 12.5
    )
    conn.close()


def test_unsupported_future_schema_version_raises(tmp_path):
    db_path = tmp_path / "future.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="unsupported ChainStore schema version"):
        ChainStore(db_path)


def test_preloaded_chain_state_persists_and_reloads(tmp_path):
    db_path = tmp_path / "preloaded.db"
    preloaded = StateDelta()
    preloaded.apply_transaction(_tx("tx-pre", "Alice", 500))
    chain = Chain(state=preloaded)

    with ChainStore(db_path) as store:
        store.add_block(chain, [_tx("tx-1", "Bob", 25)])

    with ChainStore(db_path) as store:
        reloaded = store.load_chain()

    assert reloaded.state.get_current_state() == {"Alice": 500, "Bob": 25}
    assert reloaded.state.get_applied_tx_ids() == frozenset({"tx-pre", "tx-1"})
    assert reloaded.validate_chain()


def test_prune_chain_rolls_back_db_and_memory_when_pruner_fails(tmp_path, monkeypatch):
    db_path = tmp_path / "prune_fail.db"
    with ChainStore(db_path) as store:
        chain = _build_and_persist(store, num_blocks=3)
        pruner = BlockchainPruner(chain, max_full_blocks=1)

        def _failing_prune():
            chain.get_block(0).prune()  # simulate partial in-memory mutation before error
            raise RuntimeError("injected in-memory prune failure")

        monkeypatch.setattr(pruner, "prune", _failing_prune)

        with pytest.raises(RuntimeError, match="injected in-memory prune failure"):
            store.prune_chain(pruner)

        assert all(not block.is_pruned for block in chain)
        assert all(block.transactions is not None for block in chain)
        assert chain.validate_chain()
    rows = _query_db(db_path, "SELECT is_pruned, body FROM blocks ORDER BY idx")
    assert all(row["is_pruned"] == 0 and row["body"] is not None for row in rows)


def test_load_chain_validates_corrupted_chain_and_supports_check_bodies_flag(tmp_path):
    db_path = tmp_path / "corrupt.db"
    with ChainStore(db_path) as store:
        chain = Chain()
        store.add_block(chain, [_tx("tx-1", "Alice", 10)])
    bad_body = BlockCompressor().compress_block(b"123")
    _exec_db(db_path, "UPDATE blocks SET body = ? WHERE idx = 1", (bad_body,))

    with ChainStore(db_path) as store:
        with pytest.raises(ValueError, match="failed validation"):
            store.load_chain()
        header_only = store.load_chain(validate=True, check_bodies=False)
        assert len(header_only) == 2

    # Header corruption is still caught when check_bodies=False.
    _exec_db(db_path, "UPDATE blocks SET hash = ? WHERE idx = 1", ("f" * 64,))
    with ChainStore(db_path) as store:
        with pytest.raises(ValueError, match="failed validation"):
            store.load_chain(validate=True, check_bodies=False)
        unvalidated = store.load_chain(validate=False)
        assert unvalidated.get_block(1).hash == "f" * 64


def test_load_chain_and_add_block_reject_orphaned_state(tmp_path):
    db_path = tmp_path / "orphan.db"
    with ChainStore(db_path):
        pass
    _exec_db(db_path, "INSERT INTO state_snapshot (account, balance) VALUES ('Alice', 10)")

    with ChainStore(db_path) as store:
        with pytest.raises(ValueError, match="orphaned"):
            store.load_chain()
        with pytest.raises(ValueError, match="orphaned"):
            store.add_block(Chain(), [_tx("tx-1", "Alice", 10)])


def test_load_chain_skips_genesis_creation_and_accepts_metrics(tmp_path, monkeypatch):
    db_path = tmp_path / "fast_load.db"
    with ChainStore(db_path) as store:
        chain = Chain()
        store.add_block(chain, [_tx("tx-1", "Alice", 10)])

    def _fail(*args, **kwargs):
        raise AssertionError("Block.create must not run while reloading non-empty DB")

    monkeypatch.setattr(Block, "create", _fail)
    metrics = MetricsRegistry()
    with ChainStore(db_path) as store:
        reloaded = store.load_chain(metrics=metrics)

    assert len(reloaded) == 2
    assert reloaded._metrics is metrics


def test_prune_chain_window_matches_in_memory_pruner(tmp_path):
    db_path = tmp_path / "window.db"
    with ChainStore(db_path) as store:
        persisted_chain = Chain()
        memory_chain = Chain()
        for i in range(5):
            tx = _tx(f"tx-{i}", "Alice", 10)
            store.add_block(persisted_chain, [tx])
            memory_chain.add_block([tx])

        pruner_db = BlockchainPruner(persisted_chain, max_full_blocks=2)
        pruner_mem = BlockchainPruner(memory_chain, max_full_blocks=2)
        assert pruner_db.eligible_block_indices() == pruner_mem.eligible_block_indices()
        assert store.prune_chain(pruner_db) == pruner_mem.prune()


def test_zk_attachments_persist_reload_and_stay_compatible_with_plain_blocks(tmp_path):
    db_path = tmp_path / "zk_attachments.db"
    tx1 = _tx("tx-1", "Alice", 100)
    scalar = hash_to_scalar(tx1)
    commitment, blinding = PedersenCommitment().commit(scalar)
    proof = generate_proof(message=scalar, blinding=blinding, commitment=commitment)
    att_with_proof = ZKAttachment(commitment=commitment, proof=proof, tx_id="tx-1")
    att_without_proof = ZKAttachment(commitment=commitment, proof=None, tx_id=None)

    with ChainStore(db_path) as store:
        chain = Chain()
        # Block 1 has attachments; Block 2 has none (backward-compatible mix).
        store.add_block(chain, [tx1], zk_attachments=[att_with_proof, att_without_proof])
        store.add_block(chain, [_tx("tx-2", "Bob", 50)])

    with ChainStore(db_path) as store:
        reloaded = store.load_chain()

    assert reloaded.validate_chain() is True
    assert reloaded.get_block(1).zk_attachments == (att_with_proof, att_without_proof)
    assert reloaded.get_block(2).zk_attachments == ()
    assert reloaded.get_block(1).zk_attachments[0].verify() is True
    assert reloaded.get_block(1).zk_attachments[1].verify() is False
    # Overall chain verify_zk_attachments returns False because att_without_proof has no proof.
    assert reloaded.verify_zk_attachments() is False


def test_load_chain_fails_closed_on_malformed_persisted_zk_attachment(tmp_path):
    db_path = tmp_path / "zk_corrupt.db"
    tx1 = _tx("tx-1", "Alice", 100)
    commitment, blinding = PedersenCommitment().commit(42)
    proof = generate_proof(message=42, blinding=blinding, commitment=commitment)
    att = ZKAttachment(commitment=commitment, proof=proof, tx_id="tx-1")

    with ChainStore(db_path) as store:
        chain = Chain()
        store.add_block(chain, [tx1], zk_attachments=[att])

    _exec_db(db_path, "UPDATE zk_attachments SET payload_json = '{\"bad\": true}'")
    with ChainStore(db_path) as store:
        with pytest.raises(ValueError):
            store.load_chain()


def test_v1_schema_migrates_to_v2_and_preserves_existing_blocks(tmp_path):
    db_path = tmp_path / "v1_db.db"
    chain = Chain()
    genesis = chain.get_block(0)
    b1 = chain.add_block([_tx("tx-1", "Alice", 80)])

    with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
        conn.executescript(
            """
            CREATE TABLE blocks (
                idx INTEGER PRIMARY KEY, prev_hash TEXT NOT NULL, merkle_root TEXT,
                hash TEXT NOT NULL UNIQUE, timestamp REAL NOT NULL, body BLOB,
                is_pruned INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE state_snapshot (
                account TEXT PRIMARY KEY,
                balance INTEGER NOT NULL CHECK (typeof(balance) = 'integer')
            );
            CREATE TABLE applied_tx_ids (
                tx_id TEXT PRIMARY KEY,
                block_idx INTEGER NOT NULL REFERENCES blocks(idx)
            );
            PRAGMA user_version = 1;
            """
        )
        for blk in (genesis, b1):
            conn.execute(
                "INSERT INTO blocks "
                "(idx, prev_hash, merkle_root, hash, timestamp, body, is_pruned) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    blk.index,
                    blk.prev_hash,
                    blk.merkle_root,
                    blk.hash,
                    blk.timestamp,
                    blk.compressed_body,
                    0,
                ),
            )
        conn.execute("INSERT INTO state_snapshot (account, balance) VALUES ('Alice', 80)")
        conn.execute("INSERT INTO applied_tx_ids (tx_id, block_idx) VALUES ('tx-1', 1)")
        conn.commit()

    with ChainStore(db_path) as store:
        reloaded = store.load_chain()
        assert reloaded.validate_chain()
        assert reloaded.get_block(1).zk_attachments == ()
        assert reloaded.verify_zk_attachments() is True

    assert _query_db(db_path, "PRAGMA user_version")[0][0] == 2


def test_sqlite_wal_and_durability_pragmas_and_validation(tmp_path):
    db_path = tmp_path / "wal.db"
    with ChainStore(db_path) as store:
        assert str(store.get_pragma("journal_mode")).lower() == "wal"
        assert store.get_pragma("auto_vacuum") == 2
        assert store.get_pragma("busy_timeout") == 5000
        assert store.get_pragma("synchronous") == 1  # NORMAL
        assert store.get_pragma("foreign_keys") == 1
        pragmas = store.get_pragmas()
        assert pragmas["user_version"] == 2
        assert pragmas["foreign_keys"] == 1

    db_full = tmp_path / "wal_full.db"
    with ChainStore(db_full, busy_timeout_ms=1500, synchronous="FULL") as store:
        assert store.get_pragma("busy_timeout") == 1500
        assert store.get_pragma("synchronous") == 2  # FULL

    with pytest.raises(ValueError, match="busy_timeout_ms"):
        ChainStore(tmp_path / "bad1.db", busy_timeout_ms=-1)
    with pytest.raises(ValueError, match="busy_timeout_ms"):
        ChainStore(tmp_path / "bad2.db", busy_timeout_ms=True)
    with pytest.raises(ValueError, match="synchronous"):
        ChainStore(tmp_path / "bad3.db", synchronous="BOGUS")
    with pytest.raises(TypeError, match="synchronous"):
        ChainStore(tmp_path / "bad4.db", synchronous=123)


def test_prune_and_compact_reclaim_disk_space(tmp_path):
    db_path = tmp_path / "compact.db"
    with ChainStore(db_path) as store:
        chain = Chain()
        for i in range(25):
            # Enough unique digests per block (~3.5 KiB compressed) so SQLite allocates
            # overflow pages for blocks.body that move to the freelist when pruned.
            batch = [
                {
                    "tx_id": f"tx-{i}-{j}",
                    "account": f"Acct{j % 4}",
                    "amount": 10,
                    "memo": hashlib.sha256(f"{i}-{j}".encode()).hexdigest(),
                }
                for j in range(90)
            ]
            store.add_block(chain, batch)

        store.compact()
        size_before_prune = db_path.stat().st_size

        pruner = BlockchainPruner(chain, max_full_blocks=2)
        pruned = store.prune_chain(pruner, compact=False)
        assert len(pruned) == 24
        freelist_after_prune = int(store.get_pragma("freelist_count"))
        assert freelist_after_prune > 5

        store.compact(pages=5)
        freelist_after_partial = int(store.get_pragma("freelist_count"))
        assert freelist_after_partial == freelist_after_prune - 5

        store.compact()
        freelist_after_compact = int(store.get_pragma("freelist_count"))
        size_after_compact = db_path.stat().st_size

        # Adding more blocks and pruning with compact=True compacts inline cleanly.
        for i in range(25, 30):
            batch = [
                {
                    "tx_id": f"tx-{i}-{j}",
                    "account": f"Acct{j % 4}",
                    "amount": 10,
                    "memo": hashlib.sha256(f"{i}-{j}".encode()).hexdigest(),
                }
                for j in range(90)
            ]
            store.add_block(chain, batch)
        store.compact()
        size_before_second_prune = db_path.stat().st_size
        pruned_more = store.prune_chain(pruner, compact=True)
        assert pruned_more == [24, 25, 26, 27, 28]
        size_after_second_compact = db_path.stat().st_size
        assert store.get_pragma("freelist_count") == 0
        assert size_after_second_compact < size_before_second_prune

    assert freelist_after_compact == 0
    assert size_after_compact < size_before_prune

    with ChainStore(db_path) as store:
        reloaded = store.load_chain()
    assert reloaded.validate_chain() is True
    assert len(reloaded) == 31
    assert sum(1 for b in reloaded if not b.is_pruned) == 2


def test_compact_parameter_validation_and_legacy_full_vacuum(tmp_path):
    db_path = tmp_path / "legacy_autovac_none.db"
    chain = Chain()
    genesis = chain.get_block(0)
    with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
        conn.executescript(
            """
            PRAGMA auto_vacuum = NONE;
            CREATE TABLE blocks (
                idx INTEGER PRIMARY KEY, prev_hash TEXT NOT NULL, merkle_root TEXT,
                hash TEXT NOT NULL UNIQUE, timestamp REAL NOT NULL, body BLOB,
                is_pruned INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE state_snapshot (
                account TEXT PRIMARY KEY,
                balance INTEGER NOT NULL CHECK (typeof(balance) = 'integer')
            );
            CREATE TABLE applied_tx_ids (
                tx_id TEXT PRIMARY KEY,
                block_idx INTEGER NOT NULL REFERENCES blocks(idx)
            );
            CREATE TABLE zk_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                block_idx INTEGER NOT NULL REFERENCES blocks(idx),
                tx_id TEXT,
                payload_json TEXT NOT NULL
            );
            PRAGMA user_version = 2;
            """
        )
        conn.execute(
            "INSERT INTO blocks (idx, prev_hash, merkle_root, hash, timestamp, body, is_pruned) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (
                genesis.index,
                genesis.prev_hash,
                genesis.merkle_root,
                genesis.hash,
                genesis.timestamp,
                genesis.compressed_body,
            ),
        )
        conn.commit()

    with ChainStore(db_path) as store:
        chain = store.load_chain()
        for i in range(40):
            batch = [
                {
                    "tx_id": f"tx-{i}",
                    "account": "Alice",
                    "amount": 5,
                    "memo": "".join(
                        hashlib.sha256(f"legacy-{i}-{k}".encode()).hexdigest() for k in range(16)
                    ),
                }
            ]
            store.add_block(chain, batch)
        store.compact()
        before_full = db_path.stat().st_size

        pruner = BlockchainPruner(chain, max_full_blocks=2)
        store.prune_chain(pruner)
        # Before full VACUUM, incremental_vacuum cannot shrink a DB created with auto_vacuum=0.
        assert store.get_pragma("auto_vacuum") == 0
        store.compact(full=True)
        assert store.get_pragma("auto_vacuum") == 2
        after_full = db_path.stat().st_size

        with pytest.raises(ValueError, match="pages"):
            store.compact(pages=1, full=True)
        with pytest.raises(ValueError, match="positive integer"):
            store.compact(pages=0)
        with pytest.raises(TypeError, match="full"):
            store.compact(full="yes")

    assert after_full < before_full


def test_concurrent_wal_readers_and_writer_without_sqlite_busy(tmp_path):
    db_path = tmp_path / "concurrent_wal.db"
    chain = Chain()
    with ChainStore(db_path) as store:
        store.add_block(chain, [_tx("tx-seed", "Alice", 1)])

    stop_event = threading.Event()
    readers_ready = threading.Barrier(3)
    errors: list[BaseException] = []
    observed_heights: list[int] = []
    heights_lock = threading.Lock()

    def _writer():
        try:
            with ChainStore(db_path) as writer_store:
                readers_ready.wait(timeout=5)
                for i in range(40):
                    writer_store.add_block(chain, [_tx(f"tx-w-{i}", "Alice", 1)])
        except BaseException as exc:  # pragma: no cover - surfaced by assertion
            errors.append(exc)
        finally:
            stop_event.set()

    def _reader():
        try:
            with ChainStore(db_path) as reader_store:
                readers_ready.wait(timeout=5)
                while not stop_event.is_set():
                    snap = reader_store.load_chain(validate=True)
                    with heights_lock:
                        observed_heights.append(len(snap))
                snap = reader_store.load_chain(validate=True)
                with heights_lock:
                    observed_heights.append(len(snap))
        except BaseException as exc:  # pragma: no cover - surfaced by assertion
            errors.append(exc)

    threads = [
        threading.Thread(target=_writer),
        threading.Thread(target=_reader),
        threading.Thread(target=_reader),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not any(t.is_alive() for t in threads)
    assert errors == []
    assert observed_heights and all(h >= 2 for h in observed_heights)

    with ChainStore(db_path) as store:
        final_chain = store.load_chain(validate=True)
    assert len(final_chain) == 42
    assert final_chain.state.get_current_state() == {"Alice": 41}


def test_tip_validation_and_mid_history_tampering_caught_on_reload(tmp_path):
    db_path = tmp_path / "tip_check.db"
    with ChainStore(db_path) as store:
        chain = Chain()
        for i in range(3):
            store.add_block(chain, [_tx(f"tx-{i}", "Alice", 10)])

        # Appending with a diverged in-memory tip fails cleanly without scanning history.
        forked = Chain()
        with pytest.raises(ValueError, match="tip does not match"):
            store.add_block(forked, [_tx("tx-fork", "Bob", 5)])

        # Tampering an older block's persisted body leaves the tip hash intact;
        # appends only check the tip, while load_chain(validate=True) catches it.
        _exec_db(
            db_path,
            "UPDATE blocks SET body = ? WHERE idx = 1",
            (BlockCompressor().compress_block(b"123"),),
        )
        store.add_block(chain, [_tx("tx-3", "Alice", 10)])

    with ChainStore(db_path) as store:
        with pytest.raises(ValueError, match="failed validation"):
            store.load_chain(validate=True)


def test_incremental_pruning_across_restart_and_rollback_resets_cursor(tmp_path, monkeypatch):
    db_path = tmp_path / "prune_restart.db"
    with ChainStore(db_path) as store:
        chain = Chain()
        for i in range(5):
            store.add_block(chain, [_tx(f"tx-{i}", "Alice", 10)])

        pruner = BlockchainPruner(chain, max_full_blocks=2)
        real_prune = pruner.prune

        def _failing_once():
            chain.get_block(0).prune()
            raise RuntimeError("transient prune failure")

        monkeypatch.setattr(pruner, "prune", _failing_once)
        with pytest.raises(RuntimeError, match="transient prune failure"):
            store.prune_chain(pruner)
        assert pruner.next_unpruned_index == 0

        monkeypatch.setattr(pruner, "prune", real_prune)
        assert store.prune_chain(pruner) == [0, 1, 2, 3]
        assert pruner.next_unpruned_index == 4

    with ChainStore(db_path) as store:
        reloaded = store.load_chain()
        store.add_block(reloaded, [_tx("tx-5", "Alice", 10)])
        store.add_block(reloaded, [_tx("tx-6", "Alice", 10)])
        restarted_pruner = BlockchainPruner(reloaded, max_full_blocks=2)
        assert store.prune_chain(restarted_pruner) == [4, 5]
        assert restarted_pruner.next_unpruned_index == 6
        assert reloaded.validate_chain() is True


def test_bounded_applied_tx_ids_survive_pruning_compaction_and_reload(tmp_path):
    db_path = tmp_path / "bounded_replay.db"
    with ChainStore(db_path, max_cached_tx_ids=2) as store:
        chain = Chain()
        for i in range(6):
            store.add_block(chain, [_tx(f"tx-{i}", "Alice", 10)])

        assert chain.state.is_applied_tx_cache_bounded is True
        assert chain.state.cached_applied_tx_count == 2
        assert store.applied_tx_count() == 6

        pruner = BlockchainPruner(chain, max_full_blocks=2)
        assert store.prune_chain(pruner, compact=True) == [0, 1, 2, 3, 4]

        # Old tx-0 was evicted from RAM AND its block was pruned, yet replay is rejected.
        with pytest.raises(ValueError, match="already been applied"):
            store.add_block(chain, [_tx("tx-0", "Alice", 999)])

    with ChainStore(db_path, max_cached_tx_ids=2) as store:
        reloaded = store.load_chain()
        assert reloaded.state.cached_applied_tx_count == 2
        with pytest.raises(ValueError, match="already been applied"):
            store.add_block(reloaded, [_tx("tx-1", "Alice", 999)])

    # Accessing an overflowed bounded cache after store closes fails closed.
    with pytest.raises(RuntimeError, match="closed"):
        reloaded.state.is_applied("never-seen")

    # Explicit max_cached_tx_ids=None hydrates every ID for detached operation.
    with ChainStore(db_path, max_cached_tx_ids=2) as store:
        detached = store.load_chain(max_cached_tx_ids=None)
    assert detached.state.get_applied_tx_ids() == frozenset(f"tx-{i}" for i in range(6))


def test_convert_legacy_balance_edge_cases_and_precision_warning(caplog):
    assert _convert_legacy_balance("Alice", 42) == 42

    with pytest.raises(ValueError, match="non-string or empty account"):
        _convert_legacy_balance("", 10)
    with pytest.raises(ValueError, match="non-string or empty account"):
        _convert_legacy_balance(123, 10)
    with pytest.raises(ValueError, match="unsupported balance"):
        _convert_legacy_balance("Alice", "bad")
    with pytest.raises(ValueError, match="outside supported signed 64-bit"):
        _convert_legacy_balance("Alice", MAX_BALANCE + 1)

    with caplog.at_level("WARNING"):
        large_val = _convert_legacy_balance("Alice", float(2**54))
    assert large_val == 2**54
    assert "exceeded 2**53" in caplog.text


def test_chain_store_schema_healing_and_incompatible_column_checks(tmp_path):
    # v2 DB with incompatible REAL state_snapshot.balance fails closed.
    bad_v2 = tmp_path / "bad_v2.db"
    with contextlib.closing(sqlite3.connect(str(bad_v2))) as conn:
        conn.executescript(
            """
            CREATE TABLE blocks (
                idx INTEGER PRIMARY KEY, prev_hash TEXT NOT NULL, merkle_root TEXT,
                hash TEXT NOT NULL UNIQUE, timestamp REAL NOT NULL, body BLOB,
                is_pruned INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE state_snapshot (account TEXT PRIMARY KEY, balance REAL NOT NULL);
            CREATE TABLE applied_tx_ids (
                tx_id TEXT PRIMARY KEY,
                block_idx INTEGER NOT NULL REFERENCES blocks(idx)
            );
            CREATE TABLE zk_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                block_idx INTEGER NOT NULL REFERENCES blocks(idx),
                tx_id TEXT,
                payload_json TEXT NOT NULL
            );
            PRAGMA user_version = 2;
            """
        )
        conn.commit()
    with pytest.raises(ValueError, match="incompatible state_snapshot.balance column type"):
        ChainStore(bad_v2)

    # Partial v1 DB missing applied_tx_ids heals missing v1 table and upgrades to v2.
    partial_v1 = tmp_path / "partial_v1.db"
    with contextlib.closing(sqlite3.connect(str(partial_v1))) as conn:
        conn.executescript(
            """
            CREATE TABLE blocks (
                idx INTEGER PRIMARY KEY, prev_hash TEXT NOT NULL, merkle_root TEXT,
                hash TEXT NOT NULL UNIQUE, timestamp REAL NOT NULL, body BLOB,
                is_pruned INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE state_snapshot (
                account TEXT PRIMARY KEY,
                balance INTEGER NOT NULL CHECK (typeof(balance) = 'integer')
            );
            PRAGMA user_version = 1;
            """
        )
        conn.commit()
    with ChainStore(partial_v1) as store:
        assert store.get_pragma("user_version") == 2
        assert store.applied_tx_count() == 0

    # Partial v2 DB missing zk_attachments recreates it cleanly.
    partial_v2 = tmp_path / "partial_v2.db"
    with contextlib.closing(sqlite3.connect(str(partial_v2))) as conn:
        conn.executescript(
            """
            CREATE TABLE blocks (
                idx INTEGER PRIMARY KEY, prev_hash TEXT NOT NULL, merkle_root TEXT,
                hash TEXT NOT NULL UNIQUE, timestamp REAL NOT NULL, body BLOB,
                is_pruned INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE state_snapshot (
                account TEXT PRIMARY KEY,
                balance INTEGER NOT NULL CHECK (typeof(balance) = 'integer')
            );
            CREATE TABLE applied_tx_ids (
                tx_id TEXT PRIMARY KEY,
                block_idx INTEGER NOT NULL REFERENCES blocks(idx)
            );
            PRAGMA user_version = 2;
            """
        )
        conn.commit()
    with ChainStore(partial_v2) as store:
        assert store.verify_integrity().ok is True
    tables = {
        row[0] for row in _query_db(partial_v2, "SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "zk_attachments" in tables

    # Legacy v0 DB with blocks only (no state_snapshot) creates state_snapshot on migration.
    partial_v0 = tmp_path / "partial_v0.db"
    with contextlib.closing(sqlite3.connect(str(partial_v0))) as conn:
        conn.executescript(
            """
            CREATE TABLE blocks (
                idx INTEGER PRIMARY KEY, prev_hash TEXT NOT NULL, merkle_root TEXT,
                hash TEXT NOT NULL UNIQUE, timestamp REAL NOT NULL, body BLOB,
                is_pruned INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.commit()
    with ChainStore(partial_v0) as store:
        assert store.get_pragma("user_version") == 2


def test_chain_store_error_paths_and_input_validation(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="max_cached_tx_ids"):
        ChainStore(tmp_path / "bad_cache1.db", max_cached_tx_ids=-1)
    with pytest.raises(ValueError, match="max_cached_tx_ids"):
        ChainStore(tmp_path / "bad_cache2.db", max_cached_tx_ids=True)

    db_path = tmp_path / "errors.db"
    with ChainStore(db_path) as store:
        with pytest.raises(TypeError, match="Chain"):
            store.add_block("not-a-chain", [])
        with pytest.raises(TypeError, match="BlockchainPruner"):
            store.prune_chain("not-a-pruner")
        with pytest.raises(ValueError, match="max_cached_tx_ids"):
            store.load_chain(max_cached_tx_ids=-1)

        # Reject seeding empty store from a multi-block chain or already-overflowed state.
        unpersisted = Chain()
        unpersisted.add_block([_tx("tx-u", "Alice", 1)])
        with pytest.raises(ValueError, match="unpersisted chain"):
            store.add_block(unpersisted, [_tx("tx-u2", "Alice", 1)])

        overflow_state = StateDelta(max_applied_tx_ids=1, tx_lookup=lambda _tx_id: False)
        overflow_state.apply_transaction(_tx("tx-o1", "Alice", 1))
        overflow_state.apply_transaction(_tx("tx-o2", "Alice", 1))
        with pytest.raises(ValueError, match="already overflowed"):
            store.add_block(Chain(state=overflow_state), [_tx("tx-o3", "Alice", 1)])

        # Persist real blocks cleanly, then exercise append/prune/compact error paths.
        chain = Chain()
        store.add_block(chain, [_tx("tx-1", "Alice", 10)])
        store.add_block(chain, [_tx("tx-2", "Alice", 10)])

        with pytest.raises(TypeError, match="updated_accounts"):
            store.append_block(chain.get_block(0), [], "not-a-dict")
        with pytest.raises(ValueError, match="non-empty strings"):
            store.append_block(chain.get_block(0), [], {"": 10})

        pruner = BlockchainPruner(chain, max_full_blocks=2)
        with pytest.raises(TypeError, match="compact"):
            store.prune_chain(pruner, compact="yes")
        # Retention window covers all 3 blocks (0, 1, 2); wait max_full_blocks=3 keeps all.
        pruner_noop = BlockchainPruner(chain, max_full_blocks=3)
        assert store.prune_chain(pruner_noop) == []

        # compact() inside an open transaction raises RuntimeError cleanly.
        pruner_in_tx = BlockchainPruner(chain, max_full_blocks=1)

        def _compact_during_tx():
            store.compact()
            return []

        monkeypatch.setattr(pruner_in_tx, "prune", _compact_during_tx)
        with pytest.raises(RuntimeError, match="transaction is active"):
            store.prune_chain(pruner_in_tx)
        assert all(not b.is_pruned for b in chain)

        # Mid-prune chain mutation before or inside pruner.prune() rolls back safely.
        pruner_mut = BlockchainPruner(chain, max_full_blocks=2)
        orig_eligible = pruner_mut.eligible_block_indices

        def _mutating_eligible():
            res = orig_eligible()
            chain.add_block([_tx("tx-race", "Bob", 5)])
            return res

        monkeypatch.setattr(pruner_mut, "eligible_block_indices", _mutating_eligible)
        with pytest.raises(RuntimeError, match="chain changed while pruning"):
            store.prune_chain(pruner_mut)
        assert all(not b.is_pruned for b in chain)

        chain_small = Chain()
        db_mismatch = tmp_path / "prune_mismatch.db"
        with ChainStore(db_mismatch) as store2:
            store2.add_block(chain_small, [_tx("tx-a", "Alice", 5)])
            p_mismatch = BlockchainPruner(chain_small, max_full_blocks=1)
            monkeypatch.setattr(p_mismatch, "prune", lambda: [999])
            with pytest.raises(RuntimeError, match="chain changed while pruning"):
                store2.prune_chain(p_mismatch)
            assert not chain_small.get_block(0).is_pruned

            # Missing persisted block row raises KeyError and rolls back cleanly.
            monkeypatch.undo()
            _exec_db(db_mismatch, "DELETE FROM blocks WHERE idx = 0")
            with pytest.raises(KeyError, match="no persisted block with index 0"):
                store2.prune_chain(p_mismatch)
            assert not chain_small.get_block(0).is_pruned


def test_load_chain_zero_cache_zk_mismatch_and_int64_boundaries(tmp_path):
    db_path = tmp_path / "zero_cache_and_bounds.db"
    tx_max = _tx("tx-max", "MaxAcct", MAX_BALANCE)
    tx_min = _tx("tx-min", "MinAcct", MIN_BALANCE)
    commitment, blinding = PedersenCommitment().commit(42)
    proof = generate_proof(message=42, blinding=blinding, commitment=commitment)
    att = ZKAttachment(commitment=commitment, proof=proof, tx_id="tx-max")

    with ChainStore(db_path) as store:
        chain = Chain()
        store.add_block(chain, [tx_max], zk_attachments=[att])
        store.add_block(chain, [tx_min])

        # max_cached_tx_ids=0 keeps zero tx IDs in RAM and checks SQLite directly.
        zero_cached = store.load_chain(max_cached_tx_ids=0)
        assert zero_cached.state.cached_applied_tx_count == 0
        assert zero_cached.state.is_applied("tx-max") is True
        assert zero_cached.state.is_applied("tx-unseen") is False
        assert zero_cached.state.get_current_state() == {
            "MaxAcct": MAX_BALANCE,
            "MinAcct": MIN_BALANCE,
        }

    # Corrupted zk_attachments.tx_id mismatch vs payload_json fails closed.
    _exec_db(db_path, "UPDATE zk_attachments SET tx_id = 'mismatched-id'")
    with ChainStore(db_path) as store:
        with pytest.raises(ValueError, match="does not match payload_json"):
            store.load_chain()


def test_schema_indexes_created_and_zk_index_skipped_when_table_absent(tmp_path, monkeypatch):
    fresh_db = tmp_path / "fresh_indexes.db"
    with ChainStore(fresh_db) as store:
        report = store.verify_integrity()
        assert isinstance(report, IntegrityReport)
        assert report.ok is True
        assert report.quick_check_ok is True
        assert report.quick_check_messages == ("ok",)
        assert report.chain_valid is True
        assert report.block_count == 0
        assert report.schema_version == 2
        assert "idx_applied_tx_ids_block_idx" in report.indexes
        assert "idx_zk_attachments_block_idx" in report.indexes

        serialized = report.to_dict()
        assert json.loads(json.dumps(serialized))["indexes"] == list(report.indexes)

    # Pre-existing v2 DB without indexes heals both indexes on open without altering user_version.
    v2_no_idx = tmp_path / "v2_no_indexes.db"
    with contextlib.closing(sqlite3.connect(str(v2_no_idx))) as conn:
        conn.executescript(
            """
            CREATE TABLE blocks (
                idx INTEGER PRIMARY KEY, prev_hash TEXT NOT NULL, merkle_root TEXT,
                hash TEXT NOT NULL UNIQUE, timestamp REAL NOT NULL, body BLOB,
                is_pruned INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE state_snapshot (
                account TEXT PRIMARY KEY,
                balance INTEGER NOT NULL CHECK (typeof(balance) = 'integer')
            );
            CREATE TABLE applied_tx_ids (
                tx_id TEXT PRIMARY KEY,
                block_idx INTEGER NOT NULL REFERENCES blocks(idx)
            );
            CREATE TABLE zk_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                block_idx INTEGER NOT NULL REFERENCES blocks(idx),
                tx_id TEXT,
                payload_json TEXT NOT NULL
            );
            PRAGMA user_version = 2;
            """
        )
        conn.commit()
    with ChainStore(v2_no_idx) as store:
        assert store.get_pragma("user_version") == 2
        assert set(store.verify_integrity().indexes) >= {
            "idx_applied_tx_ids_block_idx",
            "idx_zk_attachments_block_idx",
        }

    # Pre-existing v1 DB missing idx_applied_tx_ids_block_idx gains both indexes on v1->v2.
    v1_no_idx = tmp_path / "v1_no_indexes.db"
    with contextlib.closing(sqlite3.connect(str(v1_no_idx))) as conn:
        conn.executescript(
            """
            CREATE TABLE blocks (
                idx INTEGER PRIMARY KEY, prev_hash TEXT NOT NULL, merkle_root TEXT,
                hash TEXT NOT NULL UNIQUE, timestamp REAL NOT NULL, body BLOB,
                is_pruned INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE state_snapshot (
                account TEXT PRIMARY KEY,
                balance INTEGER NOT NULL CHECK (typeof(balance) = 'integer')
            );
            CREATE TABLE applied_tx_ids (
                tx_id TEXT PRIMARY KEY,
                block_idx INTEGER NOT NULL REFERENCES blocks(idx)
            );
            PRAGMA user_version = 1;
            """
        )
        conn.commit()
    with ChainStore(v1_no_idx) as store:
        assert store.get_pragma("user_version") == 2
        assert set(store.verify_integrity().indexes) >= {
            "idx_applied_tx_ids_block_idx",
            "idx_zk_attachments_block_idx",
        }

    # Without zk_attachments in a v1 schema, _ensure_indexes skips idx_zk_attachments_block_idx.
    v1_only = tmp_path / "partial_v1_no_zk.db"
    with contextlib.closing(sqlite3.connect(str(v1_only))) as conn:
        conn.executescript(
            """
            CREATE TABLE blocks (
                idx INTEGER PRIMARY KEY, prev_hash TEXT NOT NULL, merkle_root TEXT,
                hash TEXT NOT NULL UNIQUE, timestamp REAL NOT NULL, body BLOB,
                is_pruned INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE state_snapshot (
                account TEXT PRIMARY KEY,
                balance INTEGER NOT NULL CHECK (typeof(balance) = 'integer')
            );
            CREATE TABLE applied_tx_ids (
                tx_id TEXT PRIMARY KEY,
                block_idx INTEGER NOT NULL REFERENCES blocks(idx)
            );
            PRAGMA user_version = 1;
            """
        )
        conn.commit()
    monkeypatch.setattr(ChainStore, "_migrate_v1_to_v2", lambda self: None)
    with ChainStore(v1_only) as store:
        assert store.get_pragma("user_version") == 1
    idx_names = {
        row[0] for row in _query_db(v1_only, "SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert "idx_applied_tx_ids_block_idx" in idx_names
    assert "idx_zk_attachments_block_idx" not in idx_names


def test_verify_integrity_and_startup_check_behavior(tmp_path):
    db_path = tmp_path / "integrity.db"
    commitment, blinding = PedersenCommitment().commit(hash_to_scalar(_tx("tx-zk", "Alice", 5)))
    valid_proof = generate_proof(
        message=hash_to_scalar(_tx("tx-zk", "Alice", 5)),
        blinding=blinding,
        commitment=commitment,
    )
    att = ZKAttachment(commitment=commitment, proof=valid_proof, tx_id="tx-zk")

    with ChainStore(db_path) as store:
        chain = Chain()
        store.add_block(chain, [_tx("tx-zk", "Alice", 5)], zk_attachments=[att])
        store.add_block(chain, [_tx("tx-2", "Alice", 10)])
        store.prune_chain(BlockchainPruner(chain, max_full_blocks=1))

        report = store.verify_integrity(check_bodies=True, verify_zk=True)
        assert report.ok is True
        assert report.quick_check_ok is True
        assert report.chain_valid is True
        assert report.block_count == 3
        assert report.errors == ()

    # Default open does NOT run full body validation automatically, even if a body is tampered.
    _exec_db(db_path, "UPDATE blocks SET body = X'0000' WHERE idx = 2")
    with ChainStore(db_path) as store:
        report_skip_bodies = store.verify_integrity(check_bodies=False)
        assert report_skip_bodies.ok is True

        report_with_bodies = store.verify_integrity(check_bodies=True)
        assert report_with_bodies.ok is False
        assert report_with_bodies.quick_check_ok is True
        assert report_with_bodies.chain_valid is False
        assert any("chain validation failed" in err for err in report_with_bodies.errors)

        with pytest.raises(ValueError, match="integrity verification failed"):
            store.verify_integrity(check_bodies=True, raise_on_error=True)

    # Opt-in startup check rejects the tampered DB and closes cleanly.
    with pytest.raises(ValueError, match="integrity verification failed"):
        ChainStore(db_path, check_integrity_on_open=True)

    # Invalid check_integrity_on_open type fails fast.
    with pytest.raises(TypeError, match="check_integrity_on_open"):
        ChainStore(tmp_path / "bad_flag.db", check_integrity_on_open="yes")

    # ZK verification failure surfaces cleanly when verify_zk=True.
    zk_bad_db = tmp_path / "bad_zk_integrity.db"
    with ChainStore(zk_bad_db) as store:
        c2 = Chain()
        unproven = ZKAttachment(commitment=commitment, proof=None, tx_id="tx-zk2")
        store.add_block(c2, [_tx("tx-zk2", "Alice", 5)], zk_attachments=[unproven])
        assert store.verify_integrity(verify_zk=False).ok is True
        bad_zk_report = store.verify_integrity(verify_zk=True)
        assert bad_zk_report.ok is False
        assert bad_zk_report.chain_valid is False
        assert any("ZK attachments failed" in err for err in bad_zk_report.errors)


def test_verify_integrity_detects_physical_sqlite_page_corruption(tmp_path):
    db_path = tmp_path / "physical_corrupt.db"
    with ChainStore(db_path) as store:
        chain = Chain()
        for i in range(15):
            batch = [
                {
                    "tx_id": f"phys-{i}-{j}",
                    "account": f"Acct{j % 4}",
                    "amount": 1,
                    "memo": hashlib.sha256(f"{i}-{j}".encode()).hexdigest(),
                }
                for j in range(30)
            ]
            store.add_block(chain, batch)
        store.compact(full=True)

    raw = bytearray(db_path.read_bytes())
    page_size = int.from_bytes(raw[16:18], "big")
    if page_size == 1:
        page_size = 65536
    assert len(raw) >= page_size * 4
    # Corrupt payload bytes in a late data page (keeping page 1 schema/header readable).
    corrupt_offset = len(raw) - (page_size // 2)
    raw[corrupt_offset : corrupt_offset + 256] = b"\xff" * 256
    db_path.write_bytes(bytes(raw))

    with ChainStore(db_path) as store:
        report = store.verify_integrity(check_bodies=True)
        assert report.ok is False
        assert report.errors
        with pytest.raises(ValueError, match="integrity verification failed"):
            store.verify_integrity(check_bodies=True, raise_on_error=True)

    with pytest.raises(ValueError, match="integrity verification failed"):
        ChainStore(db_path, check_integrity_on_open=True)


def test_get_pragma_and_verify_integrity_input_validation(tmp_path):
    db_path = tmp_path / "pragma_api.db"
    store = ChainStore(db_path)
    try:
        pragmas = store.get_pragmas()
        assert pragmas["journal_mode"].lower() == "wal"
        assert pragmas["user_version"] == 2
        assert pragmas["foreign_keys"] == 1
        assert store.get_pragma(" PAGE_SIZE ") > 0

        with pytest.raises(TypeError, match="pragma name"):
            store.get_pragma(123)
        with pytest.raises(ValueError, match="unsupported read-only pragma"):
            store.get_pragma("writable_schema")
        with pytest.raises(TypeError, match="check_bodies"):
            store.verify_integrity(check_bodies="yes")
        with pytest.raises(TypeError, match="verify_zk"):
            store.verify_integrity(verify_zk="yes")
        with pytest.raises(TypeError, match="raise_on_error"):
            store.verify_integrity(raise_on_error="yes")
    finally:
        store.close()

    with pytest.raises(RuntimeError, match="closed"):
        store.get_pragma("user_version")
    with pytest.raises(RuntimeError, match="closed"):
        store.get_pragmas()
    with pytest.raises(RuntimeError, match="closed"):
        store.verify_integrity()
