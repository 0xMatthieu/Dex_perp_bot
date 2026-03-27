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
pip install .
```

### 2. Configure environment

Copy or create a `.env` file with your credentials:

**Required:**

| Variable | Description |
|----------|-------------|
| `HYPERLIQUID_PRIVATE_KEY` | Hyperliquid wallet private key (for signing transactions) |
| `HYPERLIQUID_ADDRESS_WALLET` | EOA address connected to the Hyperliquid account |
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
