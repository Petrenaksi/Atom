"""Decimal money helpers and EUR conversion.

All monetary values in the deterministic core are ``Decimal``. Floats are rejected at the
boundaries because binary rounding errors accumulate in P&L and fee accounting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, getcontext

getcontext().prec = 40

ZERO = Decimal(0)
ONE = Decimal(1)
BPS = Decimal(10_000)
EUR_CENT = Decimal("0.01")


def D(value: Decimal | int | str) -> Decimal:
    """Convert to Decimal, refusing floats (use strings for literals)."""
    if isinstance(value, float):
        raise TypeError("float passed to money arithmetic; use str or Decimal")
    if isinstance(value, Decimal):
        return value
    return Decimal(value)


def bps(value: Decimal | int | str) -> Decimal:
    """Basis points to a fraction: bps(50) == 0.005."""
    return D(value) / BPS


def round_eur(amount: Decimal) -> Decimal:
    return amount.quantize(EUR_CENT, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class FxRate:
    """Units of EUR per one unit of ``currency`` observed at ``as_of`` from ``source``.

    Every fill stores the FxRate used, so EUR P&L is reproducible and auditable.
    """

    currency: str
    eur_per_unit: Decimal
    as_of: datetime
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.eur_per_unit, Decimal):
            raise TypeError("eur_per_unit must be Decimal")
        if self.eur_per_unit <= 0:
            raise ValueError("FX rate must be positive")

    def to_eur(self, amount: Decimal) -> Decimal:
        return D(amount) * self.eur_per_unit

    def from_eur(self, amount_eur: Decimal) -> Decimal:
        return D(amount_eur) / self.eur_per_unit


class FxTable:
    """Latest FX rates keyed by currency. Raises rather than guessing when a rate is missing
    or stale — the system must never invent market data."""

    def __init__(self, max_age_seconds: int = 900) -> None:
        self._rates: dict[str, FxRate] = {}
        self._max_age = max_age_seconds

    def update(self, rate: FxRate) -> None:
        self._rates[rate.currency.upper()] = rate

    def get(self, currency: str, now: datetime) -> FxRate:
        cur = currency.upper()
        if cur == "EUR":
            return FxRate("EUR", ONE, now, "identity")
        rate = self._rates.get(cur)
        if rate is None:
            raise MissingMarketData(f"no FX rate for {cur}")
        age = (now - rate.as_of).total_seconds()
        if age > self._max_age:
            raise MissingMarketData(f"FX rate for {cur} is stale ({age:.0f}s old)")
        return rate


class MissingMarketData(RuntimeError):
    """Raised when required data is unavailable. Callers must skip, never fabricate."""
