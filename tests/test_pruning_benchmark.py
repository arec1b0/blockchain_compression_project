# test_pruning_benchmark.py

from blockchain_compression.benchmarks.pruning_benchmark import _build_chain, benchmark_pruning
from blockchain_compression.pruning import BlockchainPruner


def test_retained_full_block_count_is_bounded_independent_of_chain_length():
    # The "load_resistance" property: once chain length exceeds max_full_blocks,
    # the number of blocks kept full stays pinned at max_full_blocks - it must
    # not grow as the chain grows. Correctness, not timing, so no CI flakiness.
    max_full_blocks = 50
    for chain_length in (60, 600):
        chain = _build_chain(chain_length)
        BlockchainPruner(chain, max_full_blocks=max_full_blocks).prune()

        retained = sum(1 for block in chain if not block.is_pruned)
        assert retained == max_full_blocks
        assert chain.validate_chain()


def test_benchmark_pruning_smoke():
    result = benchmark_pruning(chain_length=100, max_full_blocks=10)

    assert result.chain_length == 100
    assert result.prune_seconds > 0
    assert result.memory_before_bytes > 0
    assert result.memory_freed_bytes > 0  # pruning must actually free traced memory
