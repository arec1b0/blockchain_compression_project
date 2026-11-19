# test_block_compressor.py

import lzma
import random

import pytest

from blockchain_compression.compression import (
    DEFAULT_COMPRESSION_THRESHOLD,
    DEFAULT_PRESET,
    FRAME_LZMA,
    FRAME_RAW,
    LZMA_MAGIC,
    BlockCompressor,
)


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
    with pytest.raises(ValueError):
        BlockCompressor(preset=True)


def test_invalid_max_decompressed_bytes():
    with pytest.raises(ValueError):
        BlockCompressor(max_decompressed_bytes=0)
    with pytest.raises(ValueError):
        BlockCompressor(max_decompressed_bytes=-1)
    with pytest.raises(ValueError):
        BlockCompressor(max_decompressed_bytes=True)
    with pytest.raises(ValueError):
        BlockCompressor(max_decompressed_bytes="1024")


def test_invalid_min_compress_bytes():
    with pytest.raises(ValueError, match="min_compress_bytes"):
        BlockCompressor(min_compress_bytes=-1)
    with pytest.raises(ValueError, match="min_compress_bytes"):
        BlockCompressor(min_compress_bytes=True)
    with pytest.raises(ValueError, match="min_compress_bytes"):
        BlockCompressor(min_compress_bytes="256")


def test_default_preset_and_threshold_constants():
    compressor = BlockCompressor()
    assert compressor.preset == DEFAULT_PRESET == 1
    assert compressor.min_compress_bytes == DEFAULT_COMPRESSION_THRESHOLD == 256


def test_small_payload_uses_raw_framing_and_round_trips():
    payload = b'[{"account":"Alice","amount":1,"tx_id":"tx-1"}]'
    compressor = BlockCompressor(min_compress_bytes=256)
    encoded = compressor.compress_block(payload)

    assert encoded[0] == FRAME_RAW
    assert len(encoded) == len(payload) + 1
    assert compressor.decompress_block(encoded) == payload


def test_large_compressible_payload_uses_lzma_framing_and_shrinks():
    payload = b'{"account":"Alice","amount":100}' * 50
    compressor = BlockCompressor(preset=1, min_compress_bytes=256)
    encoded = compressor.compress_block(payload)

    assert encoded[0] == FRAME_LZMA
    assert len(encoded) < len(payload)
    assert compressor.decompress_block(encoded) == payload


def test_incompressible_payload_falls_back_to_raw_unless_threshold_is_zero():
    incompressible = random.Random(42).randbytes(512)
    smart = BlockCompressor(preset=1, min_compress_bytes=256)
    encoded_smart = smart.compress_block(incompressible)
    assert encoded_smart[0] == FRAME_RAW
    assert smart.decompress_block(encoded_smart) == incompressible

    forced_lzma = BlockCompressor(preset=1, min_compress_bytes=0)
    encoded_lzma = forced_lzma.compress_block(incompressible)
    assert encoded_lzma[0] == FRAME_LZMA
    assert encoded_lzma[1:].startswith(LZMA_MAGIC)
    assert forced_lzma.decompress_block(encoded_lzma) == incompressible


def test_legacy_unframed_lzma_stream_remains_readable_and_bounded():
    payload = b'[{"account":"Alice","amount":42}]' * 20
    legacy_stream = lzma.compress(payload, preset=6)
    assert legacy_stream.startswith(LZMA_MAGIC)

    compressor = BlockCompressor(max_decompressed_bytes=len(payload))
    assert compressor.decompress_block(legacy_stream) == payload

    capped = BlockCompressor(max_decompressed_bytes=len(payload) - 1)
    with pytest.raises(ValueError, match="exceeds max_decompressed_bytes"):
        capped.decompress_block(legacy_stream)

    with pytest.raises(ValueError, match="trailing"):
        compressor.decompress_block(legacy_stream + b"extra")


def test_raw_frame_enforces_max_decompressed_bytes():
    payload = b"x" * 128
    encoded = bytes([FRAME_RAW]) + payload
    compressor = BlockCompressor(max_decompressed_bytes=64)
    with pytest.raises(ValueError, match="exceeds max_decompressed_bytes"):
        compressor.decompress_block(encoded)


def test_empty_unknown_frame_or_empty_lzma_body_raises_value_error():
    compressor = BlockCompressor()
    with pytest.raises(ValueError, match="empty"):
        compressor.decompress_block(b"")
    with pytest.raises(ValueError, match="frame header"):
        compressor.decompress_block(b"\x02payload")
    with pytest.raises(ValueError, match="truncated"):
        compressor.decompress_block(bytes([FRAME_LZMA]))


def test_decompression_at_exact_cap_succeeds():
    payload = b"A" * 256
    compressor = BlockCompressor(preset=0, max_decompressed_bytes=256)
    compressed = compressor.compress_block(payload)
    assert compressor.decompress_block(compressed) == payload


def test_decompression_exceeding_cap_raises_value_error():
    payload = b"\x00" * 10_000
    compressor = BlockCompressor(preset=0, max_decompressed_bytes=256)
    compressed = compressor.compress_block(payload)
    assert len(compressed) < 256  # high-ratio payload smaller than cap
    with pytest.raises(ValueError, match="exceeds max_decompressed_bytes"):
        compressor.decompress_block(compressed)


def test_truncated_and_trailing_lzma_streams_raise_value_error():
    compressor = BlockCompressor(preset=0, min_compress_bytes=0)
    compressed = compressor.compress_block(b"valid block payload")
    assert compressed[0] == FRAME_LZMA
    with pytest.raises(ValueError, match="truncated|corrupted"):
        compressor.decompress_block(compressed[:-4])
    with pytest.raises(ValueError, match="trailing"):
        compressor.decompress_block(compressed + b"trailing-garbage")
