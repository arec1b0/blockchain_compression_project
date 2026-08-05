# test_chain_store.py

import sqlite3

import pytest

from blockchain_compression.chain import Chain
from blockchain_compression.persistence import ChainStore
from blockchain_compression.pruning import BlockchainPruner


def _tx(tx_id, account, amount):
    return {"tx_id": tx_id, "account": account, "amount": amount}


def _sync_block(store, chain, block, transactions):
    """Test-local helper mirroring how a caller derives store.append_block's
    arguments from a Chain.add_block() call - see docs/README.md for the
    documented pattern used by main.py."""
    tx_ids = [tx["tx_id"] for tx in transactions]
    accounts = {tx["account"] for tx in transactions}
    updated = {acct: chain.state.get_current_state()[acct] for acct in accounts}
    store.append_block(block, tx_ids, updated)


def _build_and_persist(store, num_blocks):
    chain = Chain()
    _sync_block(store, chain, chain.get_block(0), [])
    for i in range(num_blocks):
        txs = [_tx(f"tx-{i}", "Alice", 10)]
        block = chain.add_block(txs)
        _sync_block(store, chain, block, txs)
    return chain


def test_round_trip_preserves_chain_and_state(tmp_path):
    db_path = tmp_path / "chain.db"
    with ChainStore(db_path) as store:
        original = _build_and_persist(store, num_blocks=5)

    with ChainStore(db_path) as store:
        reloaded = store.load_chain()

    assert len(reloaded) == len(original)
    assert [b.hash for b in reloaded] == [b.hash for b in original]
    assert reloaded.state.get_current_state() == original.state.get_current_state()
    assert reloaded.state.get_applied_tx_ids() == original.state.get_applied_tx_ids()
    assert reloaded.validate_chain()


def test_reload_after_pruning_still_validates(tmp_path):
    db_path = tmp_path / "chain.db"
    with ChainStore(db_path) as store:
        chain = _build_and_persist(store, num_blocks=6)
        pruned = BlockchainPruner(chain, max_full_blocks=2).prune()
        for index in pruned:
            store.mark_pruned(index)

    assert pruned  # sanity: the test actually exercised pruning

    with ChainStore(db_path) as store:
        reloaded = store.load_chain()

    assert reloaded.validate_chain()
    for index in pruned:
        assert reloaded.get_block(index).is_pruned
        assert reloaded.get_block(index).transactions is None
    # Header fields survive even though the body is gone.
    assert reloaded.get_block(pruned[0]).hash == chain.get_block(pruned[0]).hash


def test_reload_preserves_full_body_for_retained_blocks(tmp_path):
    db_path = tmp_path / "chain.db"
    with ChainStore(db_path) as store:
        chain = _build_and_persist(store, num_blocks=4)
        for index in BlockchainPruner(chain, max_full_blocks=1).prune():
            store.mark_pruned(index)

    with ChainStore(db_path) as store:
        reloaded = store.load_chain()

    last = reloaded.get_block(len(reloaded) - 1)
    assert not last.is_pruned
    assert last.transactions == chain.get_block(len(chain) - 1).transactions
    assert last.verify_body()


def test_empty_store_loads_genesis_only_chain(tmp_path):
    db_path = tmp_path / "chain.db"
    with ChainStore(db_path):
        pass  # just create the schema, persist nothing

    with ChainStore(db_path) as store:
        reloaded = store.load_chain()

    assert len(reloaded) == 1
    assert reloaded.validate_chain()


def test_mark_pruned_missing_index_raises(tmp_path):
    db_path = tmp_path / "chain.db"
    with ChainStore(db_path) as store:
        with pytest.raises(KeyError):
            store.mark_pruned(999)


def test_replay_protection_survives_reload(tmp_path):
    db_path = tmp_path / "chain.db"
    with ChainStore(db_path) as store:
        _build_and_persist(store, num_blocks=1)

    with ChainStore(db_path) as store:
        reloaded = store.load_chain()

    # Replaying an already-applied tx_id after reload is still a no-op.
    assert reloaded.state.apply_transaction(_tx("tx-0", "Alice", 9999)) == {}
    assert reloaded.state.get_current_state() == {"Alice": 10.0}


def test_append_block_is_atomic_across_tables(tmp_path):
    db_path = tmp_path / "chain.db"
    with ChainStore(db_path) as store:
        chain = Chain()
        genesis = chain.get_block(0)
        block = chain.add_block([_tx("tx-1", "Alice", 10)])

        # A value sqlite3 can't bind (a list) should blow up the state_snapshot
        # write partway through the transaction - the block row from the same
        # call must not survive if the transaction correctly rolls back.
        with pytest.raises(sqlite3.ProgrammingError):
            store.append_block(block, ["tx-1"], {"Alice": [10]})

        _sync_block(store, chain, genesis, [])
        rows = store._conn.execute("SELECT idx FROM blocks").fetchall()
        assert [row["idx"] for row in rows] == [0]  # only genesis, block 1 rolled back


def test_context_manager_closes_connection(tmp_path):
    db_path = tmp_path / "chain.db"
    with ChainStore(db_path) as store:
        pass

    with pytest.raises(sqlite3.ProgrammingError):
        store._conn.execute("SELECT 1")
