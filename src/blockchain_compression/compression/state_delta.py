"""Delta-based state storage with idempotent transaction application.

Instead of persisting the full state after every block, only the changes
(deltas) produced by each transaction are applied to an in-memory state.
Every transaction must carry a unique ``tx_id``; replaying an
already-applied transaction is a no-op, so retries and redelivery cannot
corrupt the state.
"""

import logging
from collections.abc import Iterable

logger = logging.getLogger(__name__)

_REQUIRED_FIELDS = ("tx_id", "account", "amount")


class StateDelta:
    """Tracks account state by applying each transaction exactly once."""

    def __init__(self):
        self.current_state = {}
        self._applied_tx_ids = set()

    @staticmethod
    def validate_transaction(transaction) -> None:
        """Raise ``TypeError``/``ValueError`` if ``transaction`` is malformed.

        Public so callers (e.g. ``Chain.add_block``) can pre-validate a whole
        batch of transactions before applying any of them, avoiding a
        partially-applied batch when a later transaction turns out invalid.
        """
        if not isinstance(transaction, dict):
            raise TypeError(f"transaction must be a dict, got {type(transaction).__name__}")
        missing = [field for field in _REQUIRED_FIELDS if field not in transaction]
        if missing:
            raise ValueError(f"transaction missing required field(s): {', '.join(missing)}")
        if not isinstance(transaction["tx_id"], str) or not transaction["tx_id"]:
            raise ValueError("tx_id must be a non-empty string")
        if not isinstance(transaction["account"], str) or not transaction["account"]:
            raise ValueError("account must be a non-empty string")
        amount = transaction["amount"]
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise ValueError("amount must be a number")

    def apply_transaction(self, transaction: dict) -> dict:
        """Apply ``transaction`` to the state and return the resulting delta.

        The transaction dict must contain ``tx_id`` (unique identifier),
        ``account`` and ``amount``. Returns ``{account: new_balance}``, or an
        empty dict when the transaction was already applied (idempotent replay).
        """
        self.validate_transaction(transaction)
        tx_id = transaction["tx_id"]
        if tx_id in self._applied_tx_ids:
            logger.warning("Transaction %s already applied; ignoring replay.", tx_id)
            return {}
        account = transaction["account"]
        new_balance = self.current_state.get(account, 0) + transaction["amount"]
        self.current_state[account] = new_balance
        self._applied_tx_ids.add(tx_id)
        return {account: new_balance}

    def get_current_state(self) -> dict:
        """Return a copy of the full current state (account -> balance)."""
        return dict(self.current_state)

    def get_applied_tx_ids(self) -> frozenset:
        """Return the set of transaction IDs already applied (replay-protection state)."""
        return frozenset(self._applied_tx_ids)

    @classmethod
    def from_snapshot(cls, state: dict, applied_tx_ids: Iterable[str]) -> "StateDelta":
        """Rehydrate a ``StateDelta`` from a persisted balance snapshot and tx-id set.

        Used when reloading from storage: pruned blocks no longer have raw
        transaction data to replay, so the snapshot (not replay) is the only
        way to restore state.
        """
        instance = cls()
        instance.current_state = dict(state)
        instance._applied_tx_ids = set(applied_tx_ids)
        return instance
