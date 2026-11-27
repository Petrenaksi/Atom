# Runbook — running Atom in paper mode

Everything here is **paper trading**: simulated fills on live market data. No wallet, key or
real order exists anywhere in the system. Live trading is not implemented and requires your
explicit authorization (see the end of this document).

## 1. Server requirements (exact)

| Item | Minimum | Recommended | Why |
|---|---|---|---|
| CPU | 2 vCPU | 4 vCPU (x86_64 or ARM64) | Engine + PostgreSQL + dashboard; TA/backtests are CPU-bound bursts |
| RAM | 4 GB | 8 GB | Python processes ~0.5–1 GB each, PostgreSQL shared buffers |
| Disk | 40 GB SSD | 80 GB SSD | Measured ≈1.1 kB per snapshot → ≈90–100 MB/day, ≈3 GB/month at default cadence (+ indexes, logs, backups) |
| OS | Ubuntu 24.04 LTS or Debian 12 | same | Docker Engine 24+ with the compose plugin |
| Network | Dedicated public IPv4, outbound HTTPS | EU region (DE/FI) | Keyless provider limits are per IP; a shared egress IP exhausts them |
| Inbound | SSH only (key auth, no passwords) | + firewall (ufw/nftables) default-deny | Dashboard is reached through an SSH tunnel, never exposed |
| Time | NTP enabled (systemd-timesyncd/chrony) | | Daily loss limit resets at local midnight (Europe/Madrid) |
| Backups | Daily `pg_dump` | + off-server copy (storage box / object storage) | Ledger, decisions and the recorded dataset are the system's memory |
| Monitoring | Dashboard heartbeat | + free uptime/cron check (e.g. a healthcheck ping) | Engine-stale alert if it stops |

No GPU, no Kubernetes, no message broker. One server runs everything.

Outbound HTTPS must reach:

| Purpose | Hosts |
|---|---|
| Market data | `api.dexscreener.com`, `api.geckoterminal.com`, `api.coingecko.com` |
| FX (ECB) | `api.frankfurter.dev` |
| DEX quotes / token lists | `lite-api.jup.ag`, `li.quest` |
| Token security | `api.rugcheck.xyz`, `api.gopluslabs.io`, `api.honeypot.is` |
| Public RPC (read-only) | `api.mainnet-beta.solana.com`, `mainnet.base.org`, `bsc-dataseed.bnbchain.org` |
| News (narratives) | `hn.algolia.com`, `api.gdeltproject.org` |
| Build/updates | Docker Hub, PyPI, GitHub |
| Optional | your webhook host; `api.anthropic.com` if the LLM classifier is enabled; Helius if added |

## 1b. Estimated monthly operating costs (EUR)

Prices change often (Hetzner raised cloud prices in 2026 and some plans have limited
availability) — confirm on the providers' pages before ordering. VAT shown at 21 % (Spain).

| Item | Lean (recommended for the 14-day paper run) | Standard (only if data limits prove binding) |
|---|---|---|
| VPS, e.g. Hetzner CAX21 (4 vCPU ARM, 8 GB, 80 GB) | ≈ €10.5–12.5 excl. VAT | same |
| Public IPv4 | ≈ €0.5–0.7 | same |
| Off-server backups (storage box / snapshot) | ≈ €2–4 | same |
| Market/security/news data | €0 (keyless) | CoinGecko API Basic ≈ $35 or Analyst ≈ $129 (higher on-chain limits) |
| Solana RPC | €0 (public) | Helius Developer ≈ $49 (10 M credits; holder polling R2) |
| Alerts webhook | €0 | €0 |
| LLM narrative classifier | €0 (off) | optional; cost depends on model and call volume — classify only new/changed narratives |
| **Total (incl. VAT)** | **≈ €16–21 / month** | **≈ €95–190 / month** |

Context on €1,000 capital: the lean setup is a ≈1.6–2.1 %/month hurdle; the standard setup
≈10–19 %/month. Only upgrade data when the paper results show the missing data is what
limits performance. Infrastructure is excluded from trading P&L in the reports.

## 2. Install (Docker — recommended)

```bash
git clone https://github.com/Petrenaksi/Atom.git && cd Atom
git checkout claude/autonomous-crypto-trading-bot-ttx0ok
cp .env.example .env
# edit .env: ATOM_DB_PASSWORD, ATOM_DASHBOARD_TOKEN (openssl rand -hex 24)
docker compose up -d --build
docker compose logs -f engine
```

Dashboard: it listens on `127.0.0.1:8080` of the server only. From your computer:
`ssh -L 8080:127.0.0.1:8080 you@server`, then open http://localhost:8080 and paste the token.

## 2b. Install (systemd, no Docker)

```bash
sudo useradd --system --create-home --home-dir /opt/atom atom
sudo -u atom git clone https://github.com/Petrenaksi/Atom.git /opt/atom/src   # or copy the tree to /opt/atom
cd /opt/atom && sudo -u atom python3 -m venv .venv
sudo -u atom .venv/bin/pip install "./src[postgres,dashboard]"
sudo apt install postgresql && sudo -u postgres createuser -P atom && sudo -u postgres createdb -O atom atom
sudo -u atom cp src/.env.example .env   # set ATOM_DATABASE_URL, ATOM_DASHBOARD_TOKEN
sudo cp src/deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now atom-engine atom-dashboard
```

## 3. First-run checks

```bash
docker compose exec engine python -m atomtrader.diagnostics.connectivity   # expect all ✅
docker compose exec engine python -m atomtrader.marketdata.base_assets      # refresh base assets (optional)
```

The engine starts with **autonomous mode OFF**: it scans, scores and logs decisions, and
entries approved by the risk engine wait (“awaiting confirmation”). Switch autonomous paper
trading on from the dashboard when you are ready.

## 4. The 14-day paper protocol

Day 0: enable autonomous mode. Do not change parameters during the first 7 days unless
something is broken; changes reset the observation.

Daily (5 minutes): read the daily report on the dashboard (or `state_kv` key
`report:<date>`). Check:
- net P&L **after fees**, number of trades, win rate, profit factor, max drawdown
- “Anomalies”: execution failures, system errors, unreconciled orders, daily-loss halts
- “Trade reviews”: false positives, late entries, premature exits, slippage problems
- proposals (never applied automatically)

Weekly:
- re-run slippage calibration on a quiet hour:
  `python -m atomtrader.calibration.slippage --tokens 12 --save-model` and compare with
  `docs/CALIBRATION.md`
- run a walk-forward over the recorded data (backtester) to compare partial-profit plans

What 14 days can and cannot tell you: it measures behaviour (does it avoid scams, respect
limits, survive outages, exit runners sensibly) and gives a first, noisy estimate of net
expectancy across whatever market conditions occurred. It is **not** proof of
profitability — and a profitable paper period alone is not a reason to go live.

Suggested minimum evidence before even discussing live: ≥ 14 days and ≥ 50 closed trades;
no unexplained anomalies; positive net expectancy after fees with a profit factor that
survives removing the single best trade; maximum drawdown within the 3 %/day design; zero
security incidents (entries into tokens later identified as honeypots/rugs).

## 5. Operations

| Situation | Action |
|---|---|
| Stop all new entries now | Dashboard → **Emergency stop** (persists across restarts; exits still run) |
| Temporarily pause | Dashboard → Pause / Resume |
| One chain misbehaving | Dashboard → chain button (pause/resume) |
| Avoid a token | Dashboard → Blacklist `chain:address` |
| Close a position | Dashboard → Close on the position row |
| Change a risk limit | Dashboard → risk field/value (hard ceilings are enforced; leverage is never allowed) |
| Engine heartbeat stale | `docker compose ps`, `docker compose logs engine`; restart: `docker compose restart engine` — state, positions and controls are restored from the database; orders with unknown outcome block trading in that token and are reported |
| Back up | `docker compose exec db pg_dump -U atom atom > atom-$(date +%F).sql` (daily cron) |
| Update | `git pull && docker compose up -d --build` |

Logs are structured JSON with secret redaction. Notifications go to the event log (shown
on the dashboard); set `ATOM_WEBHOOK_URL` to receive warnings and critical alerts in
Discord/Telegram-compatible webhooks.

## 6. What live trading would additionally require (not implemented)

1. Your explicit written authorization, plus `ATOM_MODE=live`, the exact authorization
   phrase and a live capital cap (`ATOM_LIVE_MAX_CAPITAL_EUR`) — the config refuses live
   mode otherwise.
2. A live execution provider (Jupiter Swap API v2 `/order` → sign → `/execute` on Solana;
   0x v2 on EVM) with fill reconciliation against on-chain balances.
3. A dedicated hot wallet funded only with the capped amount, its key held in an OS secret
   store or encrypted keystore on the server — never in the repository, `.env`, logs or
   dashboard; exact-amount token approvals on EVM.
4. A reconciliation period at very small size before the cap is raised by you (never
   automatically).
