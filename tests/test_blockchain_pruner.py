# test_blockchain_pruner.py

import pytest

from blockchain_compression.pruning import BlockchainPruner


def test_add_block_and_prune():
    pruner = BlockchainPruner(max_blocks=3)

    for number in (1, 2, 3, 4):
        pruner.add_block({"block_number": number, "data": f"Block {number} data"})

    assert len(pruner.get_blocks()) == 3
    assert pruner.get_blocks()[0]["block_number"] == 2


def test_no_pruning_needed():
    pruner = BlockchainPruner(max_blocks=5)

    pruner.add_block({"block_number": 1, "data": "Block 1 data"})
    pruner.add_block({"block_number": 2, "data": "Block 2 data"})

    assert len(pruner.get_blocks()) == 2


def test_invalid_max_blocks():
    with pytest.raises(ValueError):
        BlockchainPruner(max_blocks=0)
    with pytest.raises(ValueError):
        BlockchainPruner(max_blocks=-1)
    with pytest.raises(TypeError):
        BlockchainPruner(max_blocks="3")
    with pytest.raises(TypeError):
        BlockchainPruner(max_blocks=True)


def test_add_block_type_validation():
    pruner = BlockchainPruner(max_blocks=2)
    with pytest.raises(TypeError):
        pruner.add_block("not a dict")
