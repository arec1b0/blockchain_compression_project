# **API Documentation**

This document provides detailed information on the various modules, classes, and functions in the **Blockchain Compression Project**. Each section explains the purpose, input parameters, and return values of each function, along with examples of how to use them.

---

## **Table of Contents**

1. [Canonical JSON Module](#canonical-json-module)
    - [canonical_json](#canonical_jsonobj---str)
2. [Chain Module](#chain-module)
    - [Block](#block)
    - [Chain](#chain)
3. [Compression Module](#compression-module)
    - [BlockCompressor](#blockcompressor)
4. [State Delta Module](#state-delta-module)
    - [StateDelta](#statedelta)
5. [Merkle Tree Module](#merkle-tree-module)
    - [MerkleTree](#merkletree)
6. [Blockchain Pruner Module](#blockchain-pruner-module)
    - [BlockchainPruner](#blockchainpruner)
7. [Persistence Module](#persistence-module)
    - [ChainStore](#chainstore)
    - [IntegrityReport](#integrityreport)
8. [ZK Proof Module](#zk-proof-module)
    - [Commitment](#commitment)
    - [PedersenCommitment](#pedersencommitment)
    - [SchnorrProof](#schnorrproof)
    - [Schnorr proof functions](#schnorr-proof-functions)
9. [Observability Module](#observability-module)
    - [MetricsRegistry](#metricsregistry)
    - [CLI Logging Helpers](#cli-logging-helpers)
10. [CLI Entry Points](#cli-entry-points)
    - [main](#mainargv-liststr--none---none)
    - [Benchmark CLIs](#benchmark-clis)

---

## Canonical JSON Module

### **`canonical_json(obj) -> str`**
- **Description**: Deterministic RFC 8259 JSON encoder (`sort_keys=True`, compact `(",", ":")` separators, `allow_nan=False`) shared across block hashing, compression serialization, ZK attachment storage, and scalar hashing so digests and payloads never drift across call sites.
- **Raises**: `TypeError` for non-JSON-native objects (such as raw `bytes` or `set`, without `str()` coercion); `ValueError` for non-finite floats (`NaN`, `Infinity`, `-Infinity`).
- **Example**:
  ```python
  from blockchain_compression.canonical import canonical_json

  encoded = canonical_json({"tx_id": "tx-1", "account": "Alice", "amount": 100})
  ```

---

## Chain Module

### **Block**

A single block: a header (`index`, `prev_hash`, `timestamp`, `merkle_root`, `hash`)
hash-linked to its predecessor, plus an LZMA-compressed transaction body. The header hash
covers only the header fields - never the body - so a block's identity survives its body
being pruned away.

#### **Methods:**

##### `Block.create(index: int, prev_hash: str, transactions: list, timestamp: float | None = None, zk_attachments: Iterable[ZKAttachment] | None = None, compressor: BlockCompressor | None = None) -> Block`
- **Description**: Classmethod. Builds a fresh block from raw transactions. Each transaction is serialized once via `canonical_json(tx)` to compute both the Merkle root and `"[" + ",".join(tx_strings) + "]"` without re-serializing the full transaction list. Transactions are deep-copied via JSON round-tripping so caller mutations cannot desynchronize the block body. Optional `compressor` selects preset and framing thresholds; optional `zk_attachments` are structurally validated without altering header hashes.
- **Raises**: `TypeError` if `transactions` is not a list or `compressor` is not a `BlockCompressor` / `None`.
- **Example**:
  ```python
  from blockchain_compression.chain import Block

  block = Block.create(index=1, prev_hash="0" * 64, transactions=[
      {"tx_id": "tx-1", "account": "Alice", "amount": 100}
  ])
  ```

##### `Block.from_persisted(*, index, prev_hash, merkle_root, hash, timestamp, compressed_body, is_pruned) -> Block`
- **Description**: Classmethod. Reconstructs a block from trusted storage *without*
  recomputing its hash - used by `ChainStore.load_chain()`. A decompression/parse/shape
  failure (including non-list JSON) on a present body is swallowed (`transactions` stays
  `None`) rather than raised, so it surfaces as a `verify_body()` failure instead of
  crashing the reload.

##### `prune(self) -> None`
- **Description**: Drops the body (`transactions` and `compressed_body` become `None`,
  `is_pruned` becomes `True`) while keeping every header field, so hash-linkage stays
  verifiable. The only method that may mutate a `Block` after construction.

##### `verify_body(self) -> bool`
- **Description**: `True` for a properly pruned block (`compressed_body` and `transactions`
  are both `None`). `False` if a pruned block still carries body/transaction data, if an
  unpruned block's body is missing or `transactions` is not a list (corruption), or if a
  present body's content doesn't reproduce `merkle_root` (tampering). Never raises on bad
  data.

##### `Block.compute_hash(index, prev_hash, merkle_root, timestamp) -> str`
- **Description**: Static method. SHA-256 over the canonical header fields - the one place
  this concatenation happens, used both by `create()` and by `Chain.validate_chain()`.

---

### **Chain**

A hash-linked, genesis-first sequence of `Block`s with an attached `StateDelta` tracking
cumulative account state.

#### **Methods:**

##### `__init__(self, state: StateDelta | None = None, metrics: MetricsRegistry | None = None, compressor: BlockCompressor | None = None)`
- **Description**: Creates a chain with a genesis block. `state` defaults to a fresh
  `StateDelta`; `metrics` defaults to `None` (no metrics recorded); optional `compressor`
  configures block compression preset and threshold for newly appended blocks (exposed via the
  read-only `compressor` property).
- **Example**:
  ```python
  from blockchain_compression.chain import Chain

  chain = Chain()
  ```

##### `add_block(self, transactions: list, zk_attachments: Iterable[ZKAttachment] | None = None) -> Block`
- **Description**: Validates every transaction (via `StateDelta.stage_batch`) and rejects
  duplicate or already-applied `tx_id`s in `O(len(transactions))` *before* creating a block or
  mutating state, then stages and commits via `prepare_block()` / `commit_block()`. Optional
  `zk_attachments` are structurally validated and stored on the block without altering header
  hashes or mandatory chain rules. When `metrics` is supplied, records constant-cardinality
  metrics (`blocks_added_total`, `block_raw_bytes_total`, `block_compressed_bytes_total`,
  `last_block_compression_ratio` gauge, and `block_compression_ratio` histogram).
- **Raises**: `TypeError` if `transactions` isn't a list or `zk_attachments` contains non-`ZKAttachment` values; `ValueError` for duplicate/replayed `tx_id`s or whatever `StateDelta.validate_transaction` / balance bounds raise.
- **Example**:
  ```python
  block = chain.add_block([{"tx_id": "tx-1", "account": "Alice", "amount": 100}])
  ```

##### `prepare_block(self, transactions: list, zk_attachments: Iterable[ZKAttachment] | None = None) -> PreparedBlock`
- **Description**: Stages a prospective block, batch `BlockDelta` (`prepared.delta`), and raw serialized byte count in `O(len(transactions))` without mutating chain state or copying full history. Used with `commit_block()` by coordinated persistence layers such as `ChainStore`.

##### `commit_block(self, prepared: PreparedBlock) -> Block`
- **Description**: Commits a `PreparedBlock` produced by `prepare_block()` using `O(len(delta.tx_ids))` state delta application, raising `ValueError` if the chain tip changed since preparation.

##### `Chain.from_persisted(blocks, state, metrics=None, compressor=None) -> Chain`
- **Description**: Classmethod. Reconstructs a `Chain` directly from persisted `Block` objects and a hydrated `StateDelta` without creating or compressing a throwaway genesis block.

##### `validate_chain(self, check_bodies: bool = True) -> bool`
- **Description**: Walks the chain checking index continuity, `prev_hash` linkage,
  header-hash recomputation, and (if `check_bodies`) that every non-pruned block's body
  still matches its header via `Block.verify_body()`. Does *not* enforce optional
  `zk_attachments`; use `verify_zk_attachments()` for opt-in verification.
- **Example**:
  ```python
  assert chain.validate_chain()
  ```

##### `verify_zk_attachments(self, metrics: MetricsRegistry | None = None) -> bool`
- **Description**: Opt-in helper that verifies every optional `ZKAttachment` across all blocks in the chain (returns `False` if any attachment lacks a proof or fails verification).

##### `get_block(self, index: int) -> Block`
- **Description**: Returns the block at `index`.
- **Raises**: `IndexError` if out of range.

##### `blocks` (property) -> `tuple[Block, ...]`
- **Description**: Read-only tuple view of blocks in the chain, genesis-first.

##### `state` (property) -> `StateDelta`
- **Description**: The chain's cumulative account state (for inspection). Direct mutation
  outside `add_block()` is unsupported; call `get_state_snapshot()` for an isolated copy.

##### `get_state_snapshot(self) -> StateDelta`
- **Description**: Returns an independent copy of the chain's current `StateDelta`.

##### `__len__(self)` / `__iter__(self)`
- **Description**: Number of blocks (including genesis); iterates blocks oldest-first.

---

## Compression Module

### **BlockCompressor**

The `BlockCompressor` class encodes and decodes block bodies using size-aware framing (`FRAME_RAW = 0x00` or `FRAME_LZMA = 0x01`), while preserving backward compatibility with legacy unframed LZMA streams (`LZMA_MAGIC = b"\xfd7zXZ\x00"`).

#### **Methods:**

##### `__init__(self, preset: int = 1, max_decompressed_bytes: int = 16 * 1024 * 1024, min_compress_bytes: int = 256)`
- **Description**: Initializes the block compressor with LZMA `preset` (`0`–`9`, default `DEFAULT_PRESET = 1`), output cap `max_decompressed_bytes` (default `16 MiB`), and size threshold `min_compress_bytes` (default `DEFAULT_COMPRESSION_THRESHOLD = 256` bytes; pass `0` to force `FRAME_LZMA` for all inputs).
- **Raises**: `ValueError` if `preset` is not a strict `int` in `0..9`, `max_decompressed_bytes < 1`, or `min_compress_bytes < 0`.
- **Example**:
  ```python
  from blockchain_compression.compression import BlockCompressor

  compressor = BlockCompressor(preset=6, min_compress_bytes=256)
  ```

##### `compress_block(self, block_data: bytes) -> bytes`
- **Description**: Encodes `block_data` with a 1-byte header (`FRAME_RAW = 0x00` when `len(block_data) < min_compress_bytes` or when `min_compress_bytes > 0` and LZMA does not shrink the payload; `FRAME_LZMA = 0x01` otherwise).
- **Raises**: `TypeError` if `block_data` is not `bytes` or `bytearray`.

##### `decompress_block(self, compressed_block_data: bytes) -> bytes`
- **Description**: Decodes framed (`0x00` / `0x01`) payloads or legacy unframed `LZMA_MAGIC` streams while enforcing `max_decompressed_bytes`, stream EOF, and absence of trailing bytes.
- **Raises**: `TypeError` if input is not `bytes`/`bytearray`; `ValueError` if empty, unknown frame byte, corrupted, truncated, trailing data is present, or output exceeds `max_decompressed_bytes`.

---

## State Delta Module

### **StateDelta**

The `StateDelta` class handles the storage and management of state changes (deltas) in the blockchain. Replay protection uses an unbounded set by default for standalone usage, or an optional bounded LRU cache (`max_applied_tx_ids`) backed by a `tx_lookup` callable (such as `ChainStore.has_applied_tx`).

#### **Methods:**

##### `__init__(self, *, max_applied_tx_ids: int | None = None, tx_lookup: Callable[[str], bool] | None = None)`
- **Description**: Initializes state balances and replay tracking. Setting `max_applied_tx_ids` (`>= 0`) requires a callable `tx_lookup` so evictions never weaken replay detection.

##### `stage_batch(self, transactions: list[dict]) -> BlockDelta` / `commit_delta(self, delta: BlockDelta, *, check_backing_lookup: bool = True) -> None`
- **Description**: Validate and apply transaction batches in `O(len(transactions))` without full-history state or `applied_tx_ids` copies.

##### `configure_applied_tx_cache(self, max_size: int | None, tx_lookup: Callable[[str], bool] | None = None) -> None` / `copy(self) -> StateDelta` / `get_balance(self, account: str) -> int`
- **Description**: Attaches or resizes the bounded LRU replay cache, clones state cleanly, and queries a single account's balance without copying `current_state`. Read-only properties `is_applied_tx_cache_bounded`, `cached_applied_tx_count`, `has_evicted_tx_ids`, and `max_applied_tx_ids` inspect cache configuration and occupancy.

##### `apply_transaction(self, transaction: dict) -> dict[str, int]`
- **Description**: Applies a transaction to the blockchain state in exact signed 64-bit
  integer base units (`[-2**63, 2**63 - 1]`) and stores only the delta (difference).
  Direct calls to `apply_transaction` remain **idempotent**: replaying a transaction with an
  already-applied `tx_id` is a no-op and returns an empty dict.
- **Parameters**: 
  - `transaction` (dict): A dictionary representing the transaction to apply. Must contain the keys:
    - `tx_id`: A unique, non-empty string identifying the transaction (used for replay protection).
    - `account`: The account to update (non-empty string).
    - `amount`: An exact `int` (not `bool`, `float`, or `Decimal`) change in `[-2**63, 2**63 - 1]`.
- **Returns**: 
  - A dictionary representing the delta (difference) applied to the state, or `{}` if the transaction was already applied.
- **Raises**: `TypeError` / `ValueError` if the transaction is malformed or resulting balance overflows signed 64-bit bounds.
- **Example**:
  ```python
  from blockchain_compression.compression import StateDelta

  state_delta = StateDelta()
  delta = state_delta.apply_transaction({'tx_id': 'tx-001', 'account': 'Alice', 'amount': 100})
  ```

##### `is_applied(self, tx_id: str) -> bool`
- **Description**: Returns `True` if `tx_id` has already been applied to this state.

##### `StateDelta.validate_balance(value, *, field: str = "balance") -> None`
- **Description**: Static method. Raises `ValueError` if `value` is not an exact `int` (excluding `bool`) within `[-2**63, 2**63 - 1]`.

##### `get_current_state(self) -> dict`
- **Description**: Returns the current full state of the blockchain.
- **Returns**: 
  - A dictionary representing the current state of the blockchain.

##### `get_applied_tx_ids(self) -> frozenset`
- **Description**: Returns the set of transaction IDs already applied (the replay-protection state).

##### `StateDelta.validate_transaction(transaction) -> None`
- **Description**: Static method. Raises `TypeError`/`ValueError` if `transaction` is malformed; used internally by `apply_transaction`, and public so callers like `Chain.add_block` can pre-validate a whole batch before applying any of it.

##### `StateDelta.from_snapshot(state: dict, applied_tx_ids: Iterable[str]) -> StateDelta`
- **Description**: Classmethod. Rehydrates a `StateDelta` from a persisted balance snapshot and tx-id set - used when reloading from storage, where pruned blocks no longer have raw transaction data to replay.
- **Example**:
  ```python
  hydrated = StateDelta.from_snapshot({"Alice": 100}, {"tx-001"})
  ```

##### `extract_block_delta(transactions: list[dict], state: StateDelta) -> BlockDelta`
- **Description**: Shared utility returning a frozen `BlockDelta(tx_ids, accounts, updated_accounts)` summarizing the transaction IDs and updated account balances for persistence (used by `ChainStore.add_block()` and `main._sync_block()`). Raises `KeyError` if an account referenced in `transactions` is missing from `state`.

---

## Merkle Tree Module

### **MerkleTree**

The `MerkleTree` class constructs a Merkle tree from a list of transactions and provides the root hash plus per-transaction inclusion proofs. Leaf and internal-node hashes are domain-separated (`0x00` / `0x01` prefixes, as in RFC 6962), and unpaired nodes are promoted rather than duplicated, so different transaction sets cannot collide on the same root.

#### **Methods:**

##### `__init__(self, transactions: list)`
- **Description**: Initializes a Merkle tree from a list of transactions. An empty list is allowed and produces a tree with root `None`.
- **Parameters**: 
  - `transactions` (list): A list of transaction strings.
- **Raises**: `TypeError` if `transactions` is not a list of strings.
- **Example**:
  ```python
  from blockchain_compression.merkle import MerkleTree

  merkle_tree = MerkleTree(["tx1", "tx2", "tx3", "tx4"])
  ```

##### `get_root(self) -> str | None`
- **Description**: Returns the root hash of the Merkle tree, or `None` for an empty tree.
- **Returns**: 
  - The root hash as a hex string, or `None`.

##### `get_proof(self, index: int) -> list`
- **Description**: Returns the inclusion proof for the transaction at `index`.
- **Parameters**: 
  - `index` (int): Position of the transaction in the original list.
- **Returns**: 
  - A list of proof steps `{"hash": <hex digest>, "position": "left" | "right"}`, where `position` is the side of the sibling hash.
- **Raises**: `IndexError` if `index` is out of range.

##### `verify_proof(cls, transaction: str, proof: list, root: str | None) -> bool`
- **Description**: Classmethod. Verifies that `transaction` is committed under `root` using `proof`. Does not require the full tree.
- **Validation & Errors**: Raises `TypeError` if `transaction` is not a `str`, `proof` is not a `list`, `root` is not `str | None`, or a step `'hash'` is not a `str`. Raises `ValueError` if `root` (when non-`None`) or any step `'hash'` is not a 64-character hex digest, if a proof step dict lacks `'hash'`/`'position'`, or if `'position'` is not `'left'` or `'right'`. Returns `False` when `root is None` or when the derived root mismatches `root`.
- **Example**:
  ```python
  assert MerkleTree.verify_proof("tx3", proof, root_hash)
  ```

##### `hash_leaf(data: str) -> str` / `hash_node(left: str, right: str) -> str`
- **Description**: Static hashing primitives. `hash_leaf` hashes a transaction as a leaf (`0x00`-prefixed SHA-256); `hash_node` hashes two child digests as an internal node (`0x01`-prefixed SHA-256).

---

## Blockchain Pruner Module

### **BlockchainPruner**

The `BlockchainPruner` class prunes a real, hash-linked `Chain` - not a plain list. It keeps
full bodies for only the most recent blocks; older blocks keep their header (so
`Chain.validate_chain()` still passes) but lose their transaction body.

#### **Methods:**

##### `__init__(self, chain: Chain, max_full_blocks: int, metrics: MetricsRegistry | None = None)`
- **Description**: Initializes the pruner over `chain`, keeping the most recent `max_full_blocks` blocks with a full body.
- **Raises**: `TypeError` if `chain` isn't a `Chain` or `max_full_blocks` isn't an int; `ValueError` if `max_full_blocks < 1`.
- **Example**:
  ```python
  from blockchain_compression.pruning import BlockchainPruner

  pruner = BlockchainPruner(chain, max_full_blocks=10)
  ```

##### `eligible_block_indices(self) -> list[int]`
- **Description**: Returns the ordered list of unpruned block indices older than the trailing `max_full_blocks` window, scanning incrementally from `next_unpruned_index` so already-pruned prefixes are skipped cleanly. Shared by both `BlockchainPruner.prune()` and `ChainStore.prune_chain()`.

##### `prune(self) -> list[int]` / `next_unpruned_index` / `reset_cursor() -> None`
- **Description**: `prune()` drops bodies for every block returned by `eligible_block_indices()`, advances `next_unpruned_index`, and records `pruning_events_total` / `blocks_pruned_total` when `metrics` is supplied. `reset_cursor()` resets the incremental cursor (used automatically on `ChainStore.prune_chain()` rollback).
- **Example**:
  ```python
  pruned_indices = pruner.prune()
  assert chain.validate_chain()  # headers survive pruning
  ```

---

## Persistence Module

### **ChainStore**

SQLite-backed persistence for a `Chain`. Four tables back a round-trippable chain at schema version `2`:
`blocks` (headers + optional body), `state_snapshot` (current account balances),
`applied_tx_ids` (replay-protection membership), and optional `zk_attachments`. Two non-unique lookup indexes are also managed automatically:
- `idx_applied_tx_ids_block_idx` on `applied_tx_ids(block_idx)` (avoids full table scans on foreign-key checks and block lookups, adding a minor B-tree insert overhead per transaction ID).
- `idx_zk_attachments_block_idx` on `zk_attachments(block_idx)` (created whenever `zk_attachments` exists; skipped on partial/pre-v2 schemas lacking that table).

`state_snapshot` and `applied_tx_ids` aren't a cache: once a block is pruned, they're the *only* surviving record of what its transactions did.

#### **Methods:**

##### `__init__(self, path: str | Path, *, busy_timeout_ms: int = 5000, synchronous: str = "NORMAL", max_cached_tx_ids: int | None = 10_000, check_integrity_on_open: bool = False)`
- **Description**: Opens (creating if needed) a SQLite database at `path` configured with `PRAGMA auto_vacuum = INCREMENTAL`, `PRAGMA journal_mode = WAL`, `PRAGMA busy_timeout = busy_timeout_ms`, `PRAGMA synchronous = synchronous`, and `PRAGMA foreign_keys = ON`. Ensures schema version `2` and indexes exist (`state_snapshot.balance` as exact `INTEGER`, plus optional `zk_attachments`), automatically migrating v0 and v1 databases. `max_cached_tx_ids` bounds the in-memory replay LRU cache when persisting or reloading chains. Startup integrity validation stays off by default (`check_integrity_on_open=False`); pass `True` to run `verify_integrity(check_bodies=True, raise_on_error=True)` immediately on open. Also usable as a context manager (`with ChainStore(path) as store:`).

##### `get_pragma(self, name: str) -> int | str` / `get_pragmas(self) -> dict[str, int | str]`
- **Description**: Public read-only accessors over allowlisted SQLite PRAGMAs (`auto_vacuum`, `busy_timeout`, `foreign_keys`, `freelist_count`, `journal_mode`, `page_count`, `page_size`, `synchronous`, `user_version`) without exposing internal connection state.
- **Raises**: `TypeError` for non-string names, `ValueError` for non-allowlisted PRAGMA names, `RuntimeError` if called after `close()`.

##### `verify_integrity(self, *, check_bodies: bool = True, verify_zk: bool = False, raise_on_error: bool = False) -> IntegrityReport`
- **Description**: Runs SQLite `PRAGMA quick_check`, collects schema/index/PRAGMA metadata, and runs `load_chain(validate=True, check_bodies=check_bodies)` (plus optional `chain.verify_zk_attachments()` when `verify_zk=True`). Recommended after backup restores, crash recovery, or before critical migrations/exports.
- **Raises**: `TypeError` if boolean flags are invalid, `RuntimeError` if closed, `ValueError` when `raise_on_error=True` and any check fails.

### **IntegrityReport**

Frozen dataclass returned by `ChainStore.verify_integrity()` with fields `ok`, `quick_check_ok`, `quick_check_messages`, `chain_valid`, `block_count`, `schema_version`, `indexes`, read-only `pragmas`, and `errors`, plus `to_dict() -> dict[str, object]` for JSON serialization.

##### `compact(self, *, pages: int | None = None, full: bool = False) -> None`
- **Description**: Reclaims freelist pages left by logical pruning (`PRAGMA incremental_vacuum` + `PRAGMA wal_checkpoint(TRUNCATE)`). Pass `full=True` to run `PRAGMA auto_vacuum = INCREMENTAL` + a blocking `VACUUM` (required once on pre-existing databases without incremental auto-vacuum, or to coalesce sparse `<1 KiB` inline leaf pages).

##### `has_applied_tx(self, tx_id: str) -> bool` / `applied_tx_count(self) -> int`
- **Description**: Query SQLite's indexed `applied_tx_ids` table directly; `has_applied_tx` raises `RuntimeError` if called after the store closes.

##### `append_block(self, block: Block, applied_tx_ids: list, updated_accounts: dict) -> None`
- **Description**: Low-level primitive for persisting an already-created block and its state changes in one SQLite transaction. Callers must persist blocks in contiguous index order starting from genesis (`idx = 0`) and keep in-memory `Chain` state synchronized separately; duplicate `tx_id` collisions fail with `sqlite3.IntegrityError` and roll back the write. Prefer `add_block` when appending to a live chain.
- **Example**:
  ```python
  from blockchain_compression.compression import extract_block_delta
  from blockchain_compression.persistence import ChainStore

  with ChainStore("chain.db") as store:
      block = chain.add_block(transactions)
      delta = extract_block_delta(transactions, chain.state)
      store.append_block(block, delta.tx_ids, delta.updated_accounts)
  ```

##### `add_block(self, chain: Chain, transactions: list, zk_attachments: Iterable[ZKAttachment] | None = None) -> Block`
- **Description**: Prepares a block and batch `BlockDelta` via `chain.prepare_block()` *before* opening the `BEGIN IMMEDIATE` SQLite write transaction, validates persisted `COUNT(*)` and tip `(idx, hash)` (seeding genesis on empty stores), inserts the block and delta atomically, and commits to `chain`. Once committed, binds `chain.state` to the store's bounded LRU replay cache when `max_cached_tx_ids` is enabled.
- **Example**:
  ```python
  with ChainStore("chain.db") as store:
      block = store.add_block(chain, transactions)
  ```

##### `prune_chain(self, pruner: BlockchainPruner, *, compact: bool = False) -> list[int]`
- **Description**: Marks eligible block bodies (selected via `pruner.eligible_block_indices()`) pruned in SQLite and prunes the in-memory chain inside one `BEGIN IMMEDIATE` transaction, rolling back both storage and in-memory state/cursor on any failure. Set `compact=True` to invoke `compact()` afterward.

##### `mark_pruned(self, index: int) -> None`
- **Description**: Low-level storage primitive mirroring `Block.prune()`: drops the persisted body and sets `is_pruned`. Does not mutate in-memory `Block` instances; prefer `prune_chain` for coordinated atomic pruning.
- **Raises**: `KeyError` if no block with `index` is persisted.

##### `load_chain(self, validate: bool = True, check_bodies: bool = True, metrics: MetricsRegistry | None = None, compressor: BlockCompressor | None = None, max_cached_tx_ids: int | None = ...) -> Chain`
- **Description**: Reconstructs a `Chain` inside a single `BEGIN DEFERRED` WAL read snapshot without acquiring write locks or building a throwaway genesis block. Hydrates `StateDelta` from `state_snapshot`, optional `zk_attachments`, and at most `max_cached_tx_ids` recent `applied_tx_ids` (defaulting to the store setting, backed by `has_applied_tx`; pass `max_cached_tx_ids=None` to hydrate all IDs for detached use). Raises `ValueError` if orphaned rows exist, attachment payloads are malformed, or validation fails.
- **Example**:
  ```python
  with ChainStore("chain.db") as store:
      reloaded = store.load_chain()
  assert reloaded.validate_chain()
  ```

---

## ZK Proof Module

Replaces the old `ZKSnark` placeholder. A genuine (if not constant-time, not-a-SNARK) ZK
primitive: a **Pedersen commitment** (`C = g^m h^r mod p`, computationally binding and
perfectly hiding) plus a **non-interactive Schnorr proof of knowledge** (Fiat-Shamir) that
the prover knows an opening of a commitment, without revealing it. The key difference from
the old mock: `verify_proof` takes only the commitment - never the plaintext message or
blinding factor - which is what makes this actually zero-knowledge.

### **Commitment**

Frozen dataclass (`@dataclass(frozen=True)`) holding the Pedersen group element:
- **`value: int`**: Group element $C = g^m \cdot h^r \bmod p$ in the order-$Q$ subgroup of $\mathbb{Z}_P^*$.

##### `Commitment.to_dict()` / `Commitment.from_dict(data)` / `Commitment.to_bytes()` / `Commitment.from_bytes(data)`
- **Description**: Canonical versioned (`version = 1`, `group = "rfc3526-modp-2048"`) serialization for `Commitment`. `to_dict()` encodes `value` as a fixed 512-char lowercase hex string; `to_bytes()` outputs 263 bytes (`b"BCPCOM\x01"` + 256-byte big-endian `value`).
- **Raises**: `TypeError` for non-dict/non-bytes inputs or non-strict-`int` `value` (including `bool`); `ValueError` for invalid keys, headers, lengths, `(1, P)` range violations, or non-subgroup values.

### **PedersenCommitment**

##### `commit(self, message: int, blinding: int | None = None) -> tuple[Commitment, int]`
- **Description**: Commits to an integer `message` in `[0, Q)`. `blinding` defaults to a fresh cryptographically random value in `[1, Q)` if not given (`blinding=0` is rejected so commitments are always hiding).
- **Raises**: `ValueError` if `message` is outside `[0, Q)`, `blinding` is outside `[1, Q)`, or either is a `bool`/non-`int`.
- **Example**:
  ```python
  from blockchain_compression.zk_proof import PedersenCommitment

  pedersen = PedersenCommitment()
  commitment, blinding = pedersen.commit(42)
  ```

##### `verify_opening(self, commitment: Commitment, message: int, blinding: int) -> bool`
- **Description**: Checks that `(message, blinding)` is a valid opening of `commitment` - this reveals the message, unlike the Schnorr proof below.
- **Raises**: `TypeError` if `commitment` is not a `Commitment` or if `commitment.value`, `message`, or `blinding` is not a strict `int` (`bool` is rejected). Returns `False` if any value is outside its valid range.

### **SchnorrProof**

Frozen dataclass (`@dataclass(frozen=True)`) representing a non-interactive Fiat-Shamir Schnorr proof of knowledge. Deliberately stores **only** the public proof transcript (`t`, `s1`, `s2`) — never `message` or `blinding`:
- **`t: int`**: Commitment/announcement $t = g^{k_1} h^{k_2} \bmod p$ in the order-$Q$ subgroup.
- **`s1: int`**: Response scalar $s_1 = (k_1 + e \cdot m) \bmod Q$ in `[0, Q)`.
- **`s2: int`**: Response scalar $s_2 = (k_2 + e \cdot r) \bmod Q$ in `[0, Q)`.

##### `SchnorrProof.to_dict()` / `SchnorrProof.from_dict(data)` / `SchnorrProof.to_bytes()` / `SchnorrProof.from_bytes(data)`
- **Description**: Canonical versioned (`version = 1`, `group = "rfc3526-modp-2048"`) serialization for `SchnorrProof`. `to_dict()` encodes `t`, `s1`, `s2` as fixed 512-char lowercase hex strings; `to_bytes()` outputs 775 bytes (`b"BCPZKP\x01"` + three 256-byte big-endian integers).
- **Raises**: `TypeError` for non-dict/non-bytes inputs or non-strict-`int` fields (`bool` rejected); `ValueError` for invalid keys, headers, lengths, scalar range violations, or non-subgroup `t`.

### Schnorr proof functions

##### `generate_proof(message: int, blinding: int, commitment: Commitment, metrics=None) -> SchnorrProof`
- **Description**: Proves knowledge of `(message, blinding)` behind `commitment` without revealing them. Samples fresh nonces on every call - a Schnorr nonce must never be reused. Records a `proof_generation_seconds` histogram if `metrics` is given.
- **Raises**: `TypeError` for non-`Commitment` or non-strict-`int` inputs (`bool` rejected); `ValueError` if `message` is outside `[0, Q)`, `blinding` is outside `[1, Q)`, or `commitment` is not a non-identity element of the order-`Q` subgroup.
- **Example**:
  ```python
  from blockchain_compression.zk_proof import generate_proof, verify_proof

  proof = generate_proof(message=42, blinding=blinding, commitment=commitment)
  ```

##### `verify_proof(proof: SchnorrProof, commitment: Commitment, metrics=None) -> bool`
- **Description**: Verifies `proof` against `commitment` alone. Independently recomputes the Fiat-Shamir challenge; never trusts a transmitted value for it. Enforces range bounds (`1 < commitment.value < P`, `1 <= proof.t < P`, `0 <= proof.s1, proof.s2 < Q`) and order-`Q` subgroup membership (`pow(commitment.value, Q, P) == 1` and `pow(proof.t, Q, P) == 1`), which adds two 2048-bit modular exponentiations to verification.
- **Raises**: `TypeError` if `proof`/`commitment` or their integer fields are not strict `int`s (`bool` rejected). Returns `False` for out-of-range or out-of-subgroup inputs.
- **Example**:
  ```python
  assert verify_proof(proof, commitment)  # no message/blinding needed
  ```

##### `hash_to_scalar(data: dict) -> int`
- **Description**: Maps structured JSON-compatible blockchain data (e.g. a transaction dict) to a Pedersen-committable scalar by hashing `b"blockchain-compression-project/tx-scalar/v1" + canonical_json(data).encode("utf-8")` with SHA-256 mod `Q`. Changing the domain prefix alters all derived scalars.
- **Raises**: `TypeError` if `data` is not a `dict` or contains non-JSON-native types (such as raw `bytes`); `ValueError` if `data` contains `NaN` or `Infinity`.
- **Example**:
  ```python
  from blockchain_compression.zk_proof import hash_to_scalar

  message = hash_to_scalar({"tx_id": "tx-1", "account": "Alice", "amount": 100})
  ```

### Serialization & Optional Attachments

##### `ZKAttachment(commitment, proof=None, tx_id=None, version=1)`
- **Description**: Optional versioned container associating a `Commitment` (and optional `SchnorrProof` / `tx_id`) with a block. Provides `to_dict()`, `from_dict()`, and opt-in `verify(metrics=None)` (returns `False` when `proof is None`). Does not introduce confidential-transaction semantics or automatic ledger enforcement.

##### `blockchain_compression.zk_proof.groups.resolve_modexp_backend(backend: str | None = None) -> tuple[str, Callable]`
- **Description**: Selects `"python"` (built-in `pow`, default zero-dependency backend) or optional `"gmpy2"` (controlled by `BLOCKCHAIN_COMPRESSION_ZK_BACKEND`, falling back to pure Python with a warning if `gmpy2` is unavailable). Neither backend changes cryptographic groups or proof formats.

---

## Observability Module

### **MetricsRegistry**

A thread-safe (protected by `threading.RLock`) in-process metrics registry: counters, gauges, and histograms, exportable as JSON or Prometheus text exposition format. Passed explicitly (`metrics=...`) to `Chain`, `BlockchainPruner`, and the ZK proof functions - `None` (the default everywhere) is a true no-op, so nothing is recorded unless a registry is supplied. Constants `DEFAULT_HISTOGRAM_BUCKETS` (seconds) and `COMPRESSION_RATIO_BUCKETS` (dimensionless ratios) are also provided.

#### **Methods:**

##### `increment_counter(self, name, value=1.0, labels=None, help_text=None) -> None`
- **Description**: Increments a monotonic counter. `value` must be a finite, non-negative number (`bool`, `NaN`, and `Infinity` are rejected); label keys must match `^[a-zA-Z_][a-zA-Z0-9_]*$`.
- **Raises**: `TypeError` for non-dict `labels`; `ValueError` for invalid metric/label names, kind mismatch, or negative/non-finite `value`.

##### `set_gauge(self, name, value, labels=None, help_text=None) -> None`
- **Description**: Sets a point-in-time finite numeric value; label keys must match `^[a-zA-Z_][a-zA-Z0-9_]*$`.
- **Raises**: `TypeError` for non-dict `labels`; `ValueError` for invalid metric/label names, kind mismatch, or non-finite/`bool` `value`.

##### `observe_histogram(self, name, value, labels=None, help_text=None, buckets=None) -> None`
- **Description**: Records one finite observation into cumulative buckets (`buckets` only takes effect on a metric's first observation). Rejects the reserved label name `"le"` in `labels`.
- **Raises**: `TypeError` for non-dict `labels`; `ValueError` for reserved `"le"` label, invalid names, kind mismatch, or non-finite/`bool` `value`.

##### `get_counter(self, name, labels=None) -> float | None` / `get_gauge(self, name, labels=None) -> float | None` / `get_histogram(self, name, labels=None) -> HistogramSnapshot | None`
- **Description**: Public read-only accessors returning the recorded series value (or an immutable `HistogramSnapshot` with `count`, `sum`, and read-only `MappingProxyType` `buckets`), or `None` if unrecorded without registering a metric.

##### `to_json(self, indent: int | None = 2) -> str`
- **Description**: A structured snapshot of every metric family as one strict RFC 8259 JSON document (`allow_nan=False`).
- **Raises**: `ValueError` if non-finite values were injected internally.

##### `to_prometheus_text(self) -> str`
- **Description**: Hand-rolled Prometheus text exposition format (`# HELP`/`# TYPE` lines, cumulative `_bucket`/`_sum`/`_count` series for histograms) - no `prometheus_client` dependency.

##### `get_metrics_registry() -> MetricsRegistry`
- **Description**: Module-level function. Returns a process-wide convenience registry, meant for the outermost layer (`main.py`, benchmark scripts) - library code should not call this on a caller's behalf.
- **Example**:
  ```python
  from blockchain_compression.observability import get_metrics_registry

  metrics = get_metrics_registry()
  chain = Chain(metrics=metrics)
  print(metrics.to_prometheus_text())
  ```

### **CLI Logging Helpers**

- `LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")`
- `LOG_FORMATS = ("text", "json")`
- `LOG_LEVEL_ENV_VAR = "BLOCKCHAIN_COMPRESSION_LOG_LEVEL"`
- `resolve_log_level(cli_level=None, *, env_var=LOG_LEVEL_ENV_VAR, default="INFO") -> str`: Resolves log verbosity giving `--log-level` precedence over `BLOCKCHAIN_COMPRESSION_LOG_LEVEL` (raises `TypeError`/`ValueError` on invalid input).
- `configure_logging(level_name, *, log_format="text") -> int`: Configures root log level and optional single-line JSON formatter (`{"level", "logger", "message"}`).

---

## CLI Entry Points

### **`main(argv: list[str] | None = None) -> None`** (`blockchain-compress`)
- **Description**: Runs the end-to-end demo workflow (chain creation, Merkle inclusion proof, SQLite WAL persistence + pruning + compaction, optional integrity verification, reload validation, Pedersen + Schnorr ZK round-trip, and JSON/Prometheus metrics export).
- **Flags**:
  - `--max-full-blocks <N>` (`>= 1`, default `2`)
  - `--db-path <path>` (optional; temporary directory if omitted)
  - `--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}` (overrides `BLOCKCHAIN_COMPRESSION_LOG_LEVEL`, default `INFO`)
  - `--log-format {text,json}` (default `text`)
  - `--verify-integrity` (runs `ChainStore.verify_integrity()` on reload)
- **Raises**: `SystemExit` on invalid CLI/env arguments or `--help`.

### **Benchmark CLIs**

All benchmark entry points preserve tabular output by default and support `--json` (deterministic RFC 8259 JSON on stdout), `--output <path>` (writes stdout/table to file), and `--log-level`:
- `blockchain-compress-bench` (`benchmarks/compression_benchmark.py`): `--datasets`, `--presets`, `--repeats`, `--num-blocks`, `--txs-per-block`, `--data-size`, `--json`, `--output`, `--log-level`.
- `blockchain-compress-bench-merkle` (`benchmarks/merkle_benchmark.py`): `--sizes`, `--repeats`, `--json`, `--output`, `--log-level`.
- `blockchain-compress-bench-pruning` (`benchmarks/pruning_benchmark.py`): `--chain-lengths`, `--max-full-blocks`, `--json`, `--output`, `--log-level`.
- `python -m blockchain_compression.benchmarks.scalability_benchmark`: `--repeats`, `--compression-tx-counts`, `--compression-repeats`, `--chain-heights`, `--sample-blocks`, `--prune-heights`, `--noop-height`, `--noop-calls`, `--sqlite-blocks`, `--overflow-blocks`, `--overflow-txs-per-block`, `--cache-probe-txs`, `--cache-limit`, `--zk-repeats`, `--json`, `--output`, `--log-level`.
