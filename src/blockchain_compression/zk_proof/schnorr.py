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
from blockchain_compression.zk_proof.groups import G, H, P, Q
from blockchain_compression.zk_proof.pedersen import Commitment

_DOMAIN = b"blockchain-compression-project/schnorr-challenge/v1"
_FIELD_LEN = (P.bit_length() + 7) // 8


@dataclass(frozen=True)
class SchnorrProof:
    """Deliberately ONLY these three fields - ``message``/``blinding`` must
    never be stored on the proof object, or the zero-knowledge property is a
    lie. (Regression-tested directly in the test suite.)"""

    t: int
    s1: int
    s2: int


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
    """
    if not isinstance(commitment, Commitment):
        raise TypeError(f"commitment must be a Commitment, got {type(commitment).__name__}")
    if not (0 <= message < Q and 0 <= blinding < Q):
        raise ValueError("message and blinding must be integers in [0, Q)")

    start = time.perf_counter()
    k1 = secrets.randbelow(Q)
    k2 = secrets.randbelow(Q)
    t = (pow(G, k1, P) * pow(H, k2, P)) % P
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
    """
    if not isinstance(proof, SchnorrProof):
        raise TypeError(f"proof must be a SchnorrProof, got {type(proof).__name__}")
    if not isinstance(commitment, Commitment):
        raise TypeError(f"commitment must be a Commitment, got {type(commitment).__name__}")

    start = time.perf_counter()
    if not (1 <= commitment.value < P and 1 <= proof.t < P):
        result = False
    else:
        e = _challenge(commitment.value, proof.t)
        lhs = (pow(G, proof.s1, P) * pow(H, proof.s2, P)) % P
        rhs = (proof.t * pow(commitment.value, e, P)) % P
        result = lhs == rhs

    if metrics is not None:
        metrics.observe_histogram(
            "proof_verification_seconds",
            time.perf_counter() - start,
            help_text="Schnorr proof verification latency",
        )
    return result
