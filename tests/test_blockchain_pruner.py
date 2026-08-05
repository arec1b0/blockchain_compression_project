# test_blockchain_pruner.py

import pytest

from blockchain_compression.chain import Chain
from blockchain_compression.observability import MetricsRegistry
from blockchain_compression.pruning import BlockchainPruner


def _tx(tx_id, account, amount):
    return {"tx_id": tx_id, "account": account, "amount": amount}


def _chain_with_blocks(n):
    chain = Chain()
    for i in range(n):
        chain.add_block([_tx(f"tx-{i}", "Alice", 1)])
    return chain


def test_prune_keeps_last_n_full_blocks():
    chain = _chain_with_blocks(4)  # genesis + 4 = 5 blocks, indices 0..4
    pruner = BlockchainPruner(chain, max_full_blocks=2)

    pruned = pruner.prune()

    assert pruned == [0, 1, 2]  # keep indices 3, 4 full
    assert all(chain.get_block(i).is_pruned for i in (0, 1, 2))
    assert not chain.get_block(3).is_pruned
    assert not chain.get_block(4).is_pruned


def test_prune_returns_empty_when_chain_shorter_than_limit():
    chain = _chain_with_blocks(2)  # 3 blocks total
    pruner = BlockchainPruner(chain, max_full_blocks=5)

    assert pruner.prune() == []
    assert all(not b.is_pruned for b in chain)


def test_chain_still_validates_after_pruning():
    chain = _chain_with_blocks(10)
    pruner = BlockchainPruner(chain, max_full_blocks=3)

    pruner.prune()

    assert chain.validate_chain()


def test_prune_is_idempotent():
    chain = _chain_with_blocks(5)
    pruner = BlockchainPruner(chain, max_full_blocks=2)

    first = pruner.prune()
    second = pruner.prune()  # nothing new qualifies

    assert first != []
    assert second == []


def test_prune_picks_up_newly_qualifying_blocks_after_growth():
    chain = _chain_with_blocks(2)  # 3 blocks total (0, 1, 2)
    pruner = BlockchainPruner(chain, max_full_blocks=3)
    assert pruner.prune() == []  # exactly at the limit, nothing qualifies yet

    chain.add_block([_tx("tx-new", "Bob", 1)])  # now 4 blocks total, index 0 qualifies

    assert pruner.prune() == [0]


def test_prune_records_metrics_when_given_a_registry():
    metrics = MetricsRegistry()
    chain = _chain_with_blocks(5)
    pruner = BlockchainPruner(chain, max_full_blocks=2, metrics=metrics)

    pruner.prune()

    assert metrics._counters[("pruning_events_total", ())] == 1.0
    assert metrics._counters[("blocks_pruned_total", ())] == 4.0


def test_prune_with_nothing_to_prune_records_no_metrics():
    metrics = MetricsRegistry()
    chain = _chain_with_blocks(1)
    pruner = BlockchainPruner(chain, max_full_blocks=5, metrics=metrics)

    pruner.prune()

    assert metrics._counters == {}


def test_invalid_max_full_blocks():
    chain = Chain()
    with pytest.raises(ValueError):
        BlockchainPruner(chain, max_full_blocks=0)
    with pytest.raises(ValueError):
        BlockchainPruner(chain, max_full_blocks=-1)
    with pytest.raises(TypeError):
        BlockchainPruner(chain, max_full_blocks="3")
    with pytest.raises(TypeError):
        BlockchainPruner(chain, max_full_blocks=True)


def test_invalid_chain_type():
    with pytest.raises(TypeError):
        BlockchainPruner([{"block_number": 1}], max_full_blocks=2)
