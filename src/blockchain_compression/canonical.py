"""Canonical JSON encoding shared by every module that hashes or compresses data.

Using one encoder everywhere means a transaction's Merkle-leaf hash, its bytes inside a
compressed block body, and its ZK-commitment scalar can never silently drift apart because
two call sites serialized the same dict differently.
"""

import json


def canonical_json(obj) -> str:
    """Deterministic RFC 8259 JSON encoding: sorted keys, no extra whitespace.

    Raises ``TypeError`` for non-JSON-native values (e.g. ``bytes``, ``set``,
    or custom objects) rather than coercing them via ``str``, and ``ValueError``
    for non-finite floats (``NaN``, ``Infinity``, ``-Infinity``). Callers must
    explicitly encode binary values (such as hex or base64 strings) before
    serialization.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)
