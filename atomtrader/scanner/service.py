"""Scanner service: turns a watched token into a fully evaluated candidate.

Loads the token's recent snapshots (both sources), history, stored 1-minute candles and
attention signals; runs the security assessment (with known pool addresses) and the insider
exit-risk analysis against stored holder history and the liquidity series; computes features
and the Moonshot score. Security results are persisted so later checks can *measure*
distribution.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from atomtrader.core.models import TokenRef
from atomtrader.marketdata.models import Candle, PairSnapshot
from atomtrader.persistence.marketdata_store import MarketDataStore, recent_snapshots, recently_boosted
from atomtrader.persistence.security_store import SecurityStore
from atomtrader.scanner.features import TokenFeatures, build_features
from atomtrader.scanner.moonshot import MoonshotScore, moonshot_score
from atomtrader.security.assessor import SecurityAssessor
from atomtrader.security.insider import InsiderRisk, insider_exit_risk
from atomtrader.security.models import SecurityReport


@dataclass(frozen=True)
class Candidate:
    token: TokenRef
    features: TokenFeatures
    moonshot: MoonshotScore
    security: SecurityReport | None
    insider: InsiderRisk | None
    candles_1m: tuple[Candle, ...]
    snapshots: tuple[PairSnapshot, ...]


class ScannerService:
    def __init__(
        self,
        store: MarketDataStore,
        security_store: SecurityStore | None = None,
        assessor: SecurityAssessor | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        history_window: timedelta = timedelta(minutes=90),
        candle_window: timedelta = timedelta(hours=3),
    ) -> None:
        self.store = store
        self.security_store = security_store
        self.assessor = assessor
        self.clock = clock
        self.history_window = history_window
        self.candle_window = candle_window

    async def evaluate(self, token: TokenRef, pool_address: str | None = None,
                       narrative_strength: float | None = None) -> Candidate | None:
        now = self.clock()
        history = await recent_snapshots(self.store, token.address, now - self.history_window)
        if not history:
            return None
        newest = history[-1].as_of
        latest_by_source: dict[str, PairSnapshot] = {}
        for s in history:
            if newest - s.as_of <= timedelta(minutes=10):
                latest_by_source[s.source] = s
        pools = {s.pair_address for s in history}
        pool = pool_address or max(latest_by_source.values(), key=lambda s: s.liquidity_usd or 0).pair_address
        candles = await self.store.load_candles(token.chain, pool, "1m", now - self.candle_window)

        report = insider = None
        if self.assessor is not None:
            report = await self.assessor.assess(token, pool_addresses=pools)
            prior = []
            if self.security_store is not None:
                holder_hist = await self.security_store.holder_history(token.key, now - timedelta(hours=6))
                # Baseline = snapshots strictly older than this report (a cached report was
                # already stored; a fresh one is stored now for future comparisons).
                prior = [h for h in holder_hist if h.at < report.checked_at]
                if not holder_hist or holder_hist[-1].at < report.checked_at:
                    await self.security_store.save(report)
            liq_series = [(s.as_of, s.liquidity_usd) for s in history if s.source == "dexscreener" and s.liquidity_usd]
            if not liq_series:
                liq_series = [(s.as_of, s.liquidity_usd) for s in history if s.liquidity_usd]
            insider = insider_exit_risk(report, prior, liq_series, now)

        boosted = await recently_boosted(self.store, token.address, now - timedelta(hours=6))
        features = build_features(list(latest_by_source.values()), history, candles, report, insider, boosted)
        return Candidate(token, features, moonshot_score(features, narrative_strength), report, insider,
                         tuple(candles), tuple(history))
