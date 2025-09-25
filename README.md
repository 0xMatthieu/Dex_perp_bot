# Dex Perp Bot

This repository contains the initial plumbing required to build a delta-neutral strategy that trades perpetual futures across Hyperliquid and Aster. The focus of this iteration is establishing authenticated connectivity to both venues and retrieving wallet balances so the strategy can reason about available capital.

## Project layout

```
src/
  dex_perp_bot/
    config.py            # Environment-driven configuration and secrets loading
    exchanges/
      base.py            # Shared exchange models and exceptions
      hyperliquid.py     # Hyperliquid connector built on the official SDK
      aster.py           # HTTP connector for Aster
    main.py              # Example entry point that prints wallet balances
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
| `HYPERLIQUID_API_KEY` | Hyperliquid API key |
| `HYPERLIQUID_API_SECRET` | Hyperliquid API secret |
| `HYPERLIQUID_WALLET_ADDRESS` | EOA address connected to the Hyperliquid account |
| `ASTER_API_KEY` | Aster API key |
| `ASTER_API_SECRET` | Aster API secret |
| `ASTER_ACCOUNT_ID` | The Aster account identifier (wallet address or account number) |
| `ASTER_BASE_URL` | Base URL for the Aster REST API (for example `https://api.aster.fi`) |
| `ASTER_BALANCE_ENDPOINT` | Optional endpoint path for the balance query (defaults to `/v1/perp/account-summary`) |
| `ASTER_RESPONSE_PATH` | Optional dotted path that points to the JSON object containing balance fields (defaults to `data.account`) |
| `ASTER_AVAILABLE_FIELDS` | Optional comma-separated list of keys that contain the available balance |
| `ASTER_TOTAL_FIELDS` | Optional comma-separated list of keys that contain the total balance |

Configuration is loaded from the process environment. A `.env` file can be used during development.

## Usage

Once credentials are configured, run the balance check entry point:

```bash
python -m dex_perp_bot.main
```

The script prints a structured summary with the total and available collateral reported by each venue.

## Testing

```bash
pytest
```

This runs unit tests that exercise configuration loading and the exchange connectors via HTTP mocks.

