"""Delta-based state storage with idempotent transaction application.

Instead of persisting the full state after every block, only the changes
(deltas) produced by each transaction are applied to an in-memory state.
Every transaction must carry a unique ``tx_id``; replaying an
already-applied transaction is a no-op, so retries and redelivery cannot
corrupt the state.
"""

import logging

logger = logging.getLogger(__name__)

_REQUIRED_FIELDS = ("tx_id", "account", "amount")


class StateDelta:
    """Tracks account state by applying each transaction exactly once."""

    def __init__(self):
        self.current_state = {}
        self._applied_tx_ids = set()

    @staticmethod
    def _validate_transaction(transaction) -> None:
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
        self._validate_transaction(transaction)
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
