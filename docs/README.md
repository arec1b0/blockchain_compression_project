# Blockchain Compression Project

### Overview
The **Blockchain Compression Project** is a small, dependency-free blockchain system: a
real hash-linked chain of blocks, backed by SQLite persistence, with pruning that operates
on that chain (not a disposable list), Merkle inclusion proofs, a genuine Pedersen
commitment + Schnorr zero-knowledge proof, and structured metrics. Every block's header
(`prev_hash` + Merkle root + timestamp) hash-links it to its predecessor; the transaction
body is LZMA-compressed and can be pruned away independently, without breaking the header's
verifiability back to genesis.

---

### Features
- **Hash-Linked Block/Chain**: Each block's header commits to its transactions via a Merkle
  root and links to its predecessor via `prev_hash`; `Chain.validate_chain()` verifies the
  whole thing, including after pruning.
- **Block Compression**: Compresses each block's transaction body using LZMA to minimize
  storage requirements.
- **State Delta Management**: Efficiently stores only the changes (deltas) in blockchain
  state, instead of complete snapshots, with idempotent replay protection.
- **Merkle Tree Construction**: Provides cryptographic assurance for data integrity by
  hashing transactions into a Merkle tree structure, enabling efficient inclusion proofs.
- **Blockchain Pruning**: Operates on the real `Chain` - old blocks keep their header (so
  hash-linkage and Merkle-root commitments stay verifiable) but drop their transaction body.
- **SQLite Persistence**: Blocks, account state, and replay-protection state round-trip
  through a real SQLite database - `len(compressed_bytes)` is no longer the only number in
  the picture.
- **Pedersen Commitment + Schnorr Zero-Knowledge Proof**: A real (if not constant-time, not
  a SNARK) ZK primitive - `verify_proof` checks only the commitment, never the plaintext
  message or blinding factor.
- **Structured Metrics**: Compression ratio, pruning events, and proof latency, exportable
  as JSON or Prometheus text exposition format.

---

### Getting Started

#### Prerequisites
- **Python**: Version 3.10 or higher (uv installs it for you if missing)
- **[uv](https://docs.astral.sh/uv/)**: the project is managed entirely with uv — install it with
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
  (on Windows: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`)
- **Required Libraries**: The project itself relies only on Python's standard library
  (`lzma`, `sqlite3`, `hashlib`, `secrets`, ...) - no runtime dependencies. Development
  tools are declared in [PEP 735](https://peps.python.org/pep-0735/) `dependency-groups`:

  | Group       | Contents                      | Purpose                                 |
  | ----------- | ----------------------------- | --------------------------------------- |
  | `test`      | `pytest`, `pytest-cov`        | Test suite and coverage                 |
  | `lint`      | `ruff`                        | Linting, security rules, and formatting |
  | `typecheck` | `mypy`                        | Static type checking                    |
  | `dev`       | `test` + `lint` + `typecheck` | Default local environment               |

---

### Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/arec1b0/blockchain_compression_project.git
   ```

2. Navigate to the project directory:
   ```bash
   cd blockchain_compression_project
   ```

3. Create the virtual environment and install the project with its `dev` group
   (`dev` is the default group, so no flag is needed):
   ```bash
   uv sync
   ```

   To install only what a single task needs:
   ```bash
   uv sync --only-group test     # just pytest + pytest-cov
   uv sync --no-default-groups   # runtime only, no dev tooling
   ```

4. To run the project using the command-line interface (CLI):
   ```bash
   uv run blockchain-compress
   ```

5. To run the benchmarks:
   ```bash
   uv run blockchain-compress-bench          # LZMA compression ratio and throughput
   uv run blockchain-compress-bench-merkle   # Merkle proof length/latency vs. tree depth
   uv run blockchain-compress-bench-pruning  # pruning latency/memory vs. chain length
   uv run python -m blockchain_compression.benchmarks.scalability_benchmark  # full 4.x scalability suite
   ```

---

### Usage

Once installed, running the CLI walks through the whole system in order:

1. **Chain assembly**: builds a `Chain`, adds blocks with real transactions, and confirms
   `validate_chain()` passes.
2. **Merkle inclusion proof**: proves one transaction is committed under a real block's
   `merkle_root`, without needing the rest of the block.
3. **Persistence**: writes every block to a SQLite-backed `ChainStore`.
4. **Growth + pruning**: adds more blocks while a `BlockchainPruner` trims old bodies,
   keeping only the most recent full blocks - headers survive, so the chain stays valid.
5. **Reload**: reloads the chain from disk and re-validates it, pruned bodies and all.
6. **Zero-knowledge proof**: commits to a transaction with `PedersenCommitment`, then proves
   knowledge of it with a Schnorr proof that `verify_proof` checks *without* the plaintext.
7. **Metrics**: dumps everything recorded along the way as JSON and as Prometheus text.

To run the system, execute the demo module (or the `blockchain-compress` console script):

```bash
uv run python -m blockchain_compression.main
```

---

### Example Output

After running the program, you should see output like this (the metrics dump is long and
shown truncated below - the real output includes every counter/gauge/histogram):

```
=== Blockchain Data Compression and Management ===

--- Assembling a hash-linked Chain ---
Chain length (incl. genesis): 4
Account state: {'Alice': 80, 'Bob': 70, 'Carol': 75}
validate_chain(): True

--- Merkle Inclusion Proof (over a real block's transactions) ---
Proof for tx-003: 2 step(s)
Proof verifies against the tree's own root: True

--- Persisting to SQLite (...\chain.db) ---

--- Growing the chain and pruning under it ---
Pruned 3 block body(ies); keeping the last 2 full.
Pruned 1 block body(ies); keeping the last 2 full.
...
Chain length after growth: 9
Blocks with a full body: [7, 8]
validate_chain() after pruning: True

--- Reloading from disk ---
Reloaded chain length: 9
Reloaded state: {'Alice': 80, 'Bob': 95, 'Carol': 75}
validate_chain() on the reloaded chain (pruned bodies and all): True

--- Pedersen Commitment + Schnorr Zero-Knowledge Proof ---
Committed to a transaction without revealing it: C=475866447033...
verify_proof(proof, commitment) - no transaction data passed in at all: True
(contrast with the old mock's verify_proof(proof, data), which needed the plaintext to
'verify' - and so proved nothing was hidden)

--- Metrics ---
JSON:
{ "counters": [...], "gauges": [...], "histograms": [...] }
Prometheus text exposition format:
# HELP blocks_added_total Total blocks appended to the chain
# TYPE blocks_added_total counter
blocks_added_total 8.0
...
```

---

### Accounting Units, Limits & Schema Migration

- **Accounting Units & Numeric Type**: Account balances and transaction `amount` values use
  exact integer base units (`int`; `bool`, `float`, and `Decimal` values are rejected at the
  API boundary) to eliminate floating-point rounding drift and precision loss above \(2^{53}\).
- **Supported Range**: Amounts and cumulative balances must fit in a signed 64-bit integer
  (`[-2**63, 2**63 - 1]`, i.e., `-9_223_372_036_854_775_808` through
  `9_223_372_036_854_775_807`), matching SQLite's native `INTEGER` storage (`state_snapshot.balance`
  enforces `INTEGER NOT NULL CHECK (typeof(balance) = 'integer')`).
- **Schema Versioning & Migration (`PRAGMA user_version`)**:
  - New databases are initialized at schema version `2` (`blocks`, `state_snapshot`,
    `applied_tx_ids`, and optional `zk_attachments`).
  - Legacy unversioned databases (`PRAGMA user_version = 0`) whose `state_snapshot.balance`
    column used `REAL` are automatically migrated to v1 (`INTEGER` balances) inside a single
    SQLite transaction and then upgraded to v2 when opened by `ChainStore`.
  - Existing v1 databases (`PRAGMA user_version = 1`) are upgraded automatically to v2 by
    adding the optional `zk_attachments` table without modifying existing blocks or balances.
  - Migration from v0 converts finite, whole-number balances within `[-2**63, 2**63 - 1]` to
    exact `INTEGER` values (logging a warning if any legacy `REAL` exceeded `2**53`, where
    precision may already have been lost prior to upgrade).
  - Databases containing fractional, non-finite, or out-of-range legacy balances fail
    migration with a `ValueError` without altering the legacy table so operators can convert
    balances to exact integer base units manually.
  - Opening a database with a newer schema version (`PRAGMA user_version > 2`) raises a
    `ValueError`.
- **ZK Serialization & Optional Attachments**:
  - `Commitment` and `SchnorrProof` provide canonical fixed-width 512-char lowercase hex
    dictionary serialization (`to_dict()` / `from_dict()`, version `1`, group
    `rfc3526-modp-2048`) and versioned fixed-width binary codecs (`to_bytes()` /
    `from_bytes()`: 263 bytes with prefix `BCPCOM\x01` for commitments; 775 bytes with prefix
    `BCPZKP\x01` for proofs).
  - `ZKAttachment` is an optional, opt-in metadata container stored alongside blocks;
    attachments do not alter block header hashes, are not confidential transactions, and are
    not enforced by `Chain.validate_chain()` (call `Chain.verify_zk_attachments()` explicitly
    for opt-in proof verification).
- **Decompression Caps & Size-Aware Framing**:
  - `BlockCompressor` defaults to `preset=1` (`DEFAULT_PRESET`) and `min_compress_bytes=256` (`DEFAULT_COMPRESSION_THRESHOLD`), prepending a 1-byte versioned frame header (`FRAME_RAW = 0x00` for raw UTF-8 JSON payloads below threshold or incompressible data; `FRAME_LZMA = 0x01` for LZMA streams). Trade-off: `preset=1` greatly lowers per-block compression latency vs `preset=6`/`9` with a modest ratio trade-off on very large redundant blocks; callers needing maximum density can configure `preset=6`/`9` or `min_compress_bytes=0`.
  - Legacy unframed LZMA streams are auto-detected via `LZMA_MAGIC = 0xFD 0x37 0x7A 0x58 0x5A 0x00` for backward compatibility.
  - `BlockCompressor.decompress_block()` enforces `DEFAULT_MAX_DECOMPRESSED_BYTES = 16 MiB` (configurable via `max_decompressed_bytes`) on both `FRAME_RAW` and LZMA payloads, verifying stream EOF and absence of trailing bytes to prevent decompression-bomb OOMs.
- **SQLite WAL Concurrency, Durability, Tip Validation & Compaction**:
  - `ChainStore` opens SQLite with `PRAGMA journal_mode = WAL`, `PRAGMA busy_timeout = 5000`, `PRAGMA synchronous = NORMAL` (configurable via `synchronous=`), and `PRAGMA auto_vacuum = INCREMENTAL`.
  - **WAL sidecars & concurrency**: WAL creates `-wal` and `-shm` sidecar files alongside the database. Read paths (`load_chain`) pin point-in-time WAL snapshots with `BEGIN DEFERRED` (never blocking writers or upgrading to write locks), while write paths use `BEGIN IMMEDIATE` only *after* `Chain.prepare_block()` finishes CPU-intensive compression and hashing.
  - **Durability trade-off**: `synchronous = NORMAL` skips per-commit `fsync` on WAL writes (fast appends, safe against OS/process crashes, though the most recent un-checkpointed WAL transaction can roll back on sudden power loss); pass `synchronous="FULL"` for strict per-commit power-loss durability.
  - **Deferred compaction**: Logical pruning (`prune_chain` / `mark_pruned`) leaves freed pages on SQLite's freelist. Call `store.compact()` (or pass `compact=True` to `prune_chain`) to run `PRAGMA incremental_vacuum` and `PRAGMA wal_checkpoint(TRUNCATE)`. `auto_vacuum = INCREMENTAL` activates automatically only on newly created databases; pre-existing databases with `auto_vacuum = NONE` (or databases dominated by small `<1 KiB` inline rows on non-empty leaf pages) require a one-time `store.compact(full=True)` (`VACUUM`) to rewrite and compact leaf pages, whereas multi-KiB overflow-paged block bodies shrink immediately under `incremental_vacuum`.
  - **Tip validation & incremental pruning**: `ChainStore._ensure_chain_persisted()` verifies persisted `COUNT(*)` and tip `(idx, hash)` on append (assuming mid-history SQLite rows are not altered externally between appends; full history and body verification remains available via `load_chain(validate=True)` / `chain.validate_chain()`). `BlockchainPruner` maintains `_next_unpruned_idx` so repeated prune passes avoid rescanning already-pruned prefixes.
  - **Bounded replay cache**: `ChainStore` defaults `max_cached_tx_ids = 10_000`, configuring an LRU cache on `StateDelta` backed by SQLite's indexed `applied_tx_ids(tx_id)` table (`has_applied_tx`). Pruning preserves `applied_tx_ids` so replay protection stays intact across pruning and restarts. Once the RAM cache overflows, checking unseen `tx_id`s requires the backing `ChainStore` to remain open (failing closed with `RuntimeError` if closed); pass `max_cached_tx_ids=None` to `load_chain()` / `ChainStore()` for detached in-memory operation.
- **Metrics Cardinality & Thread Safety**:
  - `Chain.commit_block()` records constant-cardinality metrics (`blocks_added_total`, `block_raw_bytes_total`, `block_compressed_bytes_total`, `last_block_compression_ratio`, and the `block_compression_ratio` histogram) without unbounded per-block labels.
  - `MetricsRegistry` synchronizes all metric updates, reads, and JSON/Prometheus snapshot assembly using `threading.RLock`, while serializing JSON and formatting Prometheus text outside the lock.
- **Optional ZK Acceleration Evaluation**:
  - Zero-dependency pure Python (`powmod = pow`) remains the default, preserving RFC 3526 2048-bit MODP parameters (`GROUP_ID = "rfc3526-modp-2048"`) and proof wire formats.
  - Setting `BLOCKCHAIN_COMPRESSION_ZK_BACKEND=gmpy2` opts into `gmpy2.powmod` when `gmpy2` is installed (falling back with a warning to pure Python if absent), preserving full wire compatibility. Higher-speed elliptic-curve backends (`secp256k1` / `Ristretto255`) are documented for evaluation only and are not enabled automatically because switching groups breaks existing proofs and requires external crypto dependencies.
- **Canonical JSON & Scalar Domain Separation**:
  - `canonical_json()` enforces strict RFC 8259 JSON (`allow_nan=False`, no `default=str`),
    raising `TypeError` on non-JSON-native types (such as raw `bytes`) and `ValueError` on
    `NaN`/`Infinity`.
  - `hash_to_scalar()` prepends the versioned domain tag
    `b"blockchain-compression-project/tx-scalar/v1"` before hashing `canonical_json(data)`
    (changing this domain tag is a breaking change for derived scalars).
- **Cryptographic Validation**: `PedersenCommitment.commit()` requires `blinding` in `[1, Q)`
  (forbidding `blinding=0`), and `verify_proof()` enforces strict integer types, scalar range
  bounds (`0 <= s1, s2 < Q`), and order-`Q` subgroup membership (`pow(C, Q, P) == 1` and
  `pow(t, Q, P) == 1`, adding two 2048-bit modular exponentiations to verification).

---

### Development and Testing

If you want to contribute to the project or run tests, follow the steps below.

#### Running Unit Tests

We use **pytest** for testing (minimum enforced coverage: `fail_under = 90` in `pyproject.toml`), **ruff** (`E`, `F`, `W`, `I`, `B`, `UP`, `S`) for linting/security checks, and **mypy** (with PEP 561 `py.typed` packaged for downstream typed consumers). CI runs across both `ubuntu-latest` and `windows-latest` on Python 3.10–3.13, plus `uv lock --check` and `pip-audit`. To run checks locally:

```bash
uv run pytest --cov
uv run ruff check .
uv run ruff format --check src tests
uv run mypy src
uv lock --check
```

The tests cover every module: block/chain hash-linkage and pruning, SQLite persistence
round-trips (including atomicity across tables), Merkle tree integrity, the Pedersen
commitment and Schnorr proof (soundness, completeness, and hiding properties), and the
metrics registry's JSON/Prometheus exports.

---

### Contributing

We welcome contributions to the project. Please follow these steps:

1. Fork the repository.
2. Create a new feature branch.
3. Make changes and ensure all tests pass.
4. Submit a pull request with a clear description of your changes.

For more detailed guidelines, see our [CONTRIBUTING.md](CONTRIBUTING.md) file.

---

### License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

### Acknowledgments

Special thanks to the open-source community and Python developers who provided tools, libraries, and insights to support the development of this project.
