"""Event-driven backtester and walk-forward validation.

Replays time-ordered ``Frame``s (one token's MarketContext at one moment, built only from
data available at that moment) through exactly the production components: entry
strategies, Opportunity Score, the authoritative RiskEngine (sizing, daily loss, cooldowns,
position caps) and the ExitEngine (modes, trails, partials).

Costs on every fill: half the round-trip slippage estimate per side, platform/aggregator
fee and network fee. Results are labelled as backtests; they are weak evidence for meme
coins (survivorship bias, thin history) and are used to *compare* configurations, not to
promise returns.
"""

from __future__ import annotations

import heapq
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal

from atomtrader.analytics.performance import Metrics, TradeRecord, equity_from_trades, max_drawdown, metrics
from atomtrader.config.settings import RiskLimits
from atomtrader.core.models import Chain, TokenRef
from atomtrader.risk.controls import TradingControls
from atomtrader.risk.engine import OpenPositionView, PortfolioView, RiskEngine, TradeIntent
from atomtrader.risk.state import DailyRiskState
from atomtrader.strategy.context import MarketContext
from atomtrader.strategy.entries import EntryConfig, Signal, evaluate_all
from atomtrader.strategy.exits import ExitConfig, ExitEngine, Mode, PositionState
from atomtrader.strategy.scoring import OpportunityWeights, opportunity_score

D = Decimal


@dataclass(frozen=True)
class Frame:
    at: datetime
    ctx: MarketContext

    @property
    def key(self) -> str:
        return self.ctx.features.token.key


@dataclass(frozen=True)
class BacktestConfig:
    name: str = "default"
    entry: EntryConfig = field(default_factory=EntryConfig)
    exit: ExitConfig = field(default_factory=ExitConfig)
    weights: OpportunityWeights = field(default_factory=OpportunityWeights)
    limits: RiskLimits = field(default_factory=RiskLimits)
    min_opportunity: float = 55.0
    starting_eur: Decimal = D(1000)
    eur_per_usd: Decimal = D("0.92")
    fee_pct_per_side: Decimal = D("0.10")
    network_fee_eur: Decimal = D("0.05")
    default_round_trip_cost_bps: float = 120.0
    enabled_strategies: tuple[str, ...] | None = None
    chains: frozenset[Chain] = frozenset({Chain.SOLANA, Chain.BASE, Chain.BSC})
    timezone: str = "Europe/Madrid"


@dataclass
class _Open:
    state: PositionState
    token: str
    chain: str
    qty: Decimal
    cost_basis_eur: Decimal  # remaining basis incl. entry fees
    fees_eur: Decimal
    realized_eur: Decimal
    committed_eur: Decimal
    signal: Signal
    max_mode: Mode
    entry_h1: float | None
    last_price: float


@dataclass
class BacktestResult:
    config: str
    trades: list[TradeRecord]
    signals: int
    rejected_by_risk: dict[str, int]
    ending_cash_eur: Decimal
    starting_eur: Decimal

    @property
    def metrics(self) -> Metrics:
        return metrics(self.trades)

    @property
    def max_drawdown(self) -> tuple[Decimal, float]:
        return max_drawdown(equity_from_trades(self.starting_eur, self.trades))

    def risk_adjusted(self, min_trades: int = 5) -> float:
        """Net P&L / max drawdown (EUR); -inf with too few trades to judge."""
        m = self.metrics
        if m.trades < min_trades:
            return float("-inf")
        dd, _ = self.max_drawdown
        return float(m.net_pnl_eur / max(dd, D(1)))


def merge_frames(timelines: Iterable[Sequence[Frame]]) -> list[Frame]:
    return list(heapq.merge(*[sorted(t, key=lambda f: f.at) for t in timelines], key=lambda f: f.at))


class Backtester:
    def __init__(self, config: BacktestConfig | None = None) -> None:
        self.cfg = config or BacktestConfig()

    def _cost_pct(self, ctx: MarketContext) -> float:
        bps = ctx.round_trip_cost_bps if ctx.round_trip_cost_bps is not None else self.cfg.default_round_trip_cost_bps
        return bps / 100

    def run(self, timelines: Iterable[Sequence[Frame]]) -> BacktestResult:
        cfg = self.cfg
        frames = merge_frames(timelines)
        risk = RiskEngine(cfg.limits, TradingControls(), DailyRiskState(timezone=cfg.timezone), cfg.chains)
        exits = ExitEngine(cfg.exit)
        cash = cfg.starting_eur
        open_: dict[str, _Open] = {}
        trades: list[TradeRecord] = []
        signals = 0
        rejected: dict[str, int] = {}

        def sell(o: _Open, price: float, fraction: float, at: datetime, reason: str) -> None:
            nonlocal cash
            qty = o.qty if fraction >= 1 else o.qty * D(str(fraction))
            half_cost = D(str(o.signal.round_trip_cost_bps / 2 / 10_000))
            gross = qty * D(str(price)) * cfg.eur_per_usd * (1 - half_cost)
            fee = gross * cfg.fee_pct_per_side / 100 + cfg.network_fee_eur
            basis = o.cost_basis_eur * (qty / o.qty)
            o.realized_eur += gross - fee - basis
            o.cost_basis_eur -= basis
            o.fees_eur += fee
            o.qty -= qty
            cash += gross - fee
            if o.qty <= D("1e-12"):
                del open_[o.token]
                ret = float(o.realized_eur / o.committed_eur * 100) if o.committed_eur else None
                mfe = (o.state.highest / o.state.entry_price - 1) * 100
                trades.append(TradeRecord(
                    position_id=o.state.position_id, token=o.token, chain=o.chain, strategy=o.state.strategy,
                    opened_at=o.state.opened_at, closed_at=at, net_pnl_eur=o.realized_eur, fees_eur=o.fees_eur,
                    cost_basis_eur=o.committed_eur, exit_reason=reason, max_mode=o.max_mode.value,
                    expected_move_pct=o.signal.expected_move_pct, max_favorable_pct=mfe, realized_return_pct=ret,
                    est_round_trip_cost_bps=o.signal.round_trip_cost_bps, entry_h1_change_pct=o.entry_h1,
                    account_kind="backtest"))
                L = cfg.limits
                risk.state.record_closed_trade(o.realized_eur, at, L.max_daily_loss_pct,
                                               L.consecutive_losses_for_cooldown, L.cooldown_minutes)

        def portfolio() -> PortfolioView:
            views = []
            invested = D(0)
            for o in open_.values():
                value = o.qty * D(str(o.last_price)) * cfg.eur_per_usd
                invested += value
                stop_val = o.qty * D(str(o.state.stop)) * cfg.eur_per_usd
                ch, addr = o.token.split(":", 1)
                views.append(OpenPositionView(TokenRef(Chain(ch), addr), None, value, max(value - stop_val, D(0))))
            return PortfolioView(cash + invested, cash, tuple(views))

        for fr in frames:
            ctx, key, price = fr.ctx, fr.key, fr.ctx.price
            if key in open_:
                o = open_[key]
                o.last_price = price
                d = exits.evaluate(o.state, ctx, fr.at)
                o.state = d.state
                if d.state.mode is Mode.RUNNER or (d.state.mode is Mode.MOMENTUM and o.max_mode is Mode.NORMAL):
                    o.max_mode = d.state.mode
                if d.action in ("exit", "partial"):
                    sell(o, price, d.fraction_of_current, fr.at, d.reason)
                continue

            results = evaluate_all(ctx, cfg.entry, cfg.enabled_strategies)
            sigs = [r.signal for r in results if r.signal is not None]
            if not sigs:
                continue
            signals += 1
            sig = max(sigs, key=lambda s: s.confidence * s.reward_to_risk)
            opp = opportunity_score(ctx, sig.reward_to_risk, cfg.weights)
            if opp.score < cfg.min_opportunity:
                rejected["opportunity_below_min"] = rejected.get("opportunity_below_min", 0) + 1
                continue
            liq_eur = D(str(ctx.features.liquidity_usd or 0)) * cfg.eur_per_usd
            intent = TradeIntent(
                token=ctx.features.token, strategy=sig.strategy, entry_price_usd=D(str(sig.entry_price)),
                stop_price_usd=D(str(sig.stop_price)), pool_liquidity_eur=liq_eur,
                est_entry_slippage_bps=D(str(sig.round_trip_cost_bps / 2)), est_exit_slippage_bps=D(str(sig.round_trip_cost_bps / 2)),
                est_fee_pct_per_side=cfg.fee_pct_per_side, est_network_fee_eur=cfg.network_fee_eur,
                confidence=D(str(round(sig.confidence * opp.score / 100, 4))),
                narrative=ctx.narrative.term if ctx.narrative else None,
                insider_risk_score=D(ctx.insider.score) if ctx.insider else None,
                opportunity_score=D(str(opp.score)),
            )
            decision = risk.evaluate(intent, portfolio(), fr.at)
            if not decision.approved:
                for r in decision.reasons:
                    rejected[r.split(":")[0]] = rejected.get(r.split(":")[0], 0) + 1
                continue
            half_cost = D(str(sig.round_trip_cost_bps / 2 / 10_000))
            fill_price = price * (1 + float(half_cost))
            fee = decision.notional_eur * cfg.fee_pct_per_side / 100 + cfg.network_fee_eur
            qty = decision.notional_eur / (D(str(fill_price)) * cfg.eur_per_usd)
            cash -= decision.notional_eur + fee
            risk.state.record_entry()
            pid = f"bt-{uuid.uuid4().hex[:10]}"
            state = exits.initial_state(sig, pid, fr.at, fill_price, ctx.features.liquidity_usd,
                                        ctx.ta1.atr_pct if ctx.ta1 else None, ctx.narrative_strength)
            open_[key] = _Open(state, key, ctx.features.token.chain.value, qty, decision.notional_eur + fee, fee, D(0),
                               decision.notional_eur + fee, sig, Mode.NORMAL, ctx.features.chg_h1, price)

        for o in list(open_.values()):  # close anything left at the last seen price
            sell(o, o.last_price, 1.0, frames[-1].at if frames else o.state.opened_at, "end_of_data")
        return BacktestResult(cfg.name, trades, signals, rejected, cash, cfg.starting_eur)


# --------------------------------------------------------------------------- walk-forward


@dataclass(frozen=True)
class FoldResult:
    fold: int
    train_start: datetime
    test_start: datetime
    test_end: datetime
    chosen: str
    train_score: float
    test_score: float
    test_metrics: Metrics


@dataclass(frozen=True)
class WalkForwardResult:
    folds: tuple[FoldResult, ...]
    oos_trades: tuple[TradeRecord, ...]

    @property
    def oos_metrics(self) -> Metrics:
        return metrics(self.oos_trades)

    @property
    def degradation(self) -> float | None:
        """Mean (train - test) risk-adjusted score; large positive values suggest overfitting."""
        pairs = [(f.train_score, f.test_score) for f in self.folds
                 if f.train_score not in (float("-inf"),) and f.test_score not in (float("-inf"),)]
        return sum(a - b for a, b in pairs) / len(pairs) if pairs else None


def walk_forward(timelines: Sequence[Sequence[Frame]], configs: Sequence[BacktestConfig], folds: int = 3,
                 min_trades: int = 3, score: Callable[[BacktestResult], float] | None = None) -> WalkForwardResult:
    """Anchored walk-forward: split the time range into ``folds + 1`` equal segments; for fold
    k choose the config with the best in-sample score on segments [0, k) and evaluate it on
    segment k, which it has never seen."""
    frames = merge_frames(timelines)
    if not frames:
        return WalkForwardResult((), ())
    start, end = frames[0].at, frames[-1].at
    step = (end - start) / (folds + 1)
    score = score or (lambda r: r.risk_adjusted(min_trades))
    out: list[FoldResult] = []
    oos: list[TradeRecord] = []
    for k in range(1, folds + 1):
        t0, t1 = start + step * k, start + step * (k + 1)
        train = [[f for f in tl if f.at < t0] for tl in timelines]
        test = [[f for f in tl if t0 <= f.at < t1 or (k == folds and f.at == end)] for tl in timelines]
        scored = [(score(Backtester(c).run(train)), c) for c in configs]
        best_score, best = max(scored, key=lambda x: x[0])
        res = Backtester(replace(best, name=f"{best.name}@fold{k}")).run(test)
        out.append(FoldResult(k, start, t0, t1, best.name, best_score, score(res), res.metrics))
        oos += res.trades
    return WalkForwardResult(tuple(out), tuple(oos))
