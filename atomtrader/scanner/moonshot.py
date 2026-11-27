"""Moonshot Opportunity Score (0–100): a structured assessment of *emerging* upside potential.

It is not a probability of profit. Design rules:
  * Participation (volume/buyer acceleration, buy pressure, liquidity growth) carries most of
    the weight; raw price change is capped at 8/100 so a pump alone cannot score high.
  * Components with missing data are dropped and the remaining weights rescaled; the share of
    weight that had data is reported as ``confidence``.
  * Penalties: insider exit risk, security warnings, wash-trading patterns, falling liquidity,
    sell pressure, and late/overextended moves (big 24h gain with fading 1h momentum).
  * A failed security check forces the score to 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from atomtrader.scanner.features import TokenFeatures


@dataclass(frozen=True)
class ScoreWeights:
    volume_acceleration: float = 20
    buyer_participation: float = 20
    buy_pressure: float = 12
    liquidity: float = 15
    holder_distribution: float = 10
    narrative: float = 15
    price_momentum: float = 8  # deliberately small


@dataclass(frozen=True)
class MoonshotScore:
    score: float
    confidence: float  # 0..1 share of weight backed by data
    components: dict[str, float]  # each 0..1
    penalties: dict[str, float]  # points subtracted
    flags: tuple[str, ...] = field(default_factory=tuple)


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _ramp(x: float, x0: float, x1: float) -> float:
    return _clip((x - x0) / (x1 - x0))


def moonshot_score(f: TokenFeatures, narrative_strength: float | None = None,
                   weights: ScoreWeights | None = None) -> MoonshotScore:
    w = weights or ScoreWeights()
    comp: dict[str, float | None] = {}
    flags: list[str] = []

    # Volume acceleration: current 5m pace vs the hour, and the hour vs the day.
    va = [x for x in (f.volume_accel_m5_h1, f.volume_accel_h1_h24) if x is not None]
    comp["volume_acceleration"] = (_ramp(f.volume_accel_m5_h1 or 0, 1.0, 4.0) * 0.6
                                   + _ramp(f.volume_accel_h1_h24 or 0, 1.0, 4.0) * 0.4) if va else None

    # Buyer participation: unique buyers if available, otherwise txn acceleration (weaker).
    if f.buyer_accel_m5_h1 is not None:
        comp["buyer_participation"] = _ramp(f.buyer_accel_m5_h1, 1.0, 4.0) * 0.7 + _ramp(f.buyers_m5 or 0, 5, 60) * 0.3
    elif f.txns_accel_m5_h1 is not None:
        comp["buyer_participation"] = _ramp(f.txns_accel_m5_h1, 1.0, 4.0) * 0.6
        flags.append("participation_from_txn_counts_not_unique_wallets")
    else:
        comp["buyer_participation"] = None

    br = [x for x in (f.buy_ratio_m5, f.buy_ratio_h1) if x is not None]
    comp["buy_pressure"] = _ramp(sum(br) / len(br), 0.5, 0.7) if br else None

    if f.liquidity_usd is None:
        comp["liquidity"] = None
    else:
        depth = _ramp(f.liquidity_usd, 25_000, 500_000)
        growth = _ramp(f.liquidity_change_pct, 0, 50) if f.liquidity_change_pct is not None else 0.5
        comp["liquidity"] = depth * 0.6 + growth * 0.4

    ir = f.insider_risk
    comp["holder_distribution"] = None if ir is None or "holders" in ir.data_gaps else _clip(1 - ir.components["concentration"] / 35)
    comp["narrative"] = None if narrative_strength is None else _clip(narrative_strength)
    pm = [x for x in (f.chg_m5, f.chg_h1) if x is not None]
    comp["price_momentum"] = (_ramp(f.chg_m5 or 0, 0, 10) * 0.5 + _ramp(f.chg_h1 or 0, 0, 40) * 0.5) if pm else None

    wmap = {k: getattr(w, k) for k in comp}
    have = {k: v for k, v in comp.items() if v is not None}
    total_w = sum(wmap.values())
    have_w = sum(wmap[k] for k in have)
    base = 100 * sum(have[k] * wmap[k] for k in have) / have_w if have_w else 0.0
    confidence = have_w / total_w if total_w else 0.0

    pen: dict[str, float] = {}
    if f.security_passed is False:
        return MoonshotScore(0.0, confidence, {k: v for k, v in have.items()}, {"security_hard_fail": 100.0},
                             ("security_hard_fail",))
    if ir is not None and ir.score >= 30:
        pen["insider_risk"] = min(30.0, (ir.score - 30) * 0.6)
    warn_pts = sum(4 for x in f.security_warnings if "mint_authority" in x or "goplus:" in x)
    if warn_pts:
        pen["security_warnings"] = min(12.0, warn_pts)
    if f.liquidity_change_pct is not None and f.liquidity_change_pct < -10:
        pen["liquidity_declining"] = min(25.0, -f.liquidity_change_pct * 0.5)
    if f.buy_ratio_m5 is not None and f.buy_ratio_m5 < 0.4:
        pen["sell_pressure"] = (0.4 - f.buy_ratio_m5) * 50
    wash = 0.0
    if f.txns_per_trader_h1 is not None and f.txns_per_trader_h1 > 6:
        wash += min(15.0, (f.txns_per_trader_h1 - 6) * 2)
        flags.append("few_wallets_many_trades")
    if f.volume_to_liquidity_h24 is not None and f.volume_to_liquidity_h24 > 25:
        wash += min(10.0, (f.volume_to_liquidity_h24 - 25) * 0.4)
        flags.append("volume_far_exceeds_liquidity")
    if wash:
        pen["wash_trading_risk"] = wash
    if f.chg_h24 is not None and f.chg_h24 > 200 and (f.chg_h1 is None or f.chg_h1 < 0):
        pen["late_extended_move"] = min(20.0, 8 + f.chg_h24 / 100)
        flags.append("big_24h_move_fading")
    if f.boosted:
        flags.append("paid_promotion_active")

    score = max(0.0, min(100.0, base - sum(pen.values())))
    return MoonshotScore(round(score, 1), round(confidence, 2), {k: round(v, 3) for k, v in have.items()},
                         {k: round(v, 1) for k, v in pen.items()}, tuple(flags))
