"""Pedersen commitment + non-interactive Schnorr zero-knowledge proof of knowledge.

Not a SNARK - a genuine, explainable ZK primitive: ``verify_proof`` checks only
the commitment, never the plaintext message or blinding factor.
"""

from blockchain_compression.zk_proof.attachment import (
    ATTACHMENT_FORMAT_VERSION,
    ZKAttachment,
)
from blockchain_compression.zk_proof.encoding import hash_to_scalar
from blockchain_compression.zk_proof.pedersen import (
    COMMITMENT_FORMAT_VERSION,
    Commitment,
    PedersenCommitment,
)
from blockchain_compression.zk_proof.schnorr import (
    PROOF_FORMAT_VERSION,
    SchnorrProof,
    generate_proof,
    verify_proof,
)

__all__ = [
    "ATTACHMENT_FORMAT_VERSION",
    "COMMITMENT_FORMAT_VERSION",
    "PROOF_FORMAT_VERSION",
    "Commitment",
    "PedersenCommitment",
    "SchnorrProof",
    "ZKAttachment",
    "generate_proof",
    "verify_proof",
    "hash_to_scalar",
]
