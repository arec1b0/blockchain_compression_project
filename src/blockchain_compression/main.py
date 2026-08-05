"""Demo entry point: a hash-linked Block/Chain, SQLite persistence, pruning over
the real chain, a Merkle inclusion proof, a Pedersen commitment + Schnorr
zero-knowledge proof, and a structured metrics dump - one coherent system, not
five disconnected utility demos.
"""

import logging
import tempfile
from pathlib import Path

from blockchain_compression.chain import Chain
from blockchain_compression.merkle import MerkleTree
from blockchain_compression.observability import get_metrics_registry
from blockchain_compression.persistence import ChainStore
from blockchain_compression.pruning import BlockchainPruner
from blockchain_compression.zk_proof import (
    PedersenCommitment,
    generate_proof,
    hash_to_scalar,
    verify_proof,
)

logger = logging.getLogger(__name__)

_INITIAL_BATCHES = [
    [{"tx_id": "tx-001", "account": "Alice", "amount": 100}],
    [{"tx_id": "tx-002", "account": "Bob", "amount": 50}],
    [
        {"tx_id": "tx-003", "account": "Alice", "amount": -30},
        {"tx_id": "tx-004", "account": "Bob", "amount": 20},
        {"tx_id": "tx-005", "account": "Carol", "amount": 75},
        {"tx_id": "tx-006", "account": "Alice", "amount": 10},
    ],
]


def _sync_block(store: ChainStore, chain: Chain, block, transactions: list) -> None:
    """Persist a block plus the state changes its transactions caused.

    ``ChainStore.append_block`` is deliberately decoupled from ``Chain`` - it
    has no way to derive these from a ``Chain.add_block()`` call on its own,
    so the caller bridges the two explicitly, once, right here.
    """
    tx_ids = [tx["tx_id"] for tx in transactions]
    accounts = {tx["account"] for tx in transactions}
    updated_balances = {account: chain.state.get_current_state()[account] for account in accounts}
    store.append_block(block, tx_ids, updated_balances)


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    metrics = get_metrics_registry()

    logger.info("=== Blockchain Data Compression and Management ===")

    logger.info("\n--- Assembling a hash-linked Chain ---")
    chain = Chain(metrics=metrics)
    for batch in _INITIAL_BATCHES:
        chain.add_block(batch)
    logger.info("Chain length (incl. genesis): %d", len(chain))
    logger.info("Account state: %s", chain.state.get_current_state())
    logger.info("validate_chain(): %s", chain.validate_chain())

    logger.info("\n--- Merkle Inclusion Proof (over a real block's transactions) ---")
    proof_block = chain.get_block(3)  # the 4-transaction batch above

    def _leaf(tx):
        return f"{tx['tx_id']}:{tx['account']}:{tx['amount']}"

    # Rebuilding a MerkleTree from the block's own transactions reproduces
    # merkle_root only because Block.create used the same canonicalization -
    # this is exactly what Block.verify_body() checks internally.
    leaves = [_leaf(tx) for tx in proof_block.transactions]
    tree = MerkleTree(leaves)
    proof = tree.get_proof(0)
    verified = MerkleTree.verify_proof(leaves[0], proof, tree.get_root())
    logger.info("Proof for %s: %d step(s)", proof_block.transactions[0]["tx_id"], len(proof))
    logger.info("Proof verifies against the tree's own root: %s", verified)

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "chain.db"

        logger.info("\n--- Persisting to SQLite (%s) ---", db_path)
        with ChainStore(db_path) as store:
            _sync_block(store, chain, chain.get_block(0), [])
            for index, batch in enumerate(_INITIAL_BATCHES, start=1):
                _sync_block(store, chain, chain.get_block(index), batch)

            logger.info("\n--- Growing the chain and pruning under it ---")
            pruner = BlockchainPruner(chain, max_full_blocks=2, metrics=metrics)
            for i in range(4, 9):
                # "tx-grow-*" - distinct from the "tx-00N" ids already used above,
                # so this loop can't accidentally collide with them as replays.
                batch = [{"tx_id": f"tx-grow-{i:03d}", "account": "Bob", "amount": 5}]
                chain.add_block(batch)
                _sync_block(store, chain, chain.get_block(i), batch)
                for pruned_index in pruner.prune():
                    store.mark_pruned(pruned_index)
            logger.info("Chain length after growth: %d", len(chain))
            logger.info("Blocks with a full body: %s", [b.index for b in chain if not b.is_pruned])
            logger.info("validate_chain() after pruning: %s", chain.validate_chain())

        logger.info("\n--- Reloading from disk ---")
        with ChainStore(db_path) as store:
            reloaded = store.load_chain()
        logger.info("Reloaded chain length: %d", len(reloaded))
        logger.info("Reloaded state: %s", reloaded.state.get_current_state())
        logger.info(
            "validate_chain() on the reloaded chain (pruned bodies and all): %s",
            reloaded.validate_chain(),
        )

    logger.info("\n--- Pedersen Commitment + Schnorr Zero-Knowledge Proof ---")
    secret_tx = _INITIAL_BATCHES[0][0]  # {"tx_id": "tx-001", "account": "Alice", "amount": 100}
    message = hash_to_scalar(secret_tx)
    pedersen = PedersenCommitment()
    commitment, blinding = pedersen.commit(message)
    zk_proof = generate_proof(
        message=message, blinding=blinding, commitment=commitment, metrics=metrics
    )
    is_valid = verify_proof(zk_proof, commitment, metrics=metrics)
    logger.info(
        "Committed to a transaction without revealing it: C=%d...", commitment.value % 10**12
    )
    logger.info(
        "verify_proof(proof, commitment) - no transaction data passed in at all: %s", is_valid
    )
    logger.info(
        "(contrast with the old mock's verify_proof(proof, data), which needed the plaintext"
        " to 'verify' - and so proved nothing was hidden)"
    )

    logger.info("\n--- Metrics ---")
    logger.info("JSON:\n%s", metrics.to_json())
    logger.info("Prometheus text exposition format:\n%s", metrics.to_prometheus_text())


if __name__ == "__main__":
    main()
