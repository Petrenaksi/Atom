"""Per-token feature extraction for the scanner, detectors and strategies.

Inputs: the latest pair snapshot(s) (DexScreener: txn counts; GeckoTerminal: unique
buyers/sellers), the snapshot history (liquidity/price trend), 1-minute candles (resampled to
5m/15m), the security report and insider risk. Every feature is ``None`` when its inputs are
missing — nothing is estimated.

Acceleration features compare the most recent window with the average rate of a longer one,
e.g. ``volume_accel_m5_h1 = (vol_m5 * 12) / vol_h1``: 1.0 means "same pace as the last hour",
3.0 means "three times the hourly pace right now".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from atomtrader.analysis.indicators import TechnicalSnapshot, resample, technical_snapshot
from atomtrader.core.models import TokenRef
from atomtrader.marketdata.models import Candle, PairSnapshot, WindowStats
from atomtrader.security.insider import InsiderRisk
from atomtrader.security.models import SecurityReport


def _f(v: Decimal | int | None) -> float | None:
    return None if v is None else float(v)


def _rate_ratio(short: float | None, long: float | None, factor: float) -> float | None:
    if short is None or long is None or long <= 0:
        return None
    return short * factor / long


def merge_windows(snaps: Sequence[PairSnapshot]) -> dict[str, WindowStats]:
    """Combine sources per window: counts/volume from the first snapshot that has them,
    unique buyers/sellers from whichever source provides them (GeckoTerminal)."""
    out: dict[str, WindowStats] = {}
    for w in ("m5", "m15", "m30", "h1", "h6", "h24"):
        parts = [s.windows[w] for s in snaps if w in s.windows]
        if not parts:
            continue
        pick = lambda attr: next((getattr(p, attr) for p in parts if getattr(p, attr) is not None), None)  # noqa: E731
        out[w] = WindowStats(pick("buys"), pick("sells"), pick("buyers"), pick("sellers"),
                             pick("volume_usd"), pick("price_change_pct"))
    return out


@dataclass(frozen=True)
class TokenFeatures:
    token: TokenRef
    as_of: datetime
    pair_address: str
    price_usd: float
    liquidity_usd: float | None
    market_cap_usd: float | None
    fdv_usd: float | None
    age_minutes: float | None
    # price momentum (% change)
    chg_m5: float | None
    chg_h1: float | None
    chg_h6: float | None
    chg_h24: float | None
    # participation
    volume_m5: float | None
    volume_h1: float | None
    volume_h24: float | None
    volume_accel_m5_h1: float | None
    volume_accel_h1_h24: float | None
    txns_m5: int | None
    txns_accel_m5_h1: float | None
    buy_ratio_m5: float | None  # buys / (buys + sells)
    buy_ratio_h1: float | None
    buyers_m5: int | None  # unique wallets (GeckoTerminal only)
    buyers_h1: int | None
    buyer_accel_m5_h1: float | None
    txns_per_trader_h1: float | None  # high => few wallets doing many trades (wash risk)
    volume_to_liquidity_h24: float | None
    # liquidity trend (from snapshot history)
    liquidity_change_pct: float | None
    liquidity_window_minutes: float | None
    # technicals
    ta_1m: TechnicalSnapshot | None
    ta_5m: TechnicalSnapshot | None
    ta_15m: TechnicalSnapshot | None
    # security
    security_passed: bool | None
    security_warnings: tuple[str, ...]
    insider_risk: InsiderRisk | None
    boosted: bool = False  # paid DexScreener promotion seen recently
    sources: tuple[str, ...] = field(default_factory=tuple)


def build_features(
    latest: Sequence[PairSnapshot],
    history: Sequence[PairSnapshot] = (),
    candles_1m: Sequence[Candle] = (),
    security: SecurityReport | None = None,
    insider: InsiderRisk | None = None,
    boosted: bool = False,
    liquidity_window: timedelta = timedelta(minutes=60),
) -> TokenFeatures:
    if not latest:
        raise ValueError("need at least one snapshot")
    # Price/liquidity from the most liquid recent snapshot (DexScreener main pair preferred).
    primary = max(latest, key=lambda s: (s.source == "dexscreener", s.liquidity_usd or 0))
    w = merge_windows(sorted(latest, key=lambda s: s.source != "dexscreener"))
    m5, h1, h6, h24 = (w.get(k, WindowStats()) for k in ("m5", "h1", "h6", "h24"))

    def txns(ws: WindowStats) -> int | None:
        return None if ws.buys is None or ws.sells is None else ws.buys + ws.sells

    def ratio(ws: WindowStats) -> float | None:
        t = txns(ws)
        return None if not t else ws.buys / t  # type: ignore[operator]

    traders_h1 = None if h1.buyers is None or h1.sellers is None else h1.buyers + h1.sellers
    liq_hist = [(s.as_of, s.liquidity_usd) for s in history if s.liquidity_usd is not None
                and primary.as_of - s.as_of <= liquidity_window]
    liq_change = liq_window = None
    if liq_hist and primary.liquidity_usd:
        first_t, first_v = min(liq_hist, key=lambda x: x[0])
        if first_v and first_v > 0 and primary.as_of > first_t:
            liq_change = float((primary.liquidity_usd - first_v) / first_v * 100)
            liq_window = (primary.as_of - first_t).total_seconds() / 60

    c1 = list(candles_1m)
    return TokenFeatures(
        token=primary.base,
        as_of=primary.as_of,
        pair_address=primary.pair_address,
        price_usd=float(primary.price_usd),
        liquidity_usd=_f(primary.liquidity_usd),
        market_cap_usd=_f(primary.market_cap_usd),
        fdv_usd=_f(primary.fdv_usd),
        age_minutes=(primary.as_of - primary.pair_created_at).total_seconds() / 60 if primary.pair_created_at else None,
        chg_m5=_f(m5.price_change_pct), chg_h1=_f(h1.price_change_pct),
        chg_h6=_f(h6.price_change_pct), chg_h24=_f(h24.price_change_pct),
        volume_m5=_f(m5.volume_usd), volume_h1=_f(h1.volume_usd), volume_h24=_f(h24.volume_usd),
        volume_accel_m5_h1=_rate_ratio(_f(m5.volume_usd), _f(h1.volume_usd), 12),
        volume_accel_h1_h24=_rate_ratio(_f(h1.volume_usd), _f(h24.volume_usd), 24),
        txns_m5=txns(m5),
        txns_accel_m5_h1=_rate_ratio(_f(txns(m5)), _f(txns(h1)), 12),
        buy_ratio_m5=ratio(m5), buy_ratio_h1=ratio(h1),
        buyers_m5=m5.buyers, buyers_h1=h1.buyers,
        buyer_accel_m5_h1=_rate_ratio(_f(m5.buyers), _f(h1.buyers), 12),
        txns_per_trader_h1=(txns(h1) / traders_h1) if traders_h1 and txns(h1) is not None else None,
        volume_to_liquidity_h24=(float(h24.volume_usd) / float(primary.liquidity_usd))
        if h24.volume_usd is not None and primary.liquidity_usd else None,
        liquidity_change_pct=liq_change,
        liquidity_window_minutes=liq_window,
        ta_1m=technical_snapshot(c1) if c1 else None,
        ta_5m=technical_snapshot(resample(c1, 5)) if len(c1) >= 10 else None,
        ta_15m=technical_snapshot(resample(c1, 15)) if len(c1) >= 30 else None,
        security_passed=None if security is None else security.passed,
        security_warnings=tuple(security.warnings) if security else (),
        insider_risk=insider,
        boosted=boosted,
        sources=tuple(sorted({s.source for s in latest})),
    )
