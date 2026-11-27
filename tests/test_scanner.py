from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from atomtrader.core.models import Chain, TokenRef
from atomtrader.marketdata.dexscreener import parse_pair
from atomtrader.marketdata.geckoterminal import parse_pool
from atomtrader.marketdata.http import parse_json
from atomtrader.marketdata.models import PairSnapshot, WindowStats
from atomtrader.scanner.features import build_features, merge_windows
from atomtrader.scanner.moonshot import moonshot_score
from atomtrader.security.insider import InsiderRisk

NOW = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)
TOK = TokenRef(Chain.SOLANA, "Mint111", "TEST")
FIX = Path(__file__).parent / "fixtures"


def snap(windows: dict[str, WindowStats], liq="300000", source="geckoterminal", at=NOW, price="0.001") -> PairSnapshot:
    return PairSnapshot(Chain.SOLANA, "raydium", "Pool111", TOK, "SOL", "So1", Decimal(price),
                        Decimal(liq) if liq else None, Decimal(3_000_000), Decimal(3_000_000),
                        NOW - timedelta(hours=5), at, source, windows)


def W(buys, sells, vol, chg, buyers=None, sellers=None) -> WindowStats:
    return WindowStats(buys, sells, buyers, sellers, Decimal(vol), Decimal(chg))


LOW_RISK = InsiderRisk(10, "low", "normal", {"concentration": 5.0, "suspicious": 0, "selling": 0, "liquidity": 0}, ())


def surging() -> dict[str, WindowStats]:
    # 5-minute pace far above the hour, hour far above the day; mostly buys; many unique buyers.
    return {"m5": W(120, 30, 30_000, 6, buyers=80, sellers=20), "h1": W(400, 200, 90_000, 18, buyers=200, sellers=90),
            "h24": W(2000, 1500, 400_000, 25, buyers=900, sellers=600)}


def pump_only() -> dict[str, WindowStats]:
    # Price ripped but participation is flat (same pace as the hour/day) and few buyers.
    return {"m5": W(10, 9, 2_000, 15, buyers=4, sellers=4), "h1": W(120, 110, 24_000, 80, buyers=40, sellers=40),
            "h24": W(2900, 2800, 580_000, 300, buyers=900, sellers=900)}


def test_participation_surge_scores_high_with_full_confidence() -> None:
    history = [snap(surging(), liq="220000", at=NOW - timedelta(minutes=45))]
    f = build_features([snap(surging())], history, insider=LOW_RISK)
    assert f.volume_accel_m5_h1 == 4.0 and f.buyer_accel_m5_h1 == 4.8
    assert round(f.liquidity_change_pct, 1) == 36.4
    s = moonshot_score(f, narrative_strength=0.7)
    assert s.score > 70 and s.confidence == 1.0 and not s.penalties


def test_price_increase_alone_cannot_score_high() -> None:
    f = build_features([snap(pump_only())], insider=LOW_RISK)
    s = moonshot_score(f, narrative_strength=0.0)
    assert f.chg_h24 == 300 and s.components["price_momentum"] == 1.0
    assert s.score < 40, s
    assert "big_24h_move_fading" not in s.flags  # h1 still positive: not fading


def test_late_extended_fading_move_is_penalised() -> None:
    w = pump_only()
    w["h1"] = W(120, 110, 24_000, -12, buyers=40, sellers=40)
    s = moonshot_score(build_features([snap(w)], insider=LOW_RISK))
    assert "late_extended_move" in s.penalties and "big_24h_move_fading" in s.flags


def test_missing_unique_buyers_lowers_confidence_and_is_flagged() -> None:
    ws = {k: WindowStats(v.buys, v.sells, None, None, v.volume_usd, v.price_change_pct) for k, v in surging().items()}
    s = moonshot_score(build_features([snap(ws, source="dexscreener")]))
    assert "participation_from_txn_counts_not_unique_wallets" in s.flags
    assert s.confidence < 1.0  # no holder data, no narrative


def test_security_failure_zeroes_score() -> None:
    from atomtrader.security.models import SecurityReport

    rep = SecurityReport(TOK, NOW, sources={"rugcheck": "ok"}, hard_fail=["no_sell_route"])
    s = moonshot_score(build_features([snap(surging())], security=rep), narrative_strength=1.0)
    assert s.score == 0 and s.penalties == {"security_hard_fail": 100.0}


def test_wash_trading_and_insider_penalties() -> None:
    w = surging()
    w["h1"] = W(4000, 3900, 90_000, 18, buyers=200, sellers=190)  # ~20 trades per wallet
    w["h24"] = W(20000, 19000, 12_000_000, 25, buyers=900, sellers=600)  # 40x liquidity
    risky = InsiderRisk(75, "concentrated_suspicious", "reduce_size", {"concentration": 30.0}, ())
    s = moonshot_score(build_features([snap(w)], insider=risky))
    assert s.penalties["wash_trading_risk"] > 10 and s.penalties["insider_risk"] == 27.0
    assert {"few_wallets_many_trades", "volume_far_exceeds_liquidity"} <= set(s.flags)


def test_merge_prefers_dexscreener_counts_and_gecko_unique_buyers() -> None:
    ds = snap({"m5": W(50, 10, 1000, 1)}, source="dexscreener")
    gt = snap({"m5": W(45, 12, 900, 1, buyers=30, sellers=8)}, source="geckoterminal")
    m = merge_windows([ds, gt])["m5"]
    assert (m.buys, m.sells, m.buyers, m.sellers) == (50, 10, 30, 8)


def test_features_from_real_recorded_snapshots() -> None:
    ds = [p for p in (parse_pair(r, NOW) for r in parse_json((FIX / "dexscreener_tokens_v1_solana.json").read_text())) if p]
    bonk = next(p for p in ds if p.base.symbol == "Bonk")
    f = build_features([bonk])
    assert f.liquidity_usd and f.age_minutes and f.age_minutes > 60 * 24 * 365
    assert f.buyers_m5 is None and f.txns_m5 is not None
    gt = [p for p in (parse_pool(i, Chain.SOLANA, NOW) for i in parse_json((FIX / "geckoterminal_new_pools.json").read_text())["data"]) if p]
    g = build_features([gt[0]])
    assert g.buyers_m5 is not None
    assert 0 <= moonshot_score(g).score <= 100


async def test_scanner_service_measures_distribution_between_checks(tmp_path) -> None:
    from atomtrader.persistence.marketdata_store import MarketDataStore
    from atomtrader.persistence.repository import Repository
    from atomtrader.persistence.security_store import SecurityStore
    from atomtrader.scanner.service import ScannerService
    from atomtrader.security.models import Holder, SecurityReport

    repo = Repository(f"sqlite+aiosqlite:///{tmp_path / 'scan.db'}")
    await repo.init()
    store = MarketDataStore(repo.sessionmaker)
    for m in (40, 20, 0):
        await store.save_snapshots([snap(surging(), source="dexscreener", at=NOW - timedelta(minutes=m))])

    class Assessor:
        """Whale holds 30% at the first check and 22% at the second."""
        def __init__(self):
            self.n = 0

        async def assess(self, token, pool_addresses=(), max_age_seconds=None):
            self.n += 1
            whale = "30" if self.n == 1 else "22"
            r = SecurityReport(token, clock.now, sources={"rugcheck": "ok"})
            r.holders = [Holder("whale", Decimal(whale))] + [Holder(f"h{i}", Decimal(1)) for i in range(9)]
            return r

    class Clock:
        now = NOW

        def __call__(self):
            return self.now

    clock = Clock()
    svc = ScannerService(store, SecurityStore(repo.sessionmaker), Assessor(), clock=clock)
    first = await svc.evaluate(TOK)
    assert first.insider.situation != "large_holder_selling" and "holder_history" in first.insider.data_gaps
    clock.now = NOW + timedelta(minutes=10)
    second = await svc.evaluate(TOK)
    assert second.insider.situation == "large_holder_selling" and second.insider.response == "exit"
    assert second.moonshot.penalties["insider_risk"] > 0
    await repo.close()
