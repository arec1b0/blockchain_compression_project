"""Pedersen commitment: ``C = g^m * h^r mod p``.

Computationally binding under the discrete-log assumption in the order-Q
subgroup of ``Z_p*`` (see :mod:`groups`), and perfectly hiding: for any two
messages ``m1 != m2`` there exists a blinding ``r'`` such that
``commit(m1, r) == commit(m2, r')`` - so the commitment value alone reveals
nothing about the message.
"""

import secrets
from dataclasses import dataclass

from blockchain_compression.zk_proof.encoding import (
    FIELD_BYTE_LENGTH,
    GROUP_ID,
    fixed_hex_to_int,
    int_to_fixed_hex,
)
from blockchain_compression.zk_proof.groups import G, H, P, Q, powmod

COMMITMENT_FORMAT_VERSION = 1
COMMITMENT_WIRE_PREFIX = b"BCPCOM\x01"
_COMMITMENT_WIRE_LENGTH = len(COMMITMENT_WIRE_PREFIX) + FIELD_BYTE_LENGTH
_COMMITMENT_DICT_KEYS = frozenset({"version", "group", "value"})


def _validate_commitment_value(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"commitment.value must be an integer, got {type(value).__name__}")
    if not 1 < value < P:
        raise ValueError("commitment.value must be in (1, P)")
    if powmod(value, Q, P) != 1:
        raise ValueError("commitment.value must belong to the order-Q subgroup")
    return value


@dataclass(frozen=True)
class Commitment:
    value: int

    def to_dict(self) -> dict[str, int | str]:
        """Serialize to a canonical versioned JSON-compatible dictionary."""
        validated = _validate_commitment_value(self.value)
        return {
            "version": COMMITMENT_FORMAT_VERSION,
            "group": GROUP_ID,
            "value": int_to_fixed_hex(
                validated, min_value=2, max_exclusive=P, name="commitment.value"
            ),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Commitment":
        """Deserialize and validate a commitment dictionary."""
        if not isinstance(data, dict):
            raise TypeError(f"commitment data must be a dict, got {type(data).__name__}")
        if frozenset(data.keys()) != _COMMITMENT_DICT_KEYS:
            raise ValueError("commitment dict must contain exact keys: version, group, value")
        version = data["version"]
        if isinstance(version, bool) or version != COMMITMENT_FORMAT_VERSION:
            raise ValueError(f"unsupported commitment version: {version!r}")
        if data["group"] != GROUP_ID:
            raise ValueError(f"unsupported commitment group: {data['group']!r}")
        value = fixed_hex_to_int(
            data["value"], min_value=2, max_exclusive=P, name="commitment.value"
        )
        _validate_commitment_value(value)
        return cls(value=value)

    def to_bytes(self) -> bytes:
        """Serialize to a versioned fixed-width 263-byte wire representation."""
        validated = _validate_commitment_value(self.value)
        return COMMITMENT_WIRE_PREFIX + validated.to_bytes(FIELD_BYTE_LENGTH, "big")

    @classmethod
    def from_bytes(cls, data: bytes | bytearray) -> "Commitment":
        """Deserialize and validate a fixed-width 263-byte commitment payload."""
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError(f"commitment bytes must be bytes, got {type(data).__name__}")
        raw = bytes(data)
        if len(raw) != _COMMITMENT_WIRE_LENGTH:
            raise ValueError(
                f"commitment bytes must be exactly {_COMMITMENT_WIRE_LENGTH} bytes, got {len(raw)}"
            )
        if raw[: len(COMMITMENT_WIRE_PREFIX)] != COMMITMENT_WIRE_PREFIX:
            raise ValueError("invalid commitment wire header or version")
        value = int.from_bytes(raw[len(COMMITMENT_WIRE_PREFIX) :], "big")
        _validate_commitment_value(value)
        return cls(value=value)


class PedersenCommitment:
    """Commit to an integer message in ``[0, Q)`` with a fresh or caller-supplied
    blinding factor."""

    def commit(self, message: int, blinding: int | None = None) -> tuple:
        """Return ``(commitment, blinding)``.

        Raises ``ValueError`` if ``message`` is outside ``[0, Q)`` or
        ``blinding`` is outside ``[1, Q)`` (including ``0`` and ``bool``) -
        rejected outright rather than silently reduced mod Q, and ``blinding=0``
        is forbidden because ``commit(m, 0) = g^m`` is not hiding.
        """
        if isinstance(message, bool) or not isinstance(message, int) or not 0 <= message < Q:
            raise ValueError(f"message must be an integer in [0, {Q}), got {message!r}")
        if blinding is None:
            # Exclude 0: commit(m, 0) = g^m alone isn't hiding (checkable by
            # anyone who guesses m). randbelow(Q - 1) + 1 samples [1, Q) uniformly.
            blinding = secrets.randbelow(Q - 1) + 1
        elif isinstance(blinding, bool) or not isinstance(blinding, int) or not 1 <= blinding < Q:
            raise ValueError(f"blinding must be an integer in [1, {Q}), got {blinding!r}")
        value = (powmod(G, message, P) * powmod(H, blinding, P)) % P
        return Commitment(value), blinding

    def verify_opening(self, commitment: Commitment, message: int, blinding: int) -> bool:
        """Check that ``(message, blinding)`` is a valid opening of ``commitment``.

        Raises ``TypeError`` if ``commitment`` is not a :class:`Commitment` or
        if ``commitment.value``, ``message``, or ``blinding`` is not a strict
        ``int`` (``bool`` is rejected). Returns ``False`` if any value is
        outside its valid cryptographic range.
        """
        if not isinstance(commitment, Commitment):
            raise TypeError(f"commitment must be a Commitment, got {type(commitment).__name__}")
        if isinstance(commitment.value, bool) or not isinstance(commitment.value, int):
            raise TypeError(
                f"commitment.value must be an integer, got {type(commitment.value).__name__}"
            )
        if isinstance(message, bool) or not isinstance(message, int):
            raise TypeError(f"message must be an integer, got {type(message).__name__}")
        if isinstance(blinding, bool) or not isinstance(blinding, int):
            raise TypeError(f"blinding must be an integer, got {type(blinding).__name__}")
        if not (1 < commitment.value < P and 0 <= message < Q and 1 <= blinding < Q):
            return False
        expected = (powmod(G, message, P) * powmod(H, blinding, P)) % P
        return commitment.value == expected
