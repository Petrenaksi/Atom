"""Entry strategies A–E.

Every strategy returns a ``StrategyResult`` containing its full checklist (``checks``), so a
non-signal is logged with reasons just like a signal. A signal carries:

  * entry conditions + confirmation conditions that were met
  * invalidation conditions (handed to the exit engine as thesis checks)
  * stop price (structure-based, bounded by ATR and a cost-aware minimum distance)
  * expected move and minimum liquidity
  * expected net reward/risk after round-trip costs
  * confidence 0..1

Expected move = volatility over the intended holding horizon:
``ATR% (of the bar size used) x sqrt(horizon / bar_minutes) x horizon_vol_multiple``, floored
at ``min_expected_move_pct`` and capped. It is NOT derived from the stop distance (that would
make the reward/risk gate circular).

Shared gate: ``(expected_move - cost) / (stop_distance + cost) >= min_reward_to_risk``. A
signal that cannot pay for its own costs is not emitted. The risk engine still has the
final word on every entry.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from atomtrader.analysis.indicators import TechnicalSnapshot, swing_points
from atomtrader.marketdata.models import Candle
from atomtrader.strategy.context import MarketContext


@dataclass(frozen=True)
class EntryConfig:
    min_liquidity_usd: float = 25_000
    min_reward_to_risk: float = 1.5
    min_stop_pct: float = 3.0
    max_stop_pct: float = 15.0
    horizon_minutes: float = 20.0
    horizon_vol_multiple: float = 1.0
    min_expected_move_pct: float = 4.0
    max_expected_move_pct: float = 40.0
    max_extension_atr: float = 3.0  # don't chase: close vs EMA21 in ATRs
    default_round_trip_cost_bps: float = 120.0  # used when no exact estimate exists


@dataclass(frozen=True)
class Signal:
    strategy: str
    entry_price: float
    stop_price: float
    expected_move_pct: float
    reward_to_risk: float
    confidence: float
    conditions: tuple[str, ...]
    confirmations: tuple[str, ...]
    invalidations: tuple[str, ...]
    min_liquidity_usd: float
    round_trip_cost_bps: float
    thesis: str

    @property
    def stop_pct(self) -> float:
        return (self.entry_price - self.stop_price) / self.entry_price * 100


@dataclass(frozen=True)
class StrategyResult:
    strategy: str
    signal: Signal | None
    checks: dict[str, bool] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def failed(self) -> list[str]:
        return [k for k, v in self.checks.items() if not v]


def _use_5m(ctx: MarketContext) -> bool:
    """Indicators and candles always come from the same timeframe: 5-minute when at least
    20 five-minute bars are available, otherwise 1-minute."""
    return len(ctx.candles_5m) >= 20 and ctx.ta5 is not None and ctx.ta5.atr14 is not None


def _ta(ctx: MarketContext) -> TechnicalSnapshot | None:
    return ctx.ta5 if _use_5m(ctx) else ctx.ta1


def _candles(ctx: MarketContext) -> Sequence[Candle]:
    return ctx.candles_5m if _use_5m(ctx) else ctx.candles_1m


def _recent_swing_low(candles: Sequence[Candle], below: float) -> float | None:
    lows = [s.price for s in swing_points(candles) if s.kind == "low" and s.price < below]
    return lows[-1] if lows else None


def build_signal(ctx: MarketContext, cfg: EntryConfig, name: str, structure_stop: float | None, confidence: float,
                 conditions: list[str], confirmations: list[str], invalidations: list[str], thesis: str,
                 checks: dict[str, bool]) -> StrategyResult:
    """Common stop/target/cost logic. Fails the result (with reasons) if not worth it."""
    ta = _ta(ctx)
    price = ctx.price
    atr_pct = (ta.atr_pct * 100) if ta and ta.atr_pct else None
    bar_minutes = 5.0 if _use_5m(ctx) else 1.0
    cost_pct = (ctx.round_trip_cost_bps if ctx.round_trip_cost_bps is not None else cfg.default_round_trip_cost_bps) / 100
    # Stop: structure, but never tighter than max(min_stop, 1.5 ATR, 2x costs) nor wider than max_stop.
    floor = max(cfg.min_stop_pct, 1.5 * atr_pct if atr_pct else 0.0, 2 * cost_pct)
    struct_pct = (price - structure_stop) / price * 100 if structure_stop and structure_stop < price else None
    stop_pct = max(floor, struct_pct or floor)
    checks["stop_within_max"] = stop_pct <= cfg.max_stop_pct
    horizon_vol = (atr_pct * (cfg.horizon_minutes / bar_minutes) ** 0.5 * cfg.horizon_vol_multiple) if atr_pct else 0.0
    expected = min(cfg.max_expected_move_pct, max(cfg.min_expected_move_pct, horizon_vol))
    rr = (expected - cost_pct) / (stop_pct + cost_pct)
    checks["reward_to_risk_after_costs"] = rr >= cfg.min_reward_to_risk
    checks["liquidity_minimum"] = (ctx.features.liquidity_usd or 0) >= cfg.min_liquidity_usd
    checks["security_passed"] = ctx.security is None or ctx.security.passed
    if not all(checks.values()):
        return StrategyResult(name, None, checks)
    return StrategyResult(name, Signal(
        strategy=name, entry_price=price, stop_price=price * (1 - stop_pct / 100), expected_move_pct=round(expected, 2),
        reward_to_risk=round(rr, 2), confidence=round(max(0.0, min(1.0, confidence)), 2),
        conditions=tuple(conditions), confirmations=tuple(confirmations), invalidations=tuple(invalidations),
        min_liquidity_usd=cfg.min_liquidity_usd, round_trip_cost_bps=cost_pct * 100, thesis=thesis,
    ), checks)


def _not_extended(ta: TechnicalSnapshot | None, cfg: EntryConfig) -> bool:
    if ta is None or ta.ema21 is None or not ta.atr14:
        return False
    return (ta.close - ta.ema21) / ta.atr14 <= cfg.max_extension_atr


# --------------------------------------------------------------------------- A: narrative breakout


def narrative_breakout(ctx: MarketContext, cfg: EntryConfig) -> StrategyResult:
    f, n, ta = ctx.features, ctx.narrative, _ta(ctx)
    checks = {
        "narrative_present": n is not None,
        "narrative_gaining_attention": n is not None and n.status in ("emerging", "growing"),
        "authentic_link": ctx.narrative_link is not None and ctx.narrative_link.grade != "name_only",
        "narrative_strength": (ctx.narrative_strength or 0) >= 0.25,
        "demand_increasing": (f.volume_accel_m5_h1 or 0) >= 2.0,
        "buyers_confirm": (f.buy_ratio_m5 or 0) >= 0.55,
        "not_extended": _not_extended(ta, cfg),
    }
    if not all(checks.values()):
        return StrategyResult("A_narrative_breakout", None, checks)
    conf = 0.4 + 0.3 * min(1.0, ctx.narrative_strength or 0) + 0.3 * min(1.0, ((f.volume_accel_m5_h1 or 1) - 1) / 4)
    stop = _recent_swing_low(_candles(ctx), ctx.price)
    return build_signal(ctx, cfg, "A_narrative_breakout", stop, conf,
                        [f"narrative '{n.term}' {n.status}", f"volume pace x{f.volume_accel_m5_h1:.1f}"],  # type: ignore[union-attr]
                        [f"buy ratio {f.buy_ratio_m5:.2f}"],
                        ["narrative_fading", "volume_dries_up", "sell_pressure", "liquidity_drop"],
                        f"Real-world narrative '{n.term}' accelerating; linked token demand rising",  # type: ignore[union-attr]
                        checks)


# --------------------------------------------------------------------------- B: second-wave rotation


def second_wave_rotation(ctx: MarketContext, cfg: EntryConfig) -> StrategyResult:
    r, f = ctx.rotation, ctx.features
    checks = {
        "rotation_alert": r is not None,
        "rotation_strength": r is not None and r.score >= 0.35,
        "not_crowded": r is not None and not r.crowded,
        "participation_improving": ((f.buyer_accel_m5_h1 if f.buyer_accel_m5_h1 is not None else f.txns_accel_m5_h1) or 0) >= 1.5,
        "not_extended": _not_extended(_ta(ctx), cfg),
    }
    if not all(checks.values()):
        return StrategyResult("B_second_wave_rotation", None, checks)
    conf = 0.35 + 0.5 * r.score  # type: ignore[union-attr]
    if "name_only_link" in r.flags:  # type: ignore[union-attr]
        conf -= 0.15
    stop = _recent_swing_low(_candles(ctx), ctx.price)
    return build_signal(ctx, cfg, "B_second_wave_rotation", stop, conf,
                        [f"leader {r.leader} cooling", f"rotation score {r.score}"],  # type: ignore[union-attr]
                        [f"follower volume x{r.evidence.get('follower_volume_accel')}"],  # type: ignore[union-attr]
                        ["follower_momentum_fades", "narrative_fading", "leader_dumps_hard", "liquidity_drop"],
                        f"Second wave in '{r.narrative}' (evidence: {r.evidence_type})", checks)  # type: ignore[union-attr]


# --------------------------------------------------------------------------- C: volume-confirmed breakout


def volume_breakout(ctx: MarketContext, cfg: EntryConfig) -> StrategyResult:
    candles = _candles(ctx)
    ta, f = _ta(ctx), ctx.features
    prior = candles[:-1]
    highs = [s.price for s in swing_points(prior) if s.kind == "high"]
    level = highs[-1] if highs else None
    last = candles[-1] if candles else None
    checks = {
        "resistance_identified": level is not None,
        "close_above_resistance": level is not None and last is not None and float(last.close) > level,
        "breakout_volume": ta is not None and (ta.rel_volume or 0) >= 3.0,
        "buyers_confirm": (f.buy_ratio_m5 or 0) >= 0.55,
        "structure_not_down": ta is not None and ta.structure != "downtrend",
        "not_extended": level is not None and last is not None and ta is not None and bool(ta.atr14)
        and (float(last.close) - level) / ta.atr14 <= 1.5,  # type: ignore[operator]
    }
    if not all(checks.values()):
        return StrategyResult("C_volume_breakout", None, checks)
    stop = level - 0.5 * ta.atr14  # type: ignore[operator,union-attr]
    conf = 0.45 + 0.25 * min(1.0, ((ta.rel_volume or 3) - 3) / 5) + 0.3 * min(1.0, ((f.buy_ratio_m5 or .55) - .55) / .25)  # type: ignore[union-attr]
    return build_signal(ctx, cfg, "C_volume_breakout", stop, conf,
                        [f"close above resistance {level:.10g}", f"relative volume x{ta.rel_volume:.1f}"],  # type: ignore[union-attr]
                        [f"buy ratio {f.buy_ratio_m5:.2f}"],
                        ["close_back_below_breakout_level", "volume_dries_up", "sell_pressure"],
                        "Resistance break on outsized volume with buyer confirmation", checks)


# --------------------------------------------------------------------------- D: pullback continuation


def pullback_continuation(ctx: MarketContext, cfg: EntryConfig) -> StrategyResult:
    candles = _candles(ctx)
    ta = _ta(ctx)
    f = ctx.features
    recent = candles[-4:-1] if len(candles) >= 4 else []
    last = candles[-1] if candles else None
    touched = bool(ta and ta.ema21 and ta.atr14 and recent
                   and min(float(c.low) for c in recent) <= ta.ema21 + ta.atr14)
    checks = {
        "strong_trend": ta is not None and ta.ema_stack_bullish and ta.structure == "uptrend",
        "pulled_back_to_ema21": touched,
        "held_above_ema50": ta is not None and ta.ema50 is not None and recent != []
        and min(float(c.low) for c in recent) > ta.ema50,
        "buying_returns": last is not None and recent != [] and float(last.close) > float(recent[-1].high)
        and float(last.close) > float(last.open),
        "volume_returns": ta is not None and (ta.rel_volume or 0) >= 1.2,
        "buyers_confirm": (f.buy_ratio_m5 or 0) >= 0.5,
    }
    if not all(checks.values()):
        return StrategyResult("D_pullback_continuation", None, checks)
    stop = min(float(c.low) for c in recent)
    conf = 0.5 + 0.2 * min(1.0, ((ta.rel_volume or 1.2) - 1.2) / 2) + 0.2 * ((f.buy_ratio_m5 or .5) - .5) / .3  # type: ignore[union-attr]
    return build_signal(ctx, cfg, "D_pullback_continuation", stop, conf,
                        ["EMA9>EMA21>EMA50 uptrend", "pullback into EMA21 zone held above EMA50"],
                        ["close above prior bar high", f"relative volume x{ta.rel_volume:.1f}"],  # type: ignore[union-attr]
                        ["lose_ema50", "lower_low", "sell_pressure"],
                        "Strong trend; retracement absorbed and buying returning", checks)


# --------------------------------------------------------------------------- E: VWAP reclaim


def vwap_reclaim(ctx: MarketContext, cfg: EntryConfig) -> StrategyResult:
    from atomtrader.analysis.indicators import vwap as vwap_series

    candles = _candles(ctx)
    ta, f = _ta(ctx), ctx.features
    vw = vwap_series(candles) if candles else []
    below = sum(1 for c, v in zip(candles[-8:-1], vw[-8:-1]) if v is not None and float(c.close) < v)
    checks = {
        "was_below_vwap": below >= 4,
        "reclaimed_vwap": bool(candles) and vw and vw[-1] is not None and float(candles[-1].close) > vw[-1],
        "volume_confirms": ta is not None and (ta.rel_volume or 0) >= 1.5,
        "momentum_turning": ta is not None and ta.macd_hist is not None and ta.macd_hist_prev is not None
        and ta.macd_hist > ta.macd_hist_prev,
        "buyers_confirm": (f.buy_ratio_m5 or 0) >= 0.5,
        "liquidity_not_falling": f.liquidity_change_pct is None or f.liquidity_change_pct > -10,
    }
    if not all(checks.values()):
        return StrategyResult("E_vwap_reclaim", None, checks)
    stop = min(float(c.low) for c in candles[-8:])
    conf = 0.4 + 0.3 * min(1.0, ((ta.rel_volume or 1.5) - 1.5) / 3) + 0.3 * min(1.0, below / 7)  # type: ignore[union-attr]
    return build_signal(ctx, cfg, "E_vwap_reclaim", stop, conf,
                        [f"{below} of last 7 bars below VWAP", "close back above VWAP"],
                        [f"relative volume x{ta.rel_volume:.1f}", "MACD histogram rising"],  # type: ignore[union-attr]
                        ["close_back_below_vwap", "lower_low", "sell_pressure"],
                        "Recovery from temporary weakness: VWAP reclaimed with volume", checks)


STRATEGIES: dict[str, Callable[[MarketContext, EntryConfig], StrategyResult]] = {
    "A_narrative_breakout": narrative_breakout,
    "B_second_wave_rotation": second_wave_rotation,
    "C_volume_breakout": volume_breakout,
    "D_pullback_continuation": pullback_continuation,
    "E_vwap_reclaim": vwap_reclaim,
}


def evaluate_all(ctx: MarketContext, cfg: EntryConfig | None = None,
                 enabled: Sequence[str] | None = None) -> list[StrategyResult]:
    cfg = cfg or EntryConfig()
    names = enabled or list(STRATEGIES)
    return [STRATEGIES[n](ctx, cfg) for n in names]
