import abc
import contextlib
import logging
from contextvars import ContextVar, Token
from typing import Generic, Optional, TypeVar

logger = logging.getLogger(__name__)

Tx = TypeVar("Tx")


class UOW(Generic[Tx]):
    """
    Async transaction context with ContextVar-bound transaction object.

    Designed to allow a single (even global) UOW instance to be used safely
    across concurrent asyncio tasks. Each task gets its own bound transaction
    object via ContextVar without having to thread tx_obj through call chains.
    """

    def __init__(self) -> None:
        # Per-instance ContextVars to avoid cross-UOW interference.
        self._current_tx: ContextVar[Optional[Tx]] = ContextVar(
            f"{__name__}.UOW.current_tx:{id(self)}", default=None
        )
        # Store the token per-context so concurrent tasks using the same UOW
        # don't overwrite each other's tokens.
        self._token_var: ContextVar[Optional[Token[Optional[Tx]]]] = ContextVar(
            f"{__name__}.UOW.token:{id(self)}", default=None
        )

    @property
    def tx_obj(self) -> Tx:
        tx = self._current_tx.get()
        if tx is None:
            raise ValueError("No active transaction")
        return tx

    @property
    def in_atomic_block(self) -> bool:
        return self._current_tx.get() is not None

    @abc.abstractmethod
    async def commit(self) -> None:
        """Commit transaction"""

    @abc.abstractmethod
    async def rollback(self) -> None:
        """Rollback transaction"""

    @abc.abstractmethod
    async def start_transaction(self) -> Tx:
        """Start transaction and return transaction object"""

    async def __aenter__(self) -> "UOW[Tx]":
        """Entering the context creates and binds a new transaction."""
        if self.in_atomic_block:
            raise RuntimeError(
                "A durable transaction block cannot be nested within another transaction block."
            )

        # Start the transaction first; only set context if successful.
        tx = await self.start_transaction()
        token = self._current_tx.set(tx)
        self._token_var.set(token)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool | None:
        token = self._token_var.get()
        try:
            if exc_type is None:
                try:
                    await self.commit()
                except Exception:
                    logger.exception("Error during transaction commit; rolling back")
                    with contextlib.suppress(Exception):
                        await self.rollback()
                    # Propagate commit error
                    return False
            else:
                # On error inside the block: try rollback but don't shadow the original error
                with contextlib.suppress(Exception):
                    await self.rollback()
                return False
        finally:
            if token is not None:
                # Always restore previous context value
                with contextlib.suppress(Exception):
                    self._current_tx.reset(token)
                # Clear token in this context
                self._token_var.set(None)
