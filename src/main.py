"""Entry point for running the delta-neutral perp backtest."""
from __future__ import annotations

from .dex_perp_bot.backtester import run_example_backtest


def main() -> None:
    result = run_example_backtest()
    summary = result.summary
    for key, value in summary.items():
        print(f"{key}: {value:.4f}")


if __name__ == "__main__":
    main()
