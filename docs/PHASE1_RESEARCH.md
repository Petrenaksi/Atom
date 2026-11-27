# Phase 1 — Research, Infrastructure & Architecture

Status date: 2026-11-27. Reporting currency: EUR. Mode: **paper only** (no live execution exists in the codebase).

> **Verification caveat.** The development container's network policy blocks every
> crypto API host (proxy HTTP 403 for DexScreener, GeckoTerminal, Jupiter, GoPlus,
> RugCheck, Honeypot.is, CoinGecko, public RPCs, Reddit, GDELT, HN). Everything below
> was verified against providers' **published documentation and current third-party
> reporting**, not by live calls. Each data adapter will get a live smoke test as its
> first step once it runs somewhere with network access (the production VPS, or this
> environment after the host allowlist is widened). Numbers marked *(unconfirmed)*
> had conflicting sources.

> **Update 2026-11-27 — live verification done.** After the "Atom" environment was created
> (12 allowlisted hosts), every endpoint was verified live; see
> [`CONNECTIVITY.md`](CONNECTIVITY.md). Corrections to this document, marked ⚠️ below:
> GeckoTerminal *does* provide unique buyers/sellers per window for free; Frankfurter moved
> to `api.frankfurter.dev` (v2 API); keyless CoinGecko/GeckoTerminal limits were hit in
> practice from this environment; a pool reporting **$2.16B fake liquidity** was observed,
> so liquidity is only trusted for pools quoted in a verified base asset.

---

## 1. FOMO application

| Question | Finding |
|---|---|
| Official, documented trading API? | **No.** fomo.family publishes no developer API, API-key flow, or SDK. |
| Automated trading supported? | **No** supported mechanism. |
| Authenticated wallet/exchange integration? | No public integration. Only a tax export (Awaken) and on-chain visibility of your wallet. |
| Authorized transaction execution interface? | **None.** |
| Third-party "FOMO APIs" (fomoapi.io, getfomoapi.fun, Open-Fomo-API) | Explicitly *unofficial* and not affiliated with Fomo. They expose **read-only** social/leaderboard data. Not usable for execution; not used. |

**Decision:** FOMO is not used for execution. No private endpoints will be touched.
Because FOMO trades settle on-chain (Solana), the bot trades the same tokens through
documented DEX aggregators from a **dedicated bot wallet** that you fund separately.
Optional later: FOMO's public on-chain activity (via Bitquery's documented FOMO
dataset) as a *narrative/attention signal* — read-only and authorized.

## 2. Execution infrastructure (recommended)

Execution is self-custodial swaps through aggregators. A centralized exchange was
considered and rejected as primary: CEXs list new meme coins days-to-weeks late,
which defeats the narrative/second-wave strategy.

| Chain | Primary | Fallback | Notes |
|---|---|---|---|
| **Solana** | **Jupiter Swap API v2** (`/order` → sign → `/execute`, managed landing, MEV protection) | Jupiter `/build` (raw instructions) + own RPC (Helius) + Jito tip | Best meme-coin liquidity/routing on Solana; network fees ≈ cents. Platform fee is returned in the quote (`platformFee`), so cost is known before signing. Free API key; 1 RPS free, 10 RPS paid. |
| **Base** | **0x Swap API v2** (AllowanceHolder flow: `/price` → exact-amount approval → `/quote`) | LI.FI / OKX DEX aggregator | Base gas is cents; strong meme activity. Exact-amount approvals only (never unlimited). |
| **BNB Chain** | 0x Swap API v2 | OKX DEX aggregator | Higher honeypot/tax-token prevalence → stricter security gates. |
| **Ethereum L1** | 0x Swap API v2 via private orderflow (Flashbots Protect RPC) | 1inch | **Disabled by default** — see cost analysis §5. |

Comparison summary (documentation-based):

| Criterion | Jupiter v2 | 0x v2 | LI.FI | 1inch | OKX DEX |
|---|---|---|---|---|---|
| Chains | Solana | 15+ EVM incl. Base, BNB | 60+ (cross-chain focus) | ~13 EVM | 39–60+ incl. Solana |
| Aggregator fee | ~5–10 bps platform fee (in quote) | 0.15% on paid tier *(unconfirmed)* | free standard tier *(unconfirmed)* | 0 platform fee | not published |
| Execution | Managed landing + MEV protection | Firm quote, you submit | Routing layer (~100 ms overhead) | You submit | You submit |
| API quality | Excellent, active | Excellent | Good | Good | Good |
| Meme/new-pair coverage | Best on Solana | Good on Base | Delegates to DEX aggs | Weaker on fresh pairs | Good |

**Architecture consequence:** `ExecutionProvider` is an interface; each provider is
swappable and independent from market-data providers. A `PaperExecutionProvider`
(already implemented) uses the same interface, so strategies cannot tell paper from
live — only the risk/accounting layer and dashboard know, and they label it.

## 3. Market-data, security and narrative sources

### Market data
| Source | Access | Gives | Limits / gaps |
|---|---|---|---|
| **DexScreener API** | Free, keyless | Pairs by token, search, price, liquidity, FDV/mcap, buys/sells **counts** and volume for m5/h1/h6/h24, pair age, boosts/profiles | 300 req/min (pairs), 60 req/min (boosts). **No OHLCV candles. No unique-wallet counts.** |
| **GeckoTerminal API** | Free keyless; paid via CoinGecko | OHLCV (minute/hour/day, ≤1000 bars), new pools, trending pools, trades, ⚠️ **unique `buyers`/`sellers`/`unique_traders` per window (m5…h24)** | Documented 30 req/min (one doc says ~10); ⚠️ 429s observed live at 8–20/min from this shared-egress environment → adaptive limiter. Only a handful of tokens' 1-m candles per minute. |
| **Birdeye** | Paid (compute-unit plans; WebSocket on higher tiers ~$199/mo *(unconfirmed)*; x402 pay-per-request) | Real-time trades WS, OHLCV, holders, unique wallets, 17 chains incl. Solana/Base | Cost. Optional upgrade once paper results justify it. |
| **Helius (Solana RPC)** | Free 1M credits; paid tiers *(Developer $49 vs $499 conflicting)* | RPC, `getTokenLargestAccounts`, mint/freeze authority, Enhanced WebSockets/LaserStream (2 credits / 0.1 MB) | Building own candles from raw swaps is data-heavy; budget-gated. |
| EVM RPC (Alchemy/QuickNode/public) | Free tiers | Contract reads, holder balances, logs | Public RPCs are rate-limited and unreliable for execution. |
| **FX**: ECB reference rates (Frankfurter, ⚠️ `api.frankfurter.dev` `/v2/providers/ecb/rates`) + CoinGecko | Free | EUR/USD, SOL/ETH/BNB in EUR | Intraday USD→EUR from CoinGecko USDC EUR/USD, cross-checked against ECB (±2%); labelled ECB fallback when CoinGecko is rate-limited. Every fill stores the FX rate used. |

### Token security
| Source | Chains | Use |
|---|---|---|
| **RugCheck** | Solana | Report/summary (risks, LP lock, top holders, normalised risk score). Free reads are cached. |
| **GoPlus Token Security** | EVM (+ Solana beta) | Mint/blacklist/pause/tax/proxy/owner flags, holder concentration. Free tier exists. |
| **Honeypot.is** | Ethereum, BSC, Base | Buy/sell **simulation**, effective taxes. Keyless. |
| Own checks | All | Mint/freeze authority via RPC; **sell-route quote** from the aggregator before every entry (no sell route ⇒ hard block); liquidity delta tracking. |

### Narrative intelligence
| Source | Status | Cost / constraint |
|---|---|---|
| **X/Twitter API** | Available, pay-per-use only for new developers | ~$5 per 1,000 post reads; 7-day recent search; 3M reads/month cap. Must be used surgically (targeted queries for candidate tokens/narratives), not as a firehose. |
| **Reddit Data API** | OAuth, 100 QPM; approval required for new clients; free only for **non-commercial** use | Personal, non-commercial trading research likely qualifies, **but needs your approval application**. Reports suggest public access tightening in 2026–27 *(unconfirmed)*. |
| **GDELT** | Free | Global news events/themes, 15-min updates — good for "real-world event" detection. |
| **Hacker News (Algolia)** | Free | Tech/AI narrative detection. |
| **Telegram** | Public channels via official MTProto API (Telethon) with your own account/api_id | Authorized for public channels you join; no scraping of private groups. |
| **Google Trends** | No reliable free official API (alpha access only); unofficial scrapers violate ToS | **Not used** unless official access is granted. |
| DexScreener boosts/profiles, Pump.fun-style launch feeds | Free | Attention proxies — boosts are *paid promotion* and are scored as such (coordination signal, not organic). |
| **LLM (Claude API)** | Paid per token | Narrative classification/clustering only, with evidence + confidence stored. Never computes sizes, prices, or approves trades. |

**Never fabricated:** when a metric is unavailable (e.g. unique buyers without Birdeye),
the field is stored as `None` / "unavailable" and the scoring model **re-weights
without it and reduces confidence** — it never imputes a value.

## 4. Multi-chain feasibility

| Chain | Feasible now? | Why |
|---|---|---|
| Solana | **Yes — primary** | Deepest meme liquidity, cents in fees, best tooling (Jupiter, RugCheck, Helius). |
| Base | **Yes — secondary** | Cheap gas, 0x/GoPlus/Honeypot.is coverage. |
| BNB Chain | Yes, with stricter gates | Higher scam/tax-token rate; honeypot simulation mandatory. |
| Ethereum L1 | **Technically yes, economically no** at €1,000 | Swap gas typically $2–15 per leg; on a €50–150 position that is 3–20% round-trip before price impact. Enabled only for majors/large moves, never micro-caps, unless capital grows. |
| Others (Arbitrum, Sui, Tron…) | Deferred | Added when an aggregator + security scanner + data feed all cover them. |

**Wallet-overlap evidence** (rotation detector) requires indexed trade-level data
(Birdeye/Helius/Bitquery). Until that is funded, rotation signals are labelled
`statistical_correlation`, never `wallet_verified`.

## 5. Estimated operating costs

### Fixed monthly
| Item | Lean start (paper phase) | Full stack |
|---|---|---|
| VPS (EU, 4 vCPU/8 GB, Postgres + Redis local) | €10–25 | €25–40 |
| Solana RPC (Helius) | €0 (free tier) | €45–460 *(tier pricing unconfirmed)* |
| EVM RPC | €0 | €0–45 |
| Birdeye (WS trades, holders, unique wallets) | €0 | €90–190 |
| X API (targeted reads, ~10–30k/month) | €0–50 | €50–150 |
| LLM narrative classification (Claude, cached, small model for triage) | €10–30 | €30–80 |
| **Total** | **≈ €20–100 / month** | **≈ €250–950 / month** |

**This matters:** on €1,000 capital, a €300/month data bill is a **30%/month hurdle**
before any trading profit. Recommendation: run the paper phase on the lean stack,
and add paid data only where paper results show the missing data is what limits
performance. Operating costs are recorded and shown next to trading P&L on the
dashboard.

### Per-trade costs (modelled in the paper engine)
| Component | Solana | Base | Ethereum L1 |
|---|---|---|---|
| Network fee + priority/tip | €0.001–0.10 | €0.01–0.20 | €2–15 |
| DEX pool fee | 0.25–1% | 0.3–1% | 0.3–1% |
| Aggregator fee | ~0.05–0.10% | 0–0.15% | 0–0.15% |
| Price impact + slippage (micro-cap) | 0.3–5% | 0.3–5% | 0.3–5% |
| **Realistic round trip** | **~1–4%** | **~1–4%** | dominated by gas |

At 20 trades/day a 2% round trip is a heavy drag; the opportunity scorer requires
expected move ≫ round-trip cost, and the risk engine sizes using stop distance
**plus** round-trip cost.

## 6. Security considerations

1. **Dedicated hot wallet, limited balance.** The bot wallet holds only trading capital.
   Profits are swept manually by you to cold storage. No automatic deposits.
2. **Key handling.** Keys only via encrypted keystore / OS secret store / env injected at
   runtime; never in source, DB, logs, dashboard, or any external request. The logger
   has a redaction processor that scrubs registered secret values and private-key /
   mnemonic patterns (implemented + tested).
3. **Live gating.** Live execution requires (a) `mode=live` in config, (b) an explicit
   authorization phrase from you, and (c) a live provider that does not yet exist.
   Paper and live balances are type-separated and labelled.
4. **Kill switch without the key.** Emergency stop flips a persisted flag the engine checks
   before every order; it never requires or touches the wallet key.
5. **Approvals (EVM).** Exact-amount allowances via AllowanceHolder; never infinite.
6. **Pre-trade simulation.** Simulate every transaction; require a valid sell route before
   buying; on EVM use Honeypot.is sell simulation.
7. **MEV.** Jupiter managed execution / Jito on Solana; private orderflow on Ethereum;
   tight slippage caps everywhere.
8. **Prompt injection.** Social text fed to the LLM is untrusted. LLM output is parsed into a
   fixed schema of features with confidence; it cannot trigger orders, change limits, or
   reach any tool that can.
9. **Dashboard.** Separate process, bound to localhost/VPN, authenticated, read-mostly;
   control actions are audited. Engine keeps running if the dashboard dies.
10. **Supply chain.** Pinned dependencies with hashes; minimal dependency set; no
    "trading bot" packages of unknown provenance (a common wallet-drainer vector).
11. **Risk controls are not self-modifiable.** The self-improvement module may *propose*
    parameter changes; risk limits have hard ceilings enforced in code.

## 7. Technical architecture

```
                ┌──────────────────── data plane (read-only, replaceable) ─────────────────────┐
                │ DexScreener · GeckoTerminal · Birdeye · RPCs · RugCheck · GoPlus · Honeypot  │
                │ X · Reddit · GDELT · HN · Telegram · FX                                      │
                └───────────────┬──────────────────────────────────┬──────────────────────────┘
                                │ normalized events                 │
              ┌─────────────────▼───────────┐       ┌───────────────▼─────────────┐
              │ Market Scanner / TA / On-chain│      │ Narrative Intelligence       │
              │ (candles, indicators, flows)  │      │ (+ LLM classifier, evidence) │
              └─────────────────┬────────────┘       └───────────────┬──────────────┘
                                │                                     │
              ┌─────────────────▼─────────────────────────────────────▼──────────────┐
              │ Moonshot Detector · Rotation Detector · Token Security / Insider Risk │
              └─────────────────┬────────────────────────────────────────────────────┘
                                │ candidates + evidence
              ┌─────────────────▼──────────────┐
              │ Opportunity Scoring (0–100)     │  configurable weights, missing-data aware
              └─────────────────┬──────────────┘
                                │ TradeIntent
              ┌─────────────────▼──────────────┐   ◄── controls: kill switch, pause,
              │ RISK ENGINE (authoritative)     │       chain pause, blacklist, limits
              └─────────────────┬──────────────┘
                                │ approved size or rejection (logged either way)
              ┌─────────────────▼──────────────┐      ┌────────────────────────────┐
              │ Execution Engine                │─────►│ ExecutionProvider:          │
              │ idempotent orders, reconcile    │      │ Paper | Jupiter | 0x | ...  │
              └─────────────────┬──────────────┘      └────────────────────────────┘
                                │ reconciled fills only
              ┌─────────────────▼──────────────┐
              │ Portfolio Ledger (EUR) ·        │──► Postgres (orders, fills, decisions,
              │ Position Manager · Exit Engine  │        risk events, equity snapshots)
              └─────────────────┬──────────────┘
                                ▼
            Notifications · Performance Analytics · Backtesting · Dashboard (separate process)
```

Principles:
- **Deterministic core** (risk, sizing, accounting, execution) is pure Python with `Decimal`,
  fully unit-tested, no LLM in the loop.
- **Provider interfaces** for market data and execution, swappable independently.
- **Event-sourced accounting**: positions are rebuilt from persisted fills on restart; orders
  carry a unique `client_order_id` so a restart cannot double-submit.
- **Async** (`asyncio`, `httpx`, WebSockets), single engine process + separate dashboard.
- Postgres (prod) / SQLite (tests) via SQLAlchemy 2 async; Redis for hot caches/pub-sub later.

## 8. Development roadmap

| Stage | Content | Exit criteria |
|---|---|---|
| **2a (this commit)** | Package skeleton, config w/ live gating, Decimal money + EUR FX, domain models, structured logging + secret redaction, **risk engine**, **paper execution** (AMM impact, fees, failures, latency drift, slippage reverts), **EUR ledger**, persistence with idempotent orders + restart recovery | Unit tests green |
| 2b | Market-data adapters (DexScreener, GeckoTerminal, FX) with live smoke tests; candle store; scheduler | Live data flowing on VPS |
| 3a | TA indicators (EMA/VWAP/RSI/MACD/ATR/rel-vol/structure) + scanner | Indicator tests vs reference values |
| 3b | Security & insider-risk (RugCheck/GoPlus/Honeypot/sell-route) | Hard-fail gating tests |
| 3c | Narrative engine (GDELT/HN/Reddit/X/Telegram + LLM classifier), rotation + moonshot detectors | Evidence-logged scores |
| 4 | Opportunity scoring, strategies A–E, exit engine (Normal/Momentum/Runner, partials, adaptive targets) | Backtest harness runs |
| 5 | Dashboard + notifications + daily report | Paper label everywhere |
| 6 | Full test campaign: integration, failure injection, restart, walk-forward | Documented results |
| 7 | **≥14 days paper trading** on live data | Your review |
| 8 | Live — **only on your explicit authorization**, restricted capital | Reconciliation verified |

## 9. Identified technical limitations (honest list)

1. **No live API verification from this dev container** (network policy). Adapters get
   live smoke tests on first deploy.
2. ⚠️ **Corrected:** GeckoTerminal's free pool data *does* include unique buyers/sellers
   per window. Still missing for free: holder distribution over time, wallet-level flows,
   and candles at scale (keyless GeckoTerminal ≈ a few calls/min in practice). DexScreener
   gives buy/sell *counts* only; those fields stay `None` rather than estimated.
2a. **Fake liquidity.** Pools paired against self-priced tokens can report absurd
   liquidity (observed: $2.16B). Liquidity is only trusted when the quote asset is a
   verified base asset resolved by address (`atomtrader/data/base_assets.json`).
2b. **Token identity.** Symbols are not identity: Jupiter search returns unverified
   "USDC"/"USDT" impostors. Tokens are keyed by mint/contract address plus verification.
3. **Backtesting meme coins is weak evidence.** Dead/rugged tokens vanish from APIs
   (survivorship bias), minute data is sparse, and historical slippage is unknowable. The
   system therefore **records its own dataset from day one**; paper trading on live data
   is the primary validation, backtests secondary.
4. **Wallet-level rotation evidence** needs indexed trades (paid). Until then rotation is
   correlation-labelled.
5. **Latency.** REST polling at seconds-level is fine for 10–30-minute holds but cannot
   (and should not try to) compete with snipers in the first blocks of a launch.
6. **X/Reddit access is costly or gated**; narrative quality depends on what you approve.
7. **Edge is unproven.** Meme-coin markets are adversarial (insiders, bots, MEV). It is
   entirely possible that paper trading shows negative expectancy after costs; the
   system is built to measure that truthfully rather than hide it.
8. **Taxes/regulation.** Every fill is stored with EUR value and FX rate for your records;
   tax treatment of DEX trading in your jurisdiction is your responsibility to confirm.

## Sources
- FOMO: [fomofamilyapp.com/api](https://fomofamilyapp.com/api), [fomoapi.io docs (unofficial)](https://fomoapi.io/docs), [Bitquery FOMO dataset](https://docs.bitquery.io/docs/blockchain/Solana/fomo-api/), [Wikipedia: Fomo (platform)](https://en.wikipedia.org/wiki/Fomo_(platform))
- Jupiter: [rate limits](https://developers.jup.ag/docs/portal/rate-limits), [plans](https://developers.jup.ag/docs/portal/plans.md), [changelog 2026-03 (Swap v2)](https://developers.jup.ag/changelog/2026-03), [v2 fees](https://dev.jup.ag/docs/swap/v2/fees.md)
- 0x: [Swap API guide](https://docs.0x.org/evm/0x-swap-api/guides/swap-tokens-with-0x-swap-api), [cheat sheet](https://0x.org/docs/introduction/0x-cheat-sheet), [v2 upgrade](https://0x.org/docs/upgrading/upgrading_to_swap_v2)
- LI.FI / 1inch / OKX: [LI.FI vs aggregators](https://docs.li.fi/overview/li.fi-vs-aggregators-dexs-bridges), [DefiLlama OKX Swap](https://defillama.com/protocol/okx-swap), [1inch vs OKX](https://madeonsol.com/compare/1inch-vs-okx)
- DexScreener: [API reference](https://docs.dexscreener.com/api/reference)
- GeckoTerminal: [FAQ](https://apiguide.geckoterminal.com/faq), [changelog](https://apiguide.geckoterminal.com/changelogs), [CoinGecko keyless API](https://docs.coingecko.com/docs/keyless-public-api)
- Birdeye: [pricing](https://docs.birdeye.so/docs/pricing), [packages](https://docs.birdeye.so/docs/data-accessibility-by-packages), [websocket](https://docs.birdeye.so/docs/websocket)
- Helius: [credits](https://helius.dev/docs/billing/credits), [pricing](https://helius.dev/pricing), [plans](https://www.helius.dev/docs/billing/plans.md)
- Security: [GoPlus docs](https://docs.gopluslabs.io/reference), [GoPlus security API](https://gopluslabs.io/security-api), [Honeypot.is changelog](https://docs.honeypot.is/changelog), [RugCheck client docs](https://zazza123.github.io/cyhole/interactions/rugcheck/interaction.html)
- X API: [postproxy pricing 2026](https://postproxy.dev/blog/x-api-pricing-2026/), [social-sdk pricing](https://social-sdk.dev/guides/x-api-pricing)
- Reddit: [data365 limits](https://data365.co/blog/reddit-api-limits), [data365 pricing](https://data365.co/blog/reddit-api-pricing), [apiserpent free tier](https://apiserpent.com/blog/free-reddit-api)
