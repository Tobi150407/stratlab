# StrategyLab — always-on paper-trading & research backend

StrategyLab monitors **Gold (XAU/USD)** and the **Nasdaq-100** continuously, evaluates
strategies on every newly closed candle, and runs **simulated (paper) trades only**:
entries, stop-loss and take-profit exits, P&L and a full trade log. It also runs scheduled
research (backtests, optimisation, walk-forward validation, deterioration detection) in
the background.

> **No real money.** There is no broker connection and no code path that can place a
> real order. Every trade is simulated.

It runs on a server, not in a browser: you can close your laptop, log out, or lose your
internet connection and paper trading carries on.

## Architecture

```
            ┌──────────────┐  polls closed candles   ┌───────────────────┐
            │ market data  │ ◄────────────────────── │ engine (leader)   │──┐ paper positions,
            │ provider     │   reconnect + backoff   │ + hot standby     │  │ signals, trades
            └──────────────┘                         └───────────────────┘  │
                                                                            ▼
 ┌───────────┐ enqueue  ┌──────────────────┐ claim (SKIP LOCKED) ┌──────────────────────┐
 │ scheduler │ ───────► │  PostgreSQL       │ ◄────────────────── │ research worker(s)   │
 └───────────┘          │  (all state)      │                     │ CPU-capped container │
                        └──────────────────┘                     └──────────────────────┘
 ┌───────────┐  health checks, alerts   ▲   ▲  read-only + control
 │ watchdog  │ ─────────────────────────┘   └──────────── api + health dashboard (/)
 └───────────┘ ──► Slack/Discord webhook, email, external dead-man's switch
 ┌───────────┐
 │ backup    │ pg_dump every 6h, verified, rotated
 └───────────┘
```

| Process | What it does | Why separate |
|---|---|---|
| `engine` | Fetches candles, runs strategies, manages paper positions | Must never be delayed |
| `worker` | Backtests, optimisation, validation, reports | Heavy CPU; capped so it can't starve the engine |
| `scheduler` | Turns cron schedules (timezone-aware) into jobs | |
| `watchdog` | Detects outages, stalled strategies and data gaps; sends notifications | Independent observer |
| `api` | REST API and health dashboard | Read and control only; trading continues if it's down |
| `backup` | Database dumps | |
| `autoheal` | Restarts containers whose healthcheck fails | Recovers processes that hang |

## How the requirements are met

**Always-on (17A).** Docker `restart: always` brings services back after crashes and host
reboots. Each process writes a heartbeat (including CPU and memory) to the database every
15 seconds, and container healthchecks plus `autoheal` restart processes that hang. Logs
are JSON on stdout and rotated by Docker. The watchdog raises alerts for errors and outages.

**Continuous paper trading (17B).** See `app/engine/`. On each closed candle the engine
checks the stop-loss and take-profit, updates unrealised P&L, evaluates the strategy and
opens or closes paper positions, then records the trade and sends a notification. It keeps
running with nobody logged in.

**Automated research (17C).** See `app/research/` and `app/jobs/`. Default schedules:
daily performance (22:15 UTC), hourly deterioration check, weekly optimisation and
walk-forward validation (Saturday), and a daily strategy comparison. You can add your own
through `POST /api/schedules` with any cron expression and IANA timezone. Research runs in
a separate container, so it can't delay trading.

**Market hours (17D).** See `app/market_hours.py`.
- Gold trades Sunday 18:00 to Friday 17:00 New York time, with a daily break from 17:00 to 18:00.
- The Nasdaq cash session runs 09:30 to 16:00 New York time.
- US exchange holidays and early closes are calculated automatically, including Good Friday and the weekend-observance rules.
- Session times are set in New York time, so they stay correct when daylight saving changes.

The engine never opens a position on a stale candle or when the market is closed. Signals
rejected for either reason are stored with the reason and a notification is sent.

**Failure and recovery (17E).**
- **Data feed:** reconnects with exponential backoff and sends an alert after 3 consecutive failures.
- **Duplicates:** unique database constraints on candles and signals mean the same setup is never recorded or traded twice.
- **Leader election:** a PostgreSQL advisory lock lets only one engine trade at a time, so a doubled container can't duplicate trades.
- **Crash safety:** each candle is processed in one transaction that also moves the strategy's "last candle processed" marker. A crash rolls back or commits all of it, and after a restart the engine carries on from that marker with open positions reloaded from the database.
- **Nothing silently dropped:** every signal ends as `executed`, `rejected` or `error`, with a reason.
- **Job retries:** failed jobs are retried with backoff. If a worker dies, its job lease expires and the job is put back in the queue.

**Monitoring and notifications (17F).** The dashboard at `/` shows backend status, market
data per instrument (live, delayed, interrupted or closed), active strategies, the last
market update, the last evaluation, background jobs, failed jobs and database status.
Notifications cover new trades, closed trades, stop-loss and take-profit hits, performance
deterioration, service outages, market data interruptions and recoveries, stopped
strategies, failed jobs and failed backups. They are delivered by webhook and/or email and
retried until they go through.

**Operations (17G).** See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) and
[docs/OPERATIONS.md](docs/OPERATIONS.md).

## Quick start (local)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -q                                    # 26 tests (SQLite)
```

Full stack with Docker:

```bash
cp .env.example .env    # set POSTGRES_PASSWORD and SL_API_TOKEN
docker compose up -d --build
docker compose exec api python -m app.cli seed-demo   # optional demo strategies
open http://localhost:8000/        # health dashboard
open http://localhost:8000/docs    # API reference
```

## Adding a strategy

Subclass `StrategyBase` in `app/strategies/` and decorate it with `@register`. The same
`evaluate()` code runs in live paper trading and in backtests, so the two can't diverge.
To create and start one:

```bash
curl -X POST localhost:8000/api/strategies -H "Authorization: Bearer $SL_API_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name":"Gold SMA","kind":"sma_cross","instrument":"GOLD","timeframe":"5m","paper_active":true}'
```
