"""Building backtest frames.

``frames_from_history`` reconstructs, for each snapshot time t, the MarketContext the bot
would have had at t: snapshots with ``as_of <= t`` and candles that had *closed* by t. Nothing
from the future leaks in (tested). Security/insider/narrative history is not reconstructed;
those components are missing and the scores reweight around them.

``synthetic_timeline`` generates labelled scenarios (pump-and-dump, trend with pullbacks,
chop, steady grind) for mechanics tests, slippage stress tests and demonstrations. Synthetic
results are never presented as market evidence.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from atomtrader.analysis.indicators import resample
from atomtrader.backtest.engine import Frame
from atomtrader.core.models import Chain, TokenRef
from atomtrader.marketdata.models import Candle, PairSnapshot, WindowStats
from atomtrader.scanner.features import build_features
from atomtrader.strategy.context import MarketContext


def frames_from_history(snapshots: Sequence[PairSnapshot], candles_1m: Sequence[Candle], every: timedelta = timedelta(minutes=1),
                        candle_lookback: timedelta = timedelta(hours=3), round_trip_cost_bps: float | None = None) -> list[Frame]:
    snaps = sorted(snapshots, key=lambda s: s.as_of)
    cs = sorted(candles_1m, key=lambda c: c.ts)
    frames: list[Frame] = []
    last_t: datetime | None = None
    for i, s in enumerate(snaps):
        t = s.as_of
        if last_t is not None and t - last_t < every:
            continue
        last_t = t
        hist = [x for x in snaps[: i + 1] if t - x.as_of <= timedelta(minutes=90)]
        latest = [x for x in hist if t - x.as_of <= timedelta(minutes=5)] or [s]
        closed = [c for c in cs if c.ts + timedelta(minutes=1) <= t and t - c.ts <= candle_lookback]
        feats = build_features(latest, hist, closed)
        frames.append(Frame(t, MarketContext(features=feats, candles_1m=tuple(closed),
                                             candles_5m=tuple(resample(closed, 5)),
                                             round_trip_cost_bps=round_trip_cost_bps)))
    return frames


# --------------------------------------------------------------------------- synthetic


def _path(kind: str, n: int, rng: random.Random) -> tuple[list[float], list[float], list[float]]:
    """Returns (prices, participation intensity, buy share) per minute. Intensity scales
    volume and trade counts, so moves come with (or without) the participation real
    markets show: runs attract rising volume and buyers, dumps bring heavy selling."""
    p, prices, inten, buy_share = 1.0, [], [], []
    for i in range(n):
        x = i / n
        if kind == "pump_and_dump":
            drift, k, b = (0.004, 1 + 4 * x / 0.6, 0.7) if x < 0.6 else (-0.012, 4.0, 0.2)
        elif kind == "trend_pullbacks":
            up = (i // 6) % 3 != 2
            drift, k, b = (0.006, 1.8, 0.65) if up else (-0.006, 0.8, 0.45)
        elif kind == "runner":
            if x < 0.3:
                drift, k, b = 0.002, 1.0, 0.55
            elif x < 0.85:
                drift, k, b = 0.012, 1.0 + 6 * (x - 0.3) / 0.55, 0.72
            else:
                drift, k, b = -0.01, 3.0, 0.25
        elif kind == "chop":
            drift, k, b = 0.004 * math.sin(i / 3), 1.0, 0.5
        else:  # grind: price up, participation flat — should NOT score well
            drift, k, b = 0.0015, 1.0, 0.52
        burst = kind in ("runner", "pump_and_dump", "trend_pullbacks") and drift > 0 and i % 15 < 3
        if burst:  # real runs come in bursts: price jumps with a surge of buyers
            drift, k, b = drift * 2.5, k * 4, min(0.9, b + 0.15)
        p *= 1 + drift + rng.gauss(0, 0.006)
        prices.append(p)
        inten.append(max(0.2, k * (1 + rng.gauss(0, 0.15))))
        buy_share.append(min(0.95, max(0.05, b + rng.gauss(0, 0.05))))
    return prices, inten, buy_share


def synthetic_timeline(kind: str, start: datetime, minutes: int = 240, seed: int = 0, symbol: str | None = None,
                       liquidity_usd: float = 400_000, round_trip_cost_bps: float = 60.0) -> list[Frame]:
    rng = random.Random(f"{kind}-{seed}")
    token = TokenRef(Chain.SOLANA, f"Synth{kind}{seed}", symbol or kind[:6].upper())
    prices, inten, bshare = _path(kind, minutes, rng)
    candles, prev, vols = [], prices[0], []
    for i, p in enumerate(prices):
        v = 1500 * inten[i] * (1 + 40 * abs(p / prev - 1))
        vols.append(v)
        hi, lo = max(prev, p) * (1 + abs(rng.gauss(0, 0.006))), min(prev, p) * (1 - abs(rng.gauss(0, 0.006)))
        candles.append(Candle(start + timedelta(minutes=i), Decimal(str(prev)), Decimal(str(hi)), Decimal(str(lo)),
                              Decimal(str(p)), Decimal(str(round(v, 2)))))
        prev = p

    def ws(lo: int, hi: int, chg: float) -> WindowStats:
        trades = [6 * inten[j] for j in range(lo, hi)]
        buys = sum(t * bshare[j] for t, j in zip(trades, range(lo, hi)))
        sells = sum(trades) - buys
        return WindowStats(int(buys) + 1, int(sells) + 1, int(buys * 0.6) + 1, int(sells * 0.6) + 1,
                           Decimal(str(round(sum(vols[lo:hi]), 2))), Decimal(str(round(chg, 2))))

    snaps = []
    for i in range(60, minutes):
        t = start + timedelta(minutes=i + 1)
        liq = liquidity_usd * (prices[i] / prices[0]) ** 0.5
        snaps.append(PairSnapshot(
            Chain.SOLANA, "synthetic", f"Pool{token.address}", token, "SOL", "So1", Decimal(str(prices[i])),
            Decimal(str(round(liq, 2))), None, None, start - timedelta(days=1), t, "synthetic",
            {"m5": ws(i - 4, i + 1, (prices[i] / prices[i - 5] - 1) * 100),
             "h1": ws(i - 59, i + 1, (prices[i] / prices[i - 60] - 1) * 100),
             "h24": ws(0, i + 1, (prices[i] / prices[0] - 1) * 100)}))
    return frames_from_history(snaps, candles, round_trip_cost_bps=round_trip_cost_bps)
