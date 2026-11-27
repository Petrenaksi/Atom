"""EUR conversion rates.

* Intraday USD->EUR: CoinGecko's USDC price in EUR divided by its price in USD (both
  quotes for the same coin, so a USDC depeg cancels out).
* Reference: ECB daily rate via Frankfurter (v2). Used as a sanity cross-check, and as a
  labelled *fallback* when no intraday rate can be fetched (EUR/USD typically moves well
  under 1% per day; the source string records the ECB publication date).
* Native gas tokens (SOL, BNB, ETH) in EUR from CoinGecko, for network-fee conversion.

If the intraday rate deviates from the ECB reference by more than ``max_deviation``, the
table is NOT updated: stale rates then expire in FxTable and trading pauses rather than
using a suspicious rate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from atomtrader.core.money import FxRate, FxTable
from atomtrader.marketdata.http import ApiError, JsonHttpClient

COINGECKO_URL = "https://api.coingecko.com/api/v3"
FRANKFURTER_URL = "https://api.frankfurter.dev"
NATIVE_IDS = {"SOL": "solana", "BNB": "binancecoin", "ETH": "ethereum"}


@dataclass(frozen=True, slots=True)
class FxRefreshReport:
    updated: tuple[str, ...]
    ecb_usd_eur: Decimal | None
    intraday_usd_eur: Decimal | None
    deviation_pct: Decimal | None
    errors: tuple[str, ...]


class FxService:
    def __init__(
        self,
        table: FxTable,
        coingecko: JsonHttpClient | None = None,
        frankfurter: JsonHttpClient | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_deviation_pct: Decimal = Decimal("2"),
    ) -> None:
        self.table = table
        self.cg = coingecko or JsonHttpClient(COINGECKO_URL, per_minute=10)
        self.ff = frankfurter or JsonHttpClient(FRANKFURTER_URL, per_minute=10)
        self.clock = clock
        self.max_dev = max_deviation_pct

    async def aclose(self) -> None:
        await self.cg.aclose()
        await self.ff.aclose()

    async def refresh(self) -> FxRefreshReport:
        errors: list[str] = []
        updated: list[str] = []
        ecb: Decimal | None = None
        intraday: Decimal | None = None
        deviation: Decimal | None = None
        ecb_date = ""
        try:
            rows = await self.ff.get_json("/v2/providers/ecb/rates", {"base": "USD", "quotes": "EUR"})
            ecb = Decimal(str(rows[0]["rate"]))
            ecb_date = str(rows[0].get("date", ""))
        except (ApiError, KeyError, IndexError) as exc:
            errors.append(f"ecb: {exc}")
        try:
            prices = await self.cg.get_json(
                "/simple/price",
                {"ids": ",".join(["usd-coin", *NATIVE_IDS.values()]), "vs_currencies": "eur,usd"},
            )
        except ApiError as exc:
            errors.append(f"coingecko: {exc}")
            prices = None

        now = self.clock()
        if prices:
            usdc = prices.get("usd-coin") or {}
            if usdc.get("eur") and usdc.get("usd"):
                intraday = Decimal(str(usdc["eur"])) / Decimal(str(usdc["usd"]))
            if intraday is not None and ecb is not None:
                deviation = abs(intraday / ecb - 1) * 100
            if intraday is not None and (deviation is None or deviation <= self.max_dev):
                # Without an ECB cross-check we still accept the intraday rate (labelled).
                src = "coingecko:usdc-eur/usd" + ("" if ecb is not None else " (no ECB cross-check)")
                self.table.update(FxRate("USD", intraday, now, src))
                updated.append("USD")
            elif intraday is not None:
                errors.append(f"usd/eur deviation {deviation:.2f}% vs ECB exceeds {self.max_dev}%")
            for sym, cid in NATIVE_IDS.items():
                eur = (prices.get(cid) or {}).get("eur")
                if eur:
                    self.table.update(FxRate(sym, Decimal(str(eur)), now, "coingecko"))
                    updated.append(sym)
        if "USD" not in updated and intraday is None and ecb is not None:
            self.table.update(FxRate("USD", ecb, now, f"ecb-daily-reference:{ecb_date} (intraday unavailable)"))
            updated.append("USD")
        return FxRefreshReport(tuple(updated), ecb, intraday, deviation, tuple(errors))
