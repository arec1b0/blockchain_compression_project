"""RFC 3526 Group 14 (2048-bit MODP) safe prime, used as the Pedersen commitment group.

``P`` is a safe prime (``P = 2*Q + 1`` with ``Q`` prime), so ``Z_p*``'s only proper
subgroups have order 2 or ``Q`` - there is no small-cofactor/confused-subgroup
attack surface once ``G`` and ``H`` are confirmed (via ``_verify_group_parameters``
below, run at import time) to actually generate the order-``Q`` subgroup. Without
that check, a single hex-transcription typo in ``P`` would silently produce a
different, unverified group instead of a loud failure.
"""

import hashlib

# RFC 3526 section 3, "2048-bit MODP Group" (id 14): a standardized, publicly
# documented "nothing up my sleeve" prime derived from the digits of pi.
_P_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D"
    "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9"
    "DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AACAA68FFFFFFFFFFFFFFFF"
)
P = int(_P_HEX, 16)
Q = (P - 1) // 2
G = 2

# Second generator H, independent of G: hash a fixed domain-separated seed to a
# candidate group element, then SQUARE it to project into the order-Q subgroup.
#
# Squaring is essential, not cosmetic. The naive alternative - hash to an
# *exponent* instead, e.g. `H = pow(G, hash(seed), P)` - would make
# `log_G(H) = hash(seed) mod Q`, which is public arithmetic anyone can compute,
# completely breaking the commitment's binding property (an attacker could
# equivocate any commitment to any message). Squaring a candidate *element*
# instead means recovering log_G(H) requires solving an actual discrete-log
# problem, which is the whole point of a nothing-up-my-sleeve generator.
_H_SEED = b"blockchain-compression-project/pedersen-h/v1"
_h_candidate = int(hashlib.sha256(_H_SEED).hexdigest(), 16) % P
H = pow(_h_candidate, 2, P)


def _verify_group_parameters() -> None:
    """Fail loudly at import time rather than silently running on a broken
    group. Deliberately raises ``RuntimeError`` (not a bare ``assert``, which
    ``python -O`` strips) - this check must never be skippable."""
    if P.bit_length() != 2048:
        raise RuntimeError("P is not a 2048-bit prime - possible transcription error")
    if pow(G, Q, P) != 1:
        raise RuntimeError("G does not generate the order-Q subgroup")
    if pow(H, Q, P) != 1:
        raise RuntimeError("H does not generate the order-Q subgroup")
    if H in (0, 1):
        raise RuntimeError("degenerate H")


_verify_group_parameters()
