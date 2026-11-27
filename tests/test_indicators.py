from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from atomtrader.analysis import indicators as ta
from atomtrader.marketdata.models import Candle

T0 = datetime(2026, 11, 27, 10, 0, tzinfo=UTC)


def candles(closes: list[float], spread: float = 0.0, vol: float = 100.0) -> list[Candle]:
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        hi, lo = max(o, c) + spread, min(o, c) - spread
        out.append(Candle(T0 + timedelta(minutes=i), Decimal(str(o)), Decimal(str(hi)), Decimal(str(lo)),
                          Decimal(str(c)), Decimal(str(vol))))
        prev = c
    return out


def test_ema_seed_and_recursion() -> None:
    e = ta.ema([1, 2, 3, 4, 5], 3)
    assert e[:2] == [None, None] and e[2] == 2.0
    assert e[3] == pytest.approx(3.0) and e[4] == pytest.approx(4.0)  # k=0.5 on a line stays on it
    assert ta.ema([1, 2], 3) == [None, None]


def test_rsi_extremes_and_balance() -> None:
    assert ta.rsi(list(range(1, 20)), 14)[-1] == 100.0
    assert ta.rsi(list(range(20, 1, -1)), 14)[-1] == pytest.approx(0.0)
    alt = [10, 11] * 15
    assert 40 < ta.rsi(alt, 14)[-1] < 60
    assert ta.rsi([1.0] * 20, 14)[-1] == 50.0  # flat: neither gains nor losses


def test_macd_is_ema_difference() -> None:
    vals = [float(i) for i in range(1, 60)]
    m = ta.macd(vals)
    f, s = ta.ema(vals, 12), ta.ema(vals, 26)
    assert m.macd[-1] == pytest.approx(f[-1] - s[-1])
    assert m.histogram[-1] == pytest.approx(m.macd[-1] - m.signal[-1])
    assert m.signal[24] is None and m.signal[-1] is not None


def test_atr_constant_range() -> None:
    cs = candles([10.0] * 30, spread=0.5)
    assert ta.atr(cs, 14)[-1] == pytest.approx(1.0)
    assert ta.atr(cs[:10], 14)[-1] is None


def test_vwap_weights_by_volume() -> None:
    a = Candle(T0, Decimal(10), Decimal(10), Decimal(10), Decimal(10), Decimal(1000))  # 100 units @10
    b = Candle(T0 + timedelta(minutes=1), Decimal(20), Decimal(20), Decimal(20), Decimal(20), Decimal(1000))  # 50 units @20
    v = ta.vwap([a, b])
    assert v[0] == 10 and v[1] == pytest.approx((10 * 100 + 20 * 50) / 150)


def test_volume_and_price_acceleration() -> None:
    assert ta.relative_volume([1.0] * 20 + [5.0], 20) == 5.0
    assert ta.relative_volume([1.0] * 5, 20) is None
    assert ta.window_ratio([1, 1, 1, 2, 2, 2], 3) == 2.0
    assert ta.roc([100.0, 110.0], 1) == pytest.approx(0.1)
    # accelerating: +1% then +5%
    assert ta.price_acceleration([100.0, 101.0, 106.05], 1) == pytest.approx(0.04)


def test_swings_levels_and_structure() -> None:
    # zig-zag with rising highs and lows
    path = [10, 12, 14, 12, 11, 13, 16, 14, 13, 15, 18, 16, 15, 16]
    cs = candles([float(x) for x in path])
    swings = ta.swing_points(cs)
    assert [s.kind for s in swings].count("high") >= 2
    assert ta.market_structure(cs) == "uptrend"
    down = candles([float(x) for x in reversed(path)])
    assert ta.market_structure(down) == "downtrend"
    lv = ta.support_resistance(cs)
    assert lv.support is not None and lv.support < float(cs[-1].close)


def test_resample_only_complete_buckets() -> None:
    cs = candles([float(i) for i in range(1, 13)])  # 12 one-minute bars from 10:00
    five = ta.resample(cs, 5)
    assert len(five) == 2  # 10:00-10:04, 10:05-10:09; 10:10-10:11 incomplete
    assert five[0].open == cs[0].open and five[0].close == cs[4].close
    assert five[0].high == max(c.high for c in cs[:5])
    assert five[0].volume_usd == Decimal(500)


def test_snapshot_never_invents_values_on_short_history() -> None:
    snap = ta.technical_snapshot(candles([1.0, 1.1, 1.2]))
    assert snap is not None and snap.ema9 is None and snap.rsi14 is None and snap.atr14 is None
    assert snap.structure == "unknown" and not snap.ema_stack_bullish
    full = ta.technical_snapshot(candles([1 + i * 0.01 for i in range(80)], spread=0.002))
    assert full.ema_stack_bullish and full.rsi14 == 100.0 and full.atr_pct is not None
