"""Read-only security data adapters: RugCheck (Solana), GoPlus (EVM), Honeypot.is (EVM sell
simulation), Solana RPC (mint/freeze authority cross-check).

Each ``apply_*`` function merges one source's findings into a SecurityReport and records the
source status. Fields a source doesn't provide stay None — nothing is inferred.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from atomtrader.marketdata.http import ApiError, JsonHttpClient
from atomtrader.security.models import Holder, HolderKind, SecurityReport

EVM_CHAIN_IDS = {"ethereum": 1, "base": 8453, "bsc": 56}
_RUGCHECK_KINDS = {"AMM": HolderKind.AMM, "LOCKER": HolderKind.LOCKER, "CREATOR": HolderKind.CREATOR}


def _d(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except ArithmeticError:
        return None


def _flag(v: Any) -> bool | None:
    if v is None or v == "":
        return None
    return str(v) == "1"


# --------------------------------------------------------------------------- RugCheck


def apply_rugcheck(report: SecurityReport, raw: dict[str, Any]) -> None:
    if "error" in raw:
        report.sources["rugcheck"] = f"error: {raw['error']}"
        return
    report.sources["rugcheck"] = "ok"
    known = raw.get("knownAccounts") or {}
    holders = []
    for h in raw.get("topHolders") or []:
        label = known.get(h.get("owner")) or known.get(h.get("address")) or {}
        holders.append(Holder(
            address=h.get("owner") or h.get("address", ""),
            pct=_d(h.get("pct")) or Decimal(0),
            kind=_RUGCHECK_KINDS.get(label.get("type", ""), HolderKind.UNKNOWN),
            insider=h.get("insider"),
        ))
    report.holders = holders
    report.total_holders = raw.get("totalHolders")
    report.mint_authority_active = raw.get("mintAuthority") is not None
    report.freeze_authority_active = raw.get("freezeAuthority") is not None
    report.rugcheck_score = _d(raw.get("score_normalised"))
    supply = _d((raw.get("token") or {}).get("supply"))
    creator_bal = _d(raw.get("creatorBalance"))
    if supply and creator_bal is not None and supply > 0:
        report.creator_pct = creator_bal / supply * 100
    nets = raw.get("insiderNetworks") or []
    if nets and supply:
        held = sum((_d(n.get("currentHolding")) or Decimal(0) for n in nets), Decimal(0))
        report.insider_network_pct = held / supply * 100
        report.insider_network_wallets = sum(int(n.get("activeAccounts") or n.get("size") or 0) for n in nets)
    elif raw.get("graphInsidersDetected") is not None:
        report.insider_network_wallets = int(raw["graphInsidersDetected"])
    fee = raw.get("transferFee") or {}
    fee_pct = _d(fee.get("pct"))
    if fee_pct:  # Token-2022 transfer fee acts as a tax on every transfer
        report.buy_tax_pct = fee_pct
        report.sell_tax_pct = fee_pct
    locked = [_d((m.get("lp") or {}).get("lpLockedPct")) for m in raw.get("markets") or []]
    locked = [x for x in locked if x is not None]
    if locked:
        report.lp_locked_pct = max(locked)
    if raw.get("rugged"):
        report.hard_fail.append("rugcheck_rugged")
    for r in raw.get("risks") or []:
        if r.get("level") == "danger":
            report.warnings.append(f"rugcheck:{r.get('name')}")


# --------------------------------------------------------------------------- GoPlus

GOPLUS_HARD = {
    "is_honeypot": "goplus_honeypot",
    "cannot_sell_all": "goplus_cannot_sell_all",
    "owner_change_balance": "goplus_owner_can_change_balance",
    "slippage_modifiable": "goplus_tax_modifiable",
    "personal_slippage_modifiable": "goplus_per_wallet_tax_modifiable",
    "cannot_buy": "goplus_cannot_buy",
}
GOPLUS_WARN = {
    "is_mintable": "mintable",
    "hidden_owner": "hidden_owner",
    "can_take_back_ownership": "can_take_back_ownership",
    "transfer_pausable": "transfer_pausable",
    "is_blacklisted": "has_blacklist",
    "is_proxy": "upgradeable_proxy",
    "external_call": "external_call",
    "trading_cooldown": "trading_cooldown",
    "is_anti_whale": "anti_whale_limits",
}


def apply_goplus(report: SecurityReport, raw: dict[str, Any], address: str) -> None:
    if raw.get("code") != 1:
        report.sources["goplus"] = f"error: code={raw.get('code')} {raw.get('message', '')}"
        return
    info = (raw.get("result") or {}).get(address.lower())
    if not info:
        report.sources["goplus"] = "error: token not in result"
        return
    report.sources["goplus"] = "ok"
    for key, reason in GOPLUS_HARD.items():
        if _flag(info.get(key)):
            report.hard_fail.append(reason)
    for key, reason in GOPLUS_WARN.items():
        if _flag(info.get(key)):
            report.warnings.append(f"goplus:{reason}")
    if _flag(info.get("is_open_source")) is False:
        report.warnings.append("goplus:unverified_source")
    report.mint_authority_active = _flag(info.get("is_mintable"))
    for key, attr in (("buy_tax", "buy_tax_pct"), ("sell_tax", "sell_tax_pct")):
        v = _d(info.get(key))
        if v is not None:
            pct = v * 100
            cur = getattr(report, attr)
            setattr(report, attr, pct if cur is None else max(cur, pct))
    if info.get("holder_count"):
        report.total_holders = int(info["holder_count"])
    if not report.holders:
        report.holders = [
            Holder(
                address=h.get("address", ""),
                pct=(_d(h.get("percent")) or Decimal(0)) * 100,
                kind=HolderKind.LOCKER if _flag(h.get("is_locked")) else (
                    HolderKind.CONTRACT if _flag(h.get("is_contract")) else HolderKind.UNKNOWN),
                locked=_flag(h.get("is_locked")),
            )
            for h in info.get("holders") or []
        ]
    creator = _d(info.get("creator_percent"))
    if creator is not None:
        report.creator_pct = creator * 100
    lp = info.get("lp_holders") or []
    if lp:
        report.lp_locked_pct = sum(((_d(h.get("percent")) or Decimal(0)) * 100 for h in lp if _flag(h.get("is_locked"))), Decimal(0))


# --------------------------------------------------------------------------- Honeypot.is


def apply_honeypot(report: SecurityReport, raw: dict[str, Any]) -> None:
    if "error" in raw and "honeypotResult" not in raw:
        report.sources["honeypot_is"] = f"error: {raw.get('error')}"
        return
    report.sources["honeypot_is"] = "ok"
    if (raw.get("honeypotResult") or {}).get("isHoneypot"):
        report.hard_fail.append("honeypot_is_honeypot")
    if raw.get("simulationSuccess") is False:
        report.hard_fail.append("honeypot_is_simulation_failed")
    sim = raw.get("simulationResult") or {}
    for key, attr in (("buyTax", "buy_tax_pct"), ("sellTax", "sell_tax_pct")):
        v = _d(sim.get(key))
        if v is not None:
            cur = getattr(report, attr)
            setattr(report, attr, v if cur is None else max(cur, v))
    ha = raw.get("holderAnalysis") or {}
    if ha.get("failed") is not None:
        report.simulated_sell_failures = int(ha["failed"])
        if int(ha["failed"]) > 0:
            report.warnings.append(f"honeypot_is:{ha['failed']}_holder_sells_failed")
    for f in raw.get("flags") or []:
        report.warnings.append(f"honeypot_is:{f}")


# --------------------------------------------------------------------------- Solana RPC


def apply_solana_mint(report: SecurityReport, raw: dict[str, Any]) -> None:
    """Cross-check mint/freeze authority straight from the chain (getAccountInfo jsonParsed)."""
    if "result" not in raw or not raw["result"] or not raw["result"].get("value"):
        report.sources["solana_rpc"] = f"error: {str(raw.get('error'))[:120]}"
        return
    info = raw["result"]["value"]["data"]["parsed"]["info"]
    report.sources["solana_rpc"] = "ok"
    report.mint_authority_active = info.get("mintAuthority") is not None
    report.freeze_authority_active = info.get("freezeAuthority") is not None


# --------------------------------------------------------------------------- clients


class SecuritySources:
    def __init__(
        self,
        rugcheck: JsonHttpClient | None = None,
        goplus: JsonHttpClient | None = None,
        honeypot: JsonHttpClient | None = None,
        solana_rpc: JsonHttpClient | None = None,
    ) -> None:
        self.rugcheck = rugcheck or JsonHttpClient("https://api.rugcheck.xyz/v1", per_minute=20, max_retries=1)
        self.goplus = goplus or JsonHttpClient("https://api.gopluslabs.io/api/v1", per_minute=20, max_retries=1)
        self.honeypot = honeypot or JsonHttpClient("https://api.honeypot.is", per_minute=20, max_retries=1)
        self.solana_rpc = solana_rpc or JsonHttpClient("https://api.mainnet-beta.solana.com", per_minute=30, max_retries=1)

    async def aclose(self) -> None:
        for c in (self.rugcheck, self.goplus, self.honeypot, self.solana_rpc):
            await c.aclose()

    @staticmethod
    async def _get(client: JsonHttpClient, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return await client.get_json(path, params)
        except ApiError as err:
            return {"error": f"{err.kind.value}: {err.message}"[:200]}

    async def rugcheck_report(self, mint: str) -> dict[str, Any]:
        return await self._get(self.rugcheck, f"/tokens/{mint}/report")

    async def goplus_token(self, chain: str, address: str) -> dict[str, Any]:
        return await self._get(self.goplus, f"/token_security/{EVM_CHAIN_IDS[chain]}", {"contract_addresses": address})

    async def honeypot_check(self, chain: str, address: str) -> dict[str, Any]:
        return await self._get(self.honeypot, "/v2/IsHoneypot", {"address": address, "chainID": EVM_CHAIN_IDS[chain]})

    async def solana_mint(self, mint: str) -> dict[str, Any]:
        try:
            return await self.solana_rpc.post_json("", {"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                                                        "params": [mint, {"encoding": "jsonParsed"}]})
        except ApiError as err:
            return {"error": f"{err.kind.value}: {err.message}"[:200]}
