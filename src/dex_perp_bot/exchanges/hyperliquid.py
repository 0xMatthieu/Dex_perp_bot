"""Hyperliquid exchange connector."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Callable, Dict, List, Tuple

from hyperliquid import HyperliquidSync

from .base import BalanceParsingError, DexAPIError, WalletBalance, to_decimal
from ..config import HyperliquidCredentials


ClientFactory = Callable[[Dict[str, Any]], Any]

logger = logging.getLogger(__name__)


class HyperliquidClient:
    """Wrapper around the official Hyperliquid CCXT connector."""

    def __init__(
        self,
        credentials: HyperliquidCredentials,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._credentials = credentials
        factory = HyperliquidSync
        self._client = factory(
            {
                "privateKey": credentials.private_key,
                "walletAddress": credentials.wallet_address,
            }
        )

    def get_wallet_balance(self) -> WalletBalance:
        """Return the wallet balance reported by Hyperliquid."""

        try:
            balance = self._client.fetch_balance()
        except Exception as exc:  # pragma: no cover - defensive
            raise DexAPIError("Failed to fetch Hyperliquid balance") from exc

        usdc = balance.get("USDC", {}) if isinstance(balance, dict) else {}
        total = to_decimal(usdc.get("total")) if isinstance(usdc, dict) else None
        available = to_decimal(usdc.get("free")) if isinstance(usdc, dict) else None

        if total is None and available is None:
            raise BalanceParsingError("Hyperliquid balance response missing USDC totals")

        return WalletBalance(total=total, available=available, raw=balance)

    def get_predicted_funding_rates(self) -> List[Tuple[str, List[Tuple[str, Dict[str, Any]]]]]:
        """Retrieve predicted funding rates for different venues."""
        try:
            # The underlying CCXT client exposes public POST methods.
            rates = self._client.publicPostInfo({"type": "predictedFundings"})
        except Exception as exc:  # pragma: no cover - defensive
            raise DexAPIError("Failed to fetch Hyperliquid predicted funding rates") from exc
        return rates

    def create_order(
        self,
        side: str,
        order_type: str,
        leverage: int,
        margin_usd: float,
    ) -> Dict[str, Any]:
        """Create an order on Hyperliquid for BTC."""
        symbol = "BTC/USDC:USDC"

        # 1. Get current price
        logger.info("Fetching order book for %s to get current price", symbol)
        try:
            order_book = self._client.fetch_order_book(symbol)
            if not order_book.get("bids") or not order_book.get("asks"):
                raise DexAPIError(f"Order book for {symbol} is empty, cannot determine price")
            best_bid = order_book["bids"][0][0]
            best_ask = order_book["asks"][0][0]
            current_price = (Decimal(str(best_bid)) + Decimal(str(best_ask))) / 2
        except Exception as exc:
            raise DexAPIError(f"Failed to fetch price for {symbol}") from exc

        logger.info("Current mid-price for %s is %s", symbol, current_price)

        # 2. Compute order quantity
        qty = (Decimal(str(margin_usd)) * Decimal(leverage)) / current_price
        qty_float = float(qty)

        # 3. Set leverage
        logger.info("Setting leverage for %s to %sx", symbol, leverage)
        try:
            self._client.set_leverage(leverage, symbol)
        except Exception as exc:
            raise DexAPIError(f"Failed to set leverage for {symbol}") from exc

        # 4. Place order
        logger.info("Placing %s %s order for %s of %s", order_type, side, qty_float, symbol)
        # For market orders, ccxt hyperliquid implementation needs a price for slippage calculation.
        price_for_order = float(current_price)
        if order_type.upper() == "LIMIT":
            # For testing cancellation, place the order far from the current price
            # to ensure it is not filled immediately.
            if side.upper() == "BUY":
                limit_price = current_price * Decimal("0.8")
            else:
                limit_price = current_price * Decimal("1.2")
            price_for_order = float(limit_price)
            logger.info("Using price %s for LIMIT order", price_for_order)
        elif order_type.upper() == "MARKET":
            logger.info("Using price %s for MARKET order slippage calculation", price_for_order)

        try:
            order_response = self._client.create_order(
                symbol=symbol,
                type=order_type.lower(),
                side=side.lower(),
                amount=qty_float,
                price=price_for_order,
            )
        except Exception as exc:
            raise DexAPIError("Failed to create Hyperliquid order") from exc

        print("Final order payload sent to Hyperliquid via ccxt:", {
            "symbol": symbol, "type": order_type.lower(), "side": side.lower(),
            "amount": qty_float, "price": price_for_order,
        })
        return order_response

    def cancel_order(
        self,
        symbol: str,
        order_id: str,
    ) -> Dict[str, Any]:
        """Cancel an active order on Hyperliquid."""
        logger.info("Canceling order %s for %s", order_id, symbol)
        try:
            return self._client.cancel_order(id=order_id, symbol=symbol)
        except Exception as exc:
            raise DexAPIError(f"Failed to cancel Hyperliquid order {order_id}") from exc

    def close_position(self, symbol: str) -> Dict[str, Any]:
        """Close an open position for a given symbol on Hyperliquid."""
        logger.info("Attempting to close position for %s", symbol)

        # 1. Fetch current position
        try:
            position = self._client.fetch_position(symbol)
        except Exception as exc:
            logger.info("Could not fetch position for %s, assuming none is open. Error: %s", symbol, exc)
            return {"status": "no_position", "reason": str(exc)}

        position_size = to_decimal(position.get("contracts"))
        if not position_size or position_size.is_zero():
            logger.info("No open position found for %s", symbol)
            return {"status": "no_position"}

        side = position.get("side")
        if side not in ("long", "short"):
            raise DexAPIError(f"Unknown position side '{side}' for {symbol}")

        close_side = "sell" if side == "long" else "buy"
        size_to_close = float(position_size)

        # 2. Get current price for market order slippage calculation
        logger.info("Fetching order book for %s to get price for closing order", symbol)
        try:
            order_book = self._client.fetch_order_book(symbol)
            if not order_book.get("bids") or not order_book.get("asks"):
                raise DexAPIError(f"Order book for {symbol} is empty, cannot determine price")
            best_bid = order_book["bids"][0][0]
            best_ask = order_book["asks"][0][0]
            current_price = (Decimal(str(best_bid)) + Decimal(str(best_ask))) / 2
        except Exception as exc:
            raise DexAPIError(f"Failed to fetch price for {symbol}") from exc

        # 3. Place a reduce-only market order to close the position
        logger.info(
            "Placing reduce-only MARKET %s order for %s of %s to close position",
            close_side, size_to_close, symbol
        )
        try:
            order_response = self._client.create_order(
                symbol=symbol,
                type="market",
                side=close_side.lower(),
                amount=size_to_close,
                price=float(current_price),
                params={"reduceOnly": True},
            )
        except Exception as exc:
            raise DexAPIError(f"Failed to place closing order for {symbol}") from exc

        return order_response

