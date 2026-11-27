"""Execution engine: the only path from an approved order to a provider.

Guarantees:
  * write-ahead: order persisted PENDING, then SUBMITTED, *then* sent
  * never assumes success: only a final provider result reaches the ledger
  * idempotent: re-submitting a known order id returns the stored outcome
  * strictly bounded retries; each attempt is its own persisted order id
  * on restart, SUBMITTED orders are reconciled before any token with an unknown
    outcome may be traded again
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from atomtrader.core.models import AccountKind, ExecutionResult, OrderRequest, OrderStatus
from atomtrader.execution.base import ExecutionProvider
from atomtrader.persistence.repository import Repository
from atomtrader.portfolio.ledger import ClosedTrade, Ledger

HARD_MAX_ATTEMPTS = 3
RETRYABLE_ERRORS = frozenset({"slippage_exceeded", "simulated_tx_failure", "tx_dropped", "blockhash_expired"})


class UnreconciledOrderError(RuntimeError):
    """An order's outcome is unknown. Trading that token is blocked until resolved."""


@dataclass(frozen=True, slots=True)
class SubmitOutcome:
    result: ExecutionResult
    attempts: int
    closed_trade: ClosedTrade | None


class ExecutionEngine:
    def __init__(
        self,
        provider: ExecutionProvider,
        repo: Repository,
        ledger: Ledger,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if provider.account_kind is not ledger.account_kind:
            raise ValueError("provider and ledger account kinds differ")
        self.provider = provider
        self.repo = repo
        self.ledger = ledger
        self.clock = clock
        self.unresolved_tokens: set[str] = set()
        self._lock = asyncio.Lock()  # serializes order flow (also orders `seq`)

    @staticmethod
    def attempt_id(base_id: str, attempt: int) -> str:
        return f"{base_id}.a{attempt}"

    async def reconcile_on_startup(self) -> list[str]:
        """Resolve orders left PENDING/SUBMITTED by a crash. Returns ids still unknown."""
        unresolved: list[str] = []
        for row in await self.repo.unreconciled_orders():
            now = self.clock()
            if row.status == OrderStatus.PENDING.value:
                # Written before sending and never marked submitted: provably not sent.
                await self.repo.save_result(
                    ExecutionResult(
                        client_order_id=row.client_order_id,
                        status=OrderStatus.REJECTED,
                        account_kind=self.ledger.account_kind,
                        error="not_submitted_before_restart",
                    ),
                    now,
                )
                continue
            result = await self.provider.lookup(row.client_order_id)
            if result is None and self.provider.account_kind is AccountKind.PAPER:
                # Simulated fills exist only in process memory: a crash before recording
                # means the paper trade never happened. (A live provider must instead query
                # the chain; unknown live outcomes stay blocked below.)
                await self.repo.save_result(ExecutionResult(
                    client_order_id=row.client_order_id, status=OrderStatus.REJECTED,
                    account_kind=AccountKind.PAPER, error="paper_fill_lost_in_restart"), now)
                await self.repo.log_event(now, "paper_order_lost_in_restart", row.client_order_id)
                continue
            if result is None or result.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED):
                unresolved.append(row.client_order_id)
                self.unresolved_tokens.add(f"{row.chain}:{row.token_address}")
                await self.repo.log_event(now, "unreconciled_order", row.client_order_id)
                continue
            await self.repo.save_result(result, now)
            self.ledger.apply(result)
        return unresolved

    async def submit(self, order: OrderRequest, max_attempts: int = 1) -> SubmitOutcome:
        if not 1 <= max_attempts <= HARD_MAX_ATTEMPTS:
            raise ValueError(f"max_attempts must be within 1..{HARD_MAX_ATTEMPTS}")
        if order.token.key in self.unresolved_tokens:
            raise UnreconciledOrderError(f"unresolved order outcome for {order.token.key}")

        async with self._lock:
            result: ExecutionResult | None = None
            for attempt in range(1, max_attempts + 1):
                attempt_order = replace(order, client_order_id=self.attempt_id(order.client_order_id, attempt))
                result = await self._run_attempt(attempt_order)
                closed = None
                if result.status is OrderStatus.FILLED or result.status is OrderStatus.REJECTED:
                    if result.status is OrderStatus.FILLED:
                        closed = self.ledger.apply(result)
                    return SubmitOutcome(result, attempt, closed)
                # FAILED: fee burned; ledger books it. Retry only known-transient failures.
                self.ledger.apply(result)
                if result.error not in RETRYABLE_ERRORS:
                    break
            assert result is not None
            return SubmitOutcome(result, attempt, None)

    async def _run_attempt(self, order: OrderRequest) -> ExecutionResult:
        now = self.clock()
        if not await self.repo.insert_order(order, self.provider.account_kind, now):
            # Known id (e.g. resubmission after restart): never send it twice.
            status = await self.repo.get_order_status(order.client_order_id)
            if status in (OrderStatus.PENDING, OrderStatus.SUBMITTED):
                raise UnreconciledOrderError(order.client_order_id)
            stored = [r for r in await self.repo.load_final_results(self.provider.account_kind)
                      if r.client_order_id == order.client_order_id]
            if not stored:
                raise UnreconciledOrderError(order.client_order_id)
            # Ledger.apply de-duplicates by order id, so returning it here is safe.
            return stored[0]

        await self.repo.mark_submitted(order.client_order_id, now)
        try:
            result = await self.provider.execute(order)
        except Exception as exc:  # outcome unknown: do NOT assume success or failure
            self.unresolved_tokens.add(order.token.key)
            await self.repo.log_event(self.clock(), "execution_exception", f"{order.client_order_id}: {exc!r}")
            raise UnreconciledOrderError(order.client_order_id) from exc
        try:
            await self.repo.save_result(result, self.clock())
        except Exception as exc:  # executed but not recorded: ledger cannot be trusted for this token
            self.unresolved_tokens.add(order.token.key)
            raise UnreconciledOrderError(f"{order.client_order_id}: result not recorded ({exc!r})") from exc
        return result
