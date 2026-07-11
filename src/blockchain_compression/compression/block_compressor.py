"""LZMA compression for raw blockchain block data.

LZMA is lossless but not cryptographic: pair compressed blocks with hash or
signature checks elsewhere in the system to detect tampering.
"""

import lzma


class BlockCompressor:
    """Compresses and decompresses block data using LZMA."""

    def __init__(self, preset: int = 6):
        """``preset`` is the LZMA compression level, 0 (fastest) to 9 (smallest)."""
        if isinstance(preset, bool) or not isinstance(preset, int) or not 0 <= preset <= 9:
            raise ValueError("preset must be an integer between 0 and 9")
        self.preset = preset

    def compress_block(self, block_data: bytes) -> bytes:
        """Compress raw block bytes and return the LZMA stream."""
        if not isinstance(block_data, (bytes, bytearray)):
            raise TypeError(f"block_data must be bytes, got {type(block_data).__name__}")
        return lzma.compress(bytes(block_data), preset=self.preset)

    def decompress_block(self, compressed_block_data: bytes) -> bytes:
        """Restore the original block bytes from an LZMA stream.

        Raises ``ValueError`` if the input is not a valid LZMA stream.
        """
        if not isinstance(compressed_block_data, (bytes, bytearray)):
            raise TypeError(
                f"compressed_block_data must be bytes, got {type(compressed_block_data).__name__}"
            )
        try:
            return lzma.decompress(bytes(compressed_block_data))
        except lzma.LZMAError as exc:
            raise ValueError(f"invalid or corrupted compressed block data: {exc}") from exc
