# Autonomous Wheel Trader

Multi-tenant, fully-autonomous options/equity trader built on
**LangGraph + Claude + Alpaca**, persisted in **SQL Server Express**.

Every operational parameter — stop-loss, delta band, volume threshold, loop
interval, LLM model, Alpaca endpoints — lives in the database and is editable
from the Settings UI. No hardcoded values in agent code.

## Architecture

```
        Alpaca Market Data / Account API
                       │
                       ▼
              Multi-Tenant Runner
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
  Tenant Worker   Tenant Worker  Tenant Worker
  (isolated)      (isolated)     (isolated)
        │
        ▼
   LangGraph cycle (one per tenant):
   Scanner → Strategist (Claude) → Guardrail → Executor → loop
```

* **Scanner** — Pulls Alpaca snapshots, filters by configured price band,
  volume threshold, and percent-change floor.
* **Strategist (Claude)** — Selects CSP / Covered Call contracts inside the
  configured delta envelope, requests approval from the LLM.
* **Guardrail** — Deterministic: enforces stop-loss (configurable per project)
  and option collateral cap on every cycle.
* **Executor** — Submits Alpaca orders, persists state, sets
  `execution_status=TRADE_COMPLETED` so the worker loops back to Scanner.

## Quick start (local)

### 1. Prereqs
* SQL Server **Express 2022** (or any LocalDB / Express install).
* Microsoft **ODBC Driver 17 or 18 for SQL Server**.
* Python 3.12+

### 2. Install
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
# Edit .env: set DB_SERVER, DB_NAME, Trusted_Connection or DB_USER/DB_PASSWORD
```

Generate an encryption key for at-rest Alpaca secrets:
```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
Put it in `.env` as `SECRET_ENCRYPTION_KEY=...`.

### 3. Bootstrap database
```powershell
python main.py initdb
```

### 4. Run
```powershell
python main.py            # API + autonomous runner together
python main.py api        # API only
python main.py runner     # runner only
```

Open `http://127.0.0.1:8000/dashboard` and:
1. **Global Settings** → set `anthropic_api_key`, `anthropic_model`, loop
   interval, market-hours toggle.
2. **+ New Project** → enter Alpaca paper-trading keys, allocation, etc.
3. Click into the project to tune per-project risk and strategy parameters
   (stop-loss, delta band, volume threshold, dry-run flag).

The runner reconciles active projects every 15 seconds — toggling
`is_active` adds/removes tenants live.

## Docker

```bash
docker compose up --build
```
Spins up a SQL Server 2022 (Express edition) container plus the trader app.
UI on `http://localhost:8000/dashboard`.

## Configurable parameter reference

### Global (app_settings)
| Key                       | Type   | Purpose                                                |
|---------------------------|--------|--------------------------------------------------------|
| anthropic_api_key         | secret | Claude API key                                         |
| anthropic_model           | string | Claude model id                                        |
| anthropic_temperature     | float  | LLM temperature                                        |
| anthropic_max_tokens      | int    | Max completion tokens                                  |
| loop_interval_seconds     | int    | Default cycle interval (per-project can override)      |
| market_hours_only         | bool   | Only run when US market open                           |
| max_concurrent_tenants    | int    | Cap on parallel tenant workers                         |
| log_level                 | string | INFO / DEBUG / WARNING                                 |

### Per-project (project_settings)
| Key                     | Type  | Purpose                                       |
|-------------------------|-------|-----------------------------------------------|
| stop_loss_dollars       | float | Equity liquidation threshold                  |
| volume_threshold        | int   | Min average daily volume to consider          |
| scanner_top_n           | int   | Top movers to evaluate                        |
| scanner_min_price       | float | Min share price                               |
| scanner_max_price       | float | Max share price                               |
| scanner_min_pct_change  | float | Min absolute percent change                   |
| csp_delta_min/max       | float | CSP target delta envelope                     |
| csp_min_dte / max_dte   | int   | CSP days-to-expiration window                 |
| cc_delta_min/max        | float | Covered call delta envelope                   |
| max_open_positions      | int   | Cap on concurrent equity positions            |
| max_open_contracts      | int   | Cap on concurrent option contracts            |
| max_collateral_pct      | float | Buying-power fraction available for options   |
| loop_interval_seconds   | int   | Per-project cycle override                    |
| dry_run                 | bool  | Log decisions without submitting orders       |

## Safety notes

* **Default endpoint is Alpaca paper.** Live trading is only enabled if you
  explicitly change `alpaca_base_url` for that project.
* **dry_run defaults to true** for every project until you flip it off.
* **Encryption at rest** — set `SECRET_ENCRYPTION_KEY` so Alpaca keys are
  Fernet-encrypted in `trading_projects`.
* All decisions are logged to `agent_events` for full audit trail.
