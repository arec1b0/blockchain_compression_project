# test_chain.py

import json
import lzma

import pytest

from blockchain_compression.canonical import canonical_json
from blockchain_compression.chain import Block, Chain, PreparedBlock
from blockchain_compression.compression import (
    FRAME_LZMA,
    FRAME_RAW,
    BlockCompressor,
    BlockDelta,
    StateDelta,
)
from blockchain_compression.observability import MetricsRegistry
from blockchain_compression.zk_proof import (
    PedersenCommitment,
    ZKAttachment,
    generate_proof,
    hash_to_scalar,
)


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

    block = chain.add_block([_tx("tx-1", "Alice", 100)])

    assert metrics.get_counter("blocks_added_total") == 1.0
    assert metrics.get_gauge("compression_ratio", labels={"block_index": "1"}) is None
    raw_bytes = metrics.get_counter("block_raw_bytes_total")
    compressed_bytes = metrics.get_counter("block_compressed_bytes_total")
    last_ratio = metrics.get_gauge("last_block_compression_ratio")
    ratio_hist = metrics.get_histogram("block_compression_ratio")

    assert raw_bytes is not None and raw_bytes > 0
    assert compressed_bytes == float(len(block.compressed_body))
    assert last_ratio == pytest.approx(raw_bytes / compressed_bytes)
    assert ratio_hist is not None
    assert ratio_hist.count == 1
    assert ratio_hist.sum == pytest.approx(last_ratio)


def test_repeated_block_commits_have_constant_metric_cardinality():
    metrics = MetricsRegistry()
    chain = Chain(metrics=metrics)

    expected_raw_total = 0
    expected_compressed_total = 0
    last_ratio = 0.0
    for i in range(1, 26):
        batch = [_tx(f"tx-{i}-{j}", f"User{j}", (i + j) * 10) for j in range((i % 5) + 1)]
        prepared = chain.prepare_block(batch)
        block = chain.commit_block(prepared)
        expected_raw_total += prepared.raw_body_bytes
        expected_compressed_total += len(block.compressed_body)
        last_ratio = prepared.raw_body_bytes / len(block.compressed_body)

    parsed = json.loads(metrics.to_json())
    assert len(parsed["counters"]) == 3
    assert len(parsed["gauges"]) == 1
    assert len(parsed["histograms"]) == 1

    assert metrics.get_counter("blocks_added_total") == 25.0
    assert metrics.get_counter("block_raw_bytes_total") == float(expected_raw_total)
    assert metrics.get_counter("block_compressed_bytes_total") == float(expected_compressed_total)
    assert metrics.get_gauge("last_block_compression_ratio") == pytest.approx(last_ratio)

    ratio_hist = metrics.get_histogram("block_compression_ratio")
    assert ratio_hist is not None and ratio_hist.count == 25

    prom = metrics.to_prometheus_text()
    assert "block_index=" not in prom
    for name in (
        "blocks_added_total",
        "block_raw_bytes_total",
        "block_compressed_bytes_total",
        "last_block_compression_ratio",
        "block_compression_ratio",
    ):
        assert f"# HELP {name} " in prom
        assert f"# TYPE {name} " in prom


def test_add_block_without_metrics_is_a_silent_no_op():
    chain = Chain()  # metrics=None by default
    chain.add_block([_tx("tx-1", "Alice", 100)])  # must not raise


def test_chain_accepts_injected_state():
    state = StateDelta()
    state.apply_transaction(_tx("tx-0", "Alice", 500))
    chain = Chain(state=state)

    chain.add_block([_tx("tx-1", "Alice", 100)])

    assert chain.state is state
    assert chain.state.get_current_state() == {"Alice": 600}


def test_add_block_rejects_replayed_tx_id_before_creating_block():
    chain = Chain()
    chain.add_block([_tx("tx-1", "Alice", 100)])

    with pytest.raises(ValueError, match="already been applied"):
        chain.add_block([_tx("tx-1", "Alice", 999)])

    assert len(chain) == 2
    assert chain.state.get_current_state() == {"Alice": 100}


def test_add_block_rejects_duplicate_tx_id_in_same_batch():
    chain = Chain()

    with pytest.raises(ValueError, match="duplicate tx_id"):
        chain.add_block([_tx("tx-1", "Alice", 100), _tx("tx-1", "Bob", 50)])

    assert len(chain) == 1
    assert chain.state.get_current_state() == {}


@pytest.mark.parametrize("payload", [b"123", b'{"a": 1}', b"null", b'"hello"'])
def test_verify_body_handles_non_list_json_without_raising(payload):
    compressed = BlockCompressor().compress_block(payload)
    reloaded = Block.from_persisted(
        index=0,
        prev_hash=Chain.GENESIS_PREV_HASH,
        merkle_root=None,
        hash="c" * 64,
        timestamp=0.0,
        compressed_body=compressed,
        is_pruned=False,
    )

    assert reloaded.transactions is None
    assert reloaded.verify_body() is False


def test_verify_body_false_for_pruned_block_with_populated_body_or_transactions():
    block = Block.create(
        index=1,
        prev_hash=Chain.GENESIS_PREV_HASH,
        transactions=[_tx("tx-1", "Alice", 10)],
    )
    block.is_pruned = True  # inconsistent: compressed_body and transactions still present
    assert block.verify_body() is False

    block.compressed_body = None
    assert block.verify_body() is False

    block.transactions = None
    assert block.verify_body() is True


def test_verify_body_false_when_transactions_mutated_to_non_list():
    block = Block.create(
        index=1,
        prev_hash=Chain.GENESIS_PREV_HASH,
        transactions=[_tx("tx-1", "Alice", 10)],
    )
    block.transactions = 123
    assert block.verify_body() is False


def test_add_block_deep_copies_transactions():
    chain = Chain()
    tx = {"tx_id": "tx-1", "account": "Alice", "amount": 100, "meta": {"tags": ["a"]}}
    batch = [tx]

    block = chain.add_block(batch)
    original_root = block.merkle_root
    tx["amount"] = 999
    tx["meta"]["tags"].append("tampered")
    batch.append(_tx("tx-extra", "Bob", 50))

    assert len(block.transactions) == 1
    assert block.transactions[0]["amount"] == 100
    assert block.transactions[0]["meta"] == {"tags": ["a"]}
    assert block.merkle_root == original_root
    assert block.verify_body()


def test_chain_blocks_is_read_only():
    chain = Chain()
    assert isinstance(chain.blocks, tuple)

    with pytest.raises(AttributeError):
        chain.blocks = []
    with pytest.raises(AttributeError):
        chain.blocks.append(chain.get_block(0))
    with pytest.raises(TypeError):
        chain.blocks[0] = chain.get_block(0)


def test_chain_get_state_snapshot_is_isolated_copy():
    chain = Chain()
    chain.add_block([_tx("tx-1", "Alice", 100)])

    snapshot = chain.get_state_snapshot()
    snapshot.apply_transaction(_tx("tx-2", "Bob", 500))

    assert chain.state.get_current_state() == {"Alice": 100}
    assert not chain.state.is_applied("tx-2")


def test_from_persisted_skips_genesis_creation(monkeypatch):
    genesis = Block.create(index=0, prev_hash=Chain.GENESIS_PREV_HASH, transactions=[])
    state = StateDelta()

    def _fail(*args, **kwargs):
        raise AssertionError("Block.create must not be called by from_persisted")

    monkeypatch.setattr(Block, "create", _fail)
    reloaded = Chain.from_persisted([genesis], state)
    assert len(reloaded) == 1
    assert reloaded.get_block(0) is genesis


def test_prepare_block_and_commit_block_rejects_stale_chain():
    chain = Chain()
    chain.add_block([_tx("tx-0", "Carol", 100)])
    prepared = chain.prepare_block([_tx("tx-1", "Alice", 50)])
    assert isinstance(prepared, PreparedBlock)
    assert isinstance(prepared.delta, BlockDelta)
    assert prepared.delta.updated_accounts == {"Alice": 50}  # Carol untouched
    assert prepared.delta.tx_ids == ("tx-1",)
    assert len(chain) == 2

    # Advance the chain via another batch so `prepared` becomes stale.
    chain.add_block([_tx("tx-2", "Bob", 25)])
    with pytest.raises(ValueError, match="stale"):
        chain.commit_block(prepared)
    assert not chain.state.is_applied("tx-1")
    assert "Alice" not in chain.state.get_current_state()


def test_optional_zk_attachments_do_not_alter_header_or_mandatory_validation():
    tx = _tx("tx-1", "Alice", 100)
    scalar = hash_to_scalar(tx)
    commitment, blinding = PedersenCommitment().commit(scalar)
    proof = generate_proof(message=scalar, blinding=blinding, commitment=commitment)
    valid_att = ZKAttachment(commitment=commitment, proof=proof, tx_id="tx-1")
    unproven_att = ZKAttachment(commitment=commitment, proof=None, tx_id="tx-1")

    chain_plain = Chain()
    plain_block = chain_plain.add_block([tx])

    chain_with_valid = Chain()
    att_block = chain_with_valid.add_block([tx], zk_attachments=[valid_att])
    assert att_block.merkle_root == plain_block.merkle_root
    assert chain_with_valid.validate_chain()
    assert chain_with_valid.verify_zk_attachments() is True

    chain_with_unproven = Chain()
    chain_with_unproven.add_block([tx], zk_attachments=[unproven_att])
    # Structural chain validation stays decoupled from opt-in ZK verification.
    assert chain_with_unproven.validate_chain() is True
    assert chain_with_unproven.verify_zk_attachments() is False


def test_block_from_persisted_and_verify_body_enforce_decompression_cap():
    bomb = BlockCompressor(preset=0).compress_block(b'["' + (b"x" * 2_000) + b'"]')
    reloaded = Block.from_persisted(
        index=1,
        prev_hash=Chain.GENESIS_PREV_HASH,
        merkle_root="a" * 64,
        hash="b" * 64,
        timestamp=0.0,
        compressed_body=bomb,
        is_pruned=False,
        max_decompressed_bytes=128,
    )
    assert reloaded.transactions is None
    assert reloaded.verify_body(max_decompressed_bytes=128) is False


@pytest.mark.parametrize(
    "txs",
    [
        [],
        [_tx("tx-1", "Alice", 100)],
        [
            {"tx_id": "tx-1", "account": "Alice", "amount": -5, "meta": {"b": 2, "a": 1}},
            {"tx_id": "tx-2", "account": "Bob \u00e9", "amount": 0, "tags": ["x", "y"]},
        ],
    ],
)
def test_block_create_single_pass_serialization_is_canonical(txs, monkeypatch):
    import blockchain_compression.chain.block as block_mod

    calls = []
    real_canonical = canonical_json

    def _counting_canonical(obj):
        calls.append(obj)
        return real_canonical(obj)

    monkeypatch.setattr(block_mod, "canonical_json", _counting_canonical)
    block = Block.create(index=1, prev_hash=Chain.GENESIS_PREV_HASH, transactions=txs)
    # Each transaction is serialized once plus 1 call for the 4-element header;
    # the transactions list itself is never re-serialized.
    assert len(calls) == len(txs) + 1
    assert not any(obj is txs for obj in calls)
    raw = BlockCompressor().decompress_block(block.compressed_body).decode("utf-8")
    assert raw == canonical_json(txs)
    assert block.verify_body() is True


def test_configurable_compressor_and_legacy_lzma_block_body_compatibility():
    raw_chain = Chain(compressor=BlockCompressor(preset=1, min_compress_bytes=256))
    small_block = raw_chain.add_block([_tx("tx-1", "Alice", 10)])
    assert small_block.compressed_body[0] == FRAME_RAW
    assert small_block.verify_body()

    lzma_chain = Chain(compressor=BlockCompressor(preset=6, min_compress_bytes=0))
    forced_block = lzma_chain.add_block([_tx("tx-2", "Bob", 20)])
    assert forced_block.compressed_body[0] == FRAME_LZMA
    assert forced_block.verify_body()

    # Legacy unframed LZMA stream produced without the framing byte.
    legacy_raw_lzma = lzma.compress(
        canonical_json(small_block.transactions).encode("utf-8"), preset=6
    )
    legacy_reloaded = Block.from_persisted(
        index=small_block.index,
        prev_hash=small_block.prev_hash,
        merkle_root=small_block.merkle_root,
        hash=small_block.hash,
        timestamp=small_block.timestamp,
        compressed_body=legacy_raw_lzma,
        is_pruned=False,
    )
    assert legacy_reloaded.transactions == small_block.transactions
    assert legacy_reloaded.verify_body() is True
