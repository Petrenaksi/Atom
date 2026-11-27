"""Market-data persistence: pair snapshots, closed candles, attention signals.

Recording our own history from day one is deliberate: dead/rugged tokens disappear from
provider APIs, so later backtests would otherwise suffer survivorship bias.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Integer, String, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from atomtrader.core.models import Chain
from atomtrader.marketdata.models import AttentionSignal, Candle, PairSnapshot
from atomtrader.persistence.repository import Base, DecimalString, UTCDateTime


def _windows_json(s: PairSnapshot) -> dict[str, Any]:
    return {
        w: {
            "buys": st.buys,
            "sells": st.sells,
            "buyers": st.buyers,
            "sellers": st.sellers,
            "volume_usd": None if st.volume_usd is None else str(st.volume_usd),
            "price_change_pct": None if st.price_change_pct is None else str(st.price_change_pct),
        }
        for w, st in s.windows.items()
    }


class PairSnapshotRow(Base):
    __tablename__ = "pair_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    source: Mapped[str] = mapped_column(String(32))
    chain: Mapped[str] = mapped_column(String(16))
    dex: Mapped[str] = mapped_column(String(64))
    pair_address: Mapped[str] = mapped_column(String(128), index=True)
    token_address: Mapped[str] = mapped_column(String(128), index=True)
    symbol: Mapped[str] = mapped_column(String(64))
    quote_symbol: Mapped[str] = mapped_column(String(64))
    price_usd: Mapped[Decimal] = mapped_column(DecimalString)
    liquidity_usd: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    reported_liquidity_usd: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    fdv_usd: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    market_cap_usd: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    pair_created_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    windows: Mapped[dict[str, Any]] = mapped_column(JSON)


class CandleRow(Base):
    __tablename__ = "candles"
    __table_args__ = (UniqueConstraint("chain", "pool_address", "timeframe", "ts", name="uq_candle"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain: Mapped[str] = mapped_column(String(16))
    pool_address: Mapped[str] = mapped_column(String(128), index=True)
    timeframe: Mapped[str] = mapped_column(String(8))
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    open: Mapped[Decimal] = mapped_column(DecimalString)
    high: Mapped[Decimal] = mapped_column(DecimalString)
    low: Mapped[Decimal] = mapped_column(DecimalString)
    close: Mapped[Decimal] = mapped_column(DecimalString)
    volume_usd: Mapped[Decimal] = mapped_column(DecimalString)
    source: Mapped[str] = mapped_column(String(32))


class AttentionRow(Base):
    __tablename__ = "attention_signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    chain_id: Mapped[str] = mapped_column(String(32))
    token_address: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    boost_amount: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    links: Mapped[list[str]] = mapped_column(JSON)


class MarketDataStore:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._session = sessionmaker

    async def save_snapshots(self, snaps: list[PairSnapshot]) -> int:
        if not snaps:
            return 0
        async with self._session() as s:
            s.add_all(
                PairSnapshotRow(
                    as_of=p.as_of,
                    source=p.source,
                    chain=p.chain.value,
                    dex=p.dex,
                    pair_address=p.pair_address,
                    token_address=p.base.address,
                    symbol=p.base.symbol[:64],
                    quote_symbol=p.quote_symbol[:64],
                    price_usd=p.price_usd,
                    liquidity_usd=p.liquidity_usd,
                    reported_liquidity_usd=p.reported_liquidity_usd,
                    fdv_usd=p.fdv_usd,
                    market_cap_usd=p.market_cap_usd,
                    pair_created_at=p.pair_created_at,
                    windows=_windows_json(p),
                )
                for p in snaps
            )
            await s.commit()
        return len(snaps)

    async def save_candles(
        self, chain: Chain, pool: str, timeframe: str, candles: list[Candle], source: str
    ) -> int:
        """Insert closed candles; existing (chain, pool, timeframe, ts) rows are kept as-is."""
        if not candles:
            return 0
        async with self._session() as s:
            existing = set(
                (
                    await s.execute(
                        select(CandleRow.ts).where(
                            CandleRow.chain == chain.value,
                            CandleRow.pool_address == pool,
                            CandleRow.timeframe == timeframe,
                            CandleRow.ts >= min(c.ts for c in candles),
                        )
                    )
                ).scalars()
            )
            new = [c for c in candles if c.ts not in existing]
            s.add_all(
                CandleRow(chain=chain.value, pool_address=pool, timeframe=timeframe, ts=c.ts, open=c.open,
                          high=c.high, low=c.low, close=c.close, volume_usd=c.volume_usd, source=source)
                for c in new
            )
            await s.commit()
        return len(new)

    async def load_candles(self, chain: Chain, pool: str, timeframe: str, since: datetime | None = None) -> list[Candle]:
        async with self._session() as s:
            q = select(CandleRow).where(
                CandleRow.chain == chain.value, CandleRow.pool_address == pool, CandleRow.timeframe == timeframe
            )
            if since is not None:
                q = q.where(CandleRow.ts >= since)
            rows = (await s.execute(q.order_by(CandleRow.ts))).scalars()
            return [Candle(r.ts, r.open, r.high, r.low, r.close, r.volume_usd) for r in rows]

    async def snapshots_for_token(self, token_address: str) -> list[PairSnapshotRow]:
        async with self._session() as s:
            q = select(PairSnapshotRow).where(PairSnapshotRow.token_address == token_address).order_by(PairSnapshotRow.as_of)
            return list((await s.execute(q)).scalars())

    async def save_attention(self, signals: list[AttentionSignal]) -> int:
        if not signals:
            return 0
        async with self._session() as s:
            s.add_all(
                AttentionRow(as_of=a.as_of, chain_id=a.chain_id, token_address=a.token_address, kind=a.kind,
                             boost_amount=a.boost_amount, links=list(a.links))
                for a in signals
            )
            await s.commit()
        return len(signals)

    async def count(self, table: type[Base]) -> int:
        from sqlalchemy import func

        async with self._session() as s:
            return int((await s.execute(select(func.count()).select_from(table))).scalar() or 0)


def row_to_snapshot(r: PairSnapshotRow) -> PairSnapshot:
    from atomtrader.core.models import TokenRef
    from atomtrader.marketdata.models import WindowStats

    def dec(v: Any) -> Decimal | None:
        return None if v is None else Decimal(v)

    windows = {
        w: WindowStats(buys=v.get("buys"), sells=v.get("sells"), buyers=v.get("buyers"), sellers=v.get("sellers"),
                       volume_usd=dec(v.get("volume_usd")), price_change_pct=dec(v.get("price_change_pct")))
        for w, v in (r.windows or {}).items()
    }
    chain = Chain(r.chain)
    return PairSnapshot(
        chain=chain, dex=r.dex, pair_address=r.pair_address, base=TokenRef(chain, r.token_address, r.symbol),
        quote_symbol=r.quote_symbol, quote_address="", price_usd=r.price_usd, liquidity_usd=r.liquidity_usd,
        fdv_usd=r.fdv_usd, market_cap_usd=r.market_cap_usd, pair_created_at=r.pair_created_at, as_of=r.as_of,
        source=r.source, windows=windows, reported_liquidity_usd=r.reported_liquidity_usd,
    )


async def recent_snapshots(store: MarketDataStore, token_address: str, since: datetime) -> list[PairSnapshot]:
    async with store._session() as s:
        q = (select(PairSnapshotRow).where(PairSnapshotRow.token_address == token_address, PairSnapshotRow.as_of >= since)
             .order_by(PairSnapshotRow.as_of))
        return [row_to_snapshot(r) for r in (await s.execute(q)).scalars()]


async def recently_boosted(store: MarketDataStore, token_address: str, since: datetime) -> bool:
    async with store._session() as s:
        q = select(AttentionRow.id).where(AttentionRow.token_address == token_address, AttentionRow.kind == "boost",
                                          AttentionRow.as_of >= since).limit(1)
        return (await s.execute(q)).first() is not None
