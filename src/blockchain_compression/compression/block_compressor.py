"""LZMA and size-aware framed compression for raw blockchain block data.

Payloads produced by :meth:`BlockCompressor.compress_block` begin with a 1-byte
frame header:

* ``FRAME_RAW`` (``0x00``): uncompressed UTF-8 JSON bytes follow directly.
  Used automatically when an input is smaller than ``min_compress_bytes``
  (default ``256`` bytes) or when LZMA fails to reduce size, avoiding LZMA CPU
  cost and header inflation on small blocks.
* ``FRAME_LZMA`` (``0x01``): a standard LZMA stream follows.

For backward compatibility with blocks persisted before framing existed,
:meth:`BlockCompressor.decompress_block` also detects legacy unframed LZMA
streams via ``LZMA_MAGIC`` (``0xFD 0x37 0x7A 0x58 0x5A 0x00``) and applies the
same bounded ``max_decompressed_bytes`` limit to every frame type.

Trade-off note: ``DEFAULT_PRESET = 1`` replaces ``6`` as the default LZMA preset,
cutting per-block compression CPU time significantly on typical transaction
batches at a modest reduction in compression ratio for highly redundant large
blocks. Callers prioritizing maximum space savings over CPU time can still pass
``preset=6`` or ``preset=9``.

Compression is lossless and not cryptographic: pair compressed blocks with hash
or signature checks elsewhere in the system to detect tampering.
"""

import lzma

DEFAULT_MAX_DECOMPRESSED_BYTES = 16 * 1024 * 1024  # 16 MiB
DEFAULT_PRESET = 1
DEFAULT_COMPRESSION_THRESHOLD = 256

FRAME_RAW = 0x00
FRAME_LZMA = 0x01
LZMA_MAGIC = b"\xfd7zXZ\x00"


class BlockCompressor:
    """Compresses and decompresses block data using size-aware LZMA framing."""

    def __init__(
        self,
        preset: int = DEFAULT_PRESET,
        max_decompressed_bytes: int = DEFAULT_MAX_DECOMPRESSED_BYTES,
        min_compress_bytes: int = DEFAULT_COMPRESSION_THRESHOLD,
    ):
        """Configure compression preset, OOM bomb bound, and raw-frame threshold.

        - ``preset`` is the LZMA compression level, ``0`` (fastest) to ``9`` (smallest).
        - ``max_decompressed_bytes`` bounds decompressed output to prevent OOM bombs.
        - ``min_compress_bytes`` stores payloads smaller than this threshold (or
          incompressible payloads when ``> 0``) using ``FRAME_RAW`` (``0x00``).
          Pass ``0`` to always emit ``FRAME_LZMA`` (``0x01``).
        """
        if isinstance(preset, bool) or not isinstance(preset, int) or not 0 <= preset <= 9:
            raise ValueError("preset must be an integer between 0 and 9")
        if (
            isinstance(max_decompressed_bytes, bool)
            or not isinstance(max_decompressed_bytes, int)
            or max_decompressed_bytes < 1
        ):
            raise ValueError("max_decompressed_bytes must be a positive integer")
        if (
            isinstance(min_compress_bytes, bool)
            or not isinstance(min_compress_bytes, int)
            or min_compress_bytes < 0
        ):
            raise ValueError("min_compress_bytes must be a non-negative integer")
        self.preset = preset
        self.max_decompressed_bytes = max_decompressed_bytes
        self.min_compress_bytes = min_compress_bytes

    def compress_block(self, block_data: bytes) -> bytes:
        """Encode ``block_data`` using size-aware raw or LZMA framing."""
        if not isinstance(block_data, (bytes, bytearray)):
            raise TypeError(f"block_data must be bytes, got {type(block_data).__name__}")
        raw = bytes(block_data)
        if len(raw) < self.min_compress_bytes:
            return bytes([FRAME_RAW]) + raw
        compressed = lzma.compress(raw, preset=self.preset)
        if self.min_compress_bytes > 0 and len(compressed) >= len(raw):
            return bytes([FRAME_RAW]) + raw
        return bytes([FRAME_LZMA]) + compressed

    def _decompress_lzma(self, stream: bytes) -> bytes:
        decompressor = lzma.LZMADecompressor()
        try:
            decompressed = decompressor.decompress(stream, max_length=self.max_decompressed_bytes)
            if not decompressor.eof:
                extra = decompressor.decompress(b"", max_length=1)
                if extra or not decompressor.needs_input:
                    raise ValueError(
                        "decompressed block data exceeds max_decompressed_bytes "
                        f"({self.max_decompressed_bytes})"
                    )
                if not decompressor.eof:
                    raise ValueError("invalid or truncated compressed block data")
            if decompressor.unused_data:
                raise ValueError("invalid compressed block data: unexpected trailing bytes")
            return decompressed
        except lzma.LZMAError as exc:
            raise ValueError(f"invalid or corrupted compressed block data: {exc}") from exc

    def decompress_block(self, compressed_block_data: bytes) -> bytes:
        """Restore original block bytes from framed (``0x00``/``0x01``) or legacy LZMA data,
        bounded by ``self.max_decompressed_bytes``.

        Raises ``ValueError`` if the input is empty, has an unknown frame byte,
        is not a valid complete single LZMA stream, or exceeds
        ``max_decompressed_bytes``.
        """
        if not isinstance(compressed_block_data, (bytes, bytearray)):
            raise TypeError(
                f"compressed_block_data must be bytes, got {type(compressed_block_data).__name__}"
            )
        payload = bytes(compressed_block_data)
        if not payload:
            raise ValueError("invalid or empty compressed block data")
        if payload.startswith(LZMA_MAGIC):
            return self._decompress_lzma(payload)

        frame = payload[0]
        body = payload[1:]
        if frame == FRAME_RAW:
            if len(body) > self.max_decompressed_bytes:
                raise ValueError(
                    "decompressed block data exceeds max_decompressed_bytes "
                    f"({self.max_decompressed_bytes})"
                )
            return body
        if frame == FRAME_LZMA:
            if not body:
                raise ValueError("invalid or truncated compressed block data")
            return self._decompress_lzma(body)
        raise ValueError(f"unsupported or invalid block compression frame header: 0x{frame:02x}")
