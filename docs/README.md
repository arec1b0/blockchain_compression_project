# Blockchain Compression Project

### Overview
The **Blockchain Compression Project** is designed to optimize blockchain data storage and management by incorporating a series of advanced techniques. The project focuses on reducing the storage overhead of full blockchain nodes, while maintaining data integrity, scalability, and security. It leverages block compression, state delta management, Merkle trees, and zero-knowledge proofs (ZK-SNARKs) to efficiently manage blockchain data without compromising the reliability of the system.

---

### Features
- **Block Compression**: Compresses blockchain block data using the LZMA algorithm to minimize storage requirements.
- **State Delta Management**: Efficiently stores only the changes (deltas) in blockchain states, instead of complete snapshots, reducing unnecessary data storage.
- **Merkle Tree Construction**: Provides cryptographic assurance for data integrity by hashing transactions into a Merkle tree structure, enabling efficient verification.
- **Blockchain Pruning**: Limits the number of stored blocks, removing old, unnecessary blocks, while retaining only the most recent blocks for operational efficiency.
- **Zero-Knowledge Proofs (ZK-SNARKs)**: A conceptual framework for privacy-preserving data verification without revealing underlying data, enabling the blockchain to verify transactions without exposing sensitive information.

---

### Getting Started

#### Prerequisites
- **Python**: Version 3.10 or higher (uv installs it for you if missing)
- **[uv](https://docs.astral.sh/uv/)**: the project is managed entirely with uv — install it with
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
  (on Windows: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`)
- **Required Libraries**: The project itself relies only on Python's standard library. Development
  tools are declared in [PEP 735](https://peps.python.org/pep-0735/) `dependency-groups`:

  | Group  | Contents                | Purpose                    |
  | ------ | ----------------------- | -------------------------- |
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

5. To run the compression benchmark (compression ratio and throughput across datasets and LZMA presets):
   ```bash
   uv run blockchain-compress-bench
   ```

---

### Usage

Once installed, you can run the project to simulate blockchain compression and management. The following components are demonstrated in the system:

1. **Block Compression**: Compresses and decompresses blockchain block data to reduce storage overhead.
2. **State Delta**: Applies transactions and tracks state changes efficiently.
3. **Merkle Tree**: Builds a Merkle tree from a list of transactions, allowing for efficient cryptographic verification of transaction data.
4. **Blockchain Pruning**: Removes outdated blocks, retaining only the most recent ones.
5. **ZK-SNARKs**: Demonstrates proof generation and verification using zero-knowledge proof concepts.

To run the system, execute the demo module (or the `blockchain-compress` console script):

```bash
uv run python -m blockchain_compression.main
```

---

### Example Output

After running the program, you should see outputs demonstrating various components:

```
=== Blockchain Data Compression and Management ===

--- Block Compression ---
Original block data: b'Sample blockchain block data' (28 bytes)
Compressed size: 84 bytes
Decompressed block data: b'Sample blockchain block data'

--- State Delta Storage ---
Transaction tx-001 already applied; ignoring replay.
Current state: {'Alice': 100, 'Bob': 50}

--- Merkle Tree Verification ---
Merkle Tree root hash: dc53b6c71a68a3277479b5a1ea820eb52bbae145ef8fb4e97a414b2b6c670f7f
Inclusion proof for 'tx3': [{'hash': '590b66ca...', 'position': 'right'}, {'hash': '60e1f5f6...', 'position': 'left'}]
Proof verifies: True

--- Blockchain Pruning ---
Pruned 1 old block(s); keeping the last 2.
Blocks after pruning: [{'block_number': 2, 'data': 'Block 2 data'}, {'block_number': 3, 'data': 'Block 3 data'}]

--- ZK-SNARK Proof Generation and Verification (mock) ---
Generated proof: {'proof': 'zk-snark-proof-placeholder', 'data_hash': 'd9e9b468...'}
Proof verification result: True
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

The tests ensure that each module functions correctly, covering block compression, state delta application, Merkle tree integrity, blockchain pruning, and ZK-SNARK proof verification.

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
