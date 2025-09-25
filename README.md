# Delta Neutral Perpetual Strategy

This repository contains a research-grade implementation of a delta-neutral
trading stack for perpetual futures. The focus is on capturing positive funding
carry while dynamically hedging spot exposure and respecting tight risk limits.

## Features

- Synthetic data generator that mimics spot and perpetual markets with
  mean-reverting funding, liquidity and basis spreads.
- Feature engineering pipeline that constructs volatility, funding and basis
  signals used by the strategy.
- Delta neutral strategy class that combines carry forecasts with leverage,
  inventory and Value-at-Risk constraints.
- Lightweight backtesting harness with summary statistics for quick iteration.

## Usage

Create a virtual environment, install the single dependency and run the example backtest:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m src.main
```

The `run_example_backtest` helper produces a synthetic equity curve and prints a
summary containing final equity, annualized Sharpe ratio, maximum drawdown and
average funding carry.

## Next steps

- Connect the strategy to a real-time data feed and execution venue.
- Extend the factor model with cross-exchange spreads and orderbook signals.
- Add comprehensive unit tests and scenario analysis for stress testing.
