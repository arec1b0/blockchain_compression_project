# test_chain.py

import pytest

from blockchain_compression.chain import Block, Chain
from blockchain_compression.observability import MetricsRegistry


def _tx(tx_id, account, amount):
    return {"tx_id": tx_id, "account": account, "amount": amount}


def test_genesis_block():
    chain = Chain()
    assert len(chain) == 1
    genesis = chain.get_block(0)
    assert genesis.index == 0
    assert genesis.prev_hash == Chain.GENESIS_PREV_HASH
    assert genesis.merkle_root is None
    assert chain.validate_chain()


def test_add_block_links_to_previous():
    chain = Chain()
    block1 = chain.add_block([_tx("tx-1", "Alice", 100)])
    block2 = chain.add_block([_tx("tx-2", "Bob", 50)])

    assert block1.index == 1
    assert block1.prev_hash == chain.get_block(0).hash
    assert block2.index == 2
    assert block2.prev_hash == block1.hash


def test_add_block_applies_transactions_to_state():
    chain = Chain()
    chain.add_block([_tx("tx-1", "Alice", 100), _tx("tx-2", "Bob", 50)])
    chain.add_block([_tx("tx-3", "Alice", -30)])

    assert chain.state.get_current_state() == {"Alice": 70, "Bob": 50}


def test_add_block_validates_before_mutating_state():
    chain = Chain()
    with pytest.raises(ValueError):
        chain.add_block([_tx("tx-1", "Alice", 100), _tx("tx-2", "Bob", "not-a-number")])

    # Nothing from the failed batch was applied, and no block was appended.
    assert len(chain) == 1
    assert chain.state.get_current_state() == {}


def test_add_block_type_validation():
    chain = Chain()
    with pytest.raises(TypeError):
        chain.add_block("not a list")


def test_validate_chain_detects_tampered_header_field():
    chain = Chain()
    chain.add_block([_tx("tx-1", "Alice", 100)])
    chain.get_block(1).prev_hash = "f" * 64

    assert not chain.validate_chain()


def test_validate_chain_detects_tampered_body_without_header_change():
    chain = Chain()
    chain.add_block([_tx("tx-1", "Alice", 100)])
    block = chain.get_block(1)
    original_hash = block.hash
    block.transactions = [_tx("tx-1", "Alice", 999)]  # tampered, header untouched

    assert block.hash == original_hash  # header hash alone wouldn't catch this
    assert not chain.validate_chain(check_bodies=True)
    assert chain.validate_chain(check_bodies=False)  # header-only check is blind to it


def test_validate_chain_detects_tampered_hash_with_valid_linkage():
    # prev_hash still correctly links to the real predecessor, but the block's
    # own stored hash doesn't match its (untampered) header fields - a
    # different failure path than a broken prev_hash link.
    chain = Chain()
    chain.add_block([_tx("tx-1", "Alice", 100)])
    chain.get_block(1).hash = "f" * 64

    assert not chain.validate_chain()


def test_validate_chain_detects_broken_index_continuity():
    chain = Chain()
    chain.add_block([_tx("tx-1", "Alice", 100)])
    chain.get_block(1).index = 5

    assert not chain.validate_chain()


def _header_fields(block):
    return (block.index, block.prev_hash, block.merkle_root, block.hash, block.timestamp)


def test_prune_keeps_header_and_chain_still_validates():
    chain = Chain()
    chain.add_block([_tx("tx-1", "Alice", 100)])
    block = chain.get_block(1)
    header_fields = _header_fields(block)

    block.prune()

    assert block.is_pruned
    assert block.transactions is None
    assert block.compressed_body is None
    assert _header_fields(block) == header_fields
    assert chain.validate_chain()


def test_verify_body_true_for_pruned_block():
    block = Block.create(
        index=0, prev_hash=Chain.GENESIS_PREV_HASH, transactions=[_tx("tx-1", "Alice", 1)]
    )
    block.prune()
    assert block.verify_body()


def test_verify_body_false_for_corrupted_non_pruned_block():
    block = Block.create(
        index=0, prev_hash=Chain.GENESIS_PREV_HASH, transactions=[_tx("tx-1", "Alice", 1)]
    )
    block.compressed_body = None  # missing body, but never marked pruned

    assert not block.verify_body()


def test_verify_body_false_when_body_bytes_do_not_decompress():
    block = Block.create(
        index=0, prev_hash=Chain.GENESIS_PREV_HASH, transactions=[_tx("tx-1", "Alice", 1)]
    )
    # transactions is still populated, but compressed_body is not valid LZMA -
    # exercises verify_body's own decompression try/except, distinct from the
    # "body missing entirely" case above.
    block.compressed_body = b"not valid lzma data"

    assert not block.verify_body()


def test_block_from_persisted_round_trip():
    original = Block.create(
        index=0, prev_hash=Chain.GENESIS_PREV_HASH, transactions=[_tx("tx-1", "Alice", 1)]
    )

    reloaded = Block.from_persisted(
        index=original.index,
        prev_hash=original.prev_hash,
        merkle_root=original.merkle_root,
        hash=original.hash,
        timestamp=original.timestamp,
        compressed_body=original.compressed_body,
        is_pruned=False,
    )

    assert reloaded.transactions == original.transactions
    assert reloaded.hash == original.hash
    assert reloaded.verify_body()


def test_block_from_persisted_pruned_row_has_no_transactions():
    reloaded = Block.from_persisted(
        index=1,
        prev_hash="a" * 64,
        merkle_root="b" * 64,
        hash="c" * 64,
        timestamp=0.0,
        compressed_body=None,
        is_pruned=True,
    )

    assert reloaded.transactions is None
    assert reloaded.verify_body()  # pruned is trivially valid


def test_block_from_persisted_swallows_corrupted_body():
    reloaded = Block.from_persisted(
        index=0,
        prev_hash=Chain.GENESIS_PREV_HASH,
        merkle_root="deadbeef",
        hash="c" * 64,
        timestamp=0.0,
        compressed_body=b"not valid lzma data",
        is_pruned=False,
    )

    assert reloaded.transactions is None
    assert not reloaded.verify_body()  # surfaces as corruption, not a crash


def test_block_create_type_validation():
    with pytest.raises(TypeError):
        Block.create(index=0, prev_hash=Chain.GENESIS_PREV_HASH, transactions="not a list")


def test_get_block_out_of_range_raises():
    chain = Chain()
    with pytest.raises(IndexError):
        chain.get_block(5)


def test_len_and_iter():
    chain = Chain()
    chain.add_block([_tx("tx-1", "Alice", 1)])
    chain.add_block([_tx("tx-2", "Bob", 2)])

    assert len(chain) == 3
    assert [b.index for b in chain] == [0, 1, 2]


def test_add_block_records_metrics_when_given_a_registry():
    metrics = MetricsRegistry()
    chain = Chain(metrics=metrics)

    chain.add_block([_tx("tx-1", "Alice", 100)])

    assert metrics._counters[("blocks_added_total", ())] == 1.0
    ratio_key = ("compression_ratio", (("block_index", "1"),))
    assert metrics._gauges[ratio_key] > 0


def test_add_block_without_metrics_is_a_silent_no_op():
    chain = Chain()  # metrics=None by default
    chain.add_block([_tx("tx-1", "Alice", 100)])  # must not raise


def test_chain_accepts_injected_state():
    from blockchain_compression.compression import StateDelta

    state = StateDelta()
    state.apply_transaction(_tx("tx-0", "Alice", 500))
    chain = Chain(state=state)

    assert chain.state.get_current_state() == {"Alice": 500}
