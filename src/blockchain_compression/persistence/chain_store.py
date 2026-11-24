"""SQLite-backed persistence for a :class:`~blockchain_compression.chain.chain.Chain`.

Three tables back a round-trippable chain: ``blocks`` (headers + optional body),
``state_snapshot`` (current account balances in exact integer base units), and
``applied_tx_ids`` (replay-protection membership), plus optional
``zk_attachments`` (schema version 2). The snapshot and applied-ID tables
aren't a cache - once a block is pruned, they're the *only* surviving record of
what its transactions did, since the raw transaction data is gone by design.
``load_chain`` hydrates :class:`StateDelta` from them directly, never by
replaying block bodies (impossible for pruned blocks, redundant for present
ones).

Operational & durability defaults:
- Opens connections with ``PRAGMA journal_mode = WAL``,
  ``PRAGMA busy_timeout = 5000``, ``PRAGMA synchronous = NORMAL`` (configurable),
  and ``PRAGMA auto_vacuum = INCREMENTAL``.
- WAL creates ``-wal`` and ``-shm`` sidecar files alongside the database and
  allows concurrent readers to proceed against a point-in-time snapshot without
  blocking writers; read paths pin a snapshot with ``BEGIN DEFERRED`` and never
  upgrade to a write lock.
- Write operations (block inserts, pruning, migrations) use explicit
  ``BEGIN IMMEDIATE`` transactions after CPU-intensive block preparation has
  already finished, avoiding mid-transaction write-lock upgrades and retry storms
  under contention.
- ``synchronous = NORMAL`` avoids an ``fsync`` on every WAL commit (much higher
  append throughput, crash-safe on OS/process crashes, though the most recent
  un-checkpointed WAL transaction could roll back on sudden power loss; pass
  ``synchronous="FULL"`` when strict per-commit hardware durability is required).
- Logical pruning (``prune_chain`` / ``mark_pruned``) moves freed pages to
  SQLite's freelist without immediately shrinking the file. Call :meth:`compact`
  (or pass ``compact=True`` to :meth:`prune_chain`) to run
  ``PRAGMA incremental_vacuum`` and checkpoint the WAL. Note that SQLite only
  applies ``auto_vacuum = INCREMENTAL`` automatically to newly created
  databases; pre-existing databases with ``auto_vacuum = NONE`` require a
  one-time ``compact(full=True)`` (``VACUUM``) before incremental compaction can
  reclaim pages.
- Schema indexes: creates ``idx_applied_tx_ids_block_idx`` on
  ``applied_tx_ids(block_idx)`` (and ``idx_zk_attachments_block_idx`` on
  ``zk_attachments(block_idx)`` when the v2 table exists) to avoid full table
  scans on foreign-key checks and block-level lookups, at the cost of a small
  additional B-tree update per inserted transaction ID or attachment.
- Startup & diagnostics: by default ``ChainStore`` skips expensive full
  ``PRAGMA quick_check`` + body validation on open (``check_integrity_on_open=False``).
  Call :meth:`verify_integrity` explicitly after backup restores, crash
  recovery, or before critical migrations/exports, or pass
  ``check_integrity_on_open=True`` when strict open-time verification is needed.
"""

import json
import logging
import math
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from blockchain_compression.canonical import canonical_json
from blockchain_compression.chain.block import Block
from blockchain_compression.chain.chain import Chain
from blockchain_compression.compression.block_compressor import BlockCompressor
from blockchain_compression.compression.state_delta import StateDelta
from blockchain_compression.observability.metrics import MetricsRegistry
from blockchain_compression.pruning.blockchain_pruner import BlockchainPruner
from blockchain_compression.zk_proof.attachment import ZKAttachment

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
_MAX_EXACT_FLOAT_INT = 2**53

DEFAULT_BUSY_TIMEOUT_MS = 5000
DEFAULT_SYNCHRONOUS = "NORMAL"
DEFAULT_MAX_CACHED_TX_IDS = 10_000
_VALID_SYNCHRONOUS_MODES = frozenset({"OFF", "NORMAL", "FULL", "EXTRA"})
_UNSET = object()

_CREATE_BLOCKS_SQL = """
CREATE TABLE IF NOT EXISTS blocks (
    idx         INTEGER PRIMARY KEY,
    prev_hash   TEXT NOT NULL,
    merkle_root TEXT,
    hash        TEXT NOT NULL UNIQUE,
    timestamp   REAL NOT NULL,
    body        BLOB,
    is_pruned   INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_STATE_SNAPSHOT_SQL = """
CREATE TABLE IF NOT EXISTS state_snapshot (
    account TEXT PRIMARY KEY,
    balance INTEGER NOT NULL CHECK (typeof(balance) = 'integer')
)
"""

_CREATE_APPLIED_TX_IDS_SQL = """
CREATE TABLE IF NOT EXISTS applied_tx_ids (
    tx_id     TEXT PRIMARY KEY,
    block_idx INTEGER NOT NULL REFERENCES blocks(idx)
)
"""

_CREATE_ZK_ATTACHMENTS_SQL = """
CREATE TABLE IF NOT EXISTS zk_attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    block_idx    INTEGER NOT NULL REFERENCES blocks(idx),
    tx_id        TEXT,
    payload_json TEXT NOT NULL
)
"""

_CREATE_APPLIED_TX_IDS_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_applied_tx_ids_block_idx ON applied_tx_ids(block_idx)"
)
_CREATE_ZK_ATTACHMENTS_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_zk_attachments_block_idx ON zk_attachments(block_idx)"
)

_READABLE_PRAGMAS: dict[str, str] = {
    "auto_vacuum": "PRAGMA auto_vacuum",
    "busy_timeout": "PRAGMA busy_timeout",
    "foreign_keys": "PRAGMA foreign_keys",
    "freelist_count": "PRAGMA freelist_count",
    "journal_mode": "PRAGMA journal_mode",
    "page_count": "PRAGMA page_count",
    "page_size": "PRAGMA page_size",
    "synchronous": "PRAGMA synchronous",
    "user_version": "PRAGMA user_version",
}


@dataclass(frozen=True)
class IntegrityReport:
    """Structured diagnostic summary returned by :meth:`ChainStore.verify_integrity`."""

    ok: bool
    quick_check_ok: bool
    quick_check_messages: tuple[str, ...]
    chain_valid: bool
    block_count: int
    schema_version: int
    indexes: tuple[str, ...]
    pragmas: Mapping[str, int | str]
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Convert this diagnostic report into a JSON-serializable dictionary."""
        return {
            "ok": self.ok,
            "quick_check_ok": self.quick_check_ok,
            "quick_check_messages": list(self.quick_check_messages),
            "chain_valid": self.chain_valid,
            "block_count": self.block_count,
            "schema_version": self.schema_version,
            "indexes": list(self.indexes),
            "pragmas": dict(self.pragmas),
            "errors": list(self.errors),
        }


def _convert_legacy_balance(account: object, balance: object) -> int:
    """Convert a legacy v0 ``REAL`` balance value into an exact 64-bit integer."""
    if not isinstance(account, str) or not account:
        raise ValueError("cannot migrate state_snapshot row with non-string or empty account")
    if isinstance(balance, int) and not isinstance(balance, bool):
        StateDelta.validate_balance(balance, field=f"balance for account {account!r}")
        return balance
    if isinstance(balance, float):
        if not math.isfinite(balance) or not balance.is_integer():
            raise ValueError(
                f"cannot migrate non-integer balance {balance!r} for account {account!r} "
                "to exact integer base units"
            )
        int_val = int(balance)
        StateDelta.validate_balance(int_val, field=f"balance for account {account!r}")
        if abs(int_val) > _MAX_EXACT_FLOAT_INT:
            logger.warning(
                "Account %r migrated balance %d exceeded 2**53 in legacy REAL storage; "
                "precision may already have been rounded prior to migration.",
                account,
                int_val,
            )
        return int_val
    raise ValueError(
        f"cannot migrate unsupported balance {balance!r} ({type(balance).__name__}) "
        f"for account {account!r}"
    )


class ChainStore:
    """Persists and reloads a :class:`Chain` to/from a SQLite database file."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        synchronous: str = DEFAULT_SYNCHRONOUS,
        max_cached_tx_ids: int | None = DEFAULT_MAX_CACHED_TX_IDS,
        check_integrity_on_open: bool = False,
    ):
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or busy_timeout_ms < 0
        ):
            raise ValueError("busy_timeout_ms must be a non-negative integer")
        if not isinstance(synchronous, str):
            raise TypeError(f"synchronous must be a string, got {type(synchronous).__name__}")
        sync_mode = synchronous.strip().upper()
        if sync_mode not in _VALID_SYNCHRONOUS_MODES:
            allowed = sorted(_VALID_SYNCHRONOUS_MODES)
            raise ValueError(f"synchronous must be one of {allowed}, got {synchronous!r}")
        if max_cached_tx_ids is not None and (
            isinstance(max_cached_tx_ids, bool)
            or not isinstance(max_cached_tx_ids, int)
            or max_cached_tx_ids < 0
        ):
            raise ValueError("max_cached_tx_ids must be a non-negative integer or None")
        if not isinstance(check_integrity_on_open, bool):
            raise TypeError(
                "check_integrity_on_open must be a bool, "
                f"got {type(check_integrity_on_open).__name__}"
            )

        self._max_cached_tx_ids: int | None = max_cached_tx_ids
        self._closed = False
        self._conn = sqlite3.connect(
            str(path),
            timeout=busy_timeout_ms / 1000.0,
            isolation_level=None,
        )
        try:
            self._conn.row_factory = sqlite3.Row
            self._conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
            self._conn.execute("PRAGMA foreign_keys = ON")
            # Must precede journal_mode = WAL and initial DDL so new databases
            # record auto_vacuum = INCREMENTAL on page 1.
            self._conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
            self._conn.execute("PRAGMA journal_mode = WAL").fetchall()
            self._conn.execute(f"PRAGMA synchronous = {sync_mode}")
            self._ensure_schema()
            if check_integrity_on_open:
                self.verify_integrity(check_bodies=True, raise_on_error=True)
        except BaseException:
            self._closed = True
            self._conn.close()
            raise

    @contextmanager
    def _write_tx(self):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if not self._closed and self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    @contextmanager
    def _read_tx(self):
        self._conn.execute("BEGIN DEFERRED")
        try:
            yield
        finally:
            if not self._closed and self._conn.in_transaction:
                self._conn.execute("ROLLBACK")

    def _create_v1_tables(self) -> None:
        self._conn.execute(_CREATE_BLOCKS_SQL)
        self._conn.execute(_CREATE_STATE_SNAPSHOT_SQL)
        self._conn.execute(_CREATE_APPLIED_TX_IDS_SQL)
        self._conn.execute(_CREATE_APPLIED_TX_IDS_INDEX_SQL)

    def _create_v2_tables(self) -> None:
        self._create_v1_tables()
        self._conn.execute(_CREATE_ZK_ATTACHMENTS_SQL)
        self._conn.execute(_CREATE_ZK_ATTACHMENTS_INDEX_SQL)

    def _ensure_indexes(self, existing_tables: set[str] | None = None) -> None:
        """Ensure block_idx lookup indexes exist for every present target table."""
        tables = existing_tables
        if tables is None:
            tables = {
                row["name"]
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name IN ('applied_tx_ids', 'zk_attachments')"
                ).fetchall()
            }
        required: list[tuple[str, str]] = []
        if "applied_tx_ids" in tables:
            required.append(("idx_applied_tx_ids_block_idx", _CREATE_APPLIED_TX_IDS_INDEX_SQL))
        # zk_attachments was added in schema version 2; skip creating its index on
        # partial or pre-v2 schemas where the table does not exist yet.
        if "zk_attachments" in tables:
            required.append(("idx_zk_attachments_block_idx", _CREATE_ZK_ATTACHMENTS_INDEX_SQL))
        if not required:
            return
        existing_indexes = {
            row["name"]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND name IN ('idx_applied_tx_ids_block_idx', 'idx_zk_attachments_block_idx')"
            ).fetchall()
        }
        missing_sql = [sql for idx_name, sql in required if idx_name not in existing_indexes]
        if missing_sql:
            with self._write_tx():
                for sql in missing_sql:
                    self._conn.execute(sql)

    def _validate_integer_snapshot_column(self) -> None:
        cols = {
            row["name"]: row["type"].upper()
            for row in self._conn.execute("PRAGMA table_info(state_snapshot)").fetchall()
        }
        if cols.get("balance") != "INTEGER":
            raise ValueError(
                "incompatible state_snapshot.balance column type "
                f"{cols.get('balance')!r} for schema version {SCHEMA_VERSION}"
            )

    def _ensure_schema(self) -> None:
        user_version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if user_version < 0 or user_version > SCHEMA_VERSION:
            raise ValueError(
                f"unsupported ChainStore schema version {user_version}; "
                f"supported version is {SCHEMA_VERSION}"
            )

        existing_tables = {
            row["name"]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('blocks', 'state_snapshot', 'applied_tx_ids', 'zk_attachments')"
            ).fetchall()
        }

        if user_version == SCHEMA_VERSION and len(existing_tables) == 4:
            self._validate_integer_snapshot_column()
            self._ensure_indexes(existing_tables)
            return

        if user_version == 0:
            if not existing_tables:
                with self._write_tx():
                    tables_now = {
                        row["name"]
                        for row in self._conn.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table' "
                            "AND name IN ("
                            "'blocks', 'state_snapshot', 'applied_tx_ids', 'zk_attachments'"
                            ")"
                        ).fetchall()
                    }
                    if not tables_now:
                        self._create_v2_tables()
                        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self._ensure_indexes()
            else:
                self._migrate_v0_to_v1(existing_tables)
                self._migrate_v1_to_v2()
            return

        if user_version == 1:
            if len(existing_tables & {"blocks", "state_snapshot", "applied_tx_ids"}) < 3:
                with self._write_tx():
                    self._create_v1_tables()
            self._validate_integer_snapshot_column()
            self._migrate_v1_to_v2()
            self._ensure_indexes()
            return

        # user_version == SCHEMA_VERSION (2) with missing table(s)
        with self._write_tx():
            self._create_v2_tables()
        self._validate_integer_snapshot_column()
        self._ensure_indexes()

    def _migrate_v1_to_v2(self) -> None:
        """Upgrade a v1 database to v2 by adding the optional ``zk_attachments`` table."""
        with self._write_tx():
            self._conn.execute(_CREATE_ZK_ATTACHMENTS_SQL)
            self._conn.execute(_CREATE_ZK_ATTACHMENTS_INDEX_SQL)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _migrate_v0_to_v1(self, existing_tables: set[str]) -> None:
        """Migrate an unversioned legacy database (``REAL`` balances) to v1."""
        with self._write_tx():
            migrated_rows: list[tuple[str, int]] | None = None
            if "state_snapshot" in existing_tables:
                cols = {
                    row["name"]: row["type"].upper()
                    for row in self._conn.execute("PRAGMA table_info(state_snapshot)").fetchall()
                }
                if cols.get("balance") != "INTEGER":
                    rows = self._conn.execute(
                        "SELECT account, balance FROM state_snapshot"
                    ).fetchall()
                    migrated_rows = [
                        (row["account"], _convert_legacy_balance(row["account"], row["balance"]))
                        for row in rows
                    ]

            self._conn.execute(_CREATE_BLOCKS_SQL)
            self._conn.execute(_CREATE_APPLIED_TX_IDS_SQL)
            self._conn.execute(_CREATE_APPLIED_TX_IDS_INDEX_SQL)
            if "state_snapshot" not in existing_tables:
                self._conn.execute(_CREATE_STATE_SNAPSHOT_SQL)
            elif migrated_rows is not None:
                self._conn.execute(
                    """
                    CREATE TABLE state_snapshot_v1 (
                        account TEXT PRIMARY KEY,
                        balance INTEGER NOT NULL CHECK (typeof(balance) = 'integer')
                    )
                    """
                )
                self._conn.executemany(
                    "INSERT INTO state_snapshot_v1 (account, balance) VALUES (?, ?)",
                    migrated_rows,
                )
                self._conn.execute("DROP TABLE state_snapshot")
                self._conn.execute("ALTER TABLE state_snapshot_v1 RENAME TO state_snapshot")
            self._conn.execute("PRAGMA user_version = 1")

    def __enter__(self) -> "ChainStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._conn.close()

    def has_applied_tx(self, tx_id: str) -> bool:
        """Check SQLite's indexed ``applied_tx_ids`` table for replay protection.

        Fails closed with ``RuntimeError`` if this store has already been closed.
        """
        if self._closed:
            raise RuntimeError("ChainStore backing this bounded replay cache is closed")
        row = self._conn.execute(
            "SELECT 1 FROM applied_tx_ids WHERE tx_id = ? LIMIT 1",
            (tx_id,),
        ).fetchone()
        return row is not None

    def applied_tx_count(self) -> int:
        """Return total number of applied transaction IDs recorded in SQLite."""
        return int(self._conn.execute("SELECT COUNT(*) FROM applied_tx_ids").fetchone()[0])

    def get_pragma(self, name: str) -> int | str:
        """Read an allowlisted read-only SQLite PRAGMA without exposing the raw connection."""
        if self._closed:
            raise RuntimeError("ChainStore is closed")
        if not isinstance(name, str):
            raise TypeError(f"pragma name must be a str, got {type(name).__name__}")
        key = name.strip().lower()
        sql = _READABLE_PRAGMAS.get(key)
        if sql is None:
            allowed = sorted(_READABLE_PRAGMAS)
            raise ValueError(f"unsupported read-only pragma {name!r}; allowed: {allowed}")
        row = self._conn.execute(sql).fetchone()
        if row is None:
            raise RuntimeError(f"PRAGMA {key} returned no result")
        value = row[0]
        if not isinstance(value, (int, str)):
            return str(value)
        return value

    def get_pragmas(self) -> dict[str, int | str]:
        """Return a snapshot dictionary of all allowlisted SQLite PRAGMA settings."""
        if self._closed:
            raise RuntimeError("ChainStore is closed")
        return {name: self.get_pragma(name) for name in sorted(_READABLE_PRAGMAS)}

    def verify_integrity(
        self,
        *,
        check_bodies: bool = True,
        verify_zk: bool = False,
        raise_on_error: bool = False,
    ) -> IntegrityReport:
        """Run SQLite ``PRAGMA quick_check`` and full chain validation.

        Recommended after backup restores, crash recovery, or before critical
        migrations/exports. Expensive body and page checks are skipped during
        normal startup unless ``check_integrity_on_open=True`` is passed to
        :class:`ChainStore`.
        """
        if self._closed:
            raise RuntimeError("ChainStore is closed")
        if not isinstance(check_bodies, bool):
            raise TypeError(f"check_bodies must be a bool, got {type(check_bodies).__name__}")
        if not isinstance(verify_zk, bool):
            raise TypeError(f"verify_zk must be a bool, got {type(verify_zk).__name__}")
        if not isinstance(raise_on_error, bool):
            raise TypeError(f"raise_on_error must be a bool, got {type(raise_on_error).__name__}")

        errors: list[str] = []
        quick_check_ok = False
        quick_check_messages: tuple[str, ...] = ()
        try:
            qc_rows = self._conn.execute("PRAGMA quick_check").fetchall()
        except sqlite3.DatabaseError as exc:
            msg = str(exc)
            quick_check_messages = (msg,)
            errors.append(f"SQLite quick_check failed: {exc}")
        else:
            quick_check_messages = tuple(str(row[0]) for row in qc_rows)
            quick_check_ok = quick_check_messages == ("ok",)
            if not quick_check_ok:
                errors.append(
                    f"SQLite quick_check reported issues: {'; '.join(quick_check_messages)}"
                )

        pragmas: dict[str, int | str] = {}
        schema_version = -1
        indexes: tuple[str, ...] = ()
        block_count = 0
        try:
            pragmas = self.get_pragmas()
            schema_version = int(pragmas["user_version"])
            indexes = tuple(
                row["name"]
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index' "
                    "AND name NOT LIKE 'sqlite_autoindex_%' ORDER BY name"
                ).fetchall()
            )
            block_count = int(self._conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0])
        except sqlite3.DatabaseError as exc:
            errors.append(f"SQLite metadata query failed: {exc}")

        chain_valid = False
        try:
            chain = self.load_chain(validate=True, check_bodies=check_bodies)
            chain_valid = True
            if verify_zk and not chain.verify_zk_attachments():
                chain_valid = False
                errors.append("one or more persisted ZK attachments failed verification")
        except (ValueError, TypeError, sqlite3.DatabaseError) as exc:
            chain_valid = False
            errors.append(f"chain validation failed: {exc}")

        ok = quick_check_ok and chain_valid and not errors
        report = IntegrityReport(
            ok=ok,
            quick_check_ok=quick_check_ok,
            quick_check_messages=quick_check_messages,
            chain_valid=chain_valid,
            block_count=block_count,
            schema_version=schema_version,
            indexes=indexes,
            pragmas=MappingProxyType(pragmas),
            errors=tuple(errors),
        )
        if raise_on_error and not report.ok:
            raise ValueError(
                f"ChainStore integrity verification failed: {'; '.join(report.errors)}"
            )
        return report

    def compact(self, *, pages: int | None = None, full: bool = False) -> None:
        """Reclaim freelist pages left by pruning and checkpoint the WAL.

        By default runs ``PRAGMA incremental_vacuum`` (or up to ``pages`` pages)
        followed by ``PRAGMA wal_checkpoint(TRUNCATE)``. Pass ``full=True`` to
        run ``PRAGMA auto_vacuum = INCREMENTAL`` + a full blocking ``VACUUM``
        (required once on pre-existing databases created without incremental
        auto-vacuum before incremental compaction can reclaim space).
        """
        if not isinstance(full, bool):
            raise TypeError(f"full must be a bool, got {type(full).__name__}")
        if pages is not None:
            if full:
                raise ValueError("pages cannot be combined with full=True")
            if isinstance(pages, bool) or not isinstance(pages, int) or pages < 1:
                raise ValueError("pages must be a positive integer")

        if self._conn.in_transaction:
            raise RuntimeError("cannot compact while a transaction is active")

        if full:
            self._conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
            self._conn.execute("VACUUM")
        else:
            # executescript() steps PRAGMA incremental_vacuum to SQLITE_DONE even
            # on Python 3.10/3.11, where cursor.fetchall() stops after 1 page
            # because PRAGMA incremental_vacuum yields 0-column rows.
            if pages is None:
                self._conn.executescript("PRAGMA incremental_vacuum;")
            else:
                self._conn.executescript(f"PRAGMA incremental_vacuum({pages});")
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()

    def append_block(self, block: Block, applied_tx_ids, updated_accounts: dict) -> None:
        """Low-level persistence primitive for an already-created ``block`` and
        its state changes in one atomic SQLite transaction.

        Ordering and coordination contract:
        - Callers must persist blocks in contiguous index order starting from
          genesis (``idx = 0``), because ``applied_tx_ids.block_idx`` references
          ``blocks(idx)``.
        - ``ChainStore`` does not mutate any in-memory :class:`Chain` here;
          callers are responsible for keeping memory and storage consistent.
        - Duplicate ``tx_id`` collisions fail loudly with
          ``sqlite3.IntegrityError`` and roll back the entire block write.

        Prefer :meth:`add_block` when appending to a live :class:`Chain`.
        """
        with self._write_tx():
            self._insert_block(block, applied_tx_ids, updated_accounts)

    def add_block(
        self,
        chain: Chain,
        transactions: list,
        zk_attachments: Iterable[ZKAttachment] | None = None,
    ) -> Block:
        """Append a block and persist it as one coordinated operation.

        The prospective block and batch delta are prepared via
        :meth:`Chain.prepare_block` before acquiring the SQLite write lock so
        CPU-heavy compression and hashing never block concurrent writers. When
        the store has a bounded ``max_cached_tx_ids`` configured, the live
        chain's ``StateDelta`` is bound to SQLite-backed replay lookups after the
        first commit. A database failure leaves the in-memory chain unchanged.
        """
        if not isinstance(chain, Chain):
            raise TypeError(f"chain must be a Chain, got {type(chain).__name__}")
        prepared = chain.prepare_block(transactions, zk_attachments=zk_attachments)
        delta = prepared.delta

        with self._write_tx():
            self._ensure_chain_persisted(chain)
            self._insert_block(prepared.block, delta.tx_ids, delta.updated_accounts)

        committed = chain.commit_block(prepared)
        if self._max_cached_tx_ids is not None and not chain.state.is_applied_tx_cache_bounded:
            chain.state.configure_applied_tx_cache(
                self._max_cached_tx_ids, tx_lookup=self.has_applied_tx
            )
        return committed

    def prune_chain(self, pruner: BlockchainPruner, *, compact: bool = False) -> list[int]:
        """Atomically persist pruning updates and apply in-memory pruning.

        Uses :meth:`BlockchainPruner.eligible_block_indices` so retention window
        semantics stay identical to :meth:`BlockchainPruner.prune`. SQLite
        changes are held uncommitted until ``pruner.prune()`` succeeds; any
        failure rolls back both the database updates and in-memory pruning flags
        on the affected blocks (and resets the pruner cursor).

        Freed pages remain on SQLite's internal freelist unless ``compact=True``
        is passed or :meth:`compact` is called afterward. Note that pruning
        leaves ``applied_tx_ids`` intact so replay protection across pruned
        blocks remains exact.
        """
        if not isinstance(pruner, BlockchainPruner):
            raise TypeError(f"pruner must be a BlockchainPruner, got {type(pruner).__name__}")
        if not isinstance(compact, bool):
            raise TypeError(f"compact must be a bool, got {type(compact).__name__}")
        initial_len = len(pruner.chain)
        candidates = pruner.eligible_block_indices()
        if not candidates:
            return []
        snapshots = [
            (
                block := pruner.chain.get_block(index),
                block.transactions,
                block.compressed_body,
                block.is_pruned,
            )
            for index in candidates
        ]

        try:
            with self._write_tx():
                for index in candidates:
                    cursor = self._conn.execute(
                        "UPDATE blocks SET body = NULL, is_pruned = 1 WHERE idx = ?",
                        (index,),
                    )
                    if cursor.rowcount == 0:
                        raise KeyError(f"no persisted block with index {index}")

                if len(pruner.chain) != initial_len:
                    raise RuntimeError(
                        "chain changed while pruning; database and chain would diverge"
                    )
                pruned = pruner.prune()
                if pruned != candidates:
                    raise RuntimeError(
                        "chain changed while pruning; database and chain would diverge"
                    )
        except BaseException:
            for block, transactions, compressed_body, is_pruned in snapshots:
                block.transactions = transactions
                block.compressed_body = compressed_body
                block.is_pruned = is_pruned
            pruner.reset_cursor()
            raise

        if compact and pruned:
            self.compact()
        return pruned

    def _insert_block(self, block: Block, applied_tx_ids, updated_accounts: dict) -> None:
        if not isinstance(updated_accounts, dict):
            raise TypeError(
                f"updated_accounts must be a dict, got {type(updated_accounts).__name__}"
            )
        for account, balance in updated_accounts.items():
            if not isinstance(account, str) or not account:
                raise ValueError("account names in updated_accounts must be non-empty strings")
            StateDelta.validate_balance(balance, field="balance")

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
            "INSERT INTO applied_tx_ids (tx_id, block_idx) VALUES (?, ?)",
            [(tx_id, block.index) for tx_id in applied_tx_ids],
        )
        if block.zk_attachments:
            serialized_attachments = [
                (block.index, attachment.tx_id, canonical_json(attachment.to_dict()))
                for attachment in block.zk_attachments
            ]
            self._conn.executemany(
                "INSERT INTO zk_attachments (block_idx, tx_id, payload_json) VALUES (?, ?, ?)",
                serialized_attachments,
            )

    def _has_orphaned_state(self) -> bool:
        has_state = (
            self._conn.execute("SELECT 1 FROM state_snapshot LIMIT 1").fetchone() is not None
        )
        has_txs = self._conn.execute("SELECT 1 FROM applied_tx_ids LIMIT 1").fetchone() is not None
        has_zk = self._conn.execute("SELECT 1 FROM zk_attachments LIMIT 1").fetchone() is not None
        return has_state or has_txs or has_zk

    def _ensure_chain_persisted(self, chain: Chain) -> None:
        """Verify persisted block count and tip match ``chain`` without scanning
        every historical header on each append.

        Assumes persisted historical rows are not silently altered between
        appends; call :meth:`load_chain` (with ``validate=True``) or
        :meth:`Chain.validate_chain` for full end-to-end history verification.
        """
        persisted_count = self._conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
        if persisted_count == 0:
            if self._has_orphaned_state():
                raise ValueError(
                    "database contains orphaned state_snapshot or applied_tx_ids rows "
                    "without blocks"
                )
            if len(chain) != 1:
                raise ValueError("cannot append to an unpersisted chain with existing blocks")
            if chain.state.has_evicted_tx_ids:
                raise ValueError(
                    "cannot seed empty store from a chain whose bounded tx cache already overflowed"
                )
            self._insert_block(
                chain.get_block(0),
                sorted(chain.state.get_applied_tx_ids()),
                chain.state.get_current_state(),
            )
            return

        tip_row = self._conn.execute(
            "SELECT idx, hash FROM blocks ORDER BY idx DESC LIMIT 1"
        ).fetchone()
        chain_tip = chain.get_block(len(chain) - 1)
        if (
            persisted_count != len(chain)
            or tip_row is None
            or tip_row["idx"] != chain_tip.index
            or tip_row["hash"] != chain_tip.hash
        ):
            raise ValueError("persisted chain tip does not match the in-memory chain")

    def mark_pruned(self, index: int) -> None:
        """Low-level primitive to drop a persisted block's body in SQLite.

        Unlike :meth:`prune_chain`, this operates only on storage and leaves
        in-memory :class:`Block` instances untouched; callers must synchronize
        in-memory pruning separately (for example, after calling
        :meth:`BlockchainPruner.prune`). Raises ``KeyError`` if ``index`` is
        not persisted.

        Prefer :meth:`prune_chain` for coordinated atomic pruning.
        """
        with self._write_tx():
            cursor = self._conn.execute(
                "UPDATE blocks SET body = NULL, is_pruned = 1 WHERE idx = ?", (index,)
            )
        if cursor.rowcount == 0:
            raise KeyError(f"no persisted block with index {index}")

    def load_chain(
        self,
        validate: bool = True,
        check_bodies: bool = True,
        metrics: MetricsRegistry | None = None,
        compressor: BlockCompressor | None = None,
        max_cached_tx_ids: int | None | object = _UNSET,
    ) -> Chain:
        """Reconstruct a full :class:`Chain`, headers and all, from storage.

        Reads all tables inside a single deferred read snapshot so concurrent
        writers in WAL mode cannot cause torn cross-table reads and reads never
        upgrade to a write lock. Rows are ordered explicitly by ``idx`` - row
        order on disk is not otherwise guaranteed. Each block is rebuilt via
        ``Block.from_persisted`` (which avoids recomputing hashes from pruned
        bodies) and assembled via ``Chain.from_persisted`` without creating a
        throwaway genesis block.

        By default inherits ``max_cached_tx_ids`` from this ``ChainStore`` so
        large persisted chains bound in-memory ``applied_tx_ids`` growth via
        an LRU cache backed by :meth:`has_applied_tx` (keep this ``ChainStore``
        open if the cache overflows; pass ``max_cached_tx_ids=None`` to load
        all IDs into RAM for detached use).

        When ``validate`` is ``True`` (the default), calls
        ``chain.validate_chain(check_bodies=check_bodies)`` and raises
        ``ValueError`` if validation fails. Raises ``ValueError`` if the
        database contains orphaned state/tx-id rows with no blocks.
        """
        cache_limit = self._max_cached_tx_ids if max_cached_tx_ids is _UNSET else max_cached_tx_ids
        if cache_limit is not None and (
            isinstance(cache_limit, bool) or not isinstance(cache_limit, int) or cache_limit < 0
        ):
            raise ValueError("max_cached_tx_ids must be a non-negative integer or None")

        with self._read_tx():
            block_rows = self._conn.execute(
                "SELECT idx, prev_hash, merkle_root, hash, timestamp, body, is_pruned "
                "FROM blocks ORDER BY idx"
            ).fetchall()
            state_rows = self._conn.execute(
                "SELECT account, balance FROM state_snapshot"
            ).fetchall()
            zk_rows = self._conn.execute(
                "SELECT block_idx, tx_id, payload_json FROM zk_attachments ORDER BY id"
            ).fetchall()

            has_overflow = False
            if cache_limit is None:
                tx_ids: Iterable[str] = [
                    row["tx_id"]
                    for row in self._conn.execute("SELECT tx_id FROM applied_tx_ids").fetchall()
                ]
            elif cache_limit == 0:
                tx_ids = ()
                has_overflow = (
                    self._conn.execute("SELECT 1 FROM applied_tx_ids LIMIT 1").fetchone()
                    is not None
                )
            else:
                recent_rows = self._conn.execute(
                    "SELECT tx_id FROM applied_tx_ids ORDER BY rowid DESC LIMIT ?",
                    (cache_limit + 1,),
                ).fetchall()
                has_overflow = len(recent_rows) > cache_limit
                tx_ids = [row["tx_id"] for row in reversed(recent_rows[:cache_limit])]

        if not block_rows:
            if state_rows or tx_ids or has_overflow or zk_rows:
                raise ValueError(
                    "database contains orphaned state_snapshot, applied_tx_ids, or "
                    "zk_attachments rows without blocks"
                )
            return Chain(
                state=StateDelta(
                    max_applied_tx_ids=cache_limit,
                    tx_lookup=self.has_applied_tx if cache_limit is not None else None,
                ),
                metrics=metrics,
                compressor=compressor,
            )

        attachments_by_block: dict[int, list[ZKAttachment]] = {}
        for row in zk_rows:
            decoded_payload = json.loads(row["payload_json"])
            attachment = ZKAttachment.from_dict(decoded_payload)
            if row["tx_id"] != attachment.tx_id:
                raise ValueError("persisted zk_attachment tx_id does not match payload_json")
            attachments_by_block.setdefault(row["block_idx"], []).append(attachment)

        blocks = [
            Block.from_persisted(
                index=row["idx"],
                prev_hash=row["prev_hash"],
                merkle_root=row["merkle_root"],
                hash=row["hash"],
                timestamp=row["timestamp"],
                compressed_body=row["body"],
                is_pruned=bool(row["is_pruned"]),
                zk_attachments=attachments_by_block.get(row["idx"]),
            )
            for row in block_rows
        ]

        state = StateDelta.from_snapshot(
            {row["account"]: row["balance"] for row in state_rows},
            tx_ids,
            max_applied_tx_ids=cache_limit,
            tx_lookup=self.has_applied_tx if cache_limit is not None else None,
            has_overflow=has_overflow,
        )

        chain = Chain.from_persisted(
            blocks=blocks, state=state, metrics=metrics, compressor=compressor
        )
        if validate and not chain.validate_chain(check_bodies=check_bodies):
            raise ValueError("persisted chain failed validation")
        return chain
