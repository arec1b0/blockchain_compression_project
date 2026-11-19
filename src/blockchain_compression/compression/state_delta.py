"""Delta-based state storage with idempotent transaction application.

Instead of persisting the full state after every block, only the changes
(deltas) produced by each transaction are applied to an in-memory state.
Account balances and transaction amounts are tracked in exact signed 64-bit
integer base units (``[-2**63, 2**63 - 1]``). Every transaction must carry a
unique ``tx_id``; replaying an already-applied transaction is a no-op when
calling :meth:`StateDelta.apply_transaction` directly, so retries and
redelivery cannot corrupt the state.
"""

import logging
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_REQUIRED_FIELDS = ("tx_id", "account", "amount")

MIN_BALANCE = -(2**63)
MAX_BALANCE = 2**63 - 1


@dataclass(frozen=True)
class BlockDelta:
    """Extracted persistence delta for a block's transactions against a state."""

    tx_ids: tuple[str, ...]
    accounts: frozenset[str]
    updated_accounts: dict[str, int]


class StateDelta:
    """Tracks account state in exact integer base units, applying each tx once.

    Replay protection uses an unbounded in-memory set by default so standalone
    usage never drops transaction history. When ``max_applied_tx_ids`` is
    provided, an LRU cache of that capacity is paired with a backing
    ``tx_lookup`` callable (such as :meth:`ChainStore.has_applied_tx`) so RAM
    usage stays bounded without weakening replay detection.
    """

    def __init__(
        self,
        *,
        max_applied_tx_ids: int | None = None,
        tx_lookup: Callable[[str], bool] | None = None,
    ):
        self._validate_cache_config(max_applied_tx_ids, tx_lookup)
        self.current_state: dict[str, int] = {}
        self._max_applied_tx_ids: int | None = max_applied_tx_ids
        self._tx_lookup: Callable[[str], bool] | None = tx_lookup
        self._has_overflow: bool = False
        self._applied_tx_ids: set[str] = set()
        self._applied_tx_cache: OrderedDict[str, None] = OrderedDict()

    @staticmethod
    def _validate_cache_config(
        max_applied_tx_ids: int | None, tx_lookup: Callable[[str], bool] | None
    ) -> None:
        if max_applied_tx_ids is None:
            return
        if (
            isinstance(max_applied_tx_ids, bool)
            or not isinstance(max_applied_tx_ids, int)
            or max_applied_tx_ids < 0
        ):
            raise ValueError("max_applied_tx_ids must be a non-negative integer or None")
        if not callable(tx_lookup):
            raise ValueError(
                "max_applied_tx_ids requires a callable tx_lookup to preserve replay protection"
            )

    @property
    def max_applied_tx_ids(self) -> int | None:
        """Maximum number of ``tx_id`` values cached in RAM, or ``None`` if unbounded."""
        return self._max_applied_tx_ids

    @property
    def is_applied_tx_cache_bounded(self) -> bool:
        """Return ``True`` when in-memory replay tracking uses a bounded LRU cache."""
        return self._max_applied_tx_ids is not None

    @property
    def has_evicted_tx_ids(self) -> bool:
        """Return ``True`` once any ``tx_id`` has overflowed the bounded RAM cache."""
        return self._has_overflow

    @property
    def cached_applied_tx_count(self) -> int:
        """Number of ``tx_id`` entries currently held in RAM."""
        if self._max_applied_tx_ids is not None:
            return len(self._applied_tx_cache)
        return len(self._applied_tx_ids)

    @staticmethod
    def validate_balance(value, *, field: str = "balance") -> None:
        """Validate that ``value`` is an exact signed 64-bit integer base unit."""
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"{field} must be an exact integer base unit, got {type(value).__name__}"
            )
        if not MIN_BALANCE <= value <= MAX_BALANCE:
            raise ValueError(
                f"{field} {value} is outside supported signed 64-bit integer range "
                f"[{MIN_BALANCE}, {MAX_BALANCE}]"
            )

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
        StateDelta.validate_balance(transaction["amount"], field="amount")

    def _record_applied_tx(self, tx_id: str) -> None:
        if self._max_applied_tx_ids is None:
            self._applied_tx_ids.add(tx_id)
            return
        if self._max_applied_tx_ids == 0:
            self._has_overflow = True
            return
        self._applied_tx_cache[tx_id] = None
        self._applied_tx_cache.move_to_end(tx_id)
        while len(self._applied_tx_cache) > self._max_applied_tx_ids:
            self._applied_tx_cache.popitem(last=False)
            self._has_overflow = True

    def is_applied(self, tx_id: str) -> bool:
        """Return ``True`` if ``tx_id`` has already been applied."""
        if self._max_applied_tx_ids is None:
            return tx_id in self._applied_tx_ids
        if tx_id in self._applied_tx_cache:
            self._applied_tx_cache.move_to_end(tx_id)
            return True
        if not self._has_overflow:
            return False
        assert self._tx_lookup is not None
        found = bool(self._tx_lookup(tx_id))
        if found and self._max_applied_tx_ids > 0:
            self._record_applied_tx(tx_id)
        return found

    def get_balance(self, account: str) -> int:
        """Return the current balance for ``account`` (``0`` if unseen)."""
        return self.current_state.get(account, 0)

    def apply_transaction(self, transaction: dict) -> dict[str, int]:
        """Apply ``transaction`` to the state and return the resulting delta.

        The transaction dict must contain ``tx_id`` (unique identifier),
        ``account`` and ``amount`` (integer base units). Returns
        ``{account: new_balance}``, or an empty dict when the transaction was
        already applied (idempotent replay).
        """
        self.validate_transaction(transaction)
        tx_id = transaction["tx_id"]
        if self.is_applied(tx_id):
            logger.warning("Transaction %s already applied; ignoring replay.", tx_id)
            return {}
        account = transaction["account"]
        new_balance = self.current_state.get(account, 0) + transaction["amount"]
        self.validate_balance(new_balance, field="balance")
        self.current_state[account] = new_balance
        self._record_applied_tx(tx_id)
        return {account: new_balance}

    def stage_batch(self, transactions: list[dict]) -> BlockDelta:
        """Validate ``transactions`` and compute a prospective :class:`BlockDelta`
        without mutating this ``StateDelta`` or copying full history.

        Time and memory are proportional to ``len(transactions)`` rather than
        total account or historical transaction counts.
        """
        if not isinstance(transactions, list):
            raise TypeError(f"transactions must be a list, got {type(transactions).__name__}")
        tx_ids: list[str] = []
        seen_tx_ids: set[str] = set()
        staged_balances: dict[str, int] = {}
        for tx in transactions:
            self.validate_transaction(tx)
            tx_id = tx["tx_id"]
            if tx_id in seen_tx_ids:
                raise ValueError(f"duplicate tx_id {tx_id!r} within block batch")
            if self.is_applied(tx_id):
                raise ValueError(f"transaction {tx_id!r} has already been applied")
            seen_tx_ids.add(tx_id)

            account = tx["account"]
            base_balance = (
                staged_balances[account]
                if account in staged_balances
                else self.current_state.get(account, 0)
            )
            new_balance = base_balance + tx["amount"]
            self.validate_balance(new_balance, field="balance")
            staged_balances[account] = new_balance
            tx_ids.append(tx_id)

        updated_accounts = {
            account: staged_balances[account] for account in sorted(staged_balances)
        }
        return BlockDelta(
            tx_ids=tuple(tx_ids),
            accounts=frozenset(staged_balances),
            updated_accounts=updated_accounts,
        )

    def commit_delta(self, delta: BlockDelta, *, check_backing_lookup: bool = True) -> None:
        """Atomically apply a staged :class:`BlockDelta` to this state in
        ``O(len(delta.tx_ids))`` work.

        Set ``check_backing_lookup=False`` when committing a pre-validated
        batch immediately after the external persistence backing ``tx_lookup``
        has written the batch's ``tx_ids``.
        """
        if not isinstance(delta, BlockDelta):
            raise TypeError(f"delta must be a BlockDelta, got {type(delta).__name__}")
        for account, balance in delta.updated_accounts.items():
            if not isinstance(account, str) or not account:
                raise ValueError("account names in delta must be non-empty strings")
            self.validate_balance(balance, field="balance")
        seen_tx_ids: set[str] = set()
        for tx_id in delta.tx_ids:
            if not isinstance(tx_id, str) or not tx_id:
                raise ValueError("tx_ids in delta must be non-empty strings")
            if tx_id in seen_tx_ids:
                raise ValueError(f"duplicate tx_id {tx_id!r} within delta")
            already_applied = (
                self.is_applied(tx_id)
                if check_backing_lookup
                else (
                    tx_id in self._applied_tx_cache
                    if self._max_applied_tx_ids is not None
                    else tx_id in self._applied_tx_ids
                )
            )
            if already_applied:
                raise ValueError(f"transaction {tx_id!r} has already been applied")
            seen_tx_ids.add(tx_id)

        self.current_state.update(delta.updated_accounts)
        for tx_id in delta.tx_ids:
            self._record_applied_tx(tx_id)

    def configure_applied_tx_cache(
        self, max_size: int | None, tx_lookup: Callable[[str], bool] | None = None
    ) -> None:
        """Attach, resize, or remove a bounded LRU cache on this state."""
        self._validate_cache_config(max_size, tx_lookup)
        existing = (
            list(self._applied_tx_cache.keys())
            if self._max_applied_tx_ids is not None
            else list(self._applied_tx_ids)
        )
        if max_size is None:
            if self._has_overflow:
                raise ValueError(
                    "cannot disable bounded applied_tx_ids cache after evictions have occurred"
                )
            self._applied_tx_ids.clear()
            self._applied_tx_ids.update(existing)
            self._applied_tx_cache.clear()
            self._max_applied_tx_ids = None
            self._tx_lookup = None
            self._has_overflow = False
            return

        prior_overflow = self._has_overflow
        self._applied_tx_ids.clear()
        self._applied_tx_cache.clear()
        self._max_applied_tx_ids = max_size
        self._tx_lookup = tx_lookup
        self._has_overflow = prior_overflow
        for tx_id in existing:
            self._record_applied_tx(tx_id)

    def get_current_state(self) -> dict[str, int]:
        """Return a copy of the full current state (account -> integer balance)."""
        return dict(self.current_state)

    def get_applied_tx_ids(self) -> frozenset[str]:
        """Return transaction IDs currently held in RAM for replay protection.

        When a bounded cache is active, returns only the cached subset (older
        IDs remain enforceable through the backing lookup).
        """
        if self._max_applied_tx_ids is not None:
            return frozenset(self._applied_tx_cache.keys())
        return frozenset(self._applied_tx_ids)

    def copy(self) -> "StateDelta":
        """Return an independent copy of this :class:`StateDelta`."""
        cloned = StateDelta(
            max_applied_tx_ids=self._max_applied_tx_ids,
            tx_lookup=self._tx_lookup,
        )
        cloned.current_state = dict(self.current_state)
        cloned._has_overflow = self._has_overflow
        if self._max_applied_tx_ids is None:
            cloned._applied_tx_ids = set(self._applied_tx_ids)
        else:
            cloned._applied_tx_cache = OrderedDict.fromkeys(self._applied_tx_cache.keys())
        return cloned

    @classmethod
    def from_snapshot(
        cls,
        state: dict[str, int],
        applied_tx_ids: Iterable[str],
        *,
        max_applied_tx_ids: int | None = None,
        tx_lookup: Callable[[str], bool] | None = None,
        has_overflow: bool = False,
    ) -> "StateDelta":
        """Rehydrate a ``StateDelta`` from a persisted balance snapshot and tx-id
        collection.

        Used when reloading from storage: pruned blocks no longer have raw
        transaction data to replay, so the snapshot (not replay) is the only
        way to restore state.
        """
        if not isinstance(state, dict):
            raise TypeError(f"state must be a dict, got {type(state).__name__}")
        if not isinstance(has_overflow, bool):
            raise TypeError(f"has_overflow must be a bool, got {type(has_overflow).__name__}")
        if has_overflow and max_applied_tx_ids is None:
            raise ValueError("has_overflow requires max_applied_tx_ids to be configured")
        validated_state: dict[str, int] = {}
        for account, balance in state.items():
            if not isinstance(account, str) or not account:
                raise ValueError("account names in state snapshot must be non-empty strings")
            cls.validate_balance(balance, field="balance")
            validated_state[account] = balance

        instance = cls(max_applied_tx_ids=max_applied_tx_ids, tx_lookup=tx_lookup)
        instance.current_state = validated_state
        for tx_id in applied_tx_ids:
            if not isinstance(tx_id, str) or not tx_id:
                raise ValueError("applied_tx_ids must contain only non-empty strings")
            instance._record_applied_tx(tx_id)
        if has_overflow:
            instance._has_overflow = True
        return instance


def extract_block_delta(transactions: list[dict], state: StateDelta) -> BlockDelta:
    """Derive ``tx_ids``, ``accounts``, and ``updated_accounts`` for persistence.

    Validates every transaction and looks up resulting balances for touched
    accounts directly from ``state`` without copying the entire state map.
    Raises ``TypeError`` if ``transactions`` is not a ``list`` or ``state`` is
    not a :class:`StateDelta`, and ``KeyError`` if an account referenced in
    ``transactions`` is absent from ``state``.
    """
    if not isinstance(transactions, list):
        raise TypeError(f"transactions must be a list, got {type(transactions).__name__}")
    if not isinstance(state, StateDelta):
        raise TypeError(f"state must be a StateDelta, got {type(state).__name__}")

    tx_ids: list[str] = []
    accounts: set[str] = set()
    for tx in transactions:
        StateDelta.validate_transaction(tx)
        tx_ids.append(tx["tx_id"])
        accounts.add(tx["account"])

    missing = sorted(account for account in accounts if account not in state.current_state)
    if missing:
        raise KeyError(f"account(s) missing from state: {', '.join(missing)}")

    updated_accounts = {account: state.current_state[account] for account in sorted(accounts)}
    return BlockDelta(
        tx_ids=tuple(tx_ids),
        accounts=frozenset(accounts),
        updated_accounts=updated_accounts,
    )
