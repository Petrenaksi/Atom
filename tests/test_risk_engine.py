from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from atomtrader.config import RiskLimits
from atomtrader.core.models import Chain, TokenRef
from atomtrader.risk import (
    DailyRiskState,
    OpenPositionView,
    PortfolioView,
    RiskEngine,
    TradeIntent,
    TradingControls,
)
from tests.conftest import OTHER, T0, TOKEN

CHAINS = frozenset({Chain.SOLANA, Chain.BASE, Chain.BSC})


def intent(**kw) -> TradeIntent:
    base = dict(
        token=TOKEN,
        strategy="volume_breakout",
        entry_price_usd=Decimal("1.00"),
        stop_price_usd=Decimal("0.90"),  # 10% stop
        pool_liquidity_eur=Decimal("500000"),
        est_entry_slippage_bps=Decimal(50),
        est_exit_slippage_bps=Decimal(50),
        est_fee_pct_per_side=Decimal("0.35"),
        est_network_fee_eur=Decimal("0.05"),
        confidence=Decimal(1),
        narrative="ai-agents",
    )
    base.update(kw)
    return TradeIntent(**base)


def pf(equity: str = "1000", cash: str | None = None, positions: tuple = ()) -> PortfolioView:
    return PortfolioView(Decimal(equity), Decimal(cash if cash is not None else equity), positions)


def make_engine(limits: RiskLimits | None = None) -> RiskEngine:
    return RiskEngine(limits or RiskLimits(), TradingControls(), DailyRiskState(), CHAINS)


def test_sizes_by_risk_including_round_trip_costs() -> None:
    eng = make_engine()
    d = eng.evaluate(intent(), pf(), T0)
    assert d.approved and d.binding_constraint == "risk_budget"
    # risk 1% of 1000 = 10; per-unit loss = 0.10 stop + 0.01 slippage + 0.007 fees = 0.117
    expected = (Decimal(10) - Decimal("0.10")) / Decimal("0.117")
    assert abs(d.notional_eur - expected) < Decimal("0.01")
    # Loss at stop never exceeds the risk budget.
    assert d.risk_eur <= Decimal("10.00")


def test_confidence_scales_risk_between_min_and_max() -> None:
    lo = make_engine().evaluate(intent(confidence=Decimal(0)), pf(), T0)
    hi = make_engine().evaluate(intent(confidence=Decimal(1)), pf(), T0)
    assert lo.approved and hi.approved
    assert abs(lo.risk_eur - Decimal("5")) <= Decimal("0.01")
    assert abs(hi.risk_eur - Decimal("10")) <= Decimal("0.01")


def test_thin_liquidity_shrinks_position_to_exit_feasible_size() -> None:
    eng = make_engine(RiskLimits(max_pool_liquidity_fraction_pct=Decimal("0.5")))
    d = eng.evaluate(intent(pool_liquidity_eur=Decimal("30000"), stop_price_usd=Decimal("0.99")), pf(), T0)
    assert d.approved and d.binding_constraint == "liquidity"
    assert d.notional_eur == Decimal("150.00")  # 0.5% of pool liquidity, not market cap
    tiny = make_engine().evaluate(intent(pool_liquidity_eur=Decimal("10000")), pf(), T0)
    assert not tiny.approved and "liquidity_below_minimum" in tiny.reasons


def test_wide_stop_with_tight_caps_respects_max_position() -> None:
    d = make_engine().evaluate(intent(stop_price_usd=Decimal("0.99")), pf(), T0)  # 1% stop
    assert d.approved and d.binding_constraint == "max_position"
    assert d.notional_eur == Decimal("200.00")


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"stop_price_usd": Decimal("1.0")}, "stop_not_below_entry"),
        ({"stop_price_usd": Decimal("0.4")}, "stop_too_wide"),
        ({"security_hard_fail": True}, "security_hard_fail"),
        ({"insider_risk_score": Decimal(85)}, "insider_exit_risk_too_high"),
        ({"est_exit_slippage_bps": Decimal(900)}, "slippage_above_max"),
        ({"token": TokenRef(Chain.ETHEREUM, "0x" + "cd" * 20)}, "chain_disabled:ethereum"),
    ],
)
def test_hard_rejections(kw: dict, reason: str) -> None:
    d = make_engine().evaluate(intent(**kw), pf(), T0)
    assert not d.approved and reason in d.reasons


def test_insider_risk_between_50_and_80_halves_risk_at_most() -> None:
    base = make_engine().evaluate(intent(), pf(), T0)
    cut = make_engine().evaluate(intent(insider_risk_score=Decimal(79)), pf(), T0)
    assert cut.approved and base.notional_eur * Decimal("0.5") < cut.notional_eur < base.notional_eur


def test_controls_block_entries() -> None:
    eng = make_engine()
    eng.controls.trigger_emergency_stop(T0, "test")
    assert "emergency_stop" in eng.evaluate(intent(), pf(), T0).reasons
    eng = make_engine()
    eng.controls.pause_chain(Chain.SOLANA, T0)
    assert "chain_paused:solana" in eng.evaluate(intent(), pf(), T0).reasons
    assert eng.evaluate(intent(token=OTHER), pf(), T0).approved
    eng.controls.blacklist_token(OTHER, T0, "rug")
    assert "token_blacklisted" in eng.evaluate(intent(token=OTHER), pf(), T0).reasons
    with pytest.raises(PermissionError):
        c = TradingControls()
        c.trigger_emergency_stop(T0, "x")
        c.set_autonomous(True, T0)


def test_portfolio_limits_positions_no_averaging_and_narrative() -> None:
    held = OpenPositionView(TOKEN, "ai-agents", Decimal(100), Decimal(10))
    d = make_engine().evaluate(intent(), pf(positions=(held,)), T0)
    assert "already_holding_token" in d.reasons and "narrative_concentration" in d.reasons

    others = tuple(
        OpenPositionView(TokenRef(Chain.SOLANA, f"Mint{i}"), f"n{i}", Decimal(50), Decimal(1)) for i in range(3)
    )
    assert "max_open_positions" in make_engine().evaluate(intent(), pf(positions=others), T0).reasons


def test_exposure_and_cash_caps() -> None:
    big = (OpenPositionView(OTHER, "x", Decimal(580), Decimal(1)),)
    d = make_engine().evaluate(intent(stop_price_usd=Decimal("0.99")), pf(cash="420", positions=big), T0)
    assert d.approved and d.binding_constraint == "exposure" and d.notional_eur == Decimal("20.00")
    d = make_engine().evaluate(intent(), pf(cash="20"), T0)
    assert d.approved is False and d.reasons[0].startswith("size_below_minimum:cash")


def test_daily_loss_limit_halts_and_open_risk_counts_against_budget() -> None:
    eng = make_engine()
    eng.evaluate(intent(), pf(), T0)  # starts the day at 1000 equity -> limit 30
    L = eng.limits
    eng.state.record_closed_trade(Decimal("-25"), T0, L.max_daily_loss_pct, 99, 0)
    # 5 left; an open position risking 5 exhausts it.
    risky = (OpenPositionView(OTHER, "x", Decimal(50), Decimal(5)),)
    assert "daily_risk_budget_exhausted" in eng.evaluate(intent(), pf(positions=risky), T0).reasons
    # Remaining budget caps the trade's risk.
    d = eng.evaluate(intent(), pf(), T0)
    assert d.approved and d.risk_eur <= Decimal("5.00")
    eng.state.record_failed_tx_fee(Decimal("5"), T0, L.max_daily_loss_pct)
    assert eng.state.daily_halt
    assert "daily_loss_limit_reached" in eng.evaluate(intent(), pf(), T0).reasons
    assert any(e.kind == "daily_loss_limit" for e in eng.state.events)


def test_new_day_resets_daily_budget_in_local_timezone() -> None:
    eng = make_engine()
    late = datetime(2026, 11, 27, 22, 30, tzinfo=UTC)  # 23:30 Madrid
    eng.evaluate(intent(), pf(), late)
    eng.state.record_failed_tx_fee(Decimal("40"), late, eng.limits.max_daily_loss_pct)
    assert eng.state.daily_halt
    still_same_day = late + timedelta(minutes=20)  # 23:50 Madrid
    assert not eng.evaluate(intent(), pf(), still_same_day).approved
    next_day = late + timedelta(minutes=45)  # 00:15 Madrid next day
    assert eng.evaluate(intent(), pf("960"), next_day).approved
    assert eng.state.start_equity_eur == Decimal("960")


def test_cooldown_after_consecutive_losses_and_trade_cap() -> None:
    eng = make_engine()
    L = eng.limits
    eng.evaluate(intent(), pf(), T0)
    for _ in range(3):
        eng.state.record_closed_trade(Decimal("-1"), T0, L.max_daily_loss_pct, 3, 120)
    assert "loss_streak_cooldown" in eng.evaluate(intent(), pf(), T0 + timedelta(minutes=119)).reasons
    assert eng.evaluate(intent(), pf(), T0 + timedelta(minutes=121)).approved

    eng = make_engine()
    eng.evaluate(intent(), pf(), T0)
    for _ in range(20):
        eng.state.record_entry()
    assert "max_trades_per_day" in eng.evaluate(intent(), pf(), T0).reasons


def test_round_trip_cost_filter() -> None:
    d = make_engine().evaluate(
        intent(est_entry_slippage_bps=Decimal(250), est_exit_slippage_bps=Decimal(250), est_fee_pct_per_side=Decimal(1)),
        pf(),
        T0,
    )
    assert not d.approved and "round_trip_cost_too_high" in d.reasons
