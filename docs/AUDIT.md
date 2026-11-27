# Independent technical audit — 2026-11-27

Scope: the paper-trading system on branch `claude/autonomous-crypto-trading-bot-ttx0ok` before
VPS deployment. Method: code review of every decision/execution path, **failure probes
written before fixes** (`tests/test_audit_failures.py`), live measurements against the real
data and security providers, and a real-browser check of the dashboard.

Constraints honoured: **no risk limit was changed**; the system remains **paper-only**; no
live execution code was added.

Result: **7 critical and 6 high-priority defects found and fixed** (commits `8b3eb08`,
`e5919b3` and the dashboard fix in this commit). No known critical defect remains open for
paper deployment. Open items are listed as improvements and limitations below.

---

## 1. Critical issues (all fixed, each with a regression test)

| # | Issue | How it was found | Impact before fix | Fix |
|---|---|---|---|---|
| C1 | **A position could become impossible to exit.** Exit order IDs were derived from prior *applied* orders; a rejected exit (e.g. a momentary quote gap) is never applied, so the retry reused the same ID and the engine replayed the stored rejection forever. | Probe `test_exit_rejected_once_can_be_retried_later` failed | Stuck positions after any transient data gap | Every exit decision is a new order ID; restart safety still comes from the write-ahead SUBMITTED state |
| C2 | **Stale market data drove decisions.** The scanner used the newest stored snapshot regardless of age (up to 90 min). If ingestion stalled, entries and exits ran on old prices. | Probe failed; code review | Entries on old prices; exits/stops evaluated on stale data | Candidates carry `data_age_seconds`; >3 min = stale: never ranked or entered; open positions with stale data enforce their stop against a fresh direct quote and alert |
| C3 | **Fabricated liquidity passed every check.** Providers value the token side of a pool at the token's own price. Live: a Base pool reported **$119 M** liquidity backed by **0.501 WETH** (~$1.7k); UBR $2.17 B reported vs ~$449k real. The base-asset filter did not catch it (quote side *was* WETH). | Live EVM measurement | Risk-engine liquidity cap, paper price-impact model and insider liquidity signal all trusted fake depth; spam crowded the watchlist | Decisions use **2 × quote-side depth** from DexScreener's base/quote breakdown; GeckoTerminal reserve kept only as "reported"; spam dropped from the watchlist after verification |
| C4 | **EVM tokens from GeckoTerminal could never be priced.** GeckoTerminal returns lowercase addresses, DexScreener checksummed; exact string comparison failed. | Surfaced by the C3 regression test | No EVM entries possible from discovered pools; an open EVM position would be unpriced; history split across two keys | EVM addresses normalised at the boundary (`TokenRef`, pair/quote addresses); Solana stays case-sensitive |
| C5 | **Failed-transaction fees missing from performance figures.** Fees burned by reverted/dropped transactions reduced cash but were attributed to no trade; the daily report's "net P&L after all fees" excluded them; the backtester never simulated failures. | Code review + probe | Overstated net results | Daily report: net = trades − failed-tx fees, with failed count, fill fees and slippage-vs-mid shown separately; dashboard shows failed-tx fees; backtester simulates failures (default 3 %) and includes them in its net/ranking |
| C6 | **Dashboard XSS via token names.** Token symbols are attacker-controlled; a symbol was embedded in an inline `onclick` JavaScript string, where HTML escaping does not protect. A crafted name executed script when the operator clicked "Close". | Real-browser reproduction (`XSS executed: true`) | Script in an authenticated dashboard that can issue control commands | No data inside inline handlers; values travel as `data-*` attributes with one delegated listener (verified: payload displayed inertly); static regression test |
| C7 | **Contested honeypot verdicts forced exits.** Honeypot.is flagged 4/13 trending BSC tokens (all PancakeSwap v3 0.01 % pools) as honeypots with a 0 % sell tax while GoPlus reported them sellable. Any such verdict was a hard failure → forced exit. | Live cross-check of both sources | Unnecessary forced exits on probable false positives | Contested verdicts (generic detection, 0 % tax, GoPlus clean) are **unverified**: entry still blocked, no forced exit. Reverts (e.g. "STF" on transfer-restricted tokenised stocks), high taxes or agreeing sources remain confirmed failures |

## 2. High-priority issues (fixed)

| # | Issue | Fix |
|---|---|---|
| H1 | A crash between "submitted" and "recorded" left a *paper* order unknown → the token was blocked forever | Paper results lost in a crash are resolved as never-happened (logged); the unknown-outcome block is kept for non-paper providers and tested with a test double |
| H2 | A database failure *after* execution did not block the token (duplicate-order risk) | Unrecorded results mark the token unresolved and raise |
| H3 | Failed entries were retried every scan, burning a network fee each time | 30-minute per-token cool-off after a failed entry (execution-safety throttle; persisted; not a risk limit) |
| H4 | Rotation detector could not tell a market-wide surge from a second wave | Follower must out-accelerate tokens with no narrative link by 1.5× (median baseline recorded in evidence; flagged when no baseline exists) |
| H5 | Insider holder data refreshed only every 10 min for held tokens | Held tokens re-assessed every 2 min (see §3.5 for the remaining provider-side lag) |
| H6 | Unverified (vs confirmed) security results were not distinguished | Two tiers: confirmed failures block entry and exit; unverified results block entry only |

## 3. Answers to the seven audit questions

### 3.1 Do the tests cover trading failures, not just success?
Before the audit: 176 tests (+2 PostgreSQL). By name, roughly two-thirds exercised failure,
rejection or adversarial paths (API error classes, 429 handling, slippage reverts, failed
transactions, idempotent resubmission, unknown outcomes, restart rebuilds, emergency stop
persistence, daily-loss halts, honeypots, fake liquidity via unknown quotes, wash-trading
penalties, lookahead). **But specific high-consequence paths were missing** — and five of them
were real bugs (C1, C2, H1, H2, H3). Now 197 tests (+2 PostgreSQL); 21 new audit tests listed in §5.

### 3.2 Duplicate orders, stale data, API outages, partial fills, restart recovery

| Scenario | Behaviour now | Test |
|---|---|---|
| Same order submitted twice | Stored outcome returned, never sent twice | `test_resubmitting_same_order_never_trades_twice` |
| Entry while holding the token | Rejected (`already_holding_token`) | risk engine tests |
| Venue exception mid-order | Outcome unknown → token blocked | `test_provider_exception_marks_outcome_unknown` |
| Result cannot be recorded | Token blocked (H2) | `test_database_failure_after_execution_blocks_token` |
| Crash before send / after send | PENDING → rejected; paper SUBMITTED → resolved (H1) | `test_restart_reconciliation`, `test_paper_order_lost_in_crash…` |
| Transient exit rejection | Retried with a new order (C1) | `test_exit_rejected_once_can_be_retried_later` |
| Sell route disappears while held | Exit rejected without fee, position kept open and alerted | `test_position_with_vanished_sell_route…` |
| Stale snapshots | No entries; stop enforced on direct quote (C2) | 3 audit tests |
| Provider outage / 429 / 5xx | Classified; failing source isolated; 429 pauses the client (AIMD) | http/ingestion tests |
| Quote service down | Buys rejected; sells use labelled conservative model | `test_quote_outage_rejects_buys…` |
| FX unavailable | Labelled ECB fallback; none at all → trading pauses | FX tests |
| Restart | Ledger replayed from fills; controls, risk state, exit states, entry cool-offs restored | session/engine/PostgreSQL tests |
| **Partial fills** | **Not applicable to AMM swaps** (atomic: full output or revert). Partial *exits* (profit plan) and quantity differing from estimate (slippage) are handled | documented test |

### 3.3 Are paper fills realistic?
Modelled: Solana fills at **exact-size Jupiter quotes** (all routes, pool fees inside the
price); calibrated latency drift (measured σ≈4 bps/5 s, 10 bps floor); platform fee 10 bps;
network fee; random tx failures (3 %); slippage-cap reverts that burn the fee; no-route
rejections. EVM: single-pool AMM model against **verified** depth (after C3) + pool/aggregator
fees. Measured: same €74.75 Bonk round trip −€0.52 (quotes) vs −€1.50 (old model);
pre-trade swap cost 4.2 bps; universe median round trip 53 bps (p90 105, max 244).
**Not modelled**: MEV/sandwiching, priority-fee spikes under congestion (fixed €0.05),
landing failures above 3 %, quotes ≠ landed fills; EVM slippage uncalibrated. Verdict:
realistic for liquid/medium Solana tokens; optimistic in congestion and for EVM.

### 3.4 Can the Rotation Detector distinguish genuine second waves?
It enforces: a shared narrative link (names alone are downgraded/flagged), a leader that ran
(≥ +100 % 24 h) and is cooling, a follower that has not run (< 25 % of the leader's gain),
volume/participation acceleration with buy pressure, crowding detection and now **relative
strength vs unrelated tokens** (H4). It **cannot**: confirm shared wallets (no indexed
wallet data → every alert is labelled `statistical_correlation`), verify timing order
(leader peak vs follower onset), or be validated statistically — no labelled history of
rotations exists yet. Treat alerts as hypotheses that require strategy confirmation (they
feed Strategy B, which still passes the cost gate, Opportunity Score and risk engine).

### 3.5 Does the Insider Distribution Detector react early enough?
Measured: RugCheck updated price, liquidity and markets between two fetches 90 s apart on an
actively traded token, but **not holder balances or holder count** — holder data is cached on
RugCheck's side. The public Solana RPC refuses `getTokenLargestAccounts` (429). So:
- *Confirmed large-holder selling* (holder snapshots) is a **lagging** signal: provider
  cache + 2-min re-assessment. Useful for multi-hour positions and for blocking re-entry,
  **not** as an early warning inside a 10–30-minute trade. No pre-entry history exists, so
  distribution that happened before entry is invisible to it.
- The **early** signals are liquidity drain (≥ 25 % → exit; DexScreener every ~20 s) and
  selling pressure (m5 buy ratio < 0.35 with volume ≥ 1.5× pace → exit) — both independent of
  holder data.
- To make holder-based detection timely: paid Solana RPC (Helius) polling top holders of
  held tokens every 30–60 s (recommendation R2).

### 3.6 Are the security filters too restrictive for new EVM tokens?
Measured on live pools (21 Base/BSC pools; 20 trending pairs cross-checked):
- **New Base pools: 0/7 verifiable** — all were Uniswap v4, which Honeypot.is cannot simulate
  (same for Aerodrome Slipstream). New BSC pools: mostly unverifiable.
- **Trending tokens: 13/14 got a token-level verdict.** Of 20 cross-checked: ~10 pass
  cleanly; 2 correctly fail (transfer-restricted tokenised stocks, "STF" revert, 100 % tax);
  4 contested (C7); the rest lack a simulation.
- Conclusion: **yes, prohibitively restrictive for brand-new EVM pools — by design**
  ("never enter a token whose sell cannot be verified"), and reasonable for established
  tokens. The filter was **not loosened**. The way to open new EVM pools safely is a
  self-hosted sell simulation (R1), not a weaker rule. Solana is unaffected (Jupiter sell
  route probe + RugCheck).

### 3.7 Do the analytics account for every trading cost?

| Cost | Where it is captured |
|---|---|
| Pool fees, price impact (Solana) | Inside the exact-quote fill price; slippage vs mid reported separately |
| Pool fee + aggregator fee (EVM model) | Fill fee |
| Platform/aggregator fee | Fill fee |
| Network/priority fee | Fill fee (both legs) |
| Failed-transaction fees | **Now** in the daily report net, dashboard, backtester (C5) |
| FX (USD→EUR) | Rate stored per fill; EUR P&L reproducible |
| Slippage vs mid | Reported as a breakdown line (already inside prices) |
| Unrealised P&L | At mid — **not** liquidation value (exit costs not deducted; R4) |
| Infrastructure/data subscriptions, taxes | Not trading costs; excluded and stated in the report |

## 4. Remaining limitations and open improvements

High-priority improvements (recommended, not done):
- **R1** Self-hosted EVM sell simulation (`eth_call` with state overrides through the
  Universal Router / Aerodrome router) — the only safe way to admit new Base/BSC pools.
- **R2** Paid Solana RPC (Helius Developer) for 30–60 s holder polling of held tokens.
- **R3** EVM slippage calibration with exact-size quotes (0x key or LI.FI).
- **R4** Liquidation-value equity (mid minus estimated exit cost) on the dashboard.
- **R5** Store attention signals only on change (currently re-recorded every cycle) and add
  a data-retention job (≈3 GB/month at default cadence).
- **R6** Rotation timing evidence from leader candles; wallet-overlap evidence with paid indexing.
- **R7** Congestion-aware paper costs (priority-fee and failure-rate from recent network data).
- **R8** Narrative stop-list for generic terms ("data", "code", "browser") that rarely carry
  token meaning; crypto-native sources (X/Telegram) need your credentials.
- **R9** Exit-slippage escalation policy when a stop exit repeatedly reverts (a risk
  decision for you; not changed).
- **R10** Backtest frame building is O(n²) (≈25 s for synthetic suites) — optimise before
  large historical runs.

Limitations that remain by nature: quotes are not landed fills; backtests on meme coins are
weak evidence; no profitability has been demonstrated.

## 5. Tests performed

Automated: **197 passed + 2 PostgreSQL** (PostgreSQL 16) — was 176 + 2. New audit tests:
exit retry after rejection; vanished sell route while held; paper order lost in crash;
unrecorded result after execution; failed-entry cool-off; stale snapshot detection; stale
candidates never entered (with fresh-data control); stale position stop on direct quote;
inflated DexScreener liquidity; GeckoTerminal reserve unverified; paper fill on spam pool
reverts against real depth; EVM address normalisation; report net includes failed-tx fees;
cost breakdown from orders; backtest charges failed transactions; rotation relative strength
vs unrelated tokens; held-token security refresh; contested vs confirmed honeypot verdicts;
dashboard inline-handler XSS guard; non-paper unknown-outcome block (test double).

Live measurements (2026-11-27): Honeypot.is coverage with/without pair on 21 Base/BSC pools;
Honeypot.is vs GoPlus verdicts on 20 trending pairs; DexScreener base/quote liquidity on
inflated pools; RugCheck report freshness over 90 s; public RPC `getTokenLargestAccounts`
(429); one-scan live engine run after the fixes (verified liquidity: UBR $449k vs $2.18 B
reported; spam $1.2k–4.2k vs $134 M); real-browser XSS reproduction and fix verification;
storage per row (snapshot ≈1.1 kB).

## 6. Tests still needed

1. **14-day paper run** on the VPS (behaviour under real conditions, real failure rates).
2. **Chaos tests on the VPS**: `kill -9` mid-cycle, PostgreSQL restart, per-provider network
   blocking (firewall), disk full, clock jump.
3. **48-hour soak**: memory growth, DB growth, rate-limit adaptation, log volume.
4. **DST transition** (last Sunday of March/October, Europe/Madrid) for daily-limit reset.
5. **Labelled evaluation** of rotation and insider alerts against the recorded 14-day data.
6. **EVM fill calibration** (needs exact-size EVM quotes).
7. **Dashboard security review** beyond XSS: token rotation, rate limiting of the API,
   TLS if exposed beyond an SSH tunnel (not recommended).
8. Before any live phase: landed-fill reconciliation at tiny size (not in scope now).
