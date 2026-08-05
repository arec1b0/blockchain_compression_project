"""Map arbitrary blockchain data to a Pedersen-committable scalar."""

import hashlib

from blockchain_compression.canonical import canonical_json
from blockchain_compression.zk_proof.groups import Q


def hash_to_scalar(data: dict) -> int:
    """``canonical_json(data)`` -> SHA-256 -> integer mod Q.

    Lets a commitment be made over structured blockchain data (e.g. a
    transaction dict) without the caller having to encode it as an integer by
    hand - the same convenience the old mock's ``generate_proof(dict)``
    offered, without pretending verification can skip the plaintext (see
    :func:`~blockchain_compression.zk_proof.schnorr.verify_proof`, which
    genuinely doesn't need it).
    """
    if not isinstance(data, dict):
        raise TypeError(f"data must be a dict, got {type(data).__name__}")
    digest = hashlib.sha256(canonical_json(data).encode("utf-8")).digest()
    return int.from_bytes(digest, "big") % Q
