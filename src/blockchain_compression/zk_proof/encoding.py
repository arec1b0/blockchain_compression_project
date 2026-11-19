"""Map arbitrary blockchain data to a Pedersen-committable scalar and fixed-width codecs."""

import hashlib
import re

from blockchain_compression.canonical import canonical_json
from blockchain_compression.zk_proof.groups import P, Q

_DOMAIN = b"blockchain-compression-project/tx-scalar/v1"
GROUP_ID = "rfc3526-modp-2048"
FIELD_BYTE_LENGTH = (P.bit_length() + 7) // 8
FIELD_HEX_LENGTH = FIELD_BYTE_LENGTH * 2
_FIXED_HEX_RE = re.compile(rf"[0-9a-f]{{{FIELD_HEX_LENGTH}}}")


def int_to_fixed_hex(value: int, *, min_value: int, max_exclusive: int, name: str) -> str:
    """Encode a strict ``int`` in ``[min_value, max_exclusive)`` as lowercase
    fixed-width (512-char) big-endian hexadecimal."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer, got {type(value).__name__}")
    if not min_value <= value < max_exclusive:
        raise ValueError(f"{name} must be in [{min_value}, {max_exclusive})")
    return f"{value:0{FIELD_HEX_LENGTH}x}"


def fixed_hex_to_int(raw: object, *, min_value: int, max_exclusive: int, name: str) -> int:
    """Decode a strict lowercase 512-character hex string into an ``int`` in
    ``[min_value, max_exclusive)``."""
    if not isinstance(raw, str):
        raise TypeError(f"{name} must be a {FIELD_HEX_LENGTH}-character hex string")
    if not _FIXED_HEX_RE.fullmatch(raw):
        raise ValueError(
            f"{name} must be a {FIELD_HEX_LENGTH}-character lowercase hexadecimal string"
        )
    value = int(raw, 16)
    if not min_value <= value < max_exclusive:
        raise ValueError(f"{name} must be in [{min_value}, {max_exclusive})")
    return value


def hash_to_scalar(data: dict) -> int:
    """``_DOMAIN + canonical_json(data)`` -> SHA-256 -> integer mod Q.

    Prepends the versioned domain-separation prefix
    ``b"blockchain-compression-project/tx-scalar/v1"`` before hashing the
    strict RFC 8259 canonical JSON payload. Changing this domain prefix alters
    every derived scalar and is a compatibility-breaking change.

    Raises ``TypeError`` if ``data`` is not a ``dict`` or contains
    non-JSON-native values (such as raw ``bytes``), and ``ValueError`` if it
    contains non-finite floats (``NaN``, ``Infinity``, ``-Infinity``).
    """
    if not isinstance(data, dict):
        raise TypeError(f"data must be a dict, got {type(data).__name__}")
    digest = hashlib.sha256(_DOMAIN + canonical_json(data).encode("utf-8")).digest()
    return int.from_bytes(digest, "big") % Q
