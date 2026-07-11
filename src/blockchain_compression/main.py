"""Demo entry point wiring together block compression, state deltas,
Merkle trees, pruning, and the mock ZK-SNARK interface."""

import logging

from blockchain_compression.compression import BlockCompressor, StateDelta
from blockchain_compression.merkle import MerkleTree
from blockchain_compression.pruning import BlockchainPruner
from blockchain_compression.zk_snark import ZKSnark

logger = logging.getLogger(__name__)


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    logger.info("=== Blockchain Data Compression and Management ===")

    logger.info("\n--- Block Compression ---")
    compressor = BlockCompressor()
    block_data = b"Sample blockchain block data"
    compressed_block = compressor.compress_block(block_data)
    decompressed_block = compressor.decompress_block(compressed_block)
    logger.info("Original block data: %s (%d bytes)", block_data, len(block_data))
    logger.info("Compressed size: %d bytes", len(compressed_block))
    logger.info("Decompressed block data: %s", decompressed_block)

    logger.info("\n--- State Delta Storage ---")
    state_delta = StateDelta()
    transaction_1 = {"tx_id": "tx-001", "account": "Alice", "amount": 100}
    transaction_2 = {"tx_id": "tx-002", "account": "Bob", "amount": 50}
    state_delta.apply_transaction(transaction_1)
    state_delta.apply_transaction(transaction_2)
    state_delta.apply_transaction(transaction_1)  # replay is ignored (idempotent)
    logger.info("Current state: %s", state_delta.get_current_state())

    logger.info("\n--- Merkle Tree Verification ---")
    transactions = ["tx1", "tx2", "tx3", "tx4"]
    merkle_tree = MerkleTree(transactions)
    root_hash = merkle_tree.get_root()
    logger.info("Merkle Tree root hash: %s", root_hash)
    proof = merkle_tree.get_proof(2)
    logger.info("Inclusion proof for 'tx3': %s", proof)
    logger.info("Proof verifies: %s", MerkleTree.verify_proof("tx3", proof, root_hash))

    logger.info("\n--- Blockchain Pruning ---")
    pruner = BlockchainPruner(max_blocks=2)
    for number in (1, 2, 3):
        pruner.add_block({"block_number": number, "data": f"Block {number} data"})
    logger.info("Blocks after pruning: %s", pruner.get_blocks())

    logger.info("\n--- ZK-SNARK Proof Generation and Verification (mock) ---")
    zk = ZKSnark()
    zk_proof = zk.generate_proof(transaction_1)
    logger.info("Generated proof: %s", zk_proof)
    logger.info("Proof verification result: %s", zk.verify_proof(zk_proof, transaction_1))


if __name__ == "__main__":
    main()
