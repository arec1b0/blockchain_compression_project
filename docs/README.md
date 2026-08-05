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

  | Group  | Contents                | Purpose                    |
  | ------ | ----------------------- | --------------------------- |
  | `test` | `pytest`, `pytest-cov`  | Test suite and coverage    |
  | `lint` | `ruff`                  | Linting and import sorting |
  | `dev`  | `test` + `lint`         | Default local environment  |

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
Reloaded state: {'Alice': 80.0, 'Bob': 95.0, 'Carol': 75.0}
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

### Development and Testing

If you want to contribute to the project or run tests, follow the steps below.

#### Running Unit Tests

We use **pytest** for testing. To run all tests, use the following command:

```bash
uv run pytest
```

With coverage:

```bash
uv run pytest --cov=src
```

Linting is handled by **ruff** (`lint` group):

```bash
uv run ruff check .
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
