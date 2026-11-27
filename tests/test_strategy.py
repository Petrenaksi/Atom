from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from atomtrader.core.models import Chain, TokenRef
from atomtrader.marketdata.models import Candle, PairSnapshot, WindowStats
from atomtrader.narrative.engine import Narrative, TokenLink
from atomtrader.narrative.rotation import RotationAlert
from atomtrader.scanner.features import build_features
from atomtrader.security.insider import InsiderRisk
from atomtrader.security.models import SecurityReport
from atomtrader.strategy.context import MarketContext
from atomtrader.strategy.entries import EntryConfig, evaluate_all, pullback_continuation, volume_breakout, vwap_reclaim
from atomtrader.strategy.exits import ExitConfig, ExitEngine, Mode, PartialPlan, PositionState
from atomtrader.strategy.scoring import OpportunityWeights, opportunity_score

T0 = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
TOK = TokenRef(Chain.SOLANA, "StratMint", "STRAT")


def bars(closes: list[float], vols: list[float] | None = None, spread: float = 0.015) -> list[Candle]:
    """Meme-like 1-minute bars: ~1.5 % wicks around the open/close range."""
    vols = vols or [1000.0] * len(closes)
    out, prev = [], closes[0]
    for i, (c, v) in enumerate(zip(closes, vols)):
        o = prev
        out.append(Candle(T0 + timedelta(minutes=i), Decimal(str(o)), Decimal(str(max(o, c) * (1 + spread))),
                          Decimal(str(min(o, c) * (1 - spread))), Decimal(str(c)), Decimal(str(v))))
        prev = c
    return out


def windows(buys=120, sells=40, vol_m5=30_000, vol_h1=90_000, buyers_m5=60, buyers_h1=200, chg_h1=10.0, chg_h24=30.0):
    return {"m5": WindowStats(buys, sells, buyers_m5, 20, Decimal(vol_m5), Decimal(2)),
            "h1": WindowStats(500, 300, buyers_h1, 150, Decimal(vol_h1), Decimal(str(chg_h1))),
            "h24": WindowStats(5000, 4000, 2000, 1500, Decimal(1_500_000), Decimal(str(chg_h24)))}


def ctx(candles: list[Candle], w: dict | None = None, liq: float = 400_000, **kw) -> MarketContext:
    price = candles[-1].close
    snap = PairSnapshot(Chain.SOLANA, "raydium", "Pool", TOK, "SOL", "So1", price, Decimal(str(liq)), None, None,
                        T0 - timedelta(days=2), candles[-1].ts, "geckoterminal", w or windows())
    feats = build_features([snap], candles_1m=candles)
    return MarketContext(features=feats, candles_1m=tuple(candles), round_trip_cost_bps=kw.pop("cost", 60.0), **kw)


# ------------------------------------------------------------------ scenarios


def breakout_path() -> tuple[list[float], list[float]]:
    path = [1.00 + 0.002 * i for i in range(20)]  # gentle rise
    path += [1.04, 1.05, 1.06, 1.05, 1.04, 1.035, 1.04, 1.045, 1.04, 1.035, 1.04, 1.045, 1.05, 1.055, 1.05]  # high 1.06
    path += [1.085]  # breakout bar
    vols = [1000.0] * (len(path) - 1) + [9000.0]
    return path, vols


def test_volume_breakout_signal_and_cost_gate() -> None:
    path, vols = breakout_path()
    r = volume_breakout(ctx(bars(path, vols)), EntryConfig())
    assert r.signal is not None, r.failed
    s = r.signal
    assert s.stop_price < 1.06 < s.entry_price and s.reward_to_risk >= 1.5
    assert "close_back_below_breakout_level" in s.invalidations and 0 < s.confidence <= 1
    # Same setup but a 600 bps round trip cannot pay for itself.
    costly = volume_breakout(ctx(bars(path, vols), cost=600.0), EntryConfig())
    assert costly.signal is None and "reward_to_risk_after_costs" in costly.failed


def test_volume_breakout_requires_volume_and_buyers() -> None:
    path, _ = breakout_path()
    r = volume_breakout(ctx(bars(path)), EntryConfig())
    assert r.signal is None and "breakout_volume" in r.failed
    path2, vols = breakout_path()
    r2 = volume_breakout(ctx(bars(path2, vols), w=windows(buys=30, sells=90)), EntryConfig())
    assert r2.signal is None and "buyers_confirm" in r2.failed


def test_vwap_reclaim() -> None:
    path = [1.0 + 0.003 * i for i in range(30)] + [1.06, 1.04, 1.02, 1.0, 0.99, 0.985, 0.99, 0.995, 1.0, 1.005, 1.01]
    path += [1.08]
    vols = [1000.0] * (len(path) - 1) + [6000.0]
    r = vwap_reclaim(ctx(bars(path, vols)), EntryConfig())
    assert r.checks["was_below_vwap"] and r.checks["reclaimed_vwap"], r.checks
    assert r.signal is not None, r.failed
    assert r.signal.stop_price < min(path[-8:])


def test_pullback_continuation() -> None:
    path = [1.0]
    for i in range(72):  # zig-zag uptrend: 4 bars up, 3 bars down
        path.append(path[-1] * (1.012 if i % 7 < 4 else 0.994))
    top = max(path)
    path += [top * 0.975, top * 0.955, top * 0.95, top * 0.975]  # pullback into EMA21, buying returns
    vols = [1000.0] * (len(path) - 1) + [2500.0]
    r = pullback_continuation(ctx(bars(path, vols)), EntryConfig())
    assert r.checks["strong_trend"] and r.checks["pulled_back_to_ema21"], r.checks
    assert r.signal is not None, r.failed


def _narrative(status="emerging", strength=0.7) -> Narrative:
    return Narrative("penguin robot", status, 12, 2, 4.3, 12, 0.1, 400, 3, 0.7, T0, 2.0, strength,
                     tokens=(TokenLink(TOK, "description_and_links", 0.8),))


def test_narrative_breakout_requires_authentic_link_and_demand() -> None:
    path = [1.0 + 0.002 * i for i in range(60)]
    n = _narrative()
    good = ctx(bars(path), narrative=n, narrative_link=n.tokens[0], narrative_strength=0.56)
    results = {r.strategy: r for r in evaluate_all(good)}
    assert results["A_narrative_breakout"].signal is not None, results["A_narrative_breakout"].failed
    weak_link = replace(good, narrative_link=TokenLink(TOK, "name_only", 0.2))
    assert "authentic_link" in evaluate_all(weak_link, enabled=["A_narrative_breakout"])[0].failed
    fading = replace(good, narrative=_narrative(status="fading"))
    assert "narrative_gaining_attention" in evaluate_all(fading, enabled=["A_narrative_breakout"])[0].failed


def test_second_wave_rotation() -> None:
    path = [1.0 + 0.002 * i for i in range(60)]
    alert = RotationAlert("penguin robot", "solana:LEAD", TOK.key, 0.6, False,
                          evidence={"follower_volume_accel": 3.3})
    r = evaluate_all(ctx(bars(path), rotation=alert), enabled=["B_second_wave_rotation"])[0]
    assert r.signal is not None, r.failed
    crowded = evaluate_all(ctx(bars(path), rotation=replace(alert, crowded=True)), enabled=["B_second_wave_rotation"])[0]
    assert "not_crowded" in crowded.failed


def test_security_failure_blocks_all_strategies() -> None:
    path, vols = breakout_path()
    bad = SecurityReport(TOK, T0, sources={"rugcheck": "ok"}, hard_fail=["no_sell_route"])
    rs = evaluate_all(ctx(bars(path, vols), security=bad))
    assert all(r.signal is None for r in rs)


# ------------------------------------------------------------------ scoring


def test_opportunity_score_components_and_reweighting() -> None:
    path, vols = breakout_path()
    c = ctx(bars(path, vols))
    s = opportunity_score(c, reward_to_risk=2.5)
    assert 0 < s.score <= 100 and s.confidence < 1.0
    assert {"narrative_strength", "rotation_evidence", "insider_risk"} <= set(s.missing)
    n = _narrative()
    full = opportunity_score(replace(c, narrative=n, narrative_strength=0.6,
                                     insider=InsiderRisk(10, "low", "normal", {}, ()),
                                     security=SecurityReport(TOK, T0, sources={"rugcheck": "ok"})), reward_to_risk=2.5)
    assert full.confidence > s.confidence
    heavy = opportunity_score(c, 2.5, OpportunityWeights(execution_quality=100))
    assert heavy.score != s.score
    bad = opportunity_score(replace(c, security=SecurityReport(TOK, T0, hard_fail=["x"])))
    assert bad.score == 0


# ------------------------------------------------------------------ exits


def position(entry=1.0, stop=0.95, target=8.0, opened=T0, strategy="C_volume_breakout", inval=()) -> PositionState:
    return PositionState("p1", strategy, entry, stop, stop, target, entry, Mode.NORMAL, opened, 0.6, 0.02, 400_000,
                         tuple(inval))


def market(price: float, w=None, liq=400_000, trend_up=True, **kw) -> MarketContext:
    path = [price * (0.99 if trend_up else 1.01) ** (29 - i) for i in range(30)]  # ends exactly at price
    return ctx(bars(path), w=w or windows(), liq=liq, **kw)


def test_stop_loss_and_hard_exits() -> None:
    eng = ExitEngine()
    assert eng.evaluate(position(), market(0.94), T0 + timedelta(minutes=3)).reason == "stop_loss_hit"
    ins = InsiderRisk(85, "large_holder_selling", "exit", {}, ())
    assert eng.evaluate(position(), market(1.01, insider=ins), T0).reason == "insider_large_holder_selling"
    d = eng.evaluate(position(), market(1.01, liq=250_000), T0)
    assert d.action == "exit" and d.reason.startswith("liquidity_down_38")


def test_target_with_weak_momentum_exits_strong_momentum_upgrades_and_takes_partial() -> None:
    eng = ExitEngine()
    weak = windows(buys=50, sells=50, vol_m5=5_000)
    d = eng.evaluate(position(), market(1.09, w=weak), T0 + timedelta(minutes=12))
    assert d.action == "exit" and d.reason == "target_reached_momentum_weak"
    d = eng.evaluate(position(), market(1.09), T0 + timedelta(minutes=12))
    assert d.action == "partial" and d.fraction_of_current == 0.25 and d.state.mode is Mode.MOMENTUM
    assert d.state.stop >= 1.0 * 1.006  # at least break-even plus round-trip costs
    assert d.state.remaining_fraction == 0.75


def test_runner_mode_lets_winners_run_and_stop_only_rises() -> None:
    eng = ExitEngine()
    s = replace(position(), mode=Mode.MOMENTUM, partial_levels_done=2, remaining_fraction=0.5, stop=1.01, highest=1.5)
    strong = windows(vol_m5=40_000, buyers_m5=90)
    d = eng.evaluate(s, market(1.6, w=strong), T0 + timedelta(minutes=40))
    assert d.state.mode is Mode.RUNNER and d.action == "hold"
    runner = d.state
    # +300%: still holding — no fixed ceiling while momentum is strong
    d2 = eng.evaluate(runner, market(4.0, w=strong), T0 + timedelta(minutes=90))
    assert d2.action == "hold" and d2.state.stop >= 4.0 * 0.75
    # a 5 % dip with strong momentum stays in (trail >= 8 %)
    d3 = eng.evaluate(d2.state, market(3.8, w=strong), T0 + timedelta(minutes=95))
    assert d3.action == "hold" and d3.state.stop == d2.state.stop  # stop never lowered
    # momentum gone and sellers accelerating: trail tightens and the exit fires
    dump = windows(buys=20, sells=120, vol_m5=60_000)
    d4 = eng.evaluate(d3.state, market(3.65, w=dump, trend_up=False), T0 + timedelta(minutes=100))
    assert d4.action == "exit" and d4.reason in ("runner_trail_hit", "trailing_stop_hit")


def test_time_stop_and_selling_pressure_in_normal_mode() -> None:
    eng = ExitEngine()
    d = eng.evaluate(position(), market(1.01), T0 + timedelta(minutes=31))
    assert d.reason == "time_stop"
    sell = windows(buys=20, sells=100, vol_m5=40_000)
    assert eng.evaluate(position(), market(1.01, w=sell), T0 + timedelta(minutes=2)).reason == "selling_pressure_accelerating"


def test_thesis_invalidation() -> None:
    eng = ExitEngine()
    p = position(inval=("narrative_fading",))
    d = eng.evaluate(p, market(1.02, narrative=_narrative(status="fading", strength=0.5), narrative_strength=0.4),
                     T0 + timedelta(minutes=5))
    assert d.reason == "thesis_invalidated:narrative_fading"


def test_state_round_trip_and_partial_plan_validation() -> None:
    s = replace(position(), mode=Mode.RUNNER, notes=("a",))
    assert PositionState.from_dict(s.to_dict()) == s
    with pytest.raises(ValueError):
        PartialPlan(((1.0, 0.5), (2.0, 0.5)))
    eng = ExitEngine(ExitConfig(partial_plan=PartialPlan(((1.0, 0.5),))))
    d = eng.evaluate(position(), market(1.09), T0 + timedelta(minutes=12))
    assert d.action == "partial" and d.fraction_of_current == 0.5
