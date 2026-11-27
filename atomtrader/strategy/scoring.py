"""Composite Opportunity Score (0–100).

Twelve components, each normalised to 0..1 (``None`` when its data is missing):
narrative strength, narrative timing, rotation evidence, market momentum, volume
acceleration, buyer growth, liquidity quality, on-chain activity, token security, insider
risk, execution quality (round-trip cost) and expected net reward-to-risk.

Weights are configurable (and are what the backtester / walk-forward optimiser tunes —
never the risk limits). Missing components are dropped and the remaining weights
rescaled; ``confidence`` is the share of weight that had data. A security hard-fail forces 0.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from atomtrader.strategy.context import MarketContext

TIMING = {"emerging": 1.0, "growing": 0.8, "steady": 0.5, "saturated": 0.3, "fading": 0.1}


@dataclass(frozen=True)
class OpportunityWeights:
    narrative_strength: float = 12
    narrative_timing: float = 8
    rotation_evidence: float = 6
    market_momentum: float = 10
    volume_acceleration: float = 10
    buyer_growth: float = 10
    liquidity_quality: float = 8
    onchain_activity: float = 5
    token_security: float = 8
    insider_risk: float = 8
    execution_quality: float = 7
    reward_to_risk: float = 8

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class OpportunityScore:
    score: float
    confidence: float
    components: dict[str, float]
    missing: tuple[str, ...]


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def _ramp(x: float | None, lo: float, hi: float) -> float | None:
    return None if x is None else _clip((x - lo) / (hi - lo))


def momentum_component(ctx: MarketContext) -> float | None:
    t5, t1 = ctx.ta5, ctx.ta1
    parts: list[float] = []
    for t in (t5, t1):
        if t is None:
            continue
        if t.ema9 is not None and t.ema21 is not None:
            parts.append(1.0 if t.ema9 > t.ema21 else 0.0)
        if t.structure in ("uptrend", "downtrend", "range"):
            parts.append({"uptrend": 1.0, "range": 0.5, "downtrend": 0.0}[t.structure])
        if t.vwap is not None:
            parts.append(1.0 if t.close > t.vwap else 0.0)
    f = ctx.features
    if f.chg_h1 is not None:
        parts.append(_clip(0.5 + f.chg_h1 / 60))
    return sum(parts) / len(parts) if parts else None


def opportunity_score(ctx: MarketContext, reward_to_risk: float | None = None,
                      weights: OpportunityWeights | None = None) -> OpportunityScore:
    w = (weights or OpportunityWeights()).as_dict()
    f = ctx.features
    if ctx.security is not None and not ctx.security.passed:
        return OpportunityScore(0.0, 1.0, {"token_security": 0.0}, ())

    c: dict[str, float | None] = {
        "narrative_strength": ctx.narrative_strength,
        "narrative_timing": TIMING.get(ctx.narrative.status) if ctx.narrative else None,
        "rotation_evidence": ctx.rotation.score if ctx.rotation else None,
        "market_momentum": momentum_component(ctx),
        "volume_acceleration": _ramp(f.volume_accel_m5_h1, 1.0, 4.0),
        "buyer_growth": _ramp(f.buyer_accel_m5_h1, 1.0, 4.0) if f.buyer_accel_m5_h1 is not None
        else (None if f.txns_accel_m5_h1 is None else 0.7 * _ramp(f.txns_accel_m5_h1, 1.0, 4.0)),  # type: ignore[operator]
        "liquidity_quality": None if f.liquidity_usd is None else 0.7 * _ramp(f.liquidity_usd, 25_000, 1_000_000)
        + 0.3 * (_ramp(f.liquidity_change_pct, -20, 30) if f.liquidity_change_pct is not None else 0.5),  # type: ignore[operator]
        "onchain_activity": _ramp(f.txns_m5, 10, 150),
        "token_security": None if ctx.security is None else _clip(1 - 0.15 * len(ctx.security.warnings)),
        "insider_risk": None if ctx.insider is None else _clip(1 - ctx.insider.score / 100),
        "execution_quality": _ramp(None if ctx.round_trip_cost_bps is None else -ctx.round_trip_cost_bps, -300, -20),
        "reward_to_risk": _ramp(reward_to_risk, 1.0, 4.0),
    }
    have = {k: v for k, v in c.items() if v is not None}
    total = sum(w.values())
    have_w = sum(w[k] for k in have)
    score = 100 * sum(have[k] * w[k] for k in have) / have_w if have_w else 0.0
    return OpportunityScore(round(score, 1), round(have_w / total, 2), {k: round(v, 3) for k, v in have.items()},
                            tuple(k for k, v in c.items() if v is None))
