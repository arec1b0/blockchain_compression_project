# **API Documentation**

This document provides detailed information on the various modules, classes, and functions in the **Blockchain Compression Project**. Each section explains the purpose, input parameters, and return values of each function, along with examples of how to use them.

---

## **Table of Contents**

1. [Chain Module](#chain-module)
    - [Block](#block)
    - [Chain](#chain)
2. [Compression Module](#compression-module)
    - [BlockCompressor](#blockcompressor)
3. [State Delta Module](#state-delta-module)
    - [StateDelta](#statedelta)
4. [Merkle Tree Module](#merkle-tree-module)
    - [MerkleTree](#merkletree)
5. [Blockchain Pruner Module](#blockchain-pruner-module)
    - [BlockchainPruner](#blockchainpruner)
6. [Persistence Module](#persistence-module)
    - [ChainStore](#chainstore)
7. [ZK Proof Module](#zk-proof-module)
    - [PedersenCommitment](#pedersencommitment)
    - [Schnorr proof functions](#schnorr-proof-functions)
8. [Observability Module](#observability-module)
    - [MetricsRegistry](#metricsregistry)

---

## Chain Module

### **Block**

A single block: a header (`index`, `prev_hash`, `timestamp`, `merkle_root`, `hash`)
hash-linked to its predecessor, plus an LZMA-compressed transaction body. The header hash
covers only the header fields - never the body - so a block's identity survives its body
being pruned away.

#### **Methods:**

##### `Block.create(index: int, prev_hash: str, transactions: list, timestamp: float | None = None) -> Block`
- **Description**: Classmethod. Builds a fresh block from raw transactions. The Merkle root
  and the compressed body are derived from the same canonical per-transaction JSON strings,
  so a decompressed body is always guaranteed to reproduce `merkle_root`.
- **Raises**: `TypeError` if `transactions` is not a list.
- **Example**:
  ```python
  from blockchain_compression.chain import Block

  block = Block.create(index=1, prev_hash="0" * 64, transactions=[
      {"tx_id": "tx-1", "account": "Alice", "amount": 100}
  ])
  ```

##### `Block.from_persisted(*, index, prev_hash, merkle_root, hash, timestamp, compressed_body, is_pruned) -> Block`
- **Description**: Classmethod. Reconstructs a block from trusted storage *without*
  recomputing its hash - used by `ChainStore.load_chain()`. A decompression/parse failure
  on a present body is swallowed (`transactions` stays `None`) rather than raised, so it
  surfaces as a `verify_body()` failure instead of crashing the reload.

##### `prune(self) -> None`
- **Description**: Drops the body (`transactions` and `compressed_body` become `None`,
  `is_pruned` becomes `True`) while keeping every header field, so hash-linkage stays
  verifiable. The only method that may mutate a `Block` after construction.

##### `verify_body(self) -> bool`
- **Description**: `True` trivially for a pruned block. `False` if the body is missing
  without being marked pruned (corruption), or if a present body's content doesn't
  reproduce `merkle_root` (tampering). Never raises on bad data.

##### `Block.compute_hash(index, prev_hash, merkle_root, timestamp) -> str`
- **Description**: Static method. SHA-256 over the canonical header fields - the one place
  this concatenation happens, used both by `create()` and by `Chain.validate_chain()`.

---

### **Chain**

A hash-linked, genesis-first sequence of `Block`s with an attached `StateDelta` tracking
cumulative account state.

#### **Methods:**

##### `__init__(self, state: StateDelta | None = None, metrics: MetricsRegistry | None = None)`
- **Description**: Creates a chain with a genesis block. `state` defaults to a fresh
  `StateDelta`; `metrics` defaults to `None` (no metrics recorded).
- **Example**:
  ```python
  from blockchain_compression.chain import Chain

  chain = Chain()
  ```

##### `add_block(self, transactions: list) -> Block`
- **Description**: Validates every transaction (via `StateDelta.validate_transaction`)
  *before* applying any of them, then applies them all and appends the new block. A
  malformed transaction later in the batch can't leave state partially mutated with no
  corresponding block. Records `blocks_added_total` and a per-block `compression_ratio`
  gauge if `metrics` was provided.
- **Raises**: `TypeError` if `transactions` isn't a list; whatever `StateDelta.validate_transaction` raises for a malformed transaction.
- **Example**:
  ```python
  block = chain.add_block([{"tx_id": "tx-1", "account": "Alice", "amount": 100}])
  ```

##### `validate_chain(self, check_bodies: bool = True) -> bool`
- **Description**: Walks the chain checking index continuity, `prev_hash` linkage,
  header-hash recomputation, and (if `check_bodies`) that every non-pruned block's body
  still matches its header via `Block.verify_body()`.
- **Example**:
  ```python
  assert chain.validate_chain()
  ```

##### `get_block(self, index: int) -> Block`
- **Description**: Returns the block at `index`.
- **Raises**: `IndexError` if out of range.

##### `state` (property) -> `StateDelta`
- **Description**: The chain's cumulative account state.

##### `__len__(self)` / `__iter__(self)`
- **Description**: Number of blocks (including genesis); iterates blocks oldest-first.

---

## Compression Module

### **BlockCompressor**

The `BlockCompressor` class provides functionality to compress and decompress blockchain block data using the LZMA algorithm.

#### **Methods:**

##### `compress_block(self, block_data: bytes) -> bytes`
- **Description**: Compresses the raw blockchain block data using the LZMA algorithm.
- **Parameters**: 
  - `block_data` (bytes): The raw data of the blockchain block to be compressed.
- **Returns**: 
  - Compressed block data (bytes).
- **Example**:
  ```python
  compressor = BlockCompressor()
  compressed_data = compressor.compress_block(b"Sample blockchain block data")
  ```

##### `decompress_block(self, compressed_block_data: bytes) -> bytes`
- **Description**: Decompresses the compressed blockchain block data back to its original form.
- **Parameters**: 
  - `compressed_block_data` (bytes): The compressed block data.
- **Returns**: 
  - Decompressed block data (bytes).
- **Example**:
  ```python
  decompressed_data = compressor.decompress_block(compressed_data)
  ```

---

## State Delta Module

### **StateDelta**

The `StateDelta` class handles the storage and management of state changes (deltas) in the blockchain. Instead of storing the full state, it keeps only the changes applied by each transaction.

#### **Methods:**

##### `apply_transaction(self, transaction: dict) -> dict`
- **Description**: Applies a transaction to the blockchain state and stores only the delta (difference). Application is **idempotent**: replaying a transaction with an already-applied `tx_id` is a no-op and returns an empty dict.
- **Parameters**: 
  - `transaction` (dict): A dictionary representing the transaction to apply. Must contain the keys:
    - `tx_id`: A unique, non-empty string identifying the transaction (used for replay protection).
    - `account`: The account to update (non-empty string).
    - `amount`: The numeric change to apply to the account.
- **Returns**: 
  - A dictionary representing the delta (difference) applied to the state, or `{}` if the transaction was already applied.
- **Raises**: `TypeError` / `ValueError` if the transaction is malformed.
- **Example**:
  ```python
  from blockchain_compression.compression import StateDelta

  state_delta = StateDelta()
  delta = state_delta.apply_transaction({'tx_id': 'tx-001', 'account': 'Alice', 'amount': 100})
  ```

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

##### `verify_proof(cls, transaction: str, proof: list, root: str) -> bool`
- **Description**: Classmethod. Verifies that `transaction` is committed under `root` using `proof`. Does not require the full tree.
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

##### `prune(self) -> list`
- **Description**: Prunes every not-yet-pruned block older than the last `max_full_blocks`. Returns the list of newly-pruned block indices (empty if none qualified). Records `pruning_events_total` / `blocks_pruned_total` if `metrics` was provided.
- **Example**:
  ```python
  pruned_indices = pruner.prune()
  assert chain.validate_chain()  # headers survive pruning
  ```

---

## Persistence Module

### **ChainStore**

SQLite-backed persistence for a `Chain`. Three tables back a round-trippable chain:
`blocks` (headers + optional body), `state_snapshot` (current account balances), and
`applied_tx_ids` (replay-protection membership). The latter two aren't a cache: once a
block is pruned, they're the *only* surviving record of what its transactions did.

#### **Methods:**

##### `__init__(self, path: str | Path)`
- **Description**: Opens (creating if needed) a SQLite database at `path` and ensures the schema exists. Also usable as a context manager (`with ChainStore(path) as store:`).

##### `append_block(self, block: Block, applied_tx_ids: list, updated_accounts: dict) -> None`
- **Description**: Persists `block` plus the state changes it caused, as one atomic transaction across all three tables - a crash mid-write can't leave a block recorded with no matching state update.
- **Example**:
  ```python
  from blockchain_compression.persistence import ChainStore

  with ChainStore("chain.db") as store:
      block = chain.add_block(transactions)
      tx_ids = [tx["tx_id"] for tx in transactions]
      updated = {tx["account"]: chain.state.get_current_state()[tx["account"]] for tx in transactions}
      store.append_block(block, tx_ids, updated)
  ```

##### `mark_pruned(self, index: int) -> None`
- **Description**: Mirrors an in-memory `Block.prune()` in storage: drops the persisted body and sets `is_pruned`.
- **Raises**: `KeyError` if no block with `index` is persisted.

##### `load_chain(self) -> Chain`
- **Description**: Reconstructs a full `Chain` from storage, headers and all, hydrating its `StateDelta` from `state_snapshot`/`applied_tx_ids` (never by replaying bodies, which may be pruned).
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

### **PedersenCommitment**

##### `commit(self, message: int, blinding: int | None = None) -> tuple[Commitment, int]`
- **Description**: Commits to an integer `message` in `[0, Q)`. `blinding` defaults to a fresh cryptographically random value if not given.
- **Raises**: `ValueError` if `message`/`blinding` are out of `[0, Q)` - rejected outright, never silently reduced.
- **Example**:
  ```python
  from blockchain_compression.zk_proof import PedersenCommitment

  pedersen = PedersenCommitment()
  commitment, blinding = pedersen.commit(42)
  ```

##### `verify_opening(self, commitment: Commitment, message: int, blinding: int) -> bool`
- **Description**: Checks that `(message, blinding)` is a valid opening of `commitment` - this reveals the message, unlike the Schnorr proof below.

### Schnorr proof functions

##### `generate_proof(message: int, blinding: int, commitment: Commitment, metrics=None) -> SchnorrProof`
- **Description**: Proves knowledge of `(message, blinding)` behind `commitment` without revealing them. Samples fresh nonces on every call - a Schnorr nonce must never be reused. Records a `proof_generation_seconds` histogram if `metrics` is given.
- **Example**:
  ```python
  from blockchain_compression.zk_proof import generate_proof, verify_proof

  proof = generate_proof(message=42, blinding=blinding, commitment=commitment)
  ```

##### `verify_proof(proof: SchnorrProof, commitment: Commitment, metrics=None) -> bool`
- **Description**: Verifies `proof` against `commitment` alone. Independently recomputes the Fiat-Shamir challenge; never trusts a transmitted value for it.
- **Example**:
  ```python
  assert verify_proof(proof, commitment)  # no message/blinding needed
  ```

##### `hash_to_scalar(data: dict) -> int`
- **Description**: Maps arbitrary blockchain data (e.g. a transaction dict) to a Pedersen-committable scalar via canonical JSON + SHA-256, mod `Q`.
- **Example**:
  ```python
  from blockchain_compression.zk_proof import hash_to_scalar

  message = hash_to_scalar({"tx_id": "tx-1", "account": "Alice", "amount": 100})
  ```

---

## Observability Module

### **MetricsRegistry**

An in-process metrics registry: counters, gauges, and histograms, exportable as JSON or
Prometheus text exposition format. Passed explicitly (`metrics=...`) to `Chain`,
`BlockchainPruner`, and the ZK proof functions - `None` (the default everywhere) is a true
no-op, so nothing is recorded unless a registry is supplied.

#### **Methods:**

##### `increment_counter(self, name, value=1.0, labels=None, help_text=None) -> None`
- **Description**: Increments a monotonic counter. `value` must be non-negative.

##### `set_gauge(self, name, value, labels=None, help_text=None) -> None`
- **Description**: Sets a point-in-time value.

##### `observe_histogram(self, name, value, labels=None, help_text=None, buckets=None) -> None`
- **Description**: Records one observation into cumulative buckets (`buckets` only takes effect on a metric's first observation).

##### `to_json(self, indent: int | None = 2) -> str`
- **Description**: A structured snapshot of every metric family as one JSON document.

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
