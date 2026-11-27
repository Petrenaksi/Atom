"""The risk engine: final authority over every new entry.

Strategies submit a ``TradeIntent``; the engine either rejects it (with reasons) or
approves it with a notional size. It is deterministic and does no I/O, so every
decision is reproducible from its inputs. Exits are never blocked by the risk engine.

Position size is the minimum of:
  * risk budget  = equity x risk% (scaled by confidence and insider risk), capped by the
    remaining daily loss budget, divided by (stop distance + round-trip variable costs)
  * per-position cap (% of equity)
  * remaining portfolio exposure
  * exit-feasibility cap (fraction of pool liquidity — not market cap)
  * spendable cash after the fee reserve
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from atomtrader.config.settings import RiskLimits
from atomtrader.core.models import Chain, TokenRef
from atomtrader.core.money import ZERO, bps, round_eur
from atomtrader.risk.controls import TradingControls
from atomtrader.risk.state import DailyRiskState

HUNDRED = Decimal(100)
MAX_STOP_DISTANCE = Decimal("0.5")
INSIDER_HARD_BLOCK = Decimal(80)
INSIDER_SCALE_START = Decimal(50)


@dataclass(frozen=True, slots=True)
class TradeIntent:
    token: TokenRef
    strategy: str
    entry_price_usd: Decimal
    stop_price_usd: Decimal
    pool_liquidity_eur: Decimal
    est_entry_slippage_bps: Decimal
    est_exit_slippage_bps: Decimal
    # Pool + aggregator fee per side, as a percentage of notional.
    est_fee_pct_per_side: Decimal
    # Network fee + priority/tip per transaction, EUR.
    est_network_fee_eur: Decimal
    confidence: Decimal  # 0..1, from the opportunity scorer
    narrative: str | None = None
    security_hard_fail: bool = False
    insider_risk_score: Decimal | None = None  # 0..100, None = unavailable
    opportunity_score: Decimal | None = None


@dataclass(frozen=True, slots=True)
class OpenPositionView:
    token: TokenRef
    narrative: str | None
    value_eur: Decimal
    # Loss if the stop is hit now, including exit costs (>= 0).
    risk_to_stop_eur: Decimal


@dataclass(frozen=True, slots=True)
class PortfolioView:
    equity_eur: Decimal
    cash_eur: Decimal
    open_positions: tuple[OpenPositionView, ...] = ()

    @property
    def invested_eur(self) -> Decimal:
        return sum((p.value_eur for p in self.open_positions), ZERO)

    @property
    def open_risk_eur(self) -> Decimal:
        return sum((p.risk_to_stop_eur for p in self.open_positions), ZERO)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    notional_eur: Decimal = ZERO
    risk_eur: Decimal = ZERO
    reasons: tuple[str, ...] = ()
    binding_constraint: str | None = None
    caps: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def reject(*reasons: str, caps: dict[str, str] | None = None) -> RiskDecision:
        return RiskDecision(approved=False, reasons=tuple(reasons), caps=caps or {})


class RiskEngine:
    def __init__(
        self,
        limits: RiskLimits,
        controls: TradingControls,
        state: DailyRiskState,
        enabled_chains: frozenset[Chain],
    ) -> None:
        self.limits = limits
        self.controls = controls
        self.state = state
        self.enabled_chains = enabled_chains

    # ------------------------------------------------------------------ gating
    def _gate(self, intent: TradeIntent, pf: PortfolioView, now: datetime) -> list[str]:
        L = self.limits
        reasons: list[str] = []
        block = self.controls.entry_block_reason(intent.token)
        if block:
            reasons.append(block)
        if intent.token.chain not in self.enabled_chains:
            reasons.append(f"chain_disabled:{intent.token.chain.value}")
        if self.state.daily_halt:
            reasons.append("daily_loss_limit_reached")
        if self.state.in_cooldown(now):
            reasons.append("loss_streak_cooldown")
        if self.state.entries_today >= L.max_trades_per_day:
            reasons.append("max_trades_per_day")
        if len(pf.open_positions) >= L.max_open_positions:
            reasons.append("max_open_positions")
        if any(p.token.key == intent.token.key for p in pf.open_positions):
            # No averaging down / pyramiding into an existing position.
            reasons.append("already_holding_token")
        if intent.narrative is not None:
            same = sum(1 for p in pf.open_positions if p.narrative == intent.narrative)
            if same >= L.max_positions_per_narrative:
                reasons.append("narrative_concentration")
        if intent.security_hard_fail:
            reasons.append("security_hard_fail")
        if intent.insider_risk_score is not None and intent.insider_risk_score >= INSIDER_HARD_BLOCK:
            reasons.append("insider_exit_risk_too_high")
        if intent.entry_price_usd <= 0 or intent.stop_price_usd <= 0:
            reasons.append("invalid_prices")
        elif intent.stop_price_usd >= intent.entry_price_usd:
            reasons.append("stop_not_below_entry")
        elif (intent.entry_price_usd - intent.stop_price_usd) / intent.entry_price_usd > MAX_STOP_DISTANCE:
            reasons.append("stop_too_wide")
        if not (ZERO <= intent.confidence <= 1):
            reasons.append("invalid_confidence")
        if max(intent.est_entry_slippage_bps, intent.est_exit_slippage_bps) > L.max_slippage_bps:
            reasons.append("slippage_above_max")
        if intent.pool_liquidity_eur < L.min_pool_liquidity_eur:
            reasons.append("liquidity_below_minimum")
        if pf.equity_eur <= 0:
            reasons.append("no_equity")
        return reasons

    # ------------------------------------------------------------------ sizing
    def evaluate(self, intent: TradeIntent, pf: PortfolioView, now: datetime) -> RiskDecision:
        L = self.limits
        self.state.roll_if_new_day(now, pf.equity_eur)

        reasons = self._gate(intent, pf, now)
        if reasons:
            return RiskDecision.reject(*reasons)

        stop_frac = (intent.entry_price_usd - intent.stop_price_usd) / intent.entry_price_usd
        fee_frac = intent.est_fee_pct_per_side / HUNDRED
        var_cost_frac = bps(intent.est_entry_slippage_bps) + bps(intent.est_exit_slippage_bps) + 2 * fee_frac
        fixed_cost = 2 * intent.est_network_fee_eur

        # Risk budget: confidence interpolates between min and max risk per trade.
        risk_pct = L.min_risk_per_trade_pct + (L.max_risk_per_trade_pct - L.min_risk_per_trade_pct) * intent.confidence
        if intent.insider_risk_score is not None and intent.insider_risk_score > INSIDER_SCALE_START:
            # Linear haircut from 100% at score 50 to 50% at score 80.
            haircut = (intent.insider_risk_score - INSIDER_SCALE_START) / (INSIDER_HARD_BLOCK - INSIDER_SCALE_START)
            risk_pct *= 1 - haircut / 2
        risk_eur = pf.equity_eur * risk_pct / HUNDRED

        daily_remaining = (
            self.state.daily_loss_limit_eur(L.max_daily_loss_pct)
            + self.state.realized_net_pnl_eur
            - pf.open_risk_eur
        )
        if daily_remaining <= fixed_cost:
            return RiskDecision.reject("daily_risk_budget_exhausted")
        risk_eur = min(risk_eur, daily_remaining)

        caps: dict[str, Decimal] = {
            "risk_budget": (risk_eur - fixed_cost) / (stop_frac + var_cost_frac),
            "max_position": pf.equity_eur * L.max_position_pct_of_equity / HUNDRED,
            "exposure": pf.equity_eur * L.max_total_exposure_pct / HUNDRED - pf.invested_eur,
            "liquidity": intent.pool_liquidity_eur * L.max_pool_liquidity_fraction_pct / HUNDRED,
            "cash": (pf.cash_eur - L.fee_reserve_eur - fixed_cost) / (1 + fee_frac),
        }
        binding = min(caps, key=lambda k: caps[k])
        notional = round_eur(caps[binding])
        caps_str = {k: str(round_eur(v)) for k, v in caps.items()}

        if notional < L.min_position_eur:
            return RiskDecision.reject(f"size_below_minimum:{binding}", caps=caps_str)

        round_trip_cost_pct = (var_cost_frac + fixed_cost / notional) * HUNDRED
        if round_trip_cost_pct > L.max_round_trip_cost_pct:
            return RiskDecision.reject("round_trip_cost_too_high", caps=caps_str)

        loss_at_stop = notional * (stop_frac + var_cost_frac) + fixed_cost
        return RiskDecision(
            approved=True,
            notional_eur=notional,
            risk_eur=round_eur(loss_at_stop),
            reasons=("approved",),
            binding_constraint=binding,
            caps=caps_str,
        )
