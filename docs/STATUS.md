# Project status — 2026-11-27

Mode: **paper trading only.** No live execution provider exists; no wallet, key or real
order is used anywhere. 176 automated tests (+2 PostgreSQL tests run against a real
PostgreSQL 16 server).

## Requirements → implementation

| # | Requirement | Status | Where / notes |
|---|---|---|---|
| 1 | €1,000, EUR reporting, moderate risk, 20 trades/day cap, no leverage/martingale/averaging down | Done | `config/settings.py` hard ceilings; `risk/engine.py`; EUR ledger with FX provenance |
| 2 | FOMO investigation; documented execution infra; data vs execution separation | Done (research) / paper only | FOMO has no API (`PHASE1_RESEARCH.md`); Jupiter/0x chosen; provider interfaces separate |
| 3A | Narrative Intelligence | Done, limited sources | HN + GDELT; X, Reddit, Telegram, Google Trends need your credentials/approval → reported as data gaps; LLM classifier optional |
| 3B | Narrative Rotation Detector | Done | Leader cooling / follower accelerating within one narrative; correlation-labelled (no wallet-overlap data) |
| 3C | Moonshot Detector (0–100) | Done | Participation-weighted, price capped at 8/100, penalties incl. wash trading |
| 4 | Market scanner, multi-timeframe TA, on-chain/DEX metrics | Done | `scanner/`, `analysis/indicators.py`; unique buyers from GeckoTerminal; order-book imbalance N/A on AMMs |
| 5 | Entry strategies A–E with conditions, confirmations, invalidation, stops, costs, confidence | Done | `strategy/entries.py`; cost gate (move − costs)/(stop + costs) ≥ 1.5 |
| 6 | Token security + Insider Exit Risk with four distinct situations | Done | `security/`; confirmed failures vs unverified; holder distribution measured over time |
| 7 | Normal / Momentum / Explosive Runner modes, partial profits | Done | `strategy/exits.py`; partial plan configurable and compared in walk-forward |
| 8 | Risk engine with full authority, controls | Done | Daily loss incl. open risk and fees, cooldowns, caps, liquidity-aware sizing, kill switch, chain pause, blacklist |
| 9 | Execution engine | Done for paper | Idempotent orders, write-ahead, bounded retries, reconciliation, exact-quote fills on Solana; live provider not built |
| 10 | Adaptive profit targets | Done | ATR/horizon volatility, costs, narrative stretch; runner removes ceiling |
| 11 | Composite Opportunity Score, structured decisions, decision log | Done | `strategy/scoring.py`; every decision with evidence in `decisions` |
| 12 | Dashboard | Done | Separate process; account, positions, opportunities, narratives, rotations, performance, controls; PAPER label everywhere |
| 13 | Notifications & daily report | Done | Event log + optional webhook; daily report at local-day rollover |
| 14 | Self-improvement (proposal-only), walk-forward, OOS | Done | `analytics/performance.py`, `backtest/engine.py`; never edits risk controls |
| 15 | Architecture (async, Postgres, logging, secrets, fault recovery) | Done | Persistent state, restart-safe; Docker/systemd deployment |
| 16 | Phases 1–6 | Done | |
| 16 | Phase 7: ≥14 days paper trading | **Not done — requires a server** | This development container is ephemeral; see `RUNBOOK.md` |
| 16 | Phase 8: live | **Not started — requires your explicit authorization** | |

## Known limitations (honest list)

1. **EVM sell verification gap.** Honeypot.is cannot simulate many brand-new BSC/Base pairs
   ("pair not found"). Such tokens are blocked from entry (`sell_simulation_unavailable`),
   which is safe but means few EVM trades. A self-hosted sell simulation (eth_call against
   the router) would close this gap.
2. **EVM slippage uncalibrated.** Solana fills use exact Jupiter quotes; Base/BNB use the
   single-pool model (labelled uncalibrated).
3. **Keyless rate limits.** GeckoTerminal/CoinGecko/GDELT throttle shared IPs; a dedicated
   server helps, a paid key removes the limit.
4. **Narrative sources.** Only Hacker News and GDELT are wired in; crypto-native social
   attention (X, Telegram) is the biggest missing signal and needs your accounts/keys.
5. **Wallet-level evidence.** Rotation and insider analyses have no indexed wallet-trade
   data; rotation is correlation-only, insider selling is measured from holder snapshots.
6. **Backtests are weak evidence** for meme coins (survivorship bias, thin history); the
   system records its own dataset from day one. Synthetic scenarios test mechanics only.
7. **Unproven edge.** Nothing here demonstrates profitability. The 14-day paper period is
   the first real measurement.
