# Atom
Atom is a website designed in order to offer several specialist's  insigths on the beautiful city of Granada

---

## Trading system (`atomtrader/`)

Autonomous multi-chain narrative & momentum trading system. **Paper mode only**: simulated
fills on live market data. There is no live execution provider, and live mode requires the
owner's explicit authorization.

| Document | Contents |
|---|---|
| [`docs/PHASE1_RESEARCH.md`](docs/PHASE1_RESEARCH.md) | FOMO findings, infrastructure choice, costs, security model, limitations |
| [`docs/CONNECTIVITY.md`](docs/CONNECTIVITY.md) | Live verification matrix of every data/DEX endpoint |
| [`docs/CALIBRATION.md`](docs/CALIBRATION.md) | Slippage calibration against live Jupiter quotes |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Server requirements, costs, deployment, 14-day paper protocol, operations |
| [`docs/AUDIT.md`](docs/AUDIT.md) | Independent technical audit: issues found/fixed, limitations, tests |
| [`docs/STATUS.md`](docs/STATUS.md) | Requirements map and known limitations |

### Modules

| Area | Package |
|---|---|
| Market data (DexScreener, GeckoTerminal, Jupiter, FX/ECB, base-asset registry) | `marketdata/` |
| Narrative intelligence, rotation detector, LLM classifier hook | `narrative/` |
| Market scanner, features, Moonshot score | `scanner/` |
| Technical analysis | `analysis/` |
| Token security & Insider Exit Risk | `security/` |
| Opportunity score, strategies A–E, exit engine (Normal/Momentum/Runner) | `strategy/` |
| Risk engine (authoritative), controls, daily state | `risk/` |
| Execution engine, paper provider (exact-quote fills) | `execution/` |
| Ledger / portfolio (EUR, event-sourced) | `portfolio/` |
| Autonomous orchestrator | `engine/` |
| Dashboard (separate process) | `dashboard/` |
| Notifications | `notify.py` |
| Analytics, trade review, proposals, daily report | `analytics/` |
| Backtester + walk-forward | `backtest/` |
| Persistence (Postgres/SQLite) | `persistence/` |
| Configuration & hard ceilings | `config/` |

### Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,dashboard,postgres]"
pytest                                              # unit, fixture, integration tests
python -m atomtrader.diagnostics.connectivity       # live endpoint matrix
python -m atomtrader.engine.main --scans 3          # paper engine, bounded run
ATOM_DASHBOARD_TOKEN=$(openssl rand -hex 24) python -m atomtrader.dashboard.app
docker compose up -d --build                        # full stack (see RUNBOOK)
```
