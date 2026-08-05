"""Canonical JSON encoding shared by every module that hashes or compresses data.

Using one encoder everywhere means a transaction's Merkle-leaf hash, its bytes inside a
compressed block body, and its ZK-commitment scalar can never silently drift apart because
two call sites serialized the same dict differently.
"""

import json


def canonical_json(obj) -> str:
    """Deterministic JSON encoding: sorted keys, no extra whitespace, non-JSON-native
    values (e.g. bytes) coerced via ``str``."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
