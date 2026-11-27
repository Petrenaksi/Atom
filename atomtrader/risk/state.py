"""Per-trading-day risk state: daily loss, trade count, loss streak and cooldown.

The trading day is defined in the configured local timezone (default Europe/Madrid).
Realized P&L here is *net*: it includes every fee, plus network fees burned by failed
transactions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from atomtrader.core.money import ZERO


@dataclass(frozen=True, slots=True)
class RiskEvent:
    at: datetime
    kind: str
    detail: str


@dataclass
class DailyRiskState:
    timezone: str = "Europe/Madrid"
    trading_day: date | None = None
    start_equity_eur: Decimal = ZERO
    realized_net_pnl_eur: Decimal = ZERO
    entries_today: int = 0
    consecutive_losses: int = 0
    cooldown_until: datetime | None = None
    daily_halt: bool = False
    events: list[RiskEvent] = field(default_factory=list)

    def local_day(self, now: datetime) -> date:
        if now.tzinfo is None:
            raise ValueError("timezone-aware datetimes required")
        return now.astimezone(ZoneInfo(self.timezone)).date()

    def roll_if_new_day(self, now: datetime, equity_eur: Decimal) -> bool:
        """Start a new trading day if needed. Loss streak and cooldown carry over: a new
        calendar day is not a reason to resume revenge trading."""
        day = self.local_day(now)
        if day == self.trading_day:
            return False
        self.trading_day = day
        self.start_equity_eur = equity_eur
        self.realized_net_pnl_eur = ZERO
        self.entries_today = 0
        self.daily_halt = False
        self.events.append(RiskEvent(now, "new_day", f"start_equity={equity_eur}"))
        return True

    def daily_loss_limit_eur(self, max_daily_loss_pct: Decimal) -> Decimal:
        return self.start_equity_eur * max_daily_loss_pct / Decimal(100)

    def record_entry(self) -> None:
        self.entries_today += 1

    def record_failed_tx_fee(self, fee_eur: Decimal, now: datetime, max_daily_loss_pct: Decimal) -> None:
        self.realized_net_pnl_eur -= fee_eur
        self._check_halt(now, max_daily_loss_pct)

    def record_closed_trade(
        self,
        net_pnl_eur: Decimal,
        now: datetime,
        max_daily_loss_pct: Decimal,
        losses_for_cooldown: int,
        cooldown_minutes: int,
    ) -> None:
        self.realized_net_pnl_eur += net_pnl_eur
        if net_pnl_eur < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= losses_for_cooldown:
                self.cooldown_until = now + timedelta(minutes=cooldown_minutes)
                self.consecutive_losses = 0
                self.events.append(
                    RiskEvent(now, "cooldown", f"{losses_for_cooldown} consecutive losses; until {self.cooldown_until.isoformat()}")
                )
        else:
            self.consecutive_losses = 0
        self._check_halt(now, max_daily_loss_pct)

    def _check_halt(self, now: datetime, max_daily_loss_pct: Decimal) -> None:
        limit = self.daily_loss_limit_eur(max_daily_loss_pct)
        if not self.daily_halt and limit > 0 and -self.realized_net_pnl_eur >= limit:
            self.daily_halt = True
            self.events.append(
                RiskEvent(now, "daily_loss_limit", f"realized_net={self.realized_net_pnl_eur} limit={limit}")
            )

    def in_cooldown(self, now: datetime) -> bool:
        return self.cooldown_until is not None and now < self.cooldown_until
