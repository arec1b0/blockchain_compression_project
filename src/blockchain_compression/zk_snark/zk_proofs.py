"""Mock ZK-SNARK interface — a placeholder, NOT real zero-knowledge proofs.

The "proof" is a SHA-256 commitment over a canonical JSON encoding of the
data. It demonstrates the generate/verify API shape only; a real ZK-SNARK
system requires a trusted setup and pairing-based cryptography.
"""

import hashlib
import json


class ZKSnark:
    """Placeholder proof system based on hash commitments."""

    PROOF_TAG = "zk-snark-proof-placeholder"

    def generate_proof(self, data: dict) -> dict:
        """Generate a mock proof committing to ``data``."""
        if not isinstance(data, dict):
            raise TypeError(f"data must be a dict, got {type(data).__name__}")
        return {"proof": self.PROOF_TAG, "data_hash": self.hash_data(data)}

    def verify_proof(self, proof: dict, data: dict) -> bool:
        """Check that ``proof`` commits to ``data``; malformed proofs verify as False."""
        if not isinstance(proof, dict):
            raise TypeError(f"proof must be a dict, got {type(proof).__name__}")
        if not isinstance(data, dict):
            raise TypeError(f"data must be a dict, got {type(data).__name__}")
        data_hash = proof.get("data_hash")
        if not isinstance(data_hash, str):
            return False
        return data_hash == self.hash_data(data)

    @staticmethod
    def hash_data(data: dict) -> str:
        """SHA-256 over canonical JSON, so dict key order cannot change the hash."""
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
