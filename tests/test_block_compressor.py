# test_block_compressor.py

import pytest

from blockchain_compression.compression import BlockCompressor


def test_block_compression():
    compressor = BlockCompressor()
    block_data = b"This is some raw blockchain block data"

    compressed_data = compressor.compress_block(block_data)
    decompressed_data = compressor.decompress_block(compressed_data)

    assert decompressed_data == block_data


def test_empty_block_compression():
    compressor = BlockCompressor()
    block_data = b""

    compressed_data = compressor.compress_block(block_data)
    decompressed_data = compressor.decompress_block(compressed_data)

    assert decompressed_data == block_data


def test_input_type_validation():
    compressor = BlockCompressor()
    with pytest.raises(TypeError):
        compressor.compress_block("not bytes")
    with pytest.raises(TypeError):
        compressor.decompress_block("not bytes")


def test_corrupt_data_raises_value_error():
    compressor = BlockCompressor()
    with pytest.raises(ValueError):
        compressor.decompress_block(b"definitely not an lzma stream")


def test_invalid_preset():
    with pytest.raises(ValueError):
        BlockCompressor(preset=42)
    with pytest.raises(ValueError):
        BlockCompressor(preset="max")
