"""Adapter tests against *recorded real responses* (tests/fixtures, captured 2026-11-27)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from atomtrader.core.models import Chain, TokenRef
from atomtrader.core.money import FxTable, MissingMarketData
from atomtrader.marketdata.base_assets import BaseAssetRegistry
from atomtrader.marketdata.dexscreener import DexScreenerClient, DexScreenerQuoteSource, parse_pair
from atomtrader.marketdata.fx import FxService
from atomtrader.marketdata.geckoterminal import GeckoTerminalClient, closed_candles, parse_ohlcv, parse_pool
from atomtrader.marketdata.http import ApiError, ErrorKind, JsonHttpClient
from atomtrader.marketdata.ingestion import IngestionConfig, IngestionService
from atomtrader.persistence.marketdata_store import AttentionRow, CandleRow, MarketDataStore, PairSnapshotRow
from atomtrader.persistence.repository import Repository

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 27, 13, 15, tzinfo=UTC)
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
WIF = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"


REGISTRY = BaseAssetRegistry.load()  # resolved from providers, committed with source


def fixture(name: str) -> str:
    return (FIX / name).read_text()


async def _no_sleep(_: float) -> None:
    return None


def mock_client(base: str, routes: dict[str, tuple[int, str]]) -> JsonHttpClient:
    """Route by path prefix -> (status, body)."""

    def handler(req: httpx.Request) -> httpx.Response:
        for prefix, (status, body) in routes.items():
            if req.url.path.startswith(prefix):
                return httpx.Response(status, text=body)
        return httpx.Response(404, text='{"error":"no route"}')

    return JsonHttpClient(base, per_minute=10_000, max_retries=0, transport=httpx.MockTransport(handler), sleep=_no_sleep)


# ----------------------------------------------------------------- DexScreener parsing


def test_parse_dexscreener_pair_keeps_precision_and_marks_unique_wallets_unavailable() -> None:
    from atomtrader.marketdata.http import parse_json

    pairs = parse_json(fixture("dexscreener_tokens_v1_solana.json"))
    bonk = next(p for p in (parse_pair(r, NOW) for r in pairs) if p and p.base.address == BONK)
    assert bonk.chain is Chain.SOLANA and bonk.base.symbol == "Bonk"
    assert isinstance(bonk.price_usd, Decimal) and Decimal("0") < bonk.price_usd < Decimal("0.001")
    assert bonk.liquidity_usd is not None and bonk.liquidity_usd > 0
    m5 = bonk.window("m5")
    assert m5.buys is not None and m5.sells is not None
    assert m5.buyers is None and m5.sellers is None  # DexScreener has no unique-wallet data
    assert bonk.pair_created_at is not None and bonk.pair_created_at.year >= 2022


def test_parse_pair_without_liquidity_gives_none_not_zero() -> None:
    from atomtrader.marketdata.http import parse_json

    raw = parse_json(fixture("dexscreener_tokens_v1_solana.json"))[0]
    raw.pop("liquidity")
    assert parse_pair(raw, NOW).liquidity_usd is None
    raw["chainId"] = "tron"  # unsupported chain
    assert parse_pair(raw, NOW) is None


async def test_quote_source_uses_main_pool_and_refuses_unknown_tokens() -> None:
    dex = DexScreenerClient(
        mock_client("https://api.dexscreener.com", {
            f"/tokens/v1/solana/{BONK}": (200, fixture("dexscreener_tokens_v1_solana.json")),
            "/tokens/v1/solana/": (200, "[]"),
        }),
        clock=lambda: NOW,
    )
    qs = DexScreenerQuoteSource(dex, REGISTRY)
    q = await qs.get_quote(TokenRef(Chain.SOLANA, BONK, "Bonk"))
    assert q.source == "dexscreener" and q.liquidity_usd > 0 and q.price_usd > 0
    with pytest.raises(MissingMarketData):
        await qs.get_quote(TokenRef(Chain.SOLANA, "Unknown1111111111111111111111111111111111"))


async def test_attention_signals_parse_boost_amounts() -> None:
    dex = DexScreenerClient(
        mock_client("https://api.dexscreener.com", {
            "/token-boosts/latest/v1": (200, fixture("dexscreener_boosts_latest.json")),
            "/token-profiles/latest/v1": (200, fixture("dexscreener_profiles_latest.json")),
        }),
        clock=lambda: NOW,
    )
    boosts = await dex.latest_boosts()
    profiles = await dex.latest_profiles()
    assert boosts and all(b.kind == "boost" and b.boost_amount is not None for b in boosts)
    assert profiles and all(p.kind == "profile" and p.boost_amount is None for p in profiles)


# ----------------------------------------------------------------- GeckoTerminal parsing


def test_parse_ohlcv_oldest_first_and_drop_forming_bar() -> None:
    from atomtrader.marketdata.http import parse_json

    candles = parse_ohlcv(parse_json(fixture("geckoterminal_ohlcv_minute.json")))
    assert len(candles) == 30
    assert all(a.ts < b.ts for a, b in zip(candles, candles[1:]))
    assert all(c.low <= c.open <= c.high and c.low <= c.close <= c.high for c in candles)
    newest = candles[-1].ts
    kept = closed_candles(candles, "1m", newest + timedelta(seconds=30))
    assert kept[-1].ts < newest  # in-progress bar excluded
    assert len(closed_candles(candles, "1m", newest + timedelta(seconds=60))) == 30


def test_parse_new_pool_exposes_unique_buyers() -> None:
    from atomtrader.marketdata.http import parse_json

    raw = parse_json(fixture("geckoterminal_new_pools.json"))
    pools = [p for p in (parse_pool(i, Chain.SOLANA, NOW) for i in raw["data"]) if p]
    assert pools
    p = pools[0]
    assert p.source == "geckoterminal" and p.base.address and p.pair_address
    m5 = p.window("m5")
    assert m5.buyers is not None and m5.sellers is not None
    assert p.pair_created_at is not None and p.pair_created_at.tzinfo is not None


# ----------------------------------------------------------------- FX


async def test_fx_refresh_uses_intraday_rate_cross_checked_against_ecb() -> None:
    table = FxTable()
    svc = FxService(
        table,
        coingecko=mock_client("https://api.coingecko.com/api/v3", {"/api/v3/simple/price": (200, fixture("coingecko_simple_price.json"))}),
        frankfurter=mock_client("https://api.frankfurter.dev", {"/v2/providers/ecb/rates": (200, fixture("frankfurter_ecb_usd_eur.json"))}),
        clock=lambda: NOW,
    )
    rep = await svc.refresh()
    assert set(rep.updated) == {"USD", "SOL", "BNB", "ETH"} and not rep.errors
    usd = table.get("USD", NOW)
    assert usd.eur_per_unit == Decimal("0.912054") / Decimal("0.999698")
    assert rep.deviation_pct is not None and rep.deviation_pct < 1
    assert table.get("SOL", NOW).eur_per_unit == Decimal("66.61")


async def test_fx_refuses_rate_that_disagrees_with_ecb() -> None:
    table = FxTable()
    bad = json.dumps({"usd-coin": {"eur": 0.70, "usd": 1.0}})
    svc = FxService(
        table,
        coingecko=mock_client("https://api.coingecko.com/api/v3", {"/api/v3/simple/price": (200, bad)}),
        frankfurter=mock_client("https://api.frankfurter.dev", {"/v2/providers/ecb/rates": (200, fixture("frankfurter_ecb_usd_eur.json"))}),
        clock=lambda: NOW,
    )
    rep = await svc.refresh()
    assert "USD" not in rep.updated and rep.errors
    with pytest.raises(MissingMarketData):
        table.get("USD", NOW)


# ----------------------------------------------------------------- ingestion


@pytest.fixture
async def repo(tmp_path):
    r = Repository(f"sqlite+aiosqlite:///{tmp_path / 'md.db'}")
    await r.init()
    yield r
    await r.close()


async def test_ingestion_cycle_records_data_and_isolates_failures(repo) -> None:
    gt = GeckoTerminalClient(
        mock_client("https://api.geckoterminal.com/api/v2", {
            "/api/v2/networks/solana/new_pools": (200, fixture("geckoterminal_new_pools.json")),
            "/api/v2/networks/base/new_pools": (503, "maintenance"),
            "/api/v2/networks/solana/pools/": (200, fixture("geckoterminal_ohlcv_minute.json")),
        }),
        clock=lambda: NOW,
    )
    dex = DexScreenerClient(
        mock_client("https://api.dexscreener.com", {
            "/tokens/v1/solana/": (200, fixture("dexscreener_tokens_v1_solana.json")),
            "/token-boosts/latest/v1": (200, fixture("dexscreener_boosts_latest.json")),
            "/token-profiles/latest/v1": (429, "rate limited"),
        }),
        clock=lambda: NOW,
    )
    store = MarketDataStore(repo.sessionmaker)
    svc = IngestionService(dex, gt, store, repo, REGISTRY,
                           IngestionConfig(chains=(Chain.SOLANA, Chain.BASE), min_liquidity_usd_to_watch=Decimal(10**12)),
                           clock=lambda: NOW + timedelta(hours=1))
    svc.watch(TokenRef(Chain.SOLANA, BONK, "Bonk"), "5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9", "manual")
    svc.watch(TokenRef(Chain.SOLANA, WIF, "$WIF"), "placeholder-pool", "manual")

    rep = await svc.run_once()
    assert rep.new_pools > 0 and rep.snapshots > rep.new_pools and rep.attention > 0
    assert rep.candles > 0
    assert len(rep.errors) == 2  # base new_pools 503, profiles 429 — isolated, not fatal
    events = await repo.events("ingestion_error")
    assert {e.detail.split("]")[0] for e in events} == {"[server_error", "[rate_limited"}
    assert await store.count(PairSnapshotRow) == rep.snapshots
    assert await store.count(AttentionRow) == rep.attention
    # WIF's pool address was corrected to its main pool by the DexScreener refresh.
    assert svc.watchlist[f"solana:{WIF}"].pool_address != "placeholder-pool"

    # Both watched pools got the 30 closed candles from the recorded response.
    assert rep.candles == 60 and await store.count(CandleRow) == 60
    # Further cycles see the same timestamps: nothing is duplicated.
    again = await svc.run_once()
    assert again.candles == 0 and await store.count(CandleRow) == 60


async def test_candle_store_is_idempotent(repo) -> None:
    from atomtrader.marketdata.http import parse_json

    store = MarketDataStore(repo.sessionmaker)
    candles = parse_ohlcv(parse_json(fixture("geckoterminal_ohlcv_minute.json")))
    assert await store.save_candles(Chain.SOLANA, "pool", "1m", candles, "gt") == 30
    assert await store.save_candles(Chain.SOLANA, "pool", "1m", candles, "gt") == 0
    loaded = await store.load_candles(Chain.SOLANA, "pool", "1m")
    assert loaded == candles


async def test_api_error_kinds_surface_from_adapters() -> None:
    gt = GeckoTerminalClient(mock_client("https://api.geckoterminal.com/api/v2", {
        "/api/v2/networks/solana/pools/": (404, '{"errors":[{"status":"404","title":"Not Found"}]}'),
    }))
    with pytest.raises(ApiError) as e:
        await gt.ohlcv(Chain.SOLANA, "nope")
    assert e.value.kind is ErrorKind.API_REJECTION


# ----------------------------------------------------------------- fake-liquidity defence


def test_registry_identifies_base_assets_by_address_not_symbol() -> None:
    sol_usdc = next(a for a in REGISTRY.assets if a.chain is Chain.SOLANA and a.symbol == "USDC")
    assert REGISTRY.is_base_asset(Chain.SOLANA, sol_usdc.address)
    # Solana addresses are case-sensitive; EVM addresses are not.
    assert not REGISTRY.is_base_asset(Chain.SOLANA, sol_usdc.address.lower())
    weth = next(a for a in REGISTRY.assets if a.chain is Chain.BASE and a.symbol == "WETH")
    assert REGISTRY.is_base_asset(Chain.BASE, weth.address.upper().replace("0X", "0x"))
    # The impostor "USDC" seen in Jupiter search (unverified) is not a base asset.
    assert not REGISTRY.is_base_asset(Chain.SOLANA, "FBH46zFUTBPLoafLqMjngHUKR6uUCH2Wtia557UEpump")
    assert all(a.source in ("jupiter:verified", "lifi") for a in REGISTRY.assets)


def _with_fake_quote(raw_fixture: str, path: list[str], fake: str) -> str:
    data = json.loads(raw_fixture)
    items = data["data"] if isinstance(data, dict) else data
    for item in items:
        node = item
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = fake
    return json.dumps(data)


async def test_pools_quoted_in_unknown_tokens_are_not_watched(repo) -> None:
    fake_pools = _with_fake_quote(fixture("geckoterminal_new_pools.json"),
                                  ["relationships", "quote_token", "data", "id"], "solana_FakeQuote1111111111111111111111111111")
    gt = GeckoTerminalClient(mock_client("https://api.geckoterminal.com/api/v2", {
        "/api/v2/networks/solana/new_pools": (200, fake_pools)}), clock=lambda: NOW)
    dex = DexScreenerClient(mock_client("https://api.dexscreener.com", {}), clock=lambda: NOW)
    svc = IngestionService(dex, gt, MarketDataStore(repo.sessionmaker), repo, REGISTRY,
                           IngestionConfig(chains=(Chain.SOLANA,), min_liquidity_usd_to_watch=Decimal(0),
                                           record_attention=False), clock=lambda: NOW)
    rep = await svc.run_once()
    assert rep.new_pools == 3 and rep.rejected_non_base_quote == 3 and not svc.watchlist

    # Same pools with their real SOL quote are admitted.
    gt2 = GeckoTerminalClient(mock_client("https://api.geckoterminal.com/api/v2", {
        "/api/v2/networks/solana/new_pools": (200, fixture("geckoterminal_new_pools.json"))}), clock=lambda: NOW)
    svc2 = IngestionService(dex, gt2, MarketDataStore(repo.sessionmaker), repo, REGISTRY,
                            IngestionConfig(chains=(Chain.SOLANA,), min_liquidity_usd_to_watch=Decimal(0),
                                            record_attention=False, candle_pools_per_cycle=0), clock=lambda: NOW)
    rep2 = await svc2.run_once()
    assert rep2.rejected_non_base_quote == 0 and len(svc2.watchlist) == 3


async def test_quote_source_refuses_pairs_quoted_in_unknown_tokens() -> None:
    fake = _with_fake_quote(fixture("dexscreener_tokens_v1_solana.json"), ["quoteToken", "address"], "FakeQuote1111111111111111111111111111")
    dex = DexScreenerClient(mock_client("https://api.dexscreener.com", {"/tokens/v1/solana/": (200, fake)}), clock=lambda: NOW)
    with pytest.raises(MissingMarketData):
        await DexScreenerQuoteSource(dex, REGISTRY).get_quote(TokenRef(Chain.SOLANA, BONK))


async def test_fx_falls_back_to_labelled_ecb_rate_when_intraday_unavailable() -> None:
    table = FxTable()
    svc = FxService(
        table,
        coingecko=mock_client("https://api.coingecko.com/api/v3", {"/api/v3/simple/price": (429, "rate limited")}),
        frankfurter=mock_client("https://api.frankfurter.dev", {"/v2/providers/ecb/rates": (200, fixture("frankfurter_ecb_usd_eur.json"))}),
        clock=lambda: NOW,
    )
    rep = await svc.refresh()
    usd = table.get("USD", NOW)
    assert rep.updated == ("USD",) and usd.eur_per_unit == Decimal("0.91116")
    assert usd.source.startswith("ecb-daily-reference:2026-11-26")
    with pytest.raises(MissingMarketData):  # native-token prices are NOT backfilled
        table.get("SOL", NOW)


async def test_fx_with_no_sources_updates_nothing() -> None:
    table = FxTable()
    svc = FxService(
        table,
        coingecko=mock_client("https://api.coingecko.com/api/v3", {"/api/v3/simple/price": (503, "down")}),
        frankfurter=mock_client("https://api.frankfurter.dev", {"/v2/providers/ecb/rates": (503, "down")}),
        clock=lambda: NOW,
    )
    rep = await svc.refresh()
    assert rep.updated == () and len(rep.errors) == 2
    with pytest.raises(MissingMarketData):
        table.get("USD", NOW)
