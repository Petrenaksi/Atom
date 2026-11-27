from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from atomtrader.analytics.performance import TradeRecord, daily_report, max_drawdown, metrics, propose_adjustments, review_trade
from atomtrader.backtest.engine import BacktestConfig, Backtester, walk_forward
from atomtrader.backtest.frames import frames_from_history, synthetic_timeline
from atomtrader.strategy.exits import ExitConfig, PartialPlan

T0 = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)


def tr(pid: str, pnl: str, strategy="C_volume_breakout", minutes=15, reason="trailing_stop_hit", mode="normal", **kw) -> TradeRecord:
    return TradeRecord(pid, "solana:X", "solana", strategy, T0, T0 + timedelta(minutes=minutes), Decimal(pnl),
                       Decimal("0.30"), Decimal(100), reason, mode, account_kind=kw.pop("account_kind", "paper"), **kw)


# ------------------------------------------------------------------ analytics


def test_metrics_and_drawdown() -> None:
    ts = [tr("a", "10"), tr("b", "-4"), tr("c", "6"), tr("d", "-2")]
    m = metrics(ts)
    assert m.trades == 4 and m.win_rate == 0.5 and m.net_pnl_eur == Decimal(10)
    assert m.profit_factor == 16 / 6 and m.avg_loss_eur == Decimal(-3) and m.best_eur == Decimal(10)
    dd, pct = max_drawdown([Decimal(x) for x in (100, 110, 104, 112, 90, 95)])
    assert dd == 22 and round(pct, 2) == 19.64


def test_trade_review_findings() -> None:
    r = review_trade(tr("x", "-3", minutes=2, reason="stop_loss_hit"))
    assert any("false positive" in f for f in r.findings)
    r = review_trade(tr("y", "2", reason="time_stop", max_favorable_pct=12.0, realized_return_pct=2.0))
    assert any("premature" in f for f in r.findings)
    r = review_trade(tr("z", "5", entry_h1_change_pct=150.0, realized_slippage_bps=400, est_round_trip_cost_bps=100))
    assert any("late entry" in f for f in r.findings) and any("slippage problem" in f for f in r.findings)


def test_proposals_are_never_applied_and_need_enough_evidence() -> None:
    losing = [tr(f"l{i}", "-2", strategy="E_vwap_reclaim") for i in range(15)] + [tr("w", "3", strategy="E_vwap_reclaim")]
    props = propose_adjustments(losing)
    assert props and props[0].kind == "disable_strategy" and props[0].status == "proposed"
    assert propose_adjustments(losing[:5]) == []  # too few trades to conclude anything


def test_daily_report_contents_and_no_mixing() -> None:
    ts = [tr("a", "10", mode="runner"), tr("b", "-4", strategy="E_vwap_reclaim")]
    rep = daily_report("2026-11-20", ts, Decimal(1000), Decimal(1006), anomalies=["1 execution failure"])
    md = rep.to_markdown()
    for s in ("PAPER (simulated funds)", "Starting equity: €1,000.00", "Net P&L (after all trading costs, incl. failed transactions): €6.00",
              "By exit mode (runner performance)", "Anomalies", "E_vwap_reclaim"):
        assert s in md
    try:
        daily_report("d", [tr("live", "1", account_kind="live")], Decimal(1), None)
        raise AssertionError("mixing should fail")
    except ValueError:
        pass


# ------------------------------------------------------------------ frames & backtests


def test_frames_have_no_lookahead() -> None:
    frames = synthetic_timeline("trend_pullbacks", T0, minutes=150, seed=1)
    assert frames
    for f in frames:
        assert all(c.ts + timedelta(minutes=1) <= f.at for c in f.ctx.candles_1m)
        assert f.ctx.features.as_of <= f.at


def test_backtest_mechanics_and_accounting() -> None:
    tls = [synthetic_timeline(k, T0, minutes=300, seed=s) for k in ("runner", "pump_and_dump", "chop", "trend_pullbacks") for s in (1, 2)]
    res = Backtester(BacktestConfig(min_opportunity=30)).run(tls)
    m = res.metrics
    assert res.signals > 0 and m.trades > 0
    # every euro of cash change is explained by closed-trade P&L minus fees burned by failed
    # transactions (to 1e-20; 40-digit Decimal context)
    assert abs(res.net_pnl_eur - (res.ending_cash_eur - res.starting_eur)) < Decimal('1e-20')
    assert all(t.fees_eur > 0 for t in res.trades)
    # Risk limits hold inside the backtest: never more than 3 concurrent positions.
    opens = sorted([(t.opened_at, 1) for t in res.trades] + [(t.closed_at, -1) for t in res.trades])
    level = peak = 0
    for _, d in opens:
        level += d
        peak = max(peak, level)
    assert peak <= 3


def test_higher_costs_reduce_results() -> None:
    tl = [synthetic_timeline("runner", T0, minutes=300, seed=s, round_trip_cost_bps=c) for s in (1, 2, 3) for c in (60.0,)]
    cheap = Backtester(BacktestConfig(min_opportunity=30)).run(tl)
    tl2 = [[replace(f, ctx=replace(f.ctx, round_trip_cost_bps=250.0)) for f in t] for t in tl]
    dear = Backtester(BacktestConfig(min_opportunity=30)).run(tl2)
    assert dear.metrics.net_pnl_eur < cheap.metrics.net_pnl_eur or dear.metrics.trades < cheap.metrics.trades


def test_walk_forward_compares_partial_plans_out_of_sample() -> None:
    tls = [synthetic_timeline(k, T0 + timedelta(minutes=300 * i), minutes=300, seed=i)
           for i, k in enumerate(["runner", "chop", "trend_pullbacks", "pump_and_dump", "runner", "trend_pullbacks", "chop", "runner"])]
    configs = [
        BacktestConfig(name="partials_25_25", min_opportunity=30),
        BacktestConfig(name="partials_50", min_opportunity=30, exit=ExitConfig(partial_plan=PartialPlan(((1.0, 0.5),)))),
        BacktestConfig(name="no_partials", min_opportunity=30, exit=ExitConfig(partial_plan=PartialPlan(()))),
    ]
    wf = walk_forward(tls, configs, folds=3, min_trades=1)
    assert len(wf.folds) == 3
    assert all(f.chosen in {c.name for c in configs} for f in wf.folds)
    assert all(f.train_start < f.test_start < f.test_end for f in wf.folds)
    assert wf.oos_metrics.trades == len(wf.oos_trades)


def test_frames_from_recorded_style_history() -> None:
    frames = synthetic_timeline("grind", T0, minutes=120, seed=3)
    snaps_like = [f.ctx.features for f in frames]
    assert snaps_like[0].ta_1m is not None and frames_from_history([], []) == []
