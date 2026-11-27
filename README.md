# Atom
Atom is a website designed in order to offer several specialist's  insigths on the beautiful city of Granada

---

## Trading system (`atomtrader/`)

Autonomous multi-chain narrative & momentum trading system. **Paper mode only** — no live
execution provider exists, and live mode requires explicit owner authorization.

- Phase 1 research, infrastructure choice and architecture: [`docs/PHASE1_RESEARCH.md`](docs/PHASE1_RESEARCH.md)
- Implemented so far: config with hard risk ceilings, EUR/Decimal accounting, secret-redacting
  logs, authoritative risk engine, paper execution (AMM impact, fees, failures, slippage
  reverts), event-sourced ledger, idempotent order persistence with restart reconciliation.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```
