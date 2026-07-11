# **API Documentation**

This document provides detailed information on the various modules, classes, and functions in the **Blockchain Compression Project**. Each section explains the purpose, input parameters, and return values of each function, along with examples of how to use them.

---

## **Table of Contents**

1. [Compression Module](#compression-module)
    - [BlockCompressor](#blockcompressor)
2. [State Delta Module](#state-delta-module)
    - [StateDelta](#statedelta)
3. [Merkle Tree Module](#merkle-tree-module)
    - [MerkleTree](#merkletree)
4. [Blockchain Pruner Module](#blockchain-pruner-module)
    - [BlockchainPruner](#blockchainpruner)
5. [ZK-SNARK Module](#zk-snark-module)
    - [ZKSnark](#zksnark)

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
- **Parameters**: 
  - None.
- **Returns**: 
  - A dictionary representing the current state of the blockchain.
- **Example**:
  ```python
  current_state = state_delta.get_current_state()
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
- **Parameters**: 
  - None.
- **Returns**: 
  - The root hash as a hex string, or `None`.
- **Example**:
  ```python
  root_hash = merkle_tree.get_root()
  ```

##### `get_proof(self, index: int) -> list`
- **Description**: Returns the inclusion proof for the transaction at `index`.
- **Parameters**: 
  - `index` (int): Position of the transaction in the original list.
- **Returns**: 
  - A list of proof steps `{"hash": <hex digest>, "position": "left" | "right"}`, where `position` is the side of the sibling hash.
- **Raises**: `IndexError` if `index` is out of range.
- **Example**:
  ```python
  proof = merkle_tree.get_proof(2)
  ```

##### `verify_proof(cls, transaction: str, proof: list, root: str) -> bool`
- **Description**: Classmethod. Verifies that `transaction` is committed under `root` using `proof`. Does not require the full tree.
- **Parameters**: 
  - `transaction` (str): The transaction to verify.
  - `proof` (list): Proof steps produced by `get_proof`.
  - `root` (str): The expected root hash.
- **Returns**: 
  - `True` if the proof is valid, `False` otherwise.
- **Example**:
  ```python
  assert MerkleTree.verify_proof("tx3", proof, root_hash)
  ```

##### `hash_leaf(data: str) -> str` / `hash_node(left: str, right: str) -> str`
- **Description**: Static hashing primitives. `hash_leaf` hashes a transaction as a leaf (`0x00`-prefixed SHA-256); `hash_node` hashes two child digests as an internal node (`0x01`-prefixed SHA-256).
- **Example**:
  ```python
  leaf = MerkleTree.hash_leaf("tx1")
  ```

---

## Blockchain Pruner Module

### **BlockchainPruner**

The `BlockchainPruner` class manages the pruning (removal) of old blocks in the blockchain to reduce storage space. It ensures that only a specified number of recent blocks are retained.

#### **Methods:**

##### `__init__(self, max_blocks: int)`
- **Description**: Initializes the pruner with a maximum number of blocks to retain.
- **Parameters**: 
  - `max_blocks` (int): The maximum number of recent blocks to retain.
- **Example**:
  ```python
  pruner = BlockchainPruner(max_blocks=10)
  ```

##### `add_block(self, block: dict)`
- **Description**: Adds a new block to the blockchain and prunes old blocks if necessary.
- **Parameters**: 
  - `block` (dict): A dictionary representing a blockchain block. 
- **Example**:
  ```python
  block = {'block_number': 1, 'data': 'Block data'}
  pruner.add_block(block)
  ```

##### `get_blocks(self) -> list`
- **Description**: Returns the current list of blocks stored in the blockchain after pruning.
- **Parameters**: 
  - None.
- **Returns**: 
  - A list of dictionaries representing the retained blocks.
- **Example**:
  ```python
  blocks = pruner.get_blocks()
  ```

---

## ZK-SNARK Module

### **ZKSnark**

The `ZKSnark` class is a placeholder for implementing Zero-Knowledge Succinct Non-Interactive Arguments of Knowledge (ZK-SNARKs) for blockchain data verification.

#### **Methods:**

##### `generate_proof(self, data: dict) -> dict`
- **Description**: Generates a zero-knowledge proof for the given blockchain data. This is a placeholder and not a full ZK-SNARK implementation.
- **Parameters**: 
  - `data` (dict): A dictionary representing the blockchain data.
- **Returns**: 
  - A dictionary representing the generated proof.
- **Example**:
  ```python
  zk = ZKSnark()
  proof = zk.generate_proof({"account": "Alice", "balance": 100})
  ```

##### `verify_proof(self, proof: dict, data: dict) -> bool`
- **Description**: Verifies the given zero-knowledge proof against the provided blockchain data.
- **Parameters**: 
  - `proof` (dict): A dictionary representing the zero-knowledge proof.
  - `data` (dict): A dictionary representing the blockchain data to verify against.
- **Returns**: 
  - `True` if the proof is valid, `False` otherwise.
- **Example**:
  ```python
  is_valid = zk.verify_proof(proof, {"account": "Alice", "balance": 100})
  ```

##### `hash_data(data: dict) -> str`
- **Description**: Static method. Returns the SHA-256 hash of a canonical JSON encoding of the data, so dictionary key order cannot change the hash.
- **Parameters**: 
  - `data` (dict): The data to be hashed.
- **Returns**: 
  - A string representing the SHA-256 hash of the canonical encoding.
- **Example**:
  ```python
  hashed_data = ZKSnark.hash_data({"account": "Alice", "balance": 100})
  ```
