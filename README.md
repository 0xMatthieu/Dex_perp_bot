# Dex Perp Bot

Delta-neutral funding rate arbitrage bot between Hyperliquid and Aster DEX perpetuals. Monitors funding rates, opens opposing positions to capture yield while remaining market-neutral, and holds across funding periods to maximize returns.

---

## How It Works

1. **Rate scanning** -- fetches predicted funding rates from both exchanges every hour
2. **Opportunity ranking** -- compares rates across all common markets, prioritizes Aster (4h funding, larger payments) over Hyperliquid (1h, hedge side)
3. **Fee-aware filtering** -- only trades when net APY exceeds a configurable threshold (default 50%) that accounts for round-trip costs
4. **Hysteresis** -- won't rebalance to a new asset unless the improvement exceeds a threshold (default 20% APY), preventing churn
5. **Execution** -- opens opposing positions (long on one exchange, short on the other) using a maker-taker strategy: post-only limit first, market fallback
6. **Holding** -- keeps positions across multiple funding periods as long as the opportunity remains favorable
7. **Safety** -- partial fill rollback closes one-sided positions to prevent unhedged exposure

---

## Setup

### Prerequisites

- Python 3.11+
- Hyperliquid and Aster API credentials

### 1. Install dependencies

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
# or: pip install .
```

Hyperliquid is accessed through `ccxt.hyperliquid` (the standalone `hyperliquid` PyPI package is not used: its bundled ccxt fork crashes in `load_markets` on some spot listings).

### 2. Configure environment

Copy or create a `.env` file with your credentials:

**Required:**

| Variable | Description |
|----------|-------------|
| `HYPERLIQUID_PRIVATE_KEY` | Hyperliquid wallet private key (for signing transactions) |
| `HYPERLIQUID_ADDRESS_WALLET` | Address of the Hyperliquid **account that holds the funds** (the master address, not the API wallet's own address). The private key may belong to an API wallet authorized on that account |
| `ASTER_API_KEY` | Aster API key |
| `ASTER_API_SECRET` | Aster API secret |

**Strategy parameters (optional, sensible defaults):**

| Variable | Default | Description |
|----------|---------|-------------|
| `STRATEGY_LEVERAGE` | `4` | Trading leverage |
| `STRATEGY_CAPITAL_ALLOCATION_PCT` | `0.9` | Fraction of min(HL, Aster) balance to deploy |
| `STRATEGY_MIN_APY_DIFF_PCT` | `50` | Minimum APY difference to enter a trade |
| `STRATEGY_SPREAD_TICKS` | `1` | Ticks away from mid for limit orders |
| `STRATEGY_REBALANCE_HYSTERESIS_PCT` | `20` | APY improvement required to switch assets |
| `STRATEGY_ROUND_TRIP_COST_BPS` | `25` | Estimated round-trip cost in basis points |

**Aster API (optional, rarely needed):**

| Variable | Default | Description |
|----------|---------|-------------|
| `ASTER_BASE_URL` | `https://fapi.asterdex.com` | Aster API base URL |
| `ASTER_BALANCE_ENDPOINT` | `/fapi/v4/account` | Balance query endpoint |
| `ASTER_AVAILABLE_FIELDS` | `availableBalance,maxWithdrawAmount,totalMarginBalance` | Fields for available balance |
| `ASTER_TOTAL_FIELDS` | `totalMarginBalance,totalWalletBalance` | Fields for total balance |
| `ASTER_TIMEOUT` | `10` | Request timeout in seconds |

**Notifications (optional):**

| Variable | Default | Description |
|----------|---------|-------------|
| `DISCORD_WEBHOOK_URL` | *(disabled)* | Discord webhook URL for trade/error notifications |

### 3. Run the bot

```bash
python -m src.dex_perp_bot.main
```

The bot runs continuously. Each hour (minutes 5-55 UTC), it checks for opportunities and acts. Press `Ctrl+C` to stop.

---

## Execution: spread capture

Entries and exits are not blind limit orders any more. Four deterministic signals (all in `microstructure.py`, unit-tested in `tests/test_microstructure.py`) drive every trade, and every input and outcome is written to `logs/decisions.jsonl` so you can see what worked:

| Concept | Where | What it does |
|---------|-------|--------------|
| **Fee break-even gate** | `strategy.evaluate_entry_gate` | Every scan row carries two APYs: **next-hour** (an Aster settlement due within the hour counts its whole 1h/4h/8h payment; this ranks candidates) and **steady** (the Aster payment spread over its interval; what every later hour pays). `breakeven_hours`: the first hour repays at the next-hour rate, the remainder at the steady rate, against `fees + hedge crossing − expected basis reversion`. Skip the entry if longer than `EXEC_MAX_BREAKEVEN_HOURS`. A |basis| above `EXEC_MAX_ABS_BASIS_BPS` is rejected as `basis_out_of_range` (same ticker, different contract on the two venues, e.g. MEME) and never enters the basis history; coins Hyperliquid flags `isDelisted` are dropped from the scan. Switching positions must repay a full round trip over `EXEC_EXPECTED_HOLD_HOURS`. |
| **Basis z-score** | `basis.BasisTracker`, `strategy.check_basis_exit` | Aster-vs-HL mid basis sampled every 30 s for the watchlist. z-score vs the rolling window feeds the gate (cheap side = enter, rich side = skip) and the **basis exit**: close when the basis moved in our favour by more than exit fees + margin and is now stretched against us (`favorable_z <= -EXEC_Z_EXIT`). |
| **Order-book imbalance** | `execution.execute_pair` → `microstructure.plan_leg` | Top-5 `(bid−ask)/(bid+ask)`. If the book is pushing against a passive order (e.g. buying while imbalance ≥ +0.3), cross immediately instead of resting. |
| **Queue position** | same | Quantity resting at the touch ÷ aggressive flow rate from recent trades = expected wait. Longer than `EXEC_PASSIVE_MAX_WAIT_S` → cross. Otherwise post-only at the touch, and cross the remainder when the deadline passes. |

**Sequencing (passive on the wide book, cross the tight one).** The leg on the venue with the wider half-spread is the *anchor*: it rests post-only at the touch (re-posted when the touch moves away) for up to `EXEC_ANCHOR_MAX_WAIT_S`. Each time the anchor fills, the other leg (*hedge*) is crossed on the tighter book with an IOC limit (slippage capped at `EXEC_CROSS_CAP_BPS`), so the naked exposure lasts seconds and the taker cost is about half a tight spread plus the taker fee. A book whose half-spread exceeds `EXEC_MAX_CROSS_HALF_SPREAD_BPS` is never crossed (except to hedge); on a wide book the taker pays the spread, which dwarfs any timing edge. The anchor does not start at the touch: it **ladders in** from `EXEC_ANCHOR_START_OFFSET_BPS` beyond the touch (a better price for us) down to the touch in `EXEC_ANCHOR_STEPS` equal time slices over the time left in the trading window, so a favourable wobble in price is captured as extra edge while the last slice still gives a plain at-the-touch fill a chance. If the anchor never fills, the entry is abandoned without paying anything. Post-only orders (`Alo` on Hyperliquid, `GTX` on Aster) are rejected rather than filled as taker, so a maker leg can never pay taker fees by accident; the log records `est_fee_bps` and `maker_fraction` per leg.

Decision log kinds: `scan`, `gate`, `leg_plan`, `leg_order`, `leg_fill` (planned vs final tactic, wait, slippage vs mid at decision), `pair_result`, `basis_exit`. The dashboard's *Execution & signals* panel aggregates fill rate and average slippage per planned tactic, gate outcomes and basis exits.

**Execution parameters (optional):**

| Variable | Default | Description |
|----------|---------|-------------|
| `FEE_HL_MAKER_BPS` / `FEE_HL_TAKER_BPS` | `1.5` / `4.5` | Hyperliquid fees per fill (bps) |
| `FEE_ASTER_MAKER_BPS` / `FEE_ASTER_TAKER_BPS` | `0.0` / `4.0` | Aster fees per fill (bps, both observed live: maker fills were charged 0) |
| `EXEC_MAX_BREAKEVEN_HOURS` | `8` | Skip entries that need longer to repay costs |
| `EXEC_MAX_ABS_BASIS_BPS` | `300` | Skip when the Aster-vs-HL mid basis is wider than this (mismatched contracts) |
| `EXEC_EXPECTED_HOLD_HOURS` | `8` | Horizon used to value a switch |
| `EXEC_BASIS_WINDOW_HOURS` / `EXEC_BASIS_MIN_SAMPLES` | `6` / `60` | Rolling window for basis mean/std |
| `EXEC_Z_EXIT` / `EXEC_BASIS_EXIT_MIN_GAIN_BPS` | `1.5` / `5` | Basis exit trigger |
| `EXEC_IMBALANCE_THRESHOLD` | `0.3` | Cross when the book pushes against the passive side |
| `EXEC_PASSIVE_MAX_WAIT_S` / `EXEC_HEDGE_MAX_WAIT_S` | `90` / `15` | Passive patience, and patience once naked |
| `EXEC_CROSS_CAP_BPS` | `20` | Slippage cap on crossing IOC orders |
| `EXEC_MAX_CROSS_HALF_SPREAD_BPS` | `3` | Never cross a book wider than this (except to hedge) |
| `EXEC_ANCHOR_MAX_WAIT_S` | `2400` | Patience for the passive anchor leg (also capped by the time left in the trading window) |
| `EXEC_ANCHOR_START_OFFSET_BPS` / `EXEC_ANCHOR_STEPS` / `EXEC_ANCHOR_MIN_EDGE_BPS` | `8` / `4` / `3` | Ladder: anchor starts this far beyond the touch and tightens in equal time steps down to `MIN_EDGE` bps from mid (inside the spread on wide books) |
| `EXEC_REPOST_MIN_INTERVAL_S` | `10` | Minimum time between anchor re-posts when the touch or the ladder moves |
| `EXEC_SAMPLE_INTERVAL_S` | `30` | Basis sampler cadence |

---

## Dashboard

A zero-dependency web dashboard (Python stdlib only) shows balances, open positions, realized P&L (24h / 7d / 30d per venue: funding, trading, fees), the last funding scan, the trade report and a live tail of the bot log.

```bash
python -m src.dex_perp_bot.dashboard          # http://<host>:8765/
DASHBOARD_PORT=9000 python -m src.dex_perp_bot.dashboard
```

It only reads `logs/status.json` (refreshed by the bot every 5 minutes), `logs/trades.md` and the latest `logs/bot_*.log`. It never touches the exchange APIs or `.env`, so it is safe to expose on the LAN. Do not expose it to the internet: there is no authentication.

### Safety switch

Three buttons in the dashboard header write `logs/control.json`; the bot polls it every 30 seconds:

| Button | Effect |
|--------|--------|
| **Pause trading** | No new trades or rebalances. Open positions are held. |
| **Close all & pause** | Bot cancels all orders and closes all positions on both venues (post-only first, market fallback), then switches itself to *pause*. |
| **Resume** | Back to normal trading. |

The switch survives bot restarts (it is a file). A flatten request is picked up within ~30 s while the bot is idle, but not while a rebalance is already executing (up to the end of the trading window). `Pause` does not stop the process: `sudo systemctl stop dexbot` does, and leaves positions open.

---

## Running 24/7 on the Jetson Nano

The bot is deployed on the Jetson Nano at `192.168.1.19` (SSH port 1988, user `matthieu`) in `~/Dex_perp_bot`, with Python 3.12 managed by `uv` (`~/.local/bin/uv`). Two systemd units live in `deploy/` and are installed in `/etc/systemd/system/`:

| Unit | What | Enabled |
|------|------|---------|
| `dexdash.service` | Dashboard on http://192.168.1.19:8765/ | yes, starts at boot |
| `dexbot.service` | The trading bot | installed, **start it manually** (needs `~/Dex_perp_bot/.env`) |

```bash
# from the dev machine: copy credentials (never committed)
scp -P 1988 .env matthieu@192.168.1.19:~/Dex_perp_bot/.env

# on the Jetson
sudo systemctl enable --now dexbot.service     # start + start at boot
systemctl status dexbot dexdash                 # health
journalctl -u dexbot -f                         # live log (also logs/bot_*.log)
sudo systemctl stop dexbot.service              # stop trading (positions stay open!)
```

To redeploy after code changes: copy `src/`, `requirements.txt` and `pyproject.toml` over, then `uv pip install --python .venv/bin/python -r requirements.txt` and `sudo systemctl restart dexbot dexdash`.

---

## Strategy Details

- **Aster first** -- Aster funding (every 4h) is the primary revenue source. Hyperliquid (every 1h) serves as the hedge.
- **Net APY** -- the bot calculates the net APY across both legs: funding received minus funding paid on the hedge side.
- **Maker-taker execution** -- tries a post-only limit order first (lower fees), falls back to market if it would cross the book.
- **Position holding** -- once in a position, the bot holds across multiple funding periods. It only closes when a significantly better opportunity appears or the rate flips.

---

## Trade Report

All trades are logged to `logs/trades.md` as a markdown report with daily summary tables:

```
## 2026-03-27

| Metric | Value |
|--------|-------|
| Trades | 2 opens, 2 closes |
| Rebalances | ~1 |
| Symbols | ETH |
| Open notional | $9,543.02 |
| Close notional | $9,550.51 |

### Trades

- `08:05:12` **OPEN ETH** BUY on **Aster** ...
```

---

## Discord Notifications

Set `DISCORD_WEBHOOK_URL` to receive notifications on:
- Position opened/closed
- Holding (already in optimal position)
- No opportunity found
- Partial fill rollback
- Errors
- Bot start/stop

If the variable is empty or unset, notifications are silently disabled.

---

## Architecture

```
src/dex_perp_bot/
  main.py              Entry point: hourly loop, trading window scheduling
  config.py            Environment config, credentials, strategy parameters
  funding.py           Funding rate fetching, APY calculation, opportunity comparison
  strategy.py          Delta-neutral strategy, rebalancing, execution, cleanup
  trade_log.py         Markdown trade report generator
  notifier.py          Optional Discord webhook notifications
  exchanges/
    base.py            Shared models (WalletBalance) and exceptions
    hyperliquid.py     Hyperliquid connector (CCXT/SDK)
    aster.py           Aster connector (Binance-style HTTP + HMAC signing)
tests/
  test.py              Integration tests for funding, orders, and wallet balance
```

## Testing

```bash
python tests/test.py
```

Runs integration tests against live exchanges. Use with caution -- it places real orders.

---

## Security Notes

- **Private keys** are loaded from `.env` and never logged. Add `.env` to `.gitignore`.
- **Stablecoin risk** -- Hyperliquid uses USDC, Aster uses USDT. A depeg of either creates hidden directional exposure.
