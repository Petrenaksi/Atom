"""Exact-quote paper fills, Jupiter quoter (recorded responses) and calibration math."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from atomtrader.calibration.slippage import (
    DriftSample,
    Sample,
    analyse,
    fit_linear,
    model_from_analysis,
    round_trips,
    uncalibrated_cost_bps,
)
from atomtrader.core.models import AccountKind, Chain, OrderRequest, OrderStatus, Side
from atomtrader.execution.base import NoRoute, QuoteUnavailable
from atomtrader.execution.paper import ChainCostModel, PaperExecutionProvider, load_solana_cost_model
from atomtrader.marketdata.http import JsonHttpClient
from atomtrader.marketdata.jupiter import JupiterClient, JupiterSizedQuoter
from atomtrader.persistence.repository import Repository
from tests.conftest import TOKEN

FIX = Path(__file__).parent / "fixtures"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
COST = {c: ChainCostModel(Decimal("0.05"), Decimal(25), Decimal(10), Decimal(0), Decimal(10), Decimal(11), "test")
        for c in Chain}


class FakeQuoter:
    """Deterministic SizedQuoter: fixed executable price per token, or a scripted error."""

    def __init__(self, price_usd: Decimal, error: Exception | None = None) -> None:
        self.price = price_usd
        self.error = error
        self.calls: list[tuple[str, Decimal]] = []

    async def buy(self, address, spend_usd, slippage_bps):
        self.calls.append(("buy", spend_usd))
        if self.error:
            raise self.error
        return spend_usd / self.price, {"route": "FakeAMM"}

    async def sell(self, address, quantity, slippage_bps):
        self.calls.append(("sell", quantity))
        if self.error:
            raise self.error
        return quantity * self.price, {"route": "FakeAMM"}


def order(side: Side, cid: str = "o1", slip: int = 300, qty: Decimal | None = None) -> OrderRequest:
    if side is Side.BUY:
        return OrderRequest(cid, "p1", TOKEN, side, slip, "t", notional_eur=Decimal(92))
    return OrderRequest(cid, "p1", TOKEN, side, slip, "t", quantity=qty)


# ------------------------------------------------------------------ exact-quote fills


async def test_exact_quote_fill_uses_quote_price_and_no_pool_fee(quotes, fx, clock) -> None:
    # Spot (DexScreener) 0.002; executable quote 0.00201 (=50 bps worse).
    q = FakeQuoter(Decimal("0.00201"))
    p = PaperExecutionProvider(quotes, fx, COST, seed=5, clock=clock, sized_quoters={Chain.SOLANA: q})
    r = await p.execute(order(Side.BUY))
    assert r.status is OrderStatus.FILLED and r.details["fill_basis"] == "exact_quote"
    assert r.details["route"] == "FakeAMM"
    f = r.fill
    assert q.calls == [("buy", Decimal(92) / Decimal("0.92"))]  # €92 -> $100 of USDC
    drift = Decimal(r.details["drift_bps"]) / 10_000
    assert 0 <= drift < Decimal("0.01")
    expected = (Decimal(100) / Decimal("0.00201")) * (1 - drift)  # drift_bps is stored rounded
    assert abs(f.quantity / expected - 1) < Decimal("1e-6")
    # Fees: aggregator 10 bps + network only (pool fee is inside the quote).
    assert f.fee_eur == Decimal(92) * Decimal("0.001") + Decimal("0.05")
    assert r.realized_slippage_bps is not None and Decimal(45) < r.realized_slippage_bps < Decimal(90)


async def test_exact_quote_sell_and_drift_beyond_cap_reverts(quotes, fx, clock) -> None:
    q = FakeQuoter(Decimal("0.0019"))
    p = PaperExecutionProvider(quotes, fx, COST, seed=5, clock=clock, sized_quoters={Chain.SOLANA: q})
    r = await p.execute(order(Side.SELL, qty=Decimal(50_000)))
    assert r.status is OrderStatus.FILLED
    assert r.fill.gross_eur <= Decimal(50_000) * Decimal("0.0019") * Decimal("0.92")
    big_noise = {c: ChainCostModel(Decimal("0.05"), Decimal(25), Decimal(10), Decimal(0), Decimal(5000)) for c in Chain}
    p2 = PaperExecutionProvider(quotes, fx, big_noise, seed=5, clock=clock, sized_quoters={Chain.SOLANA: q})
    r2 = await p2.execute(order(Side.SELL, qty=Decimal(50_000), slip=50))
    assert r2.status is OrderStatus.FAILED and r2.error == "slippage_exceeded"
    assert r2.failed_fee_eur == Decimal("0.05") and r2.details["fill_basis"] == "exact_quote"


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
async def test_no_route_is_rejected_without_fee(quotes, fx, clock, side) -> None:
    q = FakeQuoter(Decimal(1), error=NoRoute("TOKEN_NOT_TRADABLE"))
    p = PaperExecutionProvider(quotes, fx, COST, seed=1, clock=clock, sized_quoters={Chain.SOLANA: q})
    r = await p.execute(order(side, qty=Decimal(10)))
    assert r.status is OrderStatus.REJECTED and r.error.startswith("no_route")
    assert r.failed_fee_eur == 0 and r.fill is None


async def test_quote_outage_rejects_buys_but_sells_fall_back_to_labelled_model(quotes, fx, clock) -> None:
    q = FakeQuoter(Decimal(1), error=QuoteUnavailable("rate_limited"))
    p = PaperExecutionProvider(quotes, fx, COST, seed=1, clock=clock, sized_quoters={Chain.SOLANA: q})
    b = await p.execute(order(Side.BUY))
    assert b.status is OrderStatus.REJECTED and b.error.startswith("quote_unavailable")
    s = await p.execute(order(Side.SELL, cid="s1", qty=Decimal(10_000)))
    assert s.status is OrderStatus.FILLED
    assert s.details == {"fill_basis": "model_fallback", "calibration": "test"}
    # Fallback charges AMM impact + 11 bps calibrated extra + noise, plus pool+agg fees.
    assert s.realized_slippage_bps >= Decimal(11)
    assert s.fill.fee_eur == s.fill.gross_eur * Decimal("0.0035") + Decimal("0.05")


async def test_fill_basis_is_persisted(tmp_path, quotes, fx, clock) -> None:
    from atomtrader.execution.engine import ExecutionEngine
    from atomtrader.portfolio.ledger import Ledger

    repo = Repository(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    await repo.init()
    p = PaperExecutionProvider(quotes, fx, COST, seed=5, clock=clock,
                               sized_quoters={Chain.SOLANA: FakeQuoter(Decimal("0.002"))})
    await ExecutionEngine(p, repo, Ledger(AccountKind.PAPER, Decimal(1000)), clock).submit(order(Side.BUY))
    [stored] = await repo.load_final_results(AccountKind.PAPER)
    assert stored.details["fill_basis"] == "exact_quote" and stored.details["route"] == "FakeAMM"
    await repo.close()


# ------------------------------------------------------------------ Jupiter quoter (recorded responses)


async def _no_sleep(_: float) -> None:
    return None


def jupiter(routes: dict[str, tuple[int, str]]) -> JupiterClient:
    def handler(req: httpx.Request) -> httpx.Response:
        for prefix, (status, body) in routes.items():
            if req.url.path.startswith(prefix):
                return httpx.Response(status, text=body)
        return httpx.Response(404, text="{}")

    return JupiterClient(JsonHttpClient("https://lite-api.jup.ag", per_minute=10_000, max_retries=0,
                                        transport=httpx.MockTransport(handler), sleep=_no_sleep))


async def test_jupiter_quoter_parses_recorded_quote() -> None:
    jq = JupiterSizedQuoter(jupiter({
        "/price/v3": (200, (FIX / "jupiter_price_v3.json").read_text()),
        "/swap/v1/quote": (200, (FIX / "jupiter_quote_usdc_bonk.json").read_text()),
    }), USDC)
    qty, details = await jq.buy(BONK, Decimal(80), 300)
    assert qty == Decimal(3631040000000) / Decimal(10**5)  # Bonk has 5 decimals
    assert details["route"] == "Scorch" and details["context_slot"] == "474575255"


async def test_jupiter_quoter_error_classes() -> None:
    price = (200, (FIX / "jupiter_price_v3.json").read_text())
    rejected = jupiter({"/price/v3": price, "/swap/v1/quote": (400, '{"error":"not tradable","errorCode":"TOKEN_NOT_TRADABLE"}')})
    with pytest.raises(NoRoute):
        await JupiterSizedQuoter(rejected, USDC).buy(BONK, Decimal(10), 300)
    limited = jupiter({"/price/v3": price, "/swap/v1/quote": (429, "slow down")})
    with pytest.raises(QuoteUnavailable):
        await JupiterSizedQuoter(limited, USDC).sell(BONK, Decimal(1_000_000), 300)
    with pytest.raises(NoRoute):  # dust amount that rounds to 0 base units
        await JupiterSizedQuoter(limited, USDC).sell(BONK, Decimal("0.000001"), 300)


async def test_round_trip_estimate_is_mid_price_free() -> None:
    class Quoter(JupiterSizedQuoter):
        async def buy(self, address, spend_usd, slippage_bps):
            return spend_usd * 1000, {}

        async def sell(self, address, quantity, slippage_bps):
            return quantity / 1000 * Decimal("0.994"), {}

    rt = await Quoter(jupiter({}), USDC).round_trip_cost_bps(BONK, Decimal(100))
    assert rt == Decimal(60)


# ------------------------------------------------------------------ calibration math


def test_fit_linear_recovers_line() -> None:
    xs = [0.0, 0.001, 0.002, 0.004]
    f = fit_linear(xs, [10 + 20_000 * x for x in xs])
    assert abs(f.intercept_bps - 10) < 1e-9 and abs(f.slope_bps - 20_000) < 1e-6
    assert f.r2 == pytest.approx(1.0) and f.rmse_bps == pytest.approx(0.0, abs=1e-9)


def test_uncalibrated_model_matches_paper_amm_formula() -> None:
    assert uncalibrated_cost_bps("buy", 100, 200_000) == pytest.approx(10 + 25)
    assert uncalibrated_cost_bps("sell", 100, 200_000) == pytest.approx((1 - 1 / 1.001) * 10_000 + 25)


def _samples() -> list[Sample]:
    out = []
    for i, (b, s) in enumerate([(-50, 55), (10, 30), (20, 40), (60, 90)]):
        for side, c in (("buy", b), ("sell", s)):
            out.append(Sample("t", f"m{i}", f"T{i}", side, 100.0, 500_000.0, 900_000.0, "ok", float(c)))
    out.append(Sample("t", "m9", "T9", "buy", 100.0, 1e5, None, "no_route", None))
    return out


def test_round_trip_cancels_mid_bias_and_model_uses_p75() -> None:
    rts = {r["symbol"]: r["round_trip_bps"] for r in round_trips(_samples())}
    assert rts == {"T0": 5.0, "T1": 40.0, "T2": 60.0, "T3": 150.0}
    a = analyse(_samples(), [DriftSample("m0", 75, 5, d) for d in (2.0, -4.0, 1.0)])
    assert a["outcomes"] == {"no_route": 1, "ok": 8}
    assert a["round_trip"]["p75_bps"] == 150.0
    m = model_from_analysis(a, "2026-11-27T00:00:00+00:00")
    assert m["fallback_extra_cost_bps_per_side"] == pytest.approx(150 / 2 - 25)
    assert m["noise_bps_sd"] == 10.0  # measured drift sd (~2.6) is below the 10 bps floor
    assert m["fill_basis"] == "jupiter_exact_quote"


def test_solana_cost_model_loads_calibration_or_falls_back(tmp_path) -> None:
    f = tmp_path / "m.json"
    f.write_text(json.dumps({"noise_bps_sd": 12.345, "fallback_extra_cost_bps_per_side": 7.0, "fitted_at": "X"}))
    m = load_solana_cost_model(f)
    assert m.adverse_noise_bps_sd == Decimal("12.35") and m.extra_cost_bps == Decimal("7.0")
    assert m.calibration == "jupiter_quotes:X" and m.pool_fee_bps == 25
    missing = load_solana_cost_model(tmp_path / "nope.json")
    assert missing.calibration == "uncalibrated" and missing.adverse_noise_bps_sd == 40
