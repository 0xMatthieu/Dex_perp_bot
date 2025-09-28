# Dex Perp Bot

This bot implements a delta-neutral funding rate arbitrage strategy between Hyperliquid and Aster perpetuals exchanges. It is designed to run continuously, identify imminent funding rate opportunities, and rebalance the portfolio to capture the funding yield while remaining market-neutral.

The core logic is to:
1.  Continuously monitor funding rates on both exchanges for all common markets.
2.  Identify opportunities where funding is imminent (e.g., within 5 minutes) and the APY difference is profitable.
3.  Dynamically determine the maximum safe leverage based on exchange limits for the target asset.
4.  Check the current portfolio. If not already in the optimal position, it will close all existing positions and open a new delta-neutral position (long on one venue, short on the other) to capture the best funding rate.
5.  Hold positions between funding events to farm potential airdrop points.

## Project layout

```
src/
  dex_perp_bot/
    config.py            # Environment-driven configuration and secrets loading
    funding.py           # Logic for fetching and comparing funding rates
    strategy.py          # Core delta-neutral strategy, rebalancing, and execution logic
    exchanges/
      base.py            # Shared exchange models and exceptions
      hyperliquid.py     # Hyperliquid connector built on the official SDK
      aster.py           # HTTP connector for Aster
    main.py              # Main entry point to run the strategy loop
tests/
  test.py                # Integration tests for funding, orders, and wallet balance
```

## Requirements

* Python 3.11+
* Hyperliquid and Aster API credentials exported as environment variables or stored in a `.env` file.

Install the runtime dependencies using `pip`:

```bash
pip install .
```

To develop or run the tests install the optional dev dependencies as well:

```bash
pip install .[dev]
```

## Configuration

The application expects the following environment variables:

| Variable | Description |
|----------|-------------|
| `HYPERLIQUID_PRIVATE_KEY` | Hyperliquid wallet private key (for signing transactions) |
| `HYPERLIQUID_ADDRESS_WALLET` | EOA address connected to the Hyperliquid account |
| `ASTER_API_KEY` | Aster API key |
| `ASTER_API_SECRET` | Aster API secret |
| `ASTER_BASE_URL` | Base URL for the Aster REST API (e.g., `https://fapi.asterdex.com`) |

The following variables for Aster are optional and have sensible defaults:

| Variable | Description |
|----------|-------------|
| `ASTER_BALANCE_ENDPOINT` | Optional endpoint path for the balance query (defaults to `/fapi/v4/account`) |
| `ASTER_RESPONSE_PATH` | Optional dotted path that points to the JSON object containing balance fields (defaults to the root object) |
| `ASTER_AVAILABLE_FIELDS` | Optional comma-separated list of keys for available balance (defaults to `availableBalance,maxWithdrawAmount,totalMarginBalance`) |
| `ASTER_TOTAL_FIELDS` | Optional comma-separated list of keys for total balance (defaults to `totalWalletBalance,totalMarginBalance`) |

Configuration is loaded from the process environment. A `.env` file can be used during development.

## Usage

Once credentials are configured, run the main strategy loop:

```bash
python -m src.dex_perp_bot.main
```

The bot will run continuously, checking for opportunities at a defined interval (currently 10 seconds for testing). Press `Ctrl+C` to stop the bot.

## Testing

The project includes an integration test suite that can be used to verify individual components of the strategy.

To run the funding rate comparison test:
```bash
python tests/test.py
```

To run unit tests (if any):
```bash
pytest
```

