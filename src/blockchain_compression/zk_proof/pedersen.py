"""Pedersen commitment: ``C = g^m * h^r mod p``.

Computationally binding under the discrete-log assumption in the order-Q
subgroup of ``Z_p*`` (see :mod:`groups`), and perfectly hiding: for any two
messages ``m1 != m2`` there exists a blinding ``r'`` such that
``commit(m1, r) == commit(m2, r')`` - so the commitment value alone reveals
nothing about the message.
"""

import secrets
from dataclasses import dataclass

from blockchain_compression.zk_proof.groups import G, H, P, Q


@dataclass(frozen=True)
class Commitment:
    value: int


class PedersenCommitment:
    """Commit to an integer message in ``[0, Q)`` with a fresh or caller-supplied
    blinding factor."""

    def commit(self, message: int, blinding: int | None = None) -> tuple:
        """Return ``(commitment, blinding)``.

        Raises ``ValueError`` if ``message``/``blinding`` are out of range -
        rejected outright rather than silently reduced mod Q, so a caller
        can't lose information without noticing.
        """
        if isinstance(message, bool) or not isinstance(message, int) or not 0 <= message < Q:
            raise ValueError(f"message must be an integer in [0, {Q}), got {message!r}")
        if blinding is None:
            # Exclude 0: commit(m, 0) = g^m alone isn't hiding (checkable by
            # anyone who guesses m). randbelow(Q - 1) + 1 samples [1, Q) uniformly.
            blinding = secrets.randbelow(Q - 1) + 1
        elif isinstance(blinding, bool) or not isinstance(blinding, int) or not 0 <= blinding < Q:
            raise ValueError(f"blinding must be an integer in [0, {Q}), got {blinding!r}")
        value = (pow(G, message, P) * pow(H, blinding, P)) % P
        return Commitment(value), blinding

    def verify_opening(self, commitment: Commitment, message: int, blinding: int) -> bool:
        """Check that ``(message, blinding)`` is a valid opening of ``commitment``."""
        if not isinstance(commitment, Commitment):
            raise TypeError(f"commitment must be a Commitment, got {type(commitment).__name__}")
        if not (0 <= message < Q and 0 <= blinding < Q):
            return False
        expected = (pow(G, message, P) * pow(H, blinding, P)) % P
        return commitment.value == expected
