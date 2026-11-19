"""Optional, explicitly versioned ZK attachment container for blocks.

Provides a minimal, opt-in schema associating a :class:`Commitment` and an
optional :class:`SchnorrProof` with a block or transaction ID. Attachments do
not alter block header hashes or mandatory ``Chain.validate_chain()`` checks;
verification is explicit via :meth:`ZKAttachment.verify` or
``Chain.verify_zk_attachments()``.
"""

from dataclasses import dataclass

from blockchain_compression.observability.metrics import MetricsRegistry
from blockchain_compression.zk_proof.pedersen import Commitment
from blockchain_compression.zk_proof.schnorr import SchnorrProof, verify_proof

ATTACHMENT_FORMAT_VERSION = 1
_ATTACHMENT_DICT_KEYS = frozenset({"version", "tx_id", "commitment", "proof"})


@dataclass(frozen=True)
class ZKAttachment:
    """Optional versioned commitment/proof attachment on a block."""

    commitment: Commitment
    proof: SchnorrProof | None = None
    tx_id: str | None = None
    version: int = ATTACHMENT_FORMAT_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or self.version != ATTACHMENT_FORMAT_VERSION:
            raise ValueError(f"unsupported attachment version: {self.version!r}")
        if not isinstance(self.commitment, Commitment):
            raise TypeError(
                f"commitment must be a Commitment, got {type(self.commitment).__name__}"
            )
        if self.proof is not None and not isinstance(self.proof, SchnorrProof):
            raise TypeError(
                f"proof must be a SchnorrProof or None, got {type(self.proof).__name__}"
            )
        if self.tx_id is not None and (not isinstance(self.tx_id, str) or not self.tx_id):
            raise ValueError("tx_id must be a non-empty string or None")

    def to_dict(self) -> dict:
        """Serialize to a canonical versioned JSON-compatible dictionary."""
        return {
            "version": self.version,
            "tx_id": self.tx_id,
            "commitment": self.commitment.to_dict(),
            "proof": self.proof.to_dict() if self.proof is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ZKAttachment":
        """Deserialize and validate a :class:`ZKAttachment` dictionary."""
        if not isinstance(data, dict):
            raise TypeError(f"attachment data must be a dict, got {type(data).__name__}")
        if frozenset(data.keys()) != _ATTACHMENT_DICT_KEYS:
            raise ValueError(
                "attachment dict must contain exact keys: version, tx_id, commitment, proof"
            )
        version = data["version"]
        if isinstance(version, bool) or version != ATTACHMENT_FORMAT_VERSION:
            raise ValueError(f"unsupported attachment version: {version!r}")
        tx_id = data["tx_id"]
        if tx_id is not None and (not isinstance(tx_id, str) or not tx_id):
            raise ValueError("tx_id must be a non-empty string or None")
        commitment = Commitment.from_dict(data["commitment"])
        raw_proof = data["proof"]
        proof = SchnorrProof.from_dict(raw_proof) if raw_proof is not None else None
        return cls(commitment=commitment, proof=proof, tx_id=tx_id, version=version)

    def verify(self, metrics: MetricsRegistry | None = None) -> bool:
        """Explicitly verify this attachment's Schnorr proof against its commitment.

        Returns ``False`` when no proof is attached so unproven attachments can
        never appear cryptographically verified.
        """
        if self.proof is None:
            return False
        return verify_proof(self.proof, self.commitment, metrics=metrics)
