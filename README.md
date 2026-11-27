# Atom
Atom is a website designed in order to offer several specialist's  insigths on the beautiful city of Granada

---

## Trading system (`atomtrader/`)

Autonomous multi-chain narrative & momentum trading system. **Paper mode only** — no live
execution provider exists, and live mode requires explicit owner authorization.

- Phase 1 research, infrastructure choice and architecture: [`docs/PHASE1_RESEARCH.md`](docs/PHASE1_RESEARCH.md)
- Live endpoint verification: [`docs/CONNECTIVITY.md`](docs/CONNECTIVITY.md)
- Implemented so far: config with hard risk ceilings, EUR/Decimal accounting, secret-redacting
  logs, authoritative risk engine, paper execution (AMM impact, fees, failures, slippage
  reverts), event-sourced ledger, idempotent order persistence with restart reconciliation,
  market-data ingestion (DexScreener, GeckoTerminal, FX with ECB cross-check), base-asset
  registry against fake liquidity, and a paper-trading session with persisted controls.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                                            # unit + recorded-fixture tests
python -m atomtrader.diagnostics.connectivity     # live connectivity matrix
python -m atomtrader.paper.runner --cycles 3      # paper mode: ingest + mark-to-market
```
