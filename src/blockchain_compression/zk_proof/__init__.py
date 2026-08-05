"""Pedersen commitment + non-interactive Schnorr zero-knowledge proof of knowledge.

Not a SNARK - a genuine, explainable ZK primitive: ``verify_proof`` checks only
the commitment, never the plaintext message or blinding factor.
"""

from blockchain_compression.zk_proof.encoding import hash_to_scalar
from blockchain_compression.zk_proof.pedersen import Commitment, PedersenCommitment
from blockchain_compression.zk_proof.schnorr import SchnorrProof, generate_proof, verify_proof

__all__ = [
    "Commitment",
    "PedersenCommitment",
    "SchnorrProof",
    "generate_proof",
    "verify_proof",
    "hash_to_scalar",
]
