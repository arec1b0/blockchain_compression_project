# test_chain_store.py

import contextlib
import hashlib
import sqlite3
import threading

import pytest

from blockchain_compression.chain import Block, Chain
from blockchain_compression.compression import BlockCompressor, StateDelta, extract_block_delta
from blockchain_compression.compression.state_delta import MAX_BALANCE, MIN_BALANCE
from blockchain_compression.observability import MetricsRegistry
from blockchain_compression.persistence import ChainStore
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
        assert store._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert store._conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert store._conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert store._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    db_full = tmp_path / "wal_full.db"
    with ChainStore(db_full, busy_timeout_ms=1500, synchronous="FULL") as store:
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1500
        assert store._conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL

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
        freelist_after_prune = store._conn.execute("PRAGMA freelist_count").fetchone()[0]
        assert freelist_after_prune > 5

        store.compact(pages=5)
        freelist_after_partial = store._conn.execute("PRAGMA freelist_count").fetchone()[0]
        assert freelist_after_partial == freelist_after_prune - 5

        store.compact()
        freelist_after_compact = store._conn.execute("PRAGMA freelist_count").fetchone()[0]
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
        store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        size_before_second_prune = db_path.stat().st_size
        pruned_more = store.prune_chain(pruner, compact=True)
        assert pruned_more == [24, 25, 26, 27, 28]
        size_after_second_compact = db_path.stat().st_size
        assert store._conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
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
        assert store._conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
        store.compact(full=True)
        assert store._conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
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
