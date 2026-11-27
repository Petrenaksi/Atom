"""Registry of recognized base (quote) assets per chain, keyed by address.

Why: a pool paired against a worthless, self-priced token can report billions in
"liquidity" (observed live: a Base "new pool" showing $2.16B). Liquidity is only
meaningful when the quote side is a real base asset (SOL, USDC, USDT, WETH, WBNB).

Addresses are *resolved from providers* (Jupiter verified list for Solana, LI.FI for EVM),
never typed from memory, and saved with source + timestamp in ``data/base_assets.json``.
Lookalike tokens with the same symbol are excluded by requiring Jupiter's verified flag and
taking the most liquid match.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from atomtrader.core.models import Chain
from atomtrader.marketdata.http import JsonHttpClient

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "base_assets.json"

WANTED: dict[Chain, tuple[str, ...]] = {
    Chain.SOLANA: ("SOL", "USDC", "USDT"),
    Chain.BASE: ("WETH", "USDC", "USDT"),
    Chain.BSC: ("WBNB", "USDT", "USDC", "WETH"),
    Chain.ETHEREUM: ("WETH", "USDC", "USDT"),
}
EVM_CHAIN_IDS = {Chain.BASE: 8453, Chain.BSC: 56, Chain.ETHEREUM: 1}


def _norm(chain: Chain, address: str) -> str:
    # EVM addresses are case-insensitive; Solana base58 addresses are case-sensitive.
    return address if chain is Chain.SOLANA else address.lower()


@dataclass(frozen=True, slots=True)
class BaseAsset:
    chain: Chain
    symbol: str
    address: str
    source: str


class BaseAssetRegistry:
    def __init__(self, assets: Iterable[BaseAsset], resolved_at: str = "") -> None:
        self.assets = list(assets)
        self.resolved_at = resolved_at
        self._index = {(a.chain, _norm(a.chain, a.address)): a for a in self.assets}

    def is_base_asset(self, chain: Chain, address: str) -> bool:
        return (chain, _norm(chain, address)) in self._index

    def get(self, chain: Chain, address: str) -> BaseAsset | None:
        return self._index.get((chain, _norm(chain, address)))

    # ---------------------------------------------------------------- io
    def to_json(self) -> dict:
        return {
            "resolved_at": self.resolved_at,
            "note": "Resolved from Jupiter (verified, most liquid) and LI.FI token APIs. Do not hand-edit.",
            "assets": [
                {"chain": a.chain.value, "symbol": a.symbol, "address": a.address, "source": a.source}
                for a in self.assets
            ],
        }

    def save(self, path: Path = DATA_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), indent=1) + "\n")

    @classmethod
    def load(cls, path: Path = DATA_FILE) -> BaseAssetRegistry:
        raw = json.loads(path.read_text())
        return cls(
            (BaseAsset(Chain(a["chain"]), a["symbol"], a["address"], a["source"]) for a in raw["assets"]),
            raw.get("resolved_at", ""),
        )

    # ---------------------------------------------------------------- resolve
    @classmethod
    async def resolve(cls, jupiter: JsonHttpClient, lifi: JsonHttpClient,
                      chains: Iterable[Chain] = tuple(WANTED)) -> tuple[BaseAssetRegistry, list[str]]:
        assets: list[BaseAsset] = []
        problems: list[str] = []
        for chain in chains:
            for symbol in WANTED[chain]:
                try:
                    if chain is Chain.SOLANA:
                        items = await jupiter.get_json("/tokens/v2/search", {"query": symbol})
                        cands = [t for t in items if t.get("symbol") == symbol and t.get("isVerified")]
                        if not cands:
                            problems.append(f"{chain.value}:{symbol} no verified match")
                            continue
                        best = max(cands, key=lambda t: t.get("liquidity") or 0)
                        assets.append(BaseAsset(chain, symbol, best["id"], "jupiter:verified"))
                    else:
                        t = await lifi.get_json("/v1/token", {"chain": EVM_CHAIN_IDS[chain], "token": symbol})
                        if t.get("symbol") != symbol or not t.get("address"):
                            problems.append(f"{chain.value}:{symbol} unexpected {t}")
                            continue
                        assets.append(BaseAsset(chain, symbol, t["address"], "lifi"))
                except Exception as exc:  # record and continue; caller decides
                    problems.append(f"{chain.value}:{symbol} {exc}")
        return cls(assets, datetime.now(UTC).isoformat(timespec="seconds")), problems


async def _main() -> int:
    async with (
        JsonHttpClient("https://lite-api.jup.ag", per_minute=20) as jup,
        JsonHttpClient("https://li.quest", per_minute=20) as lifi,
    ):
        reg, problems = await BaseAssetRegistry.resolve(jup, lifi)
    for a in reg.assets:
        print(f"{a.chain.value:9} {a.symbol:5} {a.address}  ({a.source})")
    for p in problems:
        print("PROBLEM:", p)
    if problems:
        print("not saved: resolve problems")
        return 1
    reg.save()
    print(f"saved {len(reg.assets)} assets to {DATA_FILE}")
    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(_main()))
