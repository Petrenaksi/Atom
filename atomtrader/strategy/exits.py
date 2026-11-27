"""Exit engine: Normal / Momentum / Explosive-Runner position management.

Principles
  * The stop only moves up. A position is never unprotected, whatever its upside.
  * Hard exits in every mode: stop hit, security failure, confirmed insider selling,
    liquidity deterioration, narrative collapse.
  * NORMAL: dynamic target (ATR/cost/narrative-aware) and a time stop around the preferred
    10–30 minute horizon. At target: weak momentum -> exit; strong momentum -> MOMENTUM.
  * MOMENTUM: no fixed ceiling; ATR trailing stop (tightened when momentum fades);
    break-even-plus-costs floor; partial profits per the configured plan.
  * RUNNER (exceptional moves): wide adaptive percentage trail (clamped 8–25 %), tightened
    step by step as buyer/volume momentum deteriorates; technical noise is ignored in favour
    of the trail, but insider selling, liquidity loss and narrative collapse still exit.
  * Partial-profit levels are configurable (``PartialPlan``) and are what the backtester
    compares — no percentage is assumed to be optimal.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from enum import StrEnum

from atomtrader.strategy.context import MarketContext
from atomtrader.strategy.entries import Signal


class Mode(StrEnum):
    NORMAL = "normal"
    MOMENTUM = "momentum"
    RUNNER = "runner"


@dataclass(frozen=True)
class PartialPlan:
    """(multiple of the initial target, fraction of the ORIGINAL position) per level."""

    levels: tuple[tuple[float, float], ...] = ((1.0, 0.25), (2.0, 0.25))

    def __post_init__(self) -> None:
        if sum(f for _, f in self.levels) >= 1.0:
            raise ValueError("partials must leave a remainder to trail")


@dataclass(frozen=True)
class ExitConfig:
    max_hold_minutes_normal: float = 30.0
    time_stop_min_progress: float = 0.5  # fraction of target required by the time stop
    momentum_trail_atr: float = 2.5
    momentum_trail_atr_weak: float = 1.5
    runner_activation_pct: float = 50.0
    runner_trail_atr: float = 3.0
    runner_trail_min_pct: float = 8.0
    runner_trail_max_pct: float = 25.0
    momentum_volume_accel: float = 1.5
    momentum_buy_ratio: float = 0.55
    strong_volume_accel: float = 2.0
    strong_participation_accel: float = 1.5
    sell_pressure_buy_ratio: float = 0.35
    liquidity_drop_exit_pct: float = 25.0
    narrative_collapse_strength: float = 0.15
    volume_dry_accel: float = 0.5
    volume_dry_after_minutes: float = 10.0
    partial_plan: PartialPlan = field(default_factory=PartialPlan)


@dataclass(frozen=True)
class PositionState:
    position_id: str
    strategy: str
    entry_price: float
    initial_stop: float
    stop: float
    target_pct: float
    highest: float
    mode: Mode
    opened_at: datetime
    round_trip_cost_pct: float
    atr_pct_at_entry: float | None
    entry_liquidity_usd: float | None
    invalidations: tuple[str, ...]
    partial_levels_done: int = 0
    remaining_fraction: float = 1.0  # of the original quantity
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["mode"] = self.mode.value
        d["opened_at"] = self.opened_at.isoformat()
        d["invalidations"] = list(self.invalidations)
        d["notes"] = list(self.notes[-20:])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> PositionState:
        return cls(**{**d, "mode": Mode(d["mode"]), "opened_at": datetime.fromisoformat(d["opened_at"]),
                      "invalidations": tuple(d["invalidations"]), "notes": tuple(d.get("notes", ()))})


@dataclass(frozen=True)
class ExitDecision:
    action: str  # "hold" | "partial" | "exit"
    fraction_of_current: float
    reason: str
    state: PositionState


class ExitEngine:
    def __init__(self, config: ExitConfig | None = None) -> None:
        self.cfg = config or ExitConfig()

    def initial_state(self, signal: Signal, position_id: str, opened_at: datetime, fill_price: float,
                      liquidity_usd: float | None, atr_pct: float | None, narrative_strength: float | None = None) -> PositionState:
        """Adaptive target: the signal's ATR/cost-aware expected move, stretched by up to 25 %
        for strong narrative momentum. Stop distance is kept from the signal, anchored to the
        actual fill price."""
        stop_pct = signal.stop_pct
        target = signal.expected_move_pct * (1 + 0.25 * min(1.0, narrative_strength or 0.0))
        return PositionState(
            position_id=position_id, strategy=signal.strategy, entry_price=fill_price,
            initial_stop=fill_price * (1 - stop_pct / 100), stop=fill_price * (1 - stop_pct / 100),
            target_pct=round(target, 2), highest=fill_price, mode=Mode.NORMAL, opened_at=opened_at,
            round_trip_cost_pct=signal.round_trip_cost_bps / 100, atr_pct_at_entry=atr_pct,
            entry_liquidity_usd=liquidity_usd, invalidations=signal.invalidations,
        )

    # ------------------------------------------------------------------ helpers
    def _momentum(self, ctx: MarketContext) -> tuple[bool, bool]:
        f, ta = ctx.features, ctx.ta1
        trend = ta is not None and ((ta.ema9 is not None and ta.ema21 is not None and ta.ema9 > ta.ema21)
                                    or ta.structure == "uptrend")
        liq_ok = f.liquidity_change_pct is None or f.liquidity_change_pct > -10
        ok = ((f.volume_accel_m5_h1 or 0) >= self.cfg.momentum_volume_accel
              and (f.buy_ratio_m5 or 0) >= self.cfg.momentum_buy_ratio and trend and liq_ok)
        part = f.buyer_accel_m5_h1 if f.buyer_accel_m5_h1 is not None else f.txns_accel_m5_h1
        strong = ok and (f.volume_accel_m5_h1 or 0) >= self.cfg.strong_volume_accel and (part or 0) >= self.cfg.strong_participation_accel
        return ok, strong

    def _atr_abs(self, ctx: MarketContext, s: PositionState) -> float:
        ta = ctx.ta1
        if ta is not None and ta.atr14:
            return ta.atr14
        return ctx.price * (s.atr_pct_at_entry or 0.03)

    def _exit(self, s: PositionState, reason: str) -> ExitDecision:
        return ExitDecision("exit", 1.0, reason, replace(s, notes=s.notes + (reason,)))

    def _invalidated(self, s: PositionState, ctx: MarketContext, held_min: float) -> str | None:
        f, ta1, ta5 = ctx.features, ctx.ta1, ctx.ta5
        checks = {
            "close_back_below_vwap": ta1 is not None and ta1.vwap is not None and ta1.close < ta1.vwap,
            "lose_ema50": ta5 is not None and ta5.ema50 is not None and ta5.close < ta5.ema50,
            "volume_dries_up": held_min >= self.cfg.volume_dry_after_minutes
            and f.volume_accel_m5_h1 is not None and f.volume_accel_m5_h1 < self.cfg.volume_dry_accel,
            "narrative_fading": ctx.narrative is not None and ctx.narrative.status == "fading",
        }
        for inv in s.invalidations:
            if checks.get(inv):
                return inv
        return None

    # ------------------------------------------------------------------ main
    def evaluate(self, s: PositionState, ctx: MarketContext, now: datetime) -> ExitDecision:
        c = self.cfg
        price = ctx.price
        s = replace(s, highest=max(s.highest, price))
        f = ctx.features
        ret = (price / s.entry_price - 1) * 100
        held = (now - s.opened_at).total_seconds() / 60

        # 1. hard exits — every mode
        if price <= s.stop:
            return self._exit(s, "trailing_stop_hit" if s.stop > s.initial_stop else "stop_loss_hit")
        if ctx.security is not None and not ctx.security.passed:
            return self._exit(s, "security_failure:" + ",".join(ctx.security.hard_fail))
        if ctx.insider is not None and ctx.insider.response == "exit":
            return self._exit(s, f"insider_{ctx.insider.situation}")
        if s.entry_liquidity_usd and f.liquidity_usd is not None:
            drop = (1 - f.liquidity_usd / s.entry_liquidity_usd) * 100
            if drop >= c.liquidity_drop_exit_pct:
                return self._exit(s, f"liquidity_down_{drop:.0f}pct")
        if (ctx.narrative is not None and ctx.narrative.status == "fading"
                and (ctx.narrative_strength or 0) < c.narrative_collapse_strength
                and s.strategy in ("A_narrative_breakout", "B_second_wave_rotation")):
            return self._exit(s, "narrative_collapsed")

        momentum_ok, strong = self._momentum(ctx)
        selling = ((f.buy_ratio_m5 is not None and f.buy_ratio_m5 < c.sell_pressure_buy_ratio)
                   and (f.volume_accel_m5_h1 or 0) >= 1.5)
        atr = self._atr_abs(ctx, s)
        breakeven = s.entry_price * (1 + s.round_trip_cost_pct / 100)

        # 2. mode-specific management
        if s.mode is Mode.NORMAL:
            if selling:
                return self._exit(s, "selling_pressure_accelerating")
            inv = self._invalidated(s, ctx, held)
            if inv:
                return self._exit(s, f"thesis_invalidated:{inv}")
            if ret >= s.target_pct:
                if not momentum_ok:
                    return self._exit(s, "target_reached_momentum_weak")
                s = replace(s, mode=Mode.MOMENTUM, stop=max(s.stop, breakeven),
                            notes=s.notes + ("upgraded_to_momentum",))
            elif held >= c.max_hold_minutes_normal and ret < c.time_stop_min_progress * s.target_pct:
                return self._exit(s, "time_stop")
            elif held >= 2 * c.max_hold_minutes_normal:
                return self._exit(s, "max_hold_exceeded")

        if s.mode is Mode.MOMENTUM:
            if selling:
                return self._exit(s, "selling_pressure_accelerating")
            inv = self._invalidated(s, ctx, held)
            if inv and inv != "volume_dries_up":
                return self._exit(s, f"thesis_invalidated:{inv}")
            trail_atr = c.momentum_trail_atr if momentum_ok else c.momentum_trail_atr_weak
            s = replace(s, stop=max(s.stop, s.highest - trail_atr * atr, breakeven))
            if ret >= c.runner_activation_pct and strong:
                s = replace(s, mode=Mode.RUNNER, notes=s.notes + ("upgraded_to_runner",))

        if s.mode is Mode.RUNNER:
            base = (atr / price * 100) * c.runner_trail_atr
            if not strong:
                base *= 0.66  # momentum fading: tighten
            if not momentum_ok:
                base = c.runner_trail_min_pct
            if selling:
                base = c.runner_trail_min_pct * 0.75
            trail_pct = max(c.runner_trail_min_pct * (0.75 if selling else 1.0), min(c.runner_trail_max_pct, base))
            s = replace(s, stop=max(s.stop, s.highest * (1 - trail_pct / 100), breakeven))
            if price <= s.stop:
                return self._exit(s, "runner_trail_hit")

        # 3. partial profits per plan
        levels = c.partial_plan.levels
        if s.partial_levels_done < len(levels) and s.mode is not Mode.NORMAL:
            mult, frac = levels[s.partial_levels_done]
            if ret >= mult * s.target_pct:
                of_current = min(1.0, frac / s.remaining_fraction)
                s = replace(s, partial_levels_done=s.partial_levels_done + 1,
                            remaining_fraction=round(s.remaining_fraction - frac, 6))
                return ExitDecision("partial", of_current, f"partial_{mult:g}x_target", s)

        return ExitDecision("hold", 0.0, f"hold:{s.mode.value}", s)
