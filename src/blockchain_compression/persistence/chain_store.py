"""SQLite-backed persistence for a :class:`~blockchain_compression.chain.chain.Chain`.

Three tables back a round-trippable chain: ``blocks`` (headers + optional body),
``state_snapshot`` (current account balances), and ``applied_tx_ids`` (replay-protection
membership). The latter two aren't a cache - once a block is pruned, they're the *only*
surviving record of what its transactions did, since the raw transaction data is gone by
design. ``load_chain`` hydrates :class:`StateDelta` from them directly, never by replaying
block bodies (impossible for pruned blocks, redundant for present ones).
"""

import sqlite3
from pathlib import Path

from blockchain_compression.chain.block import Block
from blockchain_compression.chain.chain import Chain
from blockchain_compression.compression.state_delta import StateDelta

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS blocks (
    idx         INTEGER PRIMARY KEY,
    prev_hash   TEXT NOT NULL,
    merkle_root TEXT,
    hash        TEXT NOT NULL UNIQUE,
    timestamp   REAL NOT NULL,
    body        BLOB,
    is_pruned   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS state_snapshot (
    account TEXT PRIMARY KEY,
    balance REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS applied_tx_ids (
    tx_id     TEXT PRIMARY KEY,
    block_idx INTEGER NOT NULL REFERENCES blocks(idx)
);
"""


class ChainStore:
    """Persists and reloads a :class:`Chain` to/from a SQLite database file."""

    def __init__(self, path: str | Path):
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        with self._conn:
            self._conn.executescript(_SCHEMA_SQL)

    def __enter__(self) -> "ChainStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def append_block(self, block: Block, applied_tx_ids, updated_accounts: dict) -> None:
        """Persist ``block`` plus the state changes it caused, as one transaction.

        ``sqlite3`` auto-begins a transaction before DML but never auto-commits;
        wrapping the whole write in ``with self._conn:`` is what makes the block
        row, the balance upserts, and the tx-id inserts commit - or roll back -
        together, so a crash mid-write can't leave a block recorded with no
        matching state update.
        """
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO blocks (idx, prev_hash, merkle_root, hash, timestamp, body, is_pruned)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    block.index,
                    block.prev_hash,
                    block.merkle_root,
                    block.hash,
                    block.timestamp,
                    block.compressed_body,
                    int(block.is_pruned),
                ),
            )
            self._conn.executemany(
                """
                INSERT INTO state_snapshot (account, balance) VALUES (?, ?)
                ON CONFLICT(account) DO UPDATE SET balance = excluded.balance
                """,
                list(updated_accounts.items()),
            )
            self._conn.executemany(
                "INSERT OR IGNORE INTO applied_tx_ids (tx_id, block_idx) VALUES (?, ?)",
                [(tx_id, block.index) for tx_id in applied_tx_ids],
            )

    def mark_pruned(self, index: int) -> None:
        """Drop a persisted block's body, mirroring an in-memory ``Block.prune()``."""
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE blocks SET body = NULL, is_pruned = 1 WHERE idx = ?", (index,)
            )
        if cursor.rowcount == 0:
            raise KeyError(f"no persisted block with index {index}")

    def load_chain(self) -> Chain:
        """Reconstruct a full :class:`Chain`, headers and all, from storage.

        Rows are ordered explicitly by ``idx`` - row order on disk is not
        otherwise guaranteed. Each block is rebuilt via ``Block.from_persisted``
        (a trusted-reload path that never recomputes hashes, since a pruned
        row has no transactions left to recompute one from).
        """
        block_rows = self._conn.execute(
            "SELECT idx, prev_hash, merkle_root, hash, timestamp, body, is_pruned "
            "FROM blocks ORDER BY idx"
        ).fetchall()
        if not block_rows:
            return Chain()

        blocks = [
            Block.from_persisted(
                index=row["idx"],
                prev_hash=row["prev_hash"],
                merkle_root=row["merkle_root"],
                hash=row["hash"],
                timestamp=row["timestamp"],
                compressed_body=row["body"],
                is_pruned=bool(row["is_pruned"]),
            )
            for row in block_rows
        ]

        state_rows = self._conn.execute("SELECT account, balance FROM state_snapshot").fetchall()
        tx_id_rows = self._conn.execute("SELECT tx_id FROM applied_tx_ids").fetchall()
        state = StateDelta.from_snapshot(
            {row["account"]: row["balance"] for row in state_rows},
            (row["tx_id"] for row in tx_id_rows),
        )

        chain = Chain(state=state)
        chain.blocks = blocks
        return chain
