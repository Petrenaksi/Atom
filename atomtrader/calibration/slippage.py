"""Slippage calibration for Solana paper execution against live Jupiter quotes.

Measures the *effective cost* of exact-size swaps versus the mid price:

    buy  (USDC -> token):  cost = 1 - (tokens_out * P_token) / (usdc_in * P_usdc)
    sell (token -> USDC):  cost = 1 - (usdc_out * P_usdc) / (tokens_in * P_token)

This includes price impact and pool fees across all Jupiter routes. It excludes the
platform fee on managed execution and the network fee (charged separately), and it is a
*quote*, not a landed fill: adverse movement between quote and landing (latency, MEV) is
estimated separately from re-quotes after a delay ("drift").

One-sided costs against a mid price are confounded by per-token mid-price bias (observed:
PUMP -53 bps to buy, +57 to sell). The *round trip* (buy + sell at the same size) cancels
that bias and is what determines P&L, so the model is built on round-trip statistics.
Linear size/liquidity fits per side are reported for reference (they explained ~0% of
variance in the first run: costs are dominated by token-specific effects, not size).

Conclusion encoded in the saved model: on Solana, paper fills use exact-size Jupiter
quotes; the fitted numbers serve as latency noise and as a conservative fallback.

    python -m atomtrader.calibration.slippage --tokens 14 --out docs/calibration
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from atomtrader.core.models import Chain, TokenRef
from atomtrader.marketdata.base_assets import BaseAssetRegistry
from atomtrader.marketdata.dexscreener import DexScreenerClient
from atomtrader.marketdata.geckoterminal import GeckoTerminalClient
from atomtrader.marketdata.http import ApiError
from atomtrader.marketdata.jupiter import JupiterClient, NoRoute

MODEL_FILE = Path(__file__).resolve().parent.parent / "data" / "slippage_model_solana.json"
DEFAULT_SIZES_USD = (25, 75, 150, 300)
UNCALIBRATED_POOL_FEE_BPS = 25.0


@dataclass
class Sample:
    at: str
    mint: str
    symbol: str
    side: str
    size_usd: float
    liq_main_usd: float | None
    liq_agg_usd: float | None
    outcome: str  # ok | no_route | rate_limited | error
    cost_bps: float | None = None
    jup_impact_pct: float | None = None
    route: str = ""
    detail: str = ""


@dataclass
class DriftSample:
    mint: str
    size_usd: float
    delay_s: float
    drift_bps: float  # +: second quote better for us; -: worse


# --------------------------------------------------------------------------- model math


def uncalibrated_cost_bps(side: str, size_usd: float, liq_usd: float) -> float:
    """What the paper model assumed before calibration (excl. aggregator/network fees)."""
    x = size_usd / (liq_usd / 2)
    impact = x if side == "buy" else 1 - 1 / (1 + x)
    return impact * 10_000 + UNCALIBRATED_POOL_FEE_BPS


@dataclass(frozen=True)
class LinearFit:
    intercept_bps: float
    slope_bps: float  # bps of cost per 1.0 of size/liquidity
    n: int
    rmse_bps: float
    r2: float
    abs_resid_p50: float
    abs_resid_p90: float

    def predict(self, x: float) -> float:
        return self.intercept_bps + self.slope_bps * x


def fit_linear(xs: list[float], ys: list[float]) -> LinearFit:
    n = len(xs)
    if n < 3:
        raise ValueError("need at least 3 samples")
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx > 0 else 0.0
    intercept = my - slope * mx
    resid = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
    ss_res = sum(r * r for r in resid)
    ss_tot = sum((y - my) ** 2 for y in ys)
    absr = sorted(abs(r) for r in resid)
    return LinearFit(
        intercept_bps=intercept,
        slope_bps=slope,
        n=n,
        rmse_bps=math.sqrt(ss_res / n),
        r2=1 - ss_res / ss_tot if ss_tot > 0 else 0.0,
        abs_resid_p50=absr[n // 2],
        abs_resid_p90=absr[min(n - 1, int(n * 0.9))],
    )


def rmse(errors: Iterable[float]) -> float:
    e = list(errors)
    return math.sqrt(sum(x * x for x in e) / len(e)) if e else float("nan")


def _pct(sorted_vals: list[float], q: float) -> float:
    return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * q))]


def round_trips(samples: list[Sample]) -> list[dict[str, Any]]:
    by: dict[tuple[str, float], dict[str, Sample]] = {}
    for s in samples:
        if s.outcome == "ok" and s.cost_bps is not None:
            by.setdefault((s.mint, s.size_usd), {})[s.side] = s
    out = []
    for (mint, size), v in by.items():
        if "buy" in v and "sell" in v:
            liq = v["buy"].liq_main_usd
            old = (uncalibrated_cost_bps("buy", size, liq) + uncalibrated_cost_bps("sell", size, liq)) if liq else None
            out.append({"mint": mint, "symbol": v["buy"].symbol, "size_usd": size, "liq_main_usd": liq,
                        "round_trip_bps": v["buy"].cost_bps + v["sell"].cost_bps,  # type: ignore[operator]
                        "uncalibrated_round_trip_bps": old})
    return out


LIQ_BUCKETS = ((0.0, 300e3), (300e3, 2e6), (2e6, float("inf")))


def analyse(samples: list[Sample], drift: list[DriftSample]) -> dict[str, Any]:
    ok = [s for s in samples if s.outcome == "ok" and s.cost_bps is not None and s.liq_main_usd]
    result: dict[str, Any] = {
        "samples_total": len(samples),
        "samples_ok": len(ok),
        "outcomes": {o: sum(1 for s in samples if s.outcome == o) for o in sorted({s.outcome for s in samples})},
        "sides": {},
    }
    for side in ("buy", "sell"):
        rows = [s for s in ok if s.side == side]
        if len(rows) < 3:
            continue
        xs_main = [s.size_usd / s.liq_main_usd for s in rows]  # type: ignore[operator]
        ys = [s.cost_bps for s in rows]  # type: ignore[misc]
        fit_main = fit_linear(xs_main, ys)
        agg_rows = [s for s in rows if s.liq_agg_usd]
        fit_agg = (
            fit_linear([s.size_usd / s.liq_agg_usd for s in agg_rows], [s.cost_bps for s in agg_rows])  # type: ignore[operator,misc]
            if len(agg_rows) >= 3 else None
        )
        old_err = [s.cost_bps - uncalibrated_cost_bps(side, s.size_usd, s.liq_main_usd) for s in rows]  # type: ignore[operator,arg-type]
        result["sides"][side] = {
            "n": len(rows),
            "observed_cost_bps": {
                "mean": statistics.fmean(ys), "median": statistics.median(ys), "min": min(ys), "max": max(ys),
            },
            "fit_main_liquidity": asdict(fit_main),
            "fit_aggregate_liquidity": asdict(fit_agg) if fit_agg else None,
            "uncalibrated_model": {"rmse_bps": rmse(old_err), "mean_error_bps": statistics.fmean(old_err)},
        }
    rts = round_trips(samples)
    if rts:
        vals = sorted(r["round_trip_bps"] for r in rts)
        errs = [r["round_trip_bps"] - r["uncalibrated_round_trip_bps"] for r in rts if r["uncalibrated_round_trip_bps"] is not None]
        buckets = []
        for lo, hi in LIQ_BUCKETS:
            sub = sorted(r["round_trip_bps"] for r in rts if r["liq_main_usd"] and lo <= r["liq_main_usd"] < hi)
            if sub:
                buckets.append({"liq_min_usd": lo, "liq_max_usd": None if hi == float("inf") else hi, "n": len(sub),
                                "median_bps": statistics.median(sub), "p75_bps": _pct(sub, 0.75), "max_bps": sub[-1]})
        result["round_trip"] = {
            "n": len(vals), "mean_bps": statistics.fmean(vals), "median_bps": statistics.median(vals),
            "p75_bps": _pct(vals, 0.75), "p90_bps": _pct(vals, 0.9), "min_bps": vals[0], "max_bps": vals[-1],
            "uncalibrated_error": {"mean_bps": statistics.fmean(errs), "rmse_bps": rmse(errs)} if errs else None,
            "by_liquidity": buckets,
        }
    if drift:
        d = [x.drift_bps for x in drift]
        result["drift"] = {
            "n": len(d),
            "delay_s": drift[0].delay_s,
            "mean_bps": statistics.fmean(d),
            "sd_bps": statistics.pstdev(d) if len(d) > 1 else 0.0,
            "abs_p50_bps": sorted(abs(x) for x in d)[len(d) // 2],
            "abs_p90_bps": sorted(abs(x) for x in d)[min(len(d) - 1, int(len(d) * 0.9))],
        }
    return result


NOISE_FLOOR_BPS = 10.0


def model_from_analysis(analysis: dict[str, Any], fitted_at: str) -> dict[str, Any]:
    """Parameters consumed by the paper engine (see execution/paper.py)."""
    rt = analysis.get("round_trip")
    if not rt:
        raise ValueError("no round-trip samples; cannot build a model")
    # Fallback (no live quote): AMM impact + pool fee + this extra cost per side, chosen so
    # a typical round trip is charged the observed p75 rather than the median.
    extra = max(0.0, rt["p75_bps"] / 2 - UNCALIBRATED_POOL_FEE_BPS)
    drift_sd = analysis.get("drift", {}).get("sd_bps", 0.0)
    return {
        "chain": "solana",
        "fitted_at": fitted_at,
        "fill_basis": "jupiter_exact_quote",
        "noise_bps_sd": max(NOISE_FLOOR_BPS, drift_sd),
        "fallback_extra_cost_bps_per_side": extra,
        "measured": {
            "round_trip": rt,
            "drift": analysis.get("drift"),
            "linear_fit_r2": {side: v["fit_main_liquidity"]["r2"] for side, v in analysis["sides"].items()},
        },
        "caveats": [
            "Quote-based costs exclude Jupiter's platform fee and network fees (charged separately).",
            "Quotes are not landed fills: MEV/latency can make real fills worse than drift suggests.",
            "Small sample of tokens/sizes; refit regularly; outside $25-$300 sizes it is extrapolation.",
        ],
    }


# --------------------------------------------------------------------------- data collection


async def select_tokens(
    n: int, registry: BaseAssetRegistry, gt: GeckoTerminalClient, min_liq: float, always: list[TokenRef]
) -> list[TokenRef]:
    seen: dict[str, TokenRef] = {t.address: t for t in always}
    for fetch in (gt.trending_pools, gt.new_pools):
        try:
            pools = await fetch(Chain.SOLANA)
        except ApiError:
            continue
        for p in sorted(pools, key=lambda p: -(p.liquidity_usd or 0)):
            if len(seen) >= n:
                break
            if p.liquidity_usd is None or p.liquidity_usd < Decimal(str(min_liq)):
                continue
            if not registry.is_base_asset(Chain.SOLANA, p.quote_address):
                continue
            if registry.is_base_asset(Chain.SOLANA, p.base.address):
                continue  # skip SOL/USDC themselves
            seen.setdefault(p.base.address, p.base)
    return list(seen.values())[:n]


async def collect(
    tokens: list[TokenRef],
    jup: JupiterClient,
    dex: DexScreenerClient,
    usdc: str,
    sizes: Iterable[float] = DEFAULT_SIZES_USD,
    drift_delay_s: float = 5.0,
    drift_size: float = 75.0,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Any] = asyncio.sleep,
    progress: Callable[[str], None] = lambda m: None,
) -> tuple[list[Sample], list[DriftSample]]:
    samples: list[Sample] = []
    drift: list[DriftSample] = []
    main_liq: dict[str, float] = {}
    try:
        for p in await dex.token_pairs(Chain.SOLANA, [t.address for t in tokens]):
            if p.liquidity_usd:
                main_liq[p.base.address] = float(p.liquidity_usd)
    except ApiError as err:
        progress(f"dexscreener liquidity unavailable: {err}")

    for t in tokens:
        try:
            prices = await jup.prices([t.address, usdc])
        except ApiError as err:
            progress(f"{t.symbol}: price error {err.kind.value}")
            continue
        if t.address not in prices or usdc not in prices:
            samples.append(Sample(clock().isoformat(), t.address, t.symbol, "both", 0, main_liq.get(t.address), None,
                                  "error", detail="no Jupiter price"))
            continue
        pt, pu = prices[t.address], prices[usdc]
        liq_agg = float(pt.liquidity_usd) if pt.liquidity_usd else None
        for size in sizes:
            for side in ("buy", "sell"):
                s = Sample(clock().isoformat(), t.address, t.symbol, side, float(size), main_liq.get(t.address), liq_agg, "ok")
                try:
                    if side == "buy":
                        q = await jup.quote(usdc, t.address, int(Decimal(str(size)) * 10**pu.decimals))
                        tokens_out = Decimal(q.out_amount_raw) / Decimal(10**pt.decimals)
                        s.cost_bps = float((1 - tokens_out * pt.usd_price / (Decimal(str(size)) * pu.usd_price)) * 10_000)
                    else:
                        qty = Decimal(str(size)) / pt.usd_price
                        q = await jup.quote(t.address, usdc, int(qty * 10**pt.decimals))
                        tokens_in = Decimal(q.in_amount_raw) / Decimal(10**pt.decimals)
                        usdc_out = Decimal(q.out_amount_raw) / Decimal(10**pu.decimals)
                        s.cost_bps = float((1 - usdc_out * pu.usd_price / (tokens_in * pt.usd_price)) * 10_000)
                    s.jup_impact_pct = float(q.price_impact_pct)
                    s.route = "+".join(q.route)
                except NoRoute as exc:
                    s.outcome, s.detail = "no_route", str(exc)[:160]
                except ApiError as err:
                    s.outcome, s.detail = ("rate_limited" if err.kind.value == "rate_limited" else "error"), str(err)[:160]
                samples.append(s)
        progress(f"{t.symbol:>10}: " + ", ".join(
            f"{x.side[0]}{int(x.size_usd)}={x.cost_bps:.0f}" if x.cost_bps is not None else f"{x.side[0]}{int(x.size_usd)}={x.outcome}"
            for x in samples if x.mint == t.address))

        # Drift: same buy quoted twice, `drift_delay_s` apart.
        try:
            amt = int(Decimal(str(drift_size)) * 10**pu.decimals)
            q1 = await jup.quote(usdc, t.address, amt)
            await sleep(drift_delay_s)
            q2 = await jup.quote(usdc, t.address, amt)
            drift.append(DriftSample(t.address, drift_size, drift_delay_s, (q2.out_amount_raw / q1.out_amount_raw - 1) * 10_000))
        except (NoRoute, ApiError):
            pass
    return samples, drift


async def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tokens", type=int, default=14)
    ap.add_argument("--min-liquidity", type=float, default=15_000)
    ap.add_argument("--out", default="docs/calibration")
    ap.add_argument("--save-model", action="store_true", help=f"write fitted model to {MODEL_FILE}")
    ap.add_argument("--from", dest="from_file", help="re-analyse a saved dataset instead of sampling")
    args = ap.parse_args()

    if args.from_file:
        raw = json.loads(Path(args.from_file).read_text())
        samples = [Sample(**s) for s in raw["samples"]]
        drift = [DriftSample(**d) for d in raw["drift"]]
        analysis = analyse(samples, drift)
        raw["analysis"] = analysis
        Path(args.from_file).write_text(json.dumps(raw, indent=1) + "\n")
        print(json.dumps(analysis.get("round_trip"), indent=1))
        if args.save_model:
            MODEL_FILE.write_text(json.dumps(model_from_analysis(analysis, raw["at"]), indent=1) + "\n")
            print(f"model saved to {MODEL_FILE}")
        return 0

    registry = BaseAssetRegistry.load()
    usdc = next(a.address for a in registry.assets if a.chain is Chain.SOLANA and a.symbol == "USDC")
    always = [TokenRef(Chain.SOLANA, "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "Bonk"),
              TokenRef(Chain.SOLANA, "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm", "$WIF")]
    jup, dex, gt = JupiterClient(), DexScreenerClient(), GeckoTerminalClient()
    try:
        tokens = await select_tokens(args.tokens, registry, gt, args.min_liquidity, always)
        print(f"sampling {len(tokens)} tokens: {[t.symbol for t in tokens]}")
        samples, drift = await collect(tokens, jup, dex, usdc, progress=print)
    finally:
        await jup.aclose()
        await dex.aclose()
        await gt.aclose()

    when = datetime.now(UTC)
    analysis = analyse(samples, drift)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = when.strftime("%Y%m%dT%H%MZ")
    (out / f"solana_{stamp}.json").write_text(json.dumps(
        {"at": when.isoformat(), "analysis": analysis,
         "samples": [asdict(s) for s in samples], "drift": [asdict(d) for d in drift]}, indent=1) + "\n")
    print(json.dumps(analysis, indent=1))
    if args.save_model:
        if not analysis["sides"]:
            print("not enough samples to fit; model not saved")
            return 1
        MODEL_FILE.write_text(json.dumps(model_from_analysis(analysis, when.isoformat()), indent=1) + "\n")
        print(f"model saved to {MODEL_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
