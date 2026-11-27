"""Connectivity matrix for every allowlisted data/DEX endpoint.

Each check records the exact host, the request made, the HTTP status, whether the body
had the expected schema, and an outcome class (see ``ErrorKind``). Negative controls
(an invalid mint, a nonexistent token) must come back as API_REJECTION — proving that a
token-specific refusal is classified as such and not as an outage.

Reference tokens are resolved at runtime from provider token lists (Jupiter verified
list, LI.FI) instead of hard-coded from memory.

Run:  python -m atomtrader.diagnostics.connectivity [--json out.json] [--md out.md]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from atomtrader.marketdata.http import ApiError, JsonHttpClient

OK = "ok"
WRAPPED_SOL = "So11111111111111111111111111111111111111112"  # validated by the Jupiter price check
NONEXISTENT_SOLANA_MINT = "11111111111111111111111111111112"  # valid base58, not a token


@dataclass
class CheckResult:
    service: str
    host: str
    test: str
    expected: str
    outcome: str
    http_status: int | None
    valid_response: bool
    detail: str

    @property
    def passed(self) -> bool:
        return self.outcome == self.expected


class SchemaError(ValueError):
    pass


def need(cond: bool, msg: str) -> None:
    if not cond:
        raise SchemaError(msg)


async def run_check(
    service: str,
    client: JsonHttpClient,
    test: str,
    call: Callable[[], Awaitable[Any]],
    validate: Callable[[Any], str],
    expected: str = OK,
) -> CheckResult:
    try:
        body = await call()
    except ApiError as err:
        return CheckResult(service, client.host, test, expected, err.kind.value, err.status,
                           err.kind.value == expected, err.message)
    try:
        detail = validate(body)
    except (SchemaError, KeyError, IndexError, TypeError) as exc:
        return CheckResult(service, client.host, test, expected, "invalid_response", 200, False, str(exc))
    return CheckResult(service, client.host, test, expected, OK, 200, True, detail)


async def run_all() -> tuple[list[CheckResult], dict[str, str]]:
    results: list[CheckResult] = []
    refs: dict[str, str] = {}

    async with (
        JsonHttpClient("https://lite-api.jup.ag", per_minute=30, max_retries=1) as jup,
        JsonHttpClient("https://li.quest", per_minute=30, max_retries=1) as lifi,
        JsonHttpClient("https://api.dexscreener.com", per_minute=60, max_retries=1) as dex,
        JsonHttpClient("https://api.geckoterminal.com/api/v2", per_minute=20, max_retries=1) as gt,
        JsonHttpClient("https://api.coingecko.com/api/v3", per_minute=10, max_retries=1) as cg,
        JsonHttpClient("https://api.frankfurter.dev", per_minute=30, max_retries=1) as fx,
        JsonHttpClient("https://api.rugcheck.xyz/v1", per_minute=20, max_retries=1) as rc,
        JsonHttpClient("https://api.gopluslabs.io/api/v1", per_minute=20, max_retries=1) as gp,
        JsonHttpClient("https://api.honeypot.is", per_minute=20, max_retries=1) as hp,
        JsonHttpClient("https://api.mainnet-beta.solana.com", per_minute=30, max_retries=1) as sol,
        JsonHttpClient("https://mainnet.base.org", per_minute=30, max_retries=1) as base,
        JsonHttpClient("https://bsc-dataseed.bnbchain.org", per_minute=30, max_retries=1) as bsc,
    ):
        # ---------------- resolve reference tokens from provider lists
        async def jup_verified(symbol: str) -> str:
            items = await jup.get_json("/tokens/v2/search", {"query": symbol})
            cands = [t for t in items if t.get("symbol") == symbol and t.get("isVerified")]
            need(bool(cands), f"no verified {symbol}")
            best = max(cands, key=lambda t: t.get("liquidity") or 0)
            refs[f"solana:{symbol}"] = best["id"]
            return f"{symbol} -> {best['id']} (verified, tags={best.get('tags')})"

        results.append(await run_check("Jupiter", jup, "token search: verified USDC by mint",
                                       lambda: jup_verified("USDC"), lambda d: d))
        results.append(await run_check("Jupiter", jup, "token search: verified Bonk by mint",
                                       lambda: jup_verified("Bonk"), lambda d: d))
        usdc = refs.get("solana:USDC", "")
        bonk = refs.get("solana:Bonk", "")

        async def lifi_token(chain_id: int, label: str) -> Any:
            t = await lifi.get_json("/v1/token", {"chain": chain_id, "token": "USDC"})
            refs[f"{label}:USDC"] = t["address"]
            return t

        for chain_id, label in ((8453, "base"), (56, "bsc")):
            results.append(await run_check(
                "LI.FI", lifi, f"token lookup: USDC on chain {chain_id}",
                lambda c=chain_id, lab=label: lifi_token(c, lab),
                lambda t: f"{t['symbol']} {t['address']} decimals={t['decimals']}",
            ))

        def v_chains(d: Any) -> str:
            ids = {c["id"] for c in d["chains"]}
            need({8453, 56, 1}.issubset(ids), "Base/BSC/Ethereum missing")
            return f"{len(ids)} chains incl. Base, BSC, Ethereum"

        results.append(await run_check("LI.FI", lifi, "GET /v1/chains", lambda: lifi.get_json("/v1/chains"), v_chains))

        # ---------------- Jupiter quotes & price
        def v_quote(decimals_out: int, unit: str) -> Callable[[Any], str]:
            def _v(d: Any) -> str:
                out = int(d["outAmount"])
                need(out > 0, "zero output")
                hops = [r["swapInfo"]["label"] for r in d["routePlan"]]
                return f"out={out / 10**decimals_out} {unit}, impact={d['priceImpactPct']}%, route={hops}"
            return _v

        results.append(await run_check(
            "Jupiter", jup, "quote 0.1 SOL -> USDC",
            lambda: jup.get_json("/swap/v1/quote", {"inputMint": WRAPPED_SOL, "outputMint": usdc,
                                                     "amount": 100_000_000, "slippageBps": 50}),
            v_quote(6, "USDC")))
        results.append(await run_check(
            "Jupiter", jup, "quote 10 USDC -> SOL",
            lambda: jup.get_json("/swap/v1/quote", {"inputMint": usdc, "outputMint": WRAPPED_SOL,
                                                     "amount": 10_000_000, "slippageBps": 50}),
            v_quote(9, "SOL")))

        def v_price(d: Any) -> str:
            sol_px, usdc_px = d[WRAPPED_SOL]["usdPrice"], d[usdc]["usdPrice"]
            need(0.98 < float(usdc_px) < 1.02, f"USDC price {usdc_px} not ~1")
            return f"SOL=${float(sol_px):.2f} USDC=${float(usdc_px):.4f}"

        results.append(await run_check("Jupiter", jup, "price v3 SOL,USDC",
                                       lambda: jup.get_json("/price/v3", {"ids": f"{WRAPPED_SOL},{usdc}"}), v_price))
        results.append(await run_check(
            "Jupiter", jup, "CONTROL: quote with invalid mint",
            lambda: jup.get_json("/swap/v1/quote", {"inputMint": WRAPPED_SOL,
                                                     "outputMint": NONEXISTENT_SOLANA_MINT,
                                                     "amount": 100_000_000, "slippageBps": 50}),
            lambda d: "unexpectedly succeeded", expected="api_rejection"))

        # ---------------- DexScreener
        def v_pairs(d: Any) -> str:
            pairs = d["pairs"]
            need(bool(pairs), "no pairs")
            p = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0))
            need(float(p["priceUsd"]) > 0, "bad price")
            refs["solana:Bonk:pool"] = p["pairAddress"]
            return (f"{len(pairs)} pairs; top {p['dexId']} liq=${float(p['liquidity']['usd']):,.0f} "
                    f"m5 buys/sells={p['txns']['m5']['buys']}/{p['txns']['m5']['sells']}")

        results.append(await run_check("DexScreener", dex, "GET /latest/dex/tokens: Bonk pairs",
                                       lambda: dex.get_json(f"/latest/dex/tokens/{bonk}"), v_pairs))

        def v_no_pairs(d: Any) -> str:
            need(not d.get("pairs"), "pairs returned for nonexistent token")
            return "200 with no pairs (token-specific: unknown token)"

        results.append(await run_check("DexScreener", dex, "CONTROL: pairs for nonexistent mint",
                                       lambda: dex.get_json(f"/latest/dex/tokens/{NONEXISTENT_SOLANA_MINT}"), v_no_pairs))

        # ---------------- GeckoTerminal
        pool = refs.get("solana:Bonk:pool", "")

        def v_ohlcv(d: Any) -> str:
            rows = d["data"]["attributes"]["ohlcv_list"]
            need(len(rows) > 0 and len(rows[0]) == 6, "bad ohlcv rows")
            return f"{len(rows)} 1m candles, newest ts={datetime.fromtimestamp(int(rows[0][0]), UTC):%H:%M}Z"

        results.append(await run_check(
            "GeckoTerminal", gt, "1-minute OHLCV for Bonk top pool",
            lambda: gt.get_json(f"/networks/solana/pools/{pool}/ohlcv/minute", {"aggregate": 1, "limit": 30}), v_ohlcv))

        def v_new(d: Any) -> str:
            a = d["data"][0]["attributes"]
            tx = a["transactions"]["m5"]
            need("buyers" in tx and "sellers" in tx, "no unique buyer/seller fields")
            return f"{len(d['data'])} new pools; unique buyers/sellers fields present"

        results.append(await run_check("GeckoTerminal", gt, "new pools (Solana) incl. unique buyers",
                                       lambda: gt.get_json("/networks/solana/new_pools"), v_new))

        # ---------------- prices & FX
        def v_cg(d: Any) -> str:
            need(float(d["solana"]["eur"]) > 0 and float(d["usd-coin"]["usd"]) > 0, "missing prices")
            return f"SOL=€{d['solana']['eur']} BNB=€{d['binancecoin']['eur']} ETH=€{d['ethereum']['eur']}"

        results.append(await run_check(
            "CoinGecko", cg, "simple price SOL/BNB/ETH/USDC in EUR+USD",
            lambda: cg.get_json("/simple/price", {"ids": "solana,binancecoin,ethereum,usd-coin",
                                                  "vs_currencies": "eur,usd"}), v_cg))

        def v_fx(d: Any) -> str:
            r = d[0]
            need(r["base"] == "USD" and r["quote"] == "EUR" and 0.5 < float(r["rate"]) < 1.5, "bad rate")
            return f"ECB USD->EUR {r['rate']} ({r['date']})"

        results.append(await run_check("Frankfurter (ECB)", fx, "v2 ECB rate USD->EUR",
                                       lambda: fx.get_json("/v2/providers/ecb/rates", {"base": "USD", "quotes": "EUR"}), v_fx))

        # ---------------- security
        def v_rc(d: Any) -> str:
            need("score_normalised" in d, "no score")
            return f"score_normalised={d['score_normalised']} risks={[r.get('name') for r in d.get('risks', [])][:3]}"

        for name, mint in (("USDC", usdc), ("Bonk", bonk)):
            results.append(await run_check("RugCheck", rc, f"report summary: {name}",
                                           lambda m=mint: rc.get_json(f"/tokens/{m}/report/summary"), v_rc))
        results.append(await run_check("RugCheck", rc, "CONTROL: report for nonexistent mint",
                                       lambda: rc.get_json(f"/tokens/{NONEXISTENT_SOLANA_MINT}/report/summary"),
                                       lambda d: "unexpectedly succeeded", expected="api_rejection"))

        base_usdc = refs.get("base:USDC", "")

        def v_gp(d: Any) -> str:
            need(d.get("code") == 1, f"code={d.get('code')} msg={d.get('message')}")
            info = d["result"][base_usdc.lower()]
            return f"is_honeypot={info.get('is_honeypot')} is_mintable={info.get('is_mintable')} holders={info.get('holder_count')}"

        results.append(await run_check("GoPlus", gp, "token_security: USDC on Base",
                                       lambda: gp.get_json("/token_security/8453", {"contract_addresses": base_usdc}), v_gp))

        def v_hp(d: Any) -> str:
            need("honeypotResult" in d or "summary" in d, "no honeypot result")
            return f"isHoneypot={d.get('honeypotResult', {}).get('isHoneypot')} risk={d.get('summary', {}).get('risk')}"

        results.append(await run_check("Honeypot.is", hp, "IsHoneypot: USDC on Base",
                                       lambda: hp.get_json("/v2/IsHoneypot", {"address": base_usdc, "chainID": 8453}), v_hp))

        # ---------------- RPC nodes (read-only)
        def rpc(client: JsonHttpClient, method: str, params: list[Any] | None = None) -> Awaitable[Any]:
            return client.post_json("", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []})

        def v_result(check: Callable[[Any], bool], fmt: Callable[[Any], str]) -> Callable[[Any], str]:
            def _v(d: Any) -> str:
                need("result" in d, f"rpc error: {d.get('error')}")
                need(check(d["result"]), f"unexpected result {d['result']}")
                return fmt(d["result"])
            return _v

        results.append(await run_check("Solana RPC", sol, "getSlot (read-only)",
                                       lambda: rpc(sol, "getSlot"), v_result(lambda r: int(r) > 0, lambda r: f"slot={r}")))
        results.append(await run_check("Base RPC", base, "eth_chainId == 8453",
                                       lambda: rpc(base, "eth_chainId"),
                                       v_result(lambda r: int(r, 16) == 8453, lambda r: f"chainId={int(r, 16)}")))
        results.append(await run_check("BNB Chain RPC", bsc, "eth_chainId == 56",
                                       lambda: rpc(bsc, "eth_chainId"),
                                       v_result(lambda r: int(r, 16) == 56, lambda r: f"chainId={int(r, 16)}")))

    # ---------------- egress policy control: a host that is NOT allowlisted
    async with JsonHttpClient("https://www.reddit.com", per_minute=10, max_retries=0) as rd:
        results.append(await run_check("Egress policy", rd, "CONTROL: non-allowlisted host is refused",
                                       lambda: rd.get_json("/r/CryptoCurrency/new.json"),
                                       lambda d: "unexpectedly reachable", expected="proxy_blocked"))
    return results, refs


def to_markdown(results: list[CheckResult], when: datetime) -> str:
    lines = [
        f"Run at {when:%Y-%m-%d %H:%M}Z. Pass = outcome matched expectation "
        "(controls are *expected* to be rejected).",
        "",
        "| Service | Host | Test | HTTP | Outcome | Valid response | Pass | Detail |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        detail = r.detail.replace("|", "/")[:140]
        lines.append(
            f"| {r.service} | `{r.host}` | {r.test} | {r.http_status or '—'} | {r.outcome} | "
            f"{'yes' if r.valid_response else 'no'} | {'✅' if r.passed else '❌'} | {detail} |"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    ap.add_argument("--md")
    args = ap.parse_args()
    when = datetime.now(UTC)
    results, refs = asyncio.run(run_all())
    md = to_markdown(results, when)
    print(md)
    print("\nresolved reference tokens:", json.dumps(refs, indent=1))
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"at": when.isoformat(), "refs": refs, "results": [asdict(r) | {"passed": r.passed} for r in results]}, f, indent=1)
    if args.md:
        with open(args.md, "w") as f:
            f.write(md + "\n")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
