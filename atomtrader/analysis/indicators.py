"""Technical indicators over OHLCV candles.

Indicators are analytics, not accounting, so they use floats (money stays Decimal). Every
function returns ``None`` (or ``None`` entries) until enough history exists — values are
never padded or extrapolated.

Conventions: Wilder smoothing for RSI/ATR; EMA seeded with the SMA of the first ``period``
values; VWAP anchored at the first candle passed in.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from atomtrader.marketdata.models import Candle


def closes(candles: Sequence[Candle]) -> list[float]:
    return [float(c.close) for c in candles]


def volumes(candles: Sequence[Candle]) -> list[float]:
    return [float(c.volume_usd) for c in candles]


# ----------------------------------------------------------------------------- averages


def ema(values: Sequence[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def last(series: Sequence[float | None]) -> float | None:
    return series[-1] if series else None


# ----------------------------------------------------------------------------- oscillators


def rsi(values: Sequence[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = [max(values[i] - values[i - 1], 0.0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0.0) for i in range(1, len(values))]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period

    def _rsi(g: float, l_: float) -> float:
        if l_ == 0:
            return 100.0 if g > 0 else 50.0
        return 100.0 - 100.0 / (1 + g / l_)

    out[period] = _rsi(avg_g, avg_l)
    for i in range(period + 1, len(values)):
        avg_g = (avg_g * (period - 1) + gains[i - 1]) / period
        avg_l = (avg_l * (period - 1) + losses[i - 1]) / period
        out[i] = _rsi(avg_g, avg_l)
    return out


@dataclass(frozen=True, slots=True)
class Macd:
    macd: list[float | None]
    signal: list[float | None]
    histogram: list[float | None]


def macd(values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Macd:
    ef, es = ema(values, fast), ema(values, slow)
    line: list[float | None] = [f - s if f is not None and s is not None else None for f, s in zip(ef, es)]
    start = next((i for i, v in enumerate(line) if v is not None), None)
    sig: list[float | None] = [None] * len(values)
    if start is not None:
        sub = ema([v for v in line[start:] if v is not None], signal)
        sig[start:] = sub
    hist = [m - s if m is not None and s is not None else None for m, s in zip(line, sig)]
    return Macd(line, sig, hist)


def atr(candles: Sequence[Candle], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(candles)
    if len(candles) <= period:
        return out
    trs = []
    for i in range(1, len(candles)):
        h, lo, pc = float(candles[i].high), float(candles[i].low), float(candles[i - 1].close)
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    val = sum(trs[:period]) / period
    out[period] = val
    for i in range(period + 1, len(candles)):
        val = (val * (period - 1) + trs[i - 1]) / period
        out[i] = val
    return out


def vwap(candles: Sequence[Candle]) -> list[float | None]:
    """Anchored VWAP using typical price (H+L+C)/3 weighted by USD volume."""
    out: list[float | None] = []
    pv = vol = 0.0
    for c in candles:
        v = float(c.volume_usd)
        tp = (float(c.high) + float(c.low) + float(c.close)) / 3
        # USD volume -> token volume weight: v / tp. Weighting tp by token volume:
        units = v / tp if tp > 0 else 0.0
        pv += tp * units
        vol += units
        out.append(pv / vol if vol > 0 else None)
    return out


# ----------------------------------------------------------------------------- volume & momentum


def relative_volume(vols: Sequence[float], lookback: int = 20) -> float | None:
    """Last bar's volume vs the mean of the previous ``lookback`` bars."""
    if len(vols) < lookback + 1:
        return None
    base = sum(vols[-lookback - 1 : -1]) / lookback
    return vols[-1] / base if base > 0 else None


def window_ratio(vals: Sequence[float], window: int) -> float | None:
    """Sum of the last ``window`` values vs the ``window`` before (acceleration > 1)."""
    if len(vals) < 2 * window:
        return None
    prev = sum(vals[-2 * window : -window])
    return sum(vals[-window:]) / prev if prev > 0 else None


def roc(vals: Sequence[float], n: int) -> float | None:
    if len(vals) <= n or vals[-n - 1] == 0:
        return None
    return vals[-1] / vals[-n - 1] - 1


def price_acceleration(vals: Sequence[float], n: int) -> float | None:
    """Change in rate of change: ROC over the last n bars minus ROC over the n before."""
    if len(vals) <= 2 * n or vals[-2 * n - 1] == 0 or vals[-n - 1] == 0:
        return None
    return (vals[-1] / vals[-n - 1] - 1) - (vals[-n - 1] / vals[-2 * n - 1] - 1)


# ----------------------------------------------------------------------------- structure


@dataclass(frozen=True, slots=True)
class Swing:
    index: int
    price: float
    kind: str  # "high" | "low"


def swing_points(candles: Sequence[Candle], left: int = 2, right: int = 2) -> list[Swing]:
    """Plateau-tolerant fractal swings.

    A swing high is at least as high as every bar within ``left``/``right`` and strictly
    higher than at least one of them. Ties are normal in contiguous candles (a bar that
    closes at its high is followed by one opening there), so equal-priced swings of the same
    kind within the window are collapsed to the first. Mirror rule for lows."""
    raw: list[Swing] = []
    for i in range(left, len(candles) - right):
        h = float(candles[i].high)
        lo = float(candles[i].low)
        neigh = [j for j in range(i - left, i + right + 1) if j != i]
        nh = [float(candles[j].high) for j in neigh]
        nl = [float(candles[j].low) for j in neigh]
        if all(h >= x for x in nh) and any(h > x for x in nh):
            raw.append(Swing(i, h, "high"))
        if all(lo <= x for x in nl) and any(lo < x for x in nl):
            raw.append(Swing(i, lo, "low"))
    out: list[Swing] = []
    for s in raw:
        dup = next((p for p in reversed(out) if p.kind == s.kind), None)
        if dup and dup.price == s.price and s.index - dup.index <= left + right:
            continue
        out.append(s)
    return out


@dataclass(frozen=True, slots=True)
class Levels:
    resistance: float | None  # nearest swing high above price
    support: float | None  # nearest swing low below price


def support_resistance(candles: Sequence[Candle], left: int = 2, right: int = 2) -> Levels:
    if not candles:
        return Levels(None, None)
    price = float(candles[-1].close)
    swings = swing_points(candles, left, right)
    above = [s.price for s in swings if s.kind == "high" and s.price > price]
    below = [s.price for s in swings if s.kind == "low" and s.price < price]
    return Levels(min(above) if above else None, max(below) if below else None)


def market_structure(candles: Sequence[Candle], left: int = 2, right: int = 2) -> str:
    """'uptrend' (higher highs and higher lows), 'downtrend', 'range' or 'unknown'."""
    swings = swing_points(candles, left, right)
    highs = [s.price for s in swings if s.kind == "high"][-2:]
    lows = [s.price for s in swings if s.kind == "low"][-2:]
    if len(highs) < 2 or len(lows) < 2:
        return "unknown"
    if highs[1] > highs[0] and lows[1] > lows[0]:
        return "uptrend"
    if highs[1] < highs[0] and lows[1] < lows[0]:
        return "downtrend"
    return "range"


def resample(candles: Sequence[Candle], minutes: int) -> list[Candle]:
    """Aggregate 1-minute candles into ``minutes`` bars aligned to the epoch. Incomplete
    buckets are kept only if every constituent minute is present."""
    buckets: dict[int, list[Candle]] = {}
    span = minutes * 60
    for c in candles:
        key = int(c.ts.timestamp()) // span * span
        buckets.setdefault(key, []).append(c)
    out = []
    for key in sorted(buckets):
        group = sorted(buckets[key], key=lambda c: c.ts)
        if len(group) != minutes:
            continue
        out.append(Candle(
            ts=group[0].ts - timedelta(seconds=int(group[0].ts.timestamp()) - key),
            open=group[0].open,
            high=max(c.high for c in group),
            low=min(c.low for c in group),
            close=group[-1].close,
            volume_usd=sum((c.volume_usd for c in group), Decimal(0)),
        ))
    return out


# ----------------------------------------------------------------------------- snapshot


@dataclass(frozen=True, slots=True)
class TechnicalSnapshot:
    close: float
    ema9: float | None
    ema21: float | None
    ema50: float | None
    vwap: float | None
    rsi14: float | None
    macd_hist: float | None
    macd_hist_prev: float | None
    atr14: float | None
    rel_volume: float | None
    volume_accel_5: float | None
    price_roc_5: float | None
    price_accel_5: float | None
    resistance: float | None
    support: float | None
    structure: str
    bars: int

    @property
    def atr_pct(self) -> float | None:
        return self.atr14 / self.close if self.atr14 and self.close else None

    @property
    def ema_stack_bullish(self) -> bool:
        return None not in (self.ema9, self.ema21, self.ema50) and self.ema9 > self.ema21 > self.ema50  # type: ignore[operator]


def technical_snapshot(candles: Sequence[Candle]) -> TechnicalSnapshot | None:
    if not candles:
        return None
    cl, vo = closes(candles), volumes(candles)
    m = macd(cl)
    lv = support_resistance(candles)
    return TechnicalSnapshot(
        close=cl[-1],
        ema9=last(ema(cl, 9)),
        ema21=last(ema(cl, 21)),
        ema50=last(ema(cl, 50)),
        vwap=last(vwap(candles)),
        rsi14=last(rsi(cl, 14)),
        macd_hist=last(m.histogram),
        macd_hist_prev=m.histogram[-2] if len(m.histogram) > 1 else None,
        atr14=last(atr(candles, 14)),
        rel_volume=relative_volume(vo, 20),
        volume_accel_5=window_ratio(vo, 5),
        price_roc_5=roc(cl, 5),
        price_accel_5=price_acceleration(cl, 5),
        resistance=lv.resistance,
        support=lv.support,
        structure=market_structure(candles),
        bars=len(candles),
    )
