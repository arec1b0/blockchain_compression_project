"""Block compression and delta-based state storage."""

from blockchain_compression.compression.block_compressor import (
    DEFAULT_COMPRESSION_THRESHOLD,
    DEFAULT_MAX_DECOMPRESSED_BYTES,
    DEFAULT_PRESET,
    FRAME_LZMA,
    FRAME_RAW,
    LZMA_MAGIC,
    BlockCompressor,
)
from blockchain_compression.compression.state_delta import (
    BlockDelta,
    StateDelta,
    extract_block_delta,
)

__all__ = [
    "BlockCompressor",
    "BlockDelta",
    "DEFAULT_COMPRESSION_THRESHOLD",
    "DEFAULT_MAX_DECOMPRESSED_BYTES",
    "DEFAULT_PRESET",
    "FRAME_LZMA",
    "FRAME_RAW",
    "LZMA_MAGIC",
    "StateDelta",
    "extract_block_delta",
]
