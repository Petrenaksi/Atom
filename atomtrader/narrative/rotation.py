"""Narrative Rotation Detector: second-wave opportunities within one narrative.

Pattern: a leader token ran hard (≥ +100% 24h) and is now cooling (1h momentum flat/negative
or 5-minute volume below its hourly pace); a related token that has NOT run yet starts to
accelerate (volume and buyers above their hourly pace, buy pressure > 50%).

Relative strength: the follower's acceleration must exceed the median acceleration of tokens
with NO link to the narrative by ``min_relative_strength`` — otherwise a market-wide surge
(everything accelerating at once) would look like a second wave. With fewer than three
unrelated tokens to compare against, the alert is flagged ``no_market_baseline``.

Tokens are related only through a shared narrative link from the Narrative Engine — never
because their names merely look alike. Name-only links are accepted but downgraded and
flagged. Wallet overlap would be the strongest evidence, but it needs indexed wallet-level
trade data that is not available keylessly; every alert is therefore labelled
``evidence_type = "statistical_correlation"`` and ``wallet_overlap = None``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from atomtrader.narrative.engine import Narrative
from atomtrader.scanner.features import TokenFeatures


@dataclass(frozen=True)
class RotationConfig:
    leader_min_gain_h24: float = 100.0
    leader_cooling_h1_max: float = 5.0
    follower_max_gain_ratio: float = 0.25  # follower 24h gain < 25% of the leader's
    follower_min_volume_accel: float = 2.0
    follower_min_participation_accel: float = 1.5
    follower_min_buy_ratio: float = 0.55
    follower_min_liquidity: float = 25_000
    crowded_followers: int = 3
    min_relative_strength: float = 1.5
    min_baseline_tokens: int = 3


@dataclass(frozen=True)
class RotationAlert:
    narrative: str
    leader: str
    follower: str
    score: float  # 0..1
    crowded: bool
    evidence_type: str = "statistical_correlation"
    wallet_overlap: float | None = None
    evidence: dict[str, float | str | None] = field(default_factory=dict)
    flags: tuple[str, ...] = ()


def _cooling(f: TokenFeatures, cfg: RotationConfig) -> bool:
    h1_flat = f.chg_h1 is not None and f.chg_h1 <= cfg.leader_cooling_h1_max
    vol_fading = f.volume_accel_m5_h1 is not None and f.volume_accel_m5_h1 < 1.0
    return h1_flat or vol_fading


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def detect_rotations(narratives: Sequence[Narrative], features: dict[str, TokenFeatures],
                     cfg: RotationConfig | None = None) -> list[RotationAlert]:
    cfg = cfg or RotationConfig()
    alerts: list[RotationAlert] = []
    for n in narratives:
        linked_keys = {lk.token.key for lk in n.tokens}
        others = [f for k, f in features.items() if k not in linked_keys]
        base_va = _median([f.volume_accel_m5_h1 for f in others if f.volume_accel_m5_h1 is not None])
        base_pa = _median([(f.buyer_accel_m5_h1 if f.buyer_accel_m5_h1 is not None else f.txns_accel_m5_h1)
                           for f in others
                           if (f.buyer_accel_m5_h1 if f.buyer_accel_m5_h1 is not None else f.txns_accel_m5_h1) is not None])
        have_baseline = len(others) >= cfg.min_baseline_tokens and base_va is not None
        linked = [(link, features[link.token.key]) for link in n.tokens if link.token.key in features]
        if len(linked) < 2:
            continue
        leaders = [(lk, f) for lk, f in linked if (f.chg_h24 or 0) >= cfg.leader_min_gain_h24]
        if not leaders:
            continue
        lead_link, leader = max(leaders, key=lambda x: (x[1].chg_h24 or 0, x[1].volume_h24 or 0))
        if not _cooling(leader, cfg):
            continue
        followers = []
        for lk, f in linked:
            if lk.token == lead_link.token:
                continue
            if (f.chg_h24 or 0) >= (leader.chg_h24 or 0) * cfg.follower_max_gain_ratio:
                continue  # already ran — not a second wave anymore
            if (f.liquidity_usd or 0) < cfg.follower_min_liquidity:
                continue
            va = f.volume_accel_m5_h1
            pa = f.buyer_accel_m5_h1 if f.buyer_accel_m5_h1 is not None else f.txns_accel_m5_h1
            br = f.buy_ratio_m5
            if va is None or pa is None or br is None:
                continue
            if va < cfg.follower_min_volume_accel or pa < cfg.follower_min_participation_accel or br < cfg.follower_min_buy_ratio:
                continue
            if have_baseline:
                if va < cfg.min_relative_strength * max(base_va, 1e-9):  # type: ignore[arg-type]
                    continue  # accelerating no more than the market: not narrative-specific
                if base_pa is not None and pa < cfg.min_relative_strength * max(base_pa, 1e-9):
                    continue
            followers.append((lk, f, va, pa, br))
        crowded = len(followers) >= cfg.crowded_followers
        for lk, f, va, pa, br in followers:
            score = (0.35 * min(1.0, (va - 1) / 4) + 0.35 * min(1.0, (pa - 1) / 4) + 0.15 * min(1.0, (br - 0.5) / 0.3)
                     + 0.15 * lk.strength) * n.strength ** 0.5
            if crowded:
                score *= 0.6
            flags = []
            if lk.grade == "name_only":
                flags.append("name_only_link")
                score *= 0.5
            if f.buyer_accel_m5_h1 is None:
                flags.append("participation_from_txn_counts")
            if not have_baseline:
                flags.append("no_market_baseline")
            alerts.append(RotationAlert(
                narrative=n.term, leader=lead_link.token.key, follower=f.token.key, score=round(min(1.0, score), 3),
                crowded=crowded,
                evidence={"leader_chg_h24": leader.chg_h24, "leader_chg_h1": leader.chg_h1,
                          "leader_volume_accel": leader.volume_accel_m5_h1, "follower_chg_h24": f.chg_h24,
                          "follower_volume_accel": round(va, 2), "follower_participation_accel": round(pa, 2),
                          "follower_buy_ratio": round(br, 2), "link_grade": lk.grade,
                          "narrative_status": n.status,
                          "market_baseline_volume_accel": round(base_va, 2) if have_baseline else None,
                          "market_baseline_participation_accel": round(base_pa, 2) if have_baseline and base_pa is not None else None,
                          "baseline_tokens": len(others)},
                flags=tuple(flags),
            ))
    # One alert per follower: the strongest narrative link wins.
    best: dict[str, RotationAlert] = {}
    for a in alerts:
        if a.follower not in best or a.score > best[a.follower].score:
            best[a.follower] = a
    return sorted(best.values(), key=lambda a: -a.score)
