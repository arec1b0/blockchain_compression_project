"""Demo entry point: a hash-linked Block/Chain, SQLite persistence, pruning over
the real chain, a Merkle inclusion proof, a Pedersen commitment + Schnorr
zero-knowledge proof, and a structured metrics dump - one coherent system, not
five disconnected utility demos.
"""

import argparse
import logging
import tempfile
from contextlib import nullcontext
from pathlib import Path

from blockchain_compression.chain import Block, Chain
from blockchain_compression.compression import extract_block_delta
from blockchain_compression.merkle import MerkleTree
from blockchain_compression.observability import (
    LOG_FORMATS,
    LOG_LEVEL_ENV_VAR,
    LOG_LEVELS,
    configure_logging,
    get_metrics_registry,
    resolve_log_level,
)
from blockchain_compression.persistence import ChainStore
from blockchain_compression.pruning import BlockchainPruner
from blockchain_compression.zk_proof import (
    Commitment,
    PedersenCommitment,
    SchnorrProof,
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


def _sync_block(store: ChainStore, chain: Chain, block: Block, transactions: list) -> None:
    """Persist a block plus the state changes its transactions caused.

    ``ChainStore.append_block`` is deliberately decoupled from ``Chain`` - it
    has no way to derive these from a ``Chain.add_block()`` call on its own,
    so the caller bridges the two explicitly using :func:`extract_block_delta`.
    """
    delta = extract_block_delta(transactions, chain.state)
    store.append_block(block, delta.tx_ids, delta.updated_accounts)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blockchain-compress",
        description=(
            "Demonstrate compressed hash-linked chain persistence, pruning, ZK proofs, and metrics."
        ),
    )
    parser.add_argument(
        "--max-full-blocks",
        type=int,
        default=2,
        help="Number of recent full block bodies to keep when pruning (minimum 1).",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Optional SQLite database file path (uses a temporary directory by default).",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        help=(
            f"Logging verbosity ({', '.join(LOG_LEVELS)}). "
            f"Overrides {LOG_LEVEL_ENV_VAR} (default: INFO)."
        ),
    )
    parser.add_argument(
        "--log-format",
        choices=LOG_FORMATS,
        default="text",
        help="Log output format: plain text (default) or single-line JSON.",
    )
    parser.add_argument(
        "--verify-integrity",
        action="store_true",
        default=False,
        help="Run ChainStore.verify_integrity() diagnostics when reopening the persisted DB.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Run the end-to-end CLI walkthrough with optional retention/storage/logging flags."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.max_full_blocks < 1:
        parser.error("--max-full-blocks must be at least 1")
    try:
        resolved_level = resolve_log_level(args.log_level)
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))

    configure_logging(resolved_level, log_format=args.log_format)
    metrics = get_metrics_registry()
    logger.debug(
        "CLI options: max_full_blocks=%d, db_path=%s, log_level=%s, "
        "log_format=%s, verify_integrity=%s",
        args.max_full_blocks,
        args.db_path,
        resolved_level,
        args.log_format,
        args.verify_integrity,
    )

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
    if proof_block.transactions is None:
        raise RuntimeError("expected transactions to be present on unpruned proof block")

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

    db_ctx = (
        tempfile.TemporaryDirectory()
        if args.db_path is None
        else nullcontext(str(args.db_path.parent))
    )
    with db_ctx as tmp_dir:
        db_path = Path(tmp_dir) / "chain.db" if args.db_path is None else args.db_path
        if args.db_path is not None:
            db_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info("\n--- Persisting to SQLite (%s) ---", db_path)
        with ChainStore(db_path) as store:
            _sync_block(store, chain, chain.get_block(0), [])
            for index, batch in enumerate(_INITIAL_BATCHES, start=1):
                _sync_block(store, chain, chain.get_block(index), batch)

            logger.info("\n--- Growing the chain and pruning under it ---")
            pruner = BlockchainPruner(chain, max_full_blocks=args.max_full_blocks, metrics=metrics)
            for i in range(4, 9):
                # "tx-grow-*" - distinct from the "tx-00N" ids already used above,
                # so this loop can't accidentally collide with them as replays.
                batch = [{"tx_id": f"tx-grow-{i:03d}", "account": "Bob", "amount": 5}]
                store.add_block(chain, batch)
                store.prune_chain(pruner)
            store.compact()
            logger.info("Chain length after growth: %d", len(chain))
            logger.info("Blocks with a full body: %s", [b.index for b in chain if not b.is_pruned])
            logger.info("validate_chain() after pruning: %s", chain.validate_chain())

        logger.info("\n--- Reloading from disk ---")
        with ChainStore(db_path) as store:
            if args.verify_integrity:
                report = store.verify_integrity(check_bodies=True, raise_on_error=True)
                logger.info(
                    "Integrity check passed: blocks=%d, schema_version=%d, indexes=%s",
                    report.block_count,
                    report.schema_version,
                    list(report.indexes),
                )
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
    restored_commitment = Commitment.from_dict(commitment.to_dict())
    restored_proof = SchnorrProof.from_dict(zk_proof.to_dict())
    is_valid = verify_proof(restored_proof, restored_commitment, metrics=metrics)
    logger.info(
        "Committed to a transaction without revealing it: C=%d...", commitment.value % 10**12
    )
    logger.info(
        "verify_proof(proof, commitment) after dict round-trip (no plaintext): %s", is_valid
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
