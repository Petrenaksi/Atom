# Slippage calibration (Solana)

Run: 2026-11-27T14:04Z · dataset: [`solana_20261127T1404Z.json`](calibration/solana_20261127T1404Z.json) · fitted model:
[`atomtrader/data/slippage_model_solana.json`](../atomtrader/data/slippage_model_solana.json)

## Method
- 12 Solana tokens (Bonk, $WIF + GeckoTerminal trending/new pools quoted in a verified base
  asset, liquidity ≥ $15k) × trade sizes $25 / $75 / $150 / $300 × buy and sell = 96 live,
  read-only Jupiter quotes (`lite-api.jup.ag/swap/v1/quote`, exact-in). 96/96 succeeded.
- Cost per side = shortfall of the quote vs Jupiter's mid price (`/price/v3`). Includes price
  impact and pool fees across Jupiter's routes; **excludes** Jupiter's platform fee on managed
  execution and network fees (charged separately by the paper engine).
- Latency drift: the same $75 buy quoted twice, 5 s apart, per token.
- Compared against the previous paper model: single-pool constant-product impact against
  DexScreener main-pool liquidity + 25 bps pool fee per side.

## Results
| Token | Main-pool liq. | Jupiter aggregate liq. | Buy $75 (bps) | Sell $75 (bps) | Round trip $25 / $75 / $150 / $300 (bps) |
|---|---|---|---|---|---|
| Bonk | $218,607 | $5,573,220 | -26 | 30 | 2 / 4 / 5 / 9 |
| $WIF | $102,372 | $4,288,889 | -5 | 27 | 12 / 22 / 35 / 48 |
| PUMP | $18,501,321 | $30,841,804 | -53 | 57 | 7 / 5 / -0 / 4 |
| pippin | $3,493,328 | $1,762,481 | 7 | 25 | 37 / 33 / 53 / 53 |
| SPCX | $2,362,322 | $2,600,115 | -2 | 2 | 1 / 0 / 7 / 2 |
| TROLL | $1,896,267 | $1,012,414 | -10 | 69 | 57 / 59 / 65 / 67 |
| CATE | $1,352,054 | $1,126,817 | 22 | 18 | 41 / 40 / 46 / 54 |
| CLAUDIA | $432,084 | $534,328 | -15 | 82 | 60 / 67 / 73 / 85 |
| mihao | $311,838 | $887,735 | 22 | 35 | 47 / 57 / 68 / 79 |
| PUMPCHAN | $266,982 | $132,185 | 52 | 20 | 65 / 72 / 83 / 105 |
| Rizzmas | $229,860 | $115,011 | 36 | -35 | 53 / 1 / 76 / 101 |
| 67 | $216,397 | $146,499 | 95 | 104 | 147 / 199 / 216 / 244 |

**Round trip** (buy + sell, same size; n=48): median **53 bps**, p75 72, p90 105,
max 244, min -0. Old model round-trip error: mean -10 bps,
RMSE **53 bps** — about the size of the cost itself.

| Main-pool liquidity | n | Median RT | p75 RT | Max RT |
|---|---|---|---|---|
| $0k – $300k | 20 | 59 | 105 | 244 |
| $300k – $2,000k | 16 | 59 | 68 | 85 |
| $2,000k – ∞ | 12 | 6 | 37 | 53 |

**Latency drift** (5 s, n=12): mean -0.8 bps, sd **3.9 bps**, |p90| 5.6 bps.

**Linear fit** `cost = a + b·size/liquidity` per side: R² = 0.11 (buy),
0.00 (sell); RMSE 40 / 34 bps vs
43 / 36 bps for the old model. Using Jupiter's aggregate
liquidity instead helps buys somewhat (R² 0.56) but not sells.

## Findings
1. **Costs at $25–$300 are token-specific, not size-driven.** Most tokens show a flat cost
   across sizes (e.g. TROLL sells 65–73 bps at every size; Bonk buys ~25 bps *better* than mid
   at every size). A size/liquidity formula cannot capture this (R² ≈ 0).
2. **Mid prices carry per-token bias.** PUMP: −53 bps to buy, +57 to sell → the reference mid is
   off by ~55 bps; its true round trip is ~5 bps. One-sided "slippage vs mid" is unreliable;
   round trips are not.
3. **Size matters for thin tokens.** Rizzmas buys jump 33 → 117 bps from $75 → $300; token "67"
   round-trips 147–244 bps (old model: 55–105 — a 2–3× underestimate).
4. **Latency drift is small** (sd ≈ 4 bps over 5 s). The old model's adverse noise
   (half-normal, sd 40 bps per side) was ~10× too pessimistic.

## What changed in the paper engine
| | Before | After |
|---|---|---|
| Solana fill price | single-pool AMM formula | **exact-size Jupiter quote** (all routes, pool fees inside) |
| Latency noise | sd 40 bps | sd 10 bps (measured 3.9, 10 bps floor) |
| Fees on Solana fills | pool 25 + agg 10 bps + network | platform 10 bps + network (pool fee inside quote) |
| No route | n/a | **rejected** (nothing sent); a stuck sell keeps the position open |
| Quote service down | n/a | buys **rejected**; sells use model fallback, labelled `model_fallback` |
| Model fallback | AMM + 25 bps | AMM + 25 bps + **10.9 bps** (charges p75 round trip) |
| Provenance | — | every order stores `fill_basis`, route, drift, calibration id |
| Pre-trade estimate | — | `JupiterSizedQuoter.round_trip_cost_bps()` — buy then sell the received amount; mid-price free |

**Live check, same €74.75 Bonk round trip:** old model −€1.50 (−2.0%); exact quotes −€0.52
(−0.69%: pre-trade swap cost 4.2 bps, fees €0.25, a 21 bps simulated latency draw on entry and
30 s of real price movement).

## Limitations
- **Quotes are not landed fills.** MEV, failed landings and priority-fee competition can make
  real fills worse; the drift measurement does not capture adversarial ordering. Live
  reconciliation in Phase 8 is the only true calibration.
- 12 tokens, one time window. Refit regularly (`python -m atomtrader.calibration.slippage
  --save-model`); re-analyse a saved dataset with `--from FILE`.
- Buy and sell legs are quoted seconds apart, so fast-moving tokens add noise: Rizzmas's $75
  round trip of 1 bps (vs 53–101 bps at other sizes) is almost certainly such an artefact and
  was kept in the statistics rather than cherry-picked out.
- Sizes outside $25–$300 are extrapolation.
- **EVM chains (Base, BNB) remain uncalibrated** (single-pool model, labelled `uncalibrated`).
  The equivalent keyless quote source there (LI.FI) needs a wallet address for quotes; to be
  addressed when EVM strategies are built.
- Keyless Jupiter quoting is ~0.5–1 req/s: fine for ≤20 trades/day, not for scanning
  hundreds of tokens with round-trip estimates.
