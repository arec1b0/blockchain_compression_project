"""Non-interactive Schnorr proof of knowledge (Fiat-Shamir).

Proves the prover knows an opening ``(m, r)`` of a Pedersen commitment
``C = g^m h^r mod p``, without revealing ``m`` or ``r``. This is the actual
zero-knowledge property: :func:`verify_proof` takes only the commitment, never
the plaintext message - the honest-verifier ZK guarantee that makes this a
genuine (if not constant-time, not-a-SNARK) ZK primitive rather than the
SHA-256 hash-commitment mock it replaces.

True zero-knowledge/hiding is a computational property that no unit test can
fully prove; the test suite for this module asserts the necessary structural
and algebraic soundness/completeness properties instead, which is the honest
ceiling of what a test suite can establish here.

Not constant-time: CPython's arbitrary-precision ``pow()`` has no side-channel
resistance, and there is no real fix for that in pure Python. Fine for
demonstrating a correct construction; not for defending against a co-located
timing adversary.

Benign, expected malleability: ``(s1 + Q, s2)`` verifies identically to
``(s1, s2)`` since ``g^Q == 1``, so raw proof bytes aren't a canonical
encoding - never key a dedup/replay mechanism on them. Transaction-level
replay protection is already ``StateDelta``'s ``tx_id`` job, not this module's.
"""

import hashlib
import secrets
import time
from dataclasses import dataclass

from blockchain_compression.observability.metrics import MetricsRegistry
from blockchain_compression.zk_proof.encoding import (
    FIELD_BYTE_LENGTH,
    GROUP_ID,
    fixed_hex_to_int,
    int_to_fixed_hex,
)
from blockchain_compression.zk_proof.groups import G, H, P, Q, powmod
from blockchain_compression.zk_proof.pedersen import Commitment

_DOMAIN = b"blockchain-compression-project/schnorr-challenge/v1"
_FIELD_LEN = (P.bit_length() + 7) // 8
PROOF_FORMAT_VERSION = 1
PROOF_WIRE_PREFIX = b"BCPZKP\x01"
_PROOF_WIRE_LENGTH = len(PROOF_WIRE_PREFIX) + 3 * FIELD_BYTE_LENGTH
_PROOF_DICT_KEYS = frozenset({"version", "group", "t", "s1", "s2"})


def _validate_proof_components(t: object, s1: object, s2: object) -> tuple[int, int, int]:
    _require_strict_int(t, "proof.t")
    _require_strict_int(s1, "proof.s1")
    _require_strict_int(s2, "proof.s2")
    assert isinstance(t, int) and isinstance(s1, int) and isinstance(s2, int)
    if not 1 <= t < P:
        raise ValueError("proof.t must be in [1, P)")
    if not 0 <= s1 < Q:
        raise ValueError("proof.s1 must be in [0, Q)")
    if not 0 <= s2 < Q:
        raise ValueError("proof.s2 must be in [0, Q)")
    if powmod(t, Q, P) != 1:
        raise ValueError("proof.t must belong to the order-Q subgroup")
    return t, s1, s2


@dataclass(frozen=True)
class SchnorrProof:
    """Deliberately ONLY these three fields - ``message``/``blinding`` must
    never be stored on the proof object, or the zero-knowledge property is a
    lie. (Regression-tested directly in the test suite.)"""

    t: int
    s1: int
    s2: int

    def to_dict(self) -> dict[str, int | str]:
        """Serialize to a canonical versioned JSON-compatible dictionary."""
        t, s1, s2 = _validate_proof_components(self.t, self.s1, self.s2)
        return {
            "version": PROOF_FORMAT_VERSION,
            "group": GROUP_ID,
            "t": int_to_fixed_hex(t, min_value=1, max_exclusive=P, name="proof.t"),
            "s1": int_to_fixed_hex(s1, min_value=0, max_exclusive=Q, name="proof.s1"),
            "s2": int_to_fixed_hex(s2, min_value=0, max_exclusive=Q, name="proof.s2"),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SchnorrProof":
        """Deserialize and validate a Schnorr proof dictionary."""
        if not isinstance(data, dict):
            raise TypeError(f"proof data must be a dict, got {type(data).__name__}")
        if frozenset(data.keys()) != _PROOF_DICT_KEYS:
            raise ValueError("proof dict must contain exact keys: version, group, t, s1, s2")
        version = data["version"]
        if isinstance(version, bool) or version != PROOF_FORMAT_VERSION:
            raise ValueError(f"unsupported proof version: {version!r}")
        if data["group"] != GROUP_ID:
            raise ValueError(f"unsupported proof group: {data['group']!r}")
        t = fixed_hex_to_int(data["t"], min_value=1, max_exclusive=P, name="proof.t")
        s1 = fixed_hex_to_int(data["s1"], min_value=0, max_exclusive=Q, name="proof.s1")
        s2 = fixed_hex_to_int(data["s2"], min_value=0, max_exclusive=Q, name="proof.s2")
        _validate_proof_components(t, s1, s2)
        return cls(t=t, s1=s1, s2=s2)

    def to_bytes(self) -> bytes:
        """Serialize to a versioned fixed-width 775-byte wire representation."""
        t, s1, s2 = _validate_proof_components(self.t, self.s1, self.s2)
        return (
            PROOF_WIRE_PREFIX
            + t.to_bytes(FIELD_BYTE_LENGTH, "big")
            + s1.to_bytes(FIELD_BYTE_LENGTH, "big")
            + s2.to_bytes(FIELD_BYTE_LENGTH, "big")
        )

    @classmethod
    def from_bytes(cls, data: bytes | bytearray) -> "SchnorrProof":
        """Deserialize and validate a fixed-width 775-byte Schnorr proof payload."""
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError(f"proof bytes must be bytes, got {type(data).__name__}")
        raw = bytes(data)
        if len(raw) != _PROOF_WIRE_LENGTH:
            raise ValueError(
                f"proof bytes must be exactly {_PROOF_WIRE_LENGTH} bytes, got {len(raw)}"
            )
        if raw[: len(PROOF_WIRE_PREFIX)] != PROOF_WIRE_PREFIX:
            raise ValueError("invalid proof wire header or version")
        offset = len(PROOF_WIRE_PREFIX)
        t = int.from_bytes(raw[offset : offset + FIELD_BYTE_LENGTH], "big")
        offset += FIELD_BYTE_LENGTH
        s1 = int.from_bytes(raw[offset : offset + FIELD_BYTE_LENGTH], "big")
        offset += FIELD_BYTE_LENGTH
        s2 = int.from_bytes(raw[offset : offset + FIELD_BYTE_LENGTH], "big")
        _validate_proof_components(t, s1, s2)
        return cls(t=t, s1=s1, s2=s2)


def _require_strict_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer, got {type(value).__name__}")


def _challenge(commitment_value: int, t: int) -> int:
    """Fiat-Shamir challenge: a domain-separated, fixed-width big-endian
    transcript over every group parameter plus the commitment and the proof's
    first message - mirrors the Merkle module's own domain-separation
    discipline. Binding G/H/P into the transcript (not just C and t) is cheap
    insurance against a future group-parameter change reopening a
    stale-transcript bug."""
    transcript = _DOMAIN + b"".join(
        x.to_bytes(_FIELD_LEN, "big") for x in (G, H, P, commitment_value, t)
    )
    return int.from_bytes(hashlib.sha256(transcript).digest(), "big") % Q


def generate_proof(
    message: int,
    blinding: int,
    commitment: Commitment,
    metrics: MetricsRegistry | None = None,
) -> SchnorrProof:
    """Prove knowledge of ``(message, blinding)`` behind ``commitment`` without
    revealing them.

    Fresh nonces every call: a Schnorr nonce must never be reused across
    proofs, which is why ``k1``/``k2`` are sampled inside this function body
    on every call rather than taken as parameters or cached on an instance.

    Raises ``TypeError`` if ``commitment`` is not a :class:`Commitment` or if
    ``commitment.value``, ``message``, or ``blinding`` is not a strict ``int``
    (``bool`` is rejected). Raises ``ValueError`` if ``message`` is outside
    ``[0, Q)``, ``blinding`` is outside ``[1, Q)``, or ``commitment`` is not a
    non-identity element of the order-``Q`` subgroup.
    """
    if not isinstance(commitment, Commitment):
        raise TypeError(f"commitment must be a Commitment, got {type(commitment).__name__}")
    _require_strict_int(commitment.value, "commitment.value")
    _require_strict_int(message, "message")
    _require_strict_int(blinding, "blinding")
    if not (0 <= message < Q and 1 <= blinding < Q):
        raise ValueError("message must be in [0, Q) and blinding must be in [1, Q)")
    if not (1 < commitment.value < P and powmod(commitment.value, Q, P) == 1):
        raise ValueError("commitment must be a non-identity element in the order-Q subgroup")

    start = time.perf_counter()
    k1 = secrets.randbelow(Q)
    k2 = secrets.randbelow(Q)
    t = (powmod(G, k1, P) * powmod(H, k2, P)) % P
    e = _challenge(commitment.value, t)
    s1 = (k1 + e * message) % Q
    s2 = (k2 + e * blinding) % Q
    proof = SchnorrProof(t=t, s1=s1, s2=s2)

    if metrics is not None:
        metrics.observe_histogram(
            "proof_generation_seconds",
            time.perf_counter() - start,
            help_text="Schnorr proof generation latency",
        )
    return proof


def verify_proof(
    proof: SchnorrProof, commitment: Commitment, metrics: MetricsRegistry | None = None
) -> bool:
    """Verify ``proof`` against ``commitment`` alone - the message/blinding are
    never needed, which is the whole point. Independently recomputes the
    challenge; a proof object never carries a transmitted ``e`` to (mis)trust.

    Validation contract:
    - Raises ``TypeError`` if ``proof`` is not a :class:`SchnorrProof`,
      ``commitment`` is not a :class:`Commitment`, or any of
      ``commitment.value``, ``proof.t``, ``proof.s1``, ``proof.s2`` is not a
      strict ``int`` (``bool`` is rejected).
    - Returns ``False`` (before any modular exponentiation) if
      ``commitment.value`` is outside ``(1, P)``, ``proof.t`` is outside
      ``[1, P)``, or ``proof.s1`` / ``proof.s2`` are outside ``[0, Q)``.
    - Enforces order-``Q`` subgroup membership (``pow(commitment.value, Q, P) == 1``
      and ``pow(proof.t, Q, P) == 1``) before checking the Schnorr verification
      equation, adding two 2048-bit modular exponentiations to verification.
    """
    if not isinstance(proof, SchnorrProof):
        raise TypeError(f"proof must be a SchnorrProof, got {type(proof).__name__}")
    if not isinstance(commitment, Commitment):
        raise TypeError(f"commitment must be a Commitment, got {type(commitment).__name__}")
    _require_strict_int(commitment.value, "commitment.value")
    _require_strict_int(proof.t, "proof.t")
    _require_strict_int(proof.s1, "proof.s1")
    _require_strict_int(proof.s2, "proof.s2")

    start = time.perf_counter()
    if not (
        1 < commitment.value < P and 1 <= proof.t < P and 0 <= proof.s1 < Q and 0 <= proof.s2 < Q
    ):
        result = False
    elif powmod(commitment.value, Q, P) != 1 or powmod(proof.t, Q, P) != 1:
        result = False
    else:
        e = _challenge(commitment.value, proof.t)
        lhs = (powmod(G, proof.s1, P) * powmod(H, proof.s2, P)) % P
        rhs = (proof.t * powmod(commitment.value, e, P)) % P
        result = lhs == rhs

    if metrics is not None:
        metrics.observe_histogram(
            "proof_verification_seconds",
            time.perf_counter() - start,
            help_text="Schnorr proof verification latency",
        )
    return result
