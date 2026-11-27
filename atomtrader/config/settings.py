"""Configuration with hard safety ceilings and live-trading gating.

Risk limits may be tuned by the operator within hard ceilings defined here in code. No
strategy, optimizer or LLM can raise them: the ceilings are validated on every load and
``RiskLimits`` is immutable at runtime.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from atomtrader.core.models import Chain

# Hard ceilings: changing these requires a code change + review, never a config edit.
HARD_MAX_RISK_PER_TRADE_PCT = Decimal("2.0")
HARD_MAX_DAILY_LOSS_PCT = Decimal("10.0")
HARD_MAX_POSITIONS = 10
HARD_MAX_TRADES_PER_DAY = 50
HARD_MAX_SLIPPAGE_BPS = 1500

LIVE_AUTHORIZATION_PHRASE = "I AUTHORIZE LIVE TRADING WITH REAL FUNDS"


class TradingMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class RiskLimits(BaseModel):
    """Portfolio-level risk budget. Percentages are of equity (e.g. 1.0 == 1%)."""

    model_config = ConfigDict(frozen=True)

    min_risk_per_trade_pct: Decimal = Decimal("0.5")
    max_risk_per_trade_pct: Decimal = Decimal("1.0")
    max_daily_loss_pct: Decimal = Decimal("3.0")
    max_open_positions: int = 3
    max_trades_per_day: int = 20
    consecutive_losses_for_cooldown: int = 3
    cooldown_minutes: int = 120
    max_position_pct_of_equity: Decimal = Decimal("20")
    max_total_exposure_pct: Decimal = Decimal("60")
    max_positions_per_narrative: int = 1
    # Position notional as a fraction of total pool liquidity (exit feasibility).
    max_pool_liquidity_fraction_pct: Decimal = Decimal("1.0")
    min_pool_liquidity_eur: Decimal = Decimal("25000")
    max_slippage_bps: int = 300
    max_round_trip_cost_pct: Decimal = Decimal("4.0")
    min_position_eur: Decimal = Decimal("15")
    # Cash kept aside for exit transaction fees.
    fee_reserve_eur: Decimal = Decimal("10")
    leverage: Decimal = Decimal("1")

    @model_validator(mode="after")
    def _enforce_ceilings(self) -> RiskLimits:
        if self.leverage != 1:
            raise ValueError("leverage is not permitted")
        if not (0 < self.min_risk_per_trade_pct <= self.max_risk_per_trade_pct):
            raise ValueError("risk per trade range invalid")
        if self.max_risk_per_trade_pct > HARD_MAX_RISK_PER_TRADE_PCT:
            raise ValueError(f"risk per trade exceeds hard ceiling {HARD_MAX_RISK_PER_TRADE_PCT}%")
        if not (0 < self.max_daily_loss_pct <= HARD_MAX_DAILY_LOSS_PCT):
            raise ValueError(f"daily loss limit must be in (0, {HARD_MAX_DAILY_LOSS_PCT}]%")
        if not (1 <= self.max_open_positions <= HARD_MAX_POSITIONS):
            raise ValueError("max_open_positions out of bounds")
        if not (1 <= self.max_trades_per_day <= HARD_MAX_TRADES_PER_DAY):
            raise ValueError("max_trades_per_day out of bounds")
        if not (0 < self.max_slippage_bps <= HARD_MAX_SLIPPAGE_BPS):
            raise ValueError("max_slippage_bps out of bounds")
        if self.consecutive_losses_for_cooldown < 1 or self.cooldown_minutes < 0:
            raise ValueError("cooldown configuration invalid")
        if not (0 < self.max_position_pct_of_equity <= 100):
            raise ValueError("max_position_pct_of_equity out of bounds")
        if not (0 < self.max_total_exposure_pct <= 100):
            raise ValueError("max_total_exposure_pct must not exceed 100 (no leverage)")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ATOM_", env_nested_delimiter="__", env_file=".env", extra="ignore"
    )

    mode: TradingMode = TradingMode.PAPER
    # Must equal LIVE_AUTHORIZATION_PHRASE for live mode. Provided by the account owner only.
    live_authorization: SecretStr | None = None
    # Live capital is capped separately from paper capital; never increased automatically.
    live_max_capital_eur: Decimal | None = None

    starting_capital_eur: Decimal = Decimal("1000")
    reporting_currency: str = "EUR"
    timezone: str = "Europe/Madrid"
    database_url: str = "sqlite+aiosqlite:///./atom.db"

    enabled_chains: frozenset[Chain] = Field(
        default=frozenset({Chain.SOLANA, Chain.BASE, Chain.BSC})
    )
    risk: RiskLimits = Field(default_factory=RiskLimits)
    log_json: bool = True


class LiveTradingNotAuthorized(PermissionError):
    pass


def assert_live_authorized(settings: Settings) -> None:
    """Raise unless the owner has explicitly authorized live trading.

    Paper mode is the default. Live requires mode=live, the exact authorization phrase,
    and an explicit live capital cap.
    """
    if settings.mode is not TradingMode.LIVE:
        raise LiveTradingNotAuthorized("mode is not 'live'")
    phrase = settings.live_authorization.get_secret_value() if settings.live_authorization else ""
    if phrase != LIVE_AUTHORIZATION_PHRASE:
        raise LiveTradingNotAuthorized("live authorization phrase missing or incorrect")
    if settings.live_max_capital_eur is None or settings.live_max_capital_eur <= 0:
        raise LiveTradingNotAuthorized("live_max_capital_eur must be set explicitly")
