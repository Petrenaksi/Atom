"""Domain models shared by the deterministic core.

Values are immutable. Monetary amounts are Decimal; token quantities are Decimal in whole
token units (not raw on-chain integer units — adapters convert using token decimals).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from atomtrader.core.money import FxRate


class Chain(StrEnum):
    SOLANA = "solana"
    ETHEREUM = "ethereum"
    BASE = "base"
    BSC = "bsc"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class AccountKind(StrEnum):
    """Every balance, fill and P&L figure is tagged so simulated values can never be
    displayed or summed as real ones."""

    PAPER = "paper"
    LIVE = "live"


class OrderStatus(StrEnum):
    PENDING = "pending"  # persisted, not yet sent
    SUBMITTED = "submitted"  # sent, outcome unknown — must be reconciled
    FILLED = "filled"
    FAILED = "failed"  # landed/attempted and failed (fees may still be paid)
    REJECTED = "rejected"  # refused before submission (risk, controls, validation)


def norm_address(chain: Chain, address: str) -> str:
    """EVM addresses are case-insensitive (providers mix checksummed and lowercase forms):
    normalise to lowercase. Solana base58 addresses are case-sensitive: keep as is."""
    return address if chain is Chain.SOLANA else address.lower()


@dataclass(frozen=True, slots=True)
class TokenRef:
    chain: Chain
    address: str
    symbol: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "address", norm_address(self.chain, self.address))

    @property
    def key(self) -> str:
        return f"{self.chain.value}:{self.address}"


@dataclass(frozen=True, slots=True)
class MarketQuote:
    """Observed market state for a token's main pool. ``liquidity_usd`` is total pool
    liquidity (both sides). Market cap is deliberately *not* used for exit feasibility."""

    token: TokenRef
    price_usd: Decimal
    liquidity_usd: Decimal
    as_of: datetime
    source: str


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """A buy spends ``notional_eur``; a sell disposes of ``quantity`` tokens."""

    client_order_id: str
    position_id: str
    token: TokenRef
    side: Side
    max_slippage_bps: int
    reason: str
    notional_eur: Decimal | None = None
    quantity: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.client_order_id:
            raise ValueError("client_order_id is required for idempotency")
        if self.side is Side.BUY and (self.notional_eur is None or self.notional_eur <= 0):
            raise ValueError("buy orders need a positive notional_eur")
        if self.side is Side.SELL and (self.quantity is None or self.quantity <= 0):
            raise ValueError("sell orders need a positive quantity")
        if self.max_slippage_bps <= 0:
            raise ValueError("max_slippage_bps must be positive")


@dataclass(frozen=True, slots=True)
class Fill:
    """A reconciled execution. ``gross_eur`` is tokens x execution price (EUR); ``fee_eur``
    is every explicit cost (network, priority/tip, pool and aggregator fees).

    Cash effect: buy = -(gross + fee); sell = +(gross - fee)."""

    client_order_id: str
    position_id: str
    token: TokenRef
    side: Side
    quantity: Decimal
    price_usd: Decimal
    gross_eur: Decimal
    fee_eur: Decimal
    fx: FxRate
    executed_at: datetime
    tx_ref: str
    account_kind: AccountKind

    @property
    def cash_delta_eur(self) -> Decimal:
        if self.side is Side.BUY:
            return -(self.gross_eur + self.fee_eur)
        return self.gross_eur - self.fee_eur


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    client_order_id: str
    status: OrderStatus
    account_kind: AccountKind
    fill: Fill | None = None
    # Network fees paid even when a transaction fails on-chain (gas/base fee).
    failed_fee_eur: Decimal = Decimal(0)
    realized_slippage_bps: Decimal | None = None
    error: str | None = None
    attempts: int = 1
    details: dict[str, str] = field(default_factory=dict)
