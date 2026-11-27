"""EUR ledger built only from reconciled execution results.

Profit is never reported from a submitted order — only from fills. Network fees burned by
failed transactions are booked as realized losses. The ledger can be rebuilt exactly from
the persisted results (event sourcing), which is how state survives restarts.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from atomtrader.core.models import AccountKind, ExecutionResult, Fill, OrderStatus, Side, TokenRef
from atomtrader.core.money import ZERO

# Positions with less than this many tokens left after a sell are treated as closed (dust).
DUST_QUANTITY = Decimal("1e-12")


class LedgerError(RuntimeError):
    pass


@dataclass
class Position:
    position_id: str
    token: TokenRef
    account_kind: AccountKind
    opened_at: datetime
    quantity: Decimal = ZERO
    cost_basis_eur: Decimal = ZERO  # includes entry fees of the remaining quantity
    realized_pnl_eur: Decimal = ZERO  # net, across partial exits
    fees_eur: Decimal = ZERO
    entry_price_usd: Decimal = ZERO
    closed_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def market_value_eur(self, price_eur: Decimal) -> Decimal:
        return self.quantity * price_eur

    def unrealized_pnl_eur(self, price_eur: Decimal) -> Decimal:
        return self.market_value_eur(price_eur) - self.cost_basis_eur


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    position_id: str
    token: TokenRef
    net_pnl_eur: Decimal
    fees_eur: Decimal
    opened_at: datetime
    closed_at: datetime
    account_kind: AccountKind


@dataclass
class Ledger:
    account_kind: AccountKind
    starting_cash_eur: Decimal
    cash_eur: Decimal = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict)
    closed_trades: list[ClosedTrade] = field(default_factory=list)
    total_fees_eur: Decimal = ZERO
    failed_tx_fees_eur: Decimal = ZERO
    applied_order_ids: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.cash_eur = self.starting_cash_eur

    # --------------------------------------------------------------- queries
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.is_open]

    def realized_pnl_eur(self) -> Decimal:
        return sum((t.net_pnl_eur for t in self.closed_trades), ZERO) + sum(
            (p.realized_pnl_eur for p in self.open_positions()), ZERO
        ) - self.failed_tx_fees_eur

    def equity_eur(self, prices_eur: dict[str, Decimal]) -> Decimal:
        """Mark-to-market equity. Raises if an open position has no price — an unpriced
        position is not silently valued at zero or at cost."""
        total = self.cash_eur
        for p in self.open_positions():
            if p.token.key not in prices_eur:
                raise LedgerError(f"no price for open position {p.token.key}")
            total += p.market_value_eur(prices_eur[p.token.key])
        return total

    # --------------------------------------------------------------- mutation
    def apply(self, result: ExecutionResult) -> ClosedTrade | None:
        """Apply a final execution result exactly once. Returns a ClosedTrade when a
        position is fully exited."""
        if result.account_kind is not self.account_kind:
            raise LedgerError("account kind mismatch: paper and live results never mix")
        if result.client_order_id in self.applied_order_ids:
            return None
        if result.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED):
            raise LedgerError("cannot apply an unreconciled order")
        self.applied_order_ids.add(result.client_order_id)

        if result.status is OrderStatus.FAILED:
            self.cash_eur -= result.failed_fee_eur
            self.total_fees_eur += result.failed_fee_eur
            self.failed_tx_fees_eur += result.failed_fee_eur
            return None
        if result.status is OrderStatus.REJECTED:
            return None
        if result.fill is None:
            raise LedgerError("filled result without fill")
        return self._apply_fill(result.fill)

    def _apply_fill(self, fill: Fill) -> ClosedTrade | None:
        self.cash_eur += fill.cash_delta_eur
        self.total_fees_eur += fill.fee_eur
        pos = self.positions.get(fill.position_id)

        if fill.side is Side.BUY:
            if pos is None:
                pos = Position(
                    position_id=fill.position_id,
                    token=fill.token,
                    account_kind=fill.account_kind,
                    opened_at=fill.executed_at,
                    entry_price_usd=fill.price_usd,
                )
                self.positions[fill.position_id] = pos
            elif not pos.is_open:
                raise LedgerError("buy into a closed position")
            pos.quantity += fill.quantity
            pos.cost_basis_eur += fill.gross_eur + fill.fee_eur
            pos.fees_eur += fill.fee_eur
            return None

        if pos is None or not pos.is_open:
            raise LedgerError(f"sell for unknown/closed position {fill.position_id}")
        if fill.quantity > pos.quantity + DUST_QUANTITY:
            raise LedgerError("sell quantity exceeds position")
        fraction = min(fill.quantity / pos.quantity, Decimal(1))
        basis_out = pos.cost_basis_eur * fraction
        pos.realized_pnl_eur += fill.gross_eur - fill.fee_eur - basis_out
        pos.cost_basis_eur -= basis_out
        pos.quantity -= fill.quantity
        pos.fees_eur += fill.fee_eur
        if pos.quantity <= DUST_QUANTITY:
            pos.quantity = ZERO
            pos.cost_basis_eur = ZERO
            pos.closed_at = fill.executed_at
            trade = ClosedTrade(
                position_id=pos.position_id,
                token=pos.token,
                net_pnl_eur=pos.realized_pnl_eur,
                fees_eur=pos.fees_eur,
                opened_at=pos.opened_at,
                closed_at=fill.executed_at,
                account_kind=pos.account_kind,
            )
            self.closed_trades.append(trade)
            return trade
        return None

    @classmethod
    def replay(
        cls, account_kind: AccountKind, starting_cash_eur: Decimal, results: Iterable[ExecutionResult]
    ) -> Ledger:
        ledger = cls(account_kind=account_kind, starting_cash_eur=starting_cash_eur)
        for r in results:
            ledger.apply(r)
        return ledger
