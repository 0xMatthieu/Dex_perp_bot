"""Hyperliquid exchange connector."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

import ccxt

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
        factory = client_factory or ccxt.hyperliquid
        self._client = factory(
            {
                "privateKey": credentials.private_key,
                "walletAddress": credentials.wallet_address,
            }
        )
        try:
            logger.info("Loading markets for Hyperliquid...")
            self._client.load_markets()
            logger.info("Hyperliquid markets loaded.")
        except Exception as exc:
            raise DexAPIError("Failed to load Hyperliquid markets") from exc

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

    def get_all_open_orders(self) -> List[Dict[str, Any]]:
        """Fetch all open orders."""
        logger.debug("Fetching all open orders from Hyperliquid")
        try:
            # Returns a list of ccxt order structures
            return self._client.fetch_open_orders()
        except Exception as exc:
            raise DexAPIError("Failed to fetch Hyperliquid open orders") from exc

    def get_all_positions(self) -> List[Dict[str, Any]]:
        """Fetch all open positions."""
        logger.debug("Fetching all open positions from Hyperliquid")
        try:
            positions = self._client.fetch_positions()
            # The ccxt method returns a list of position structures.
            # Filter for positions that are actually open (non-zero contracts).
            open_positions = [p for p in positions if to_decimal(p.get("contracts")) and not to_decimal(p.get("contracts")).is_zero()]
            return open_positions
        except Exception as exc:
            raise DexAPIError("Failed to fetch Hyperliquid positions") from exc

    def get_predicted_funding_rates(self) -> List[Tuple[str, List[Tuple[str, Dict[str, Any]]]]]:
        """Retrieve predicted funding rates for different venues."""
        try:
            # The underlying CCXT client exposes public POST methods.
            rates = self._client.publicPostInfo({"type": "predictedFundings"})
        except Exception as exc:  # pragma: no cover - defensive
            raise DexAPIError("Failed to fetch Hyperliquid predicted funding rates") from exc
        return rates

    def get_delisted_coins(self) -> set:
        """Coin names flagged ``isDelisted`` in the perp universe (still present in predictedFundings)."""
        try:
            meta = self._client.publicPostInfo({"type": "meta"})
        except Exception as exc:  # pragma: no cover - defensive
            raise DexAPIError("Failed to fetch Hyperliquid meta") from exc
        universe = meta.get("universe", []) if isinstance(meta, dict) else []
        return {u.get("name") for u in universe if isinstance(u, dict) and u.get("isDelisted")}

    def get_funding_history(self, start_time_ms: int) -> List[Dict[str, Any]]:
        """Funding payments credited/debited to the wallet since start_time_ms."""
        try:
            return self._client.publicPostInfo({
                "type": "userFunding",
                "user": self._credentials.wallet_address,
                "startTime": start_time_ms,
            }) or []
        except Exception as exc:
            raise DexAPIError("Failed to fetch Hyperliquid funding history") from exc

    def get_fills(self, start_time_ms: int) -> List[Dict[str, Any]]:
        """Fills (with closedPnl and fee) since start_time_ms."""
        try:
            return self._client.publicPostInfo({
                "type": "userFillsByTime",
                "user": self._credentials.wallet_address,
                "startTime": start_time_ms,
            }) or []
        except Exception as exc:
            raise DexAPIError("Failed to fetch Hyperliquid fills") from exc

    # ------------------------------------------------------------------
    # Execution primitives (shared shape with AsterClient)
    # ------------------------------------------------------------------
    venue_name = "Hyperliquid"

    def get_book(self, symbol: str, depth: int = 5) -> Dict[str, List[Tuple[Decimal, Decimal]]]:
        try:
            raw = self._client.fetch_order_book(symbol)
        except Exception as exc:
            raise DexAPIError(f"Failed to fetch Hyperliquid order book for {symbol}") from exc
        return {
            "bids": [(Decimal(str(p)), Decimal(str(q))) for p, q, *_ in raw.get("bids", [])[:depth]],
            "asks": [(Decimal(str(p)), Decimal(str(q))) for p, q, *_ in raw.get("asks", [])[:depth]],
        }

    def get_recent_trades(self, symbol: str, limit: int = 100) -> List[Tuple[float, Decimal, Decimal, str]]:
        """Public market trades via info/recentTrades (ccxt's fetch_trades returns *our* fills when a wallet is set)."""
        market = self._client.market(symbol)
        coin = (market.get("info") or {}).get("name") or market.get("base")
        try:
            raw = self._client.publicPostInfo({"type": "recentTrades", "coin": coin}) or []
        except Exception as exc:
            raise DexAPIError(f"Failed to fetch Hyperliquid recent trades for {symbol}") from exc
        out = []
        for t in raw[-limit:]:
            side = "buy" if str(t.get("side", "")).upper() == "B" else "sell"
            out.append((int(t.get("time", 0)) / 1000.0, Decimal(str(t.get("px"))), Decimal(str(t.get("sz"))), side))
        # recentTrades is capped at a handful of prints, which under-estimates flow on liquid
        # coins. Add the last 5 one-minute candles as pseudo-trades (volume split 50/50 by side)
        # so the queue-wait estimate is based on minutes of volume, not seconds.
        try:
            import time as _time
            end_ms = int(_time.time() * 1000)
            candles = self._client.publicPostInfo({
                "type": "candleSnapshot",
                "req": {"coin": coin, "interval": "1m", "startTime": end_ms - 6 * 60_000, "endTime": end_ms},
            }) or []
            for c in candles[-5:]:
                vol = Decimal(str(c.get("v", "0")))
                ts = int(c.get("t", 0)) / 1000.0
                px = Decimal(str(c.get("c", "0")))
                if vol > 0:
                    out.append((ts, px, vol / 2, "buy"))
                    out.append((ts, px, vol / 2, "sell"))
        except Exception as exc:  # flow proxy is best-effort
            logger.debug("candleSnapshot for %s failed: %s", coin, exc)
        return out

    def get_increments(self, symbol: str) -> Tuple[Decimal, Decimal]:
        m = self._client.market(symbol)
        step = Decimal(str(m["precision"]["amount"]))
        limits_min = (m.get("limits") or {}).get("price", {}).get("min")
        tick = Decimal(str(limits_min)) if limits_min else Decimal(str(m["precision"]["price"]))
        return tick, step

    def place_limit(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        *,
        post_only: bool = False,
        ioc: bool = False,
        reduce_only: bool = False,
    ) -> str:
        params: Dict[str, Any] = {}
        if post_only:
            params["postOnly"] = True
        elif ioc:
            params["timeInForce"] = "IOC"
        if reduce_only:
            params["reduceOnly"] = True
        try:
            resp = self._client.create_order(symbol, "limit", side.lower(), float(quantity), float(price), params)
        except Exception as exc:
            raise DexAPIError(f"Failed to place Hyperliquid limit order for {symbol}: {exc}") from exc
        order_id = resp.get("id")
        if not order_id:
            raise DexAPIError(f"Hyperliquid order response missing id: {resp}")
        return str(order_id)

    def get_order_state(self, symbol: str, order_id: str) -> Dict[str, Any]:
        try:
            raw = self._client.fetch_order(order_id, symbol)
        except Exception as exc:
            raise DexAPIError(f"Failed to fetch Hyperliquid order {order_id}: {exc}") from exc
        st = str(raw.get("status") or "").lower()
        filled = Decimal(str(raw.get("filled") or 0))
        # ccxt leaves ``average`` empty for Hyperliquid; derive it from cost/filled, else the limit price.
        avg = raw.get("average")
        cost = raw.get("cost")
        if avg:
            avg_price: Optional[Decimal] = Decimal(str(avg))
        elif cost and filled > 0:
            avg_price = Decimal(str(cost)) / filled
        elif filled > 0 and raw.get("price"):
            avg_price = Decimal(str(raw.get("price")))
        else:
            avg_price = None
        if st == "closed" or (raw.get("remaining") == 0 and filled > 0):
            status = "filled"
        elif st == "open":
            status = "open"
        else:
            status = "canceled"
        return {"status": status, "filled": filled, "avg_price": avg_price, "raw": raw}

    def cancel_by_id(self, symbol: str, order_id: str) -> None:
        try:
            self._client.cancel_order(order_id, symbol)
        except Exception as exc:
            msg = str(exc).lower()
            if "never placed" in msg or "already canceled" in msg or "filled" in msg or "not found" in msg:
                return
            raise DexAPIError(f"Failed to cancel Hyperliquid order {order_id}: {exc}") from exc

    def get_price(self, symbol: str) -> Decimal:
        """Fetch the current mid-price for a symbol."""
        logger.debug("Fetching order book for %s to get current price", symbol)
        try:
            order_book = self._client.fetch_order_book(symbol)
            if not order_book.get("bids") or not order_book.get("asks"):
                raise DexAPIError(f"Order book for {symbol} is empty, cannot determine price")
            best_bid = order_book["bids"][0][0]
            best_ask = order_book["asks"][0][0]
            return (Decimal(str(best_bid)) + Decimal(str(best_ask))) / 2
        except Exception as exc:
            raise DexAPIError(f"Failed to fetch price for {symbol}") from exc

    def get_max_leverage(self, symbol: str) -> int:
        """Fetch the maximum leverage for a symbol."""
        try:
            # load_markets is implicitly called by market() if needed
            market_info = self._client.market(symbol)
            max_leverage = market_info.get("limits", {}).get("leverage", {}).get("max")
            if max_leverage is None:
                raise DexAPIError(f"Max leverage not found for {symbol}")
            return int(max_leverage)
        except Exception as exc:
            raise DexAPIError(f"Failed to fetch max leverage for {symbol} on Hyperliquid") from exc

    def set_leverage(self, symbol: str, leverage: int) -> None:
        """Set leverage for a given symbol."""
        logger.info("Setting leverage for %s to %sx", symbol, leverage)
        try:
            self._client.set_leverage(leverage, symbol)
        except Exception as exc:
            raise DexAPIError(f"Failed to set leverage for {symbol}") from exc

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Place an order on Hyperliquid.
        If order_type is MAKER_TAKER, attempts a post-only limit order before
        falling back to a market order.
        """
        order_params = params or {}

        if order_type.upper() == "MAKER_TAKER":
            # --- 1. Attempt Post-Only Limit Order ---
            try:
                logger.info(f"Attempting to open {symbol} with post-only limit order.")
                market_info = self._client.market(symbol)
                step_size = Decimal(str(market_info['precision']['amount']))

                if price:
                    limit_price = Decimal(str(price))
                else:
                    tick_size = Decimal(str(market_info['precision']['price']))
                    order_book = self._client.fetch_order_book(symbol)
                    if not order_book.get("bids") or not order_book.get("asks"):
                        raise DexAPIError(f"Order book for {symbol} is empty.")

                    best_bid = Decimal(str(order_book["bids"][0][0]))
                    best_ask = Decimal(str(order_book["asks"][0][0]))

                    limit_price = (best_bid - tick_size) if side.lower() == "buy" else (best_ask + tick_size)
                qty_rounded = float((Decimal(str(quantity)) // step_size) * step_size)
                if qty_rounded == 0:
                    raise ValueError(f"Quantity {quantity} rounded to zero with step size {step_size}")

                post_only_params = order_params.copy()
                post_only_params["postOnly"] = False

                logger.info(f"Placing post-only LIMIT {side} for {qty_rounded} {symbol} @ {limit_price}")
                return self._client.create_order(
                    symbol=symbol, type="limit", side=side.lower(),
                    amount=qty_rounded, price=float(limit_price), params=post_only_params,
                )
            except Exception as exc:
                err_msg = str(exc).lower()
                if "post-only" in err_msg or "would cross" in err_msg or "fill immediately" in err_msg:
                    logger.warning(f"Post-only for {symbol} failed, would cross book. Falling back. Error: {exc}")
                else:
                    logger.warning(f"Post-only for {symbol} failed: {exc}. Falling back.")

            # --- 2. Fallback to Market Order ---
            logger.info(f"Fallback: Opening {symbol} with a market order.")
            market_params = order_params.copy()
            market_params.pop("postOnly", None)
            return self.place_order(symbol, side, "MARKET", quantity, None, market_params)

        # --- Standard Order Logic ---
        price_for_order = price
        if order_type.upper() == "MARKET" and price is None:
            price_for_order = float(self.get_price(symbol))

        try:
            return self._client.create_order(
                symbol=symbol, type=order_type.lower(), side=side.lower(),
                amount=quantity, price=price_for_order, params=order_params,
            )
        except Exception as exc:
            raise DexAPIError("Failed to create Hyperliquid order") from exc

    def cancel_or_close(self, symbol: str, order_id: str) -> Dict[str, Any]:
        """Cancels an open order or closes the position if the order was filled."""
        logger.info("Checking status of order %s for %s to cancel or close.", order_id, symbol)

        try:
            order = self._client.fetch_order(id=order_id, symbol=symbol)
        except Exception as exc:
            # ccxt might raise OrderNotFound if it's not in the open/closed history.
            # This could mean it was filled and is now a position.
            logger.warning("Could not fetch order %s, assuming it was filled. Will try to close position. Error: %s", order_id, exc)
            return self.close_position(symbol=symbol)

        status = order.get("status")
        logger.info("Order %s has status: '%s'", order_id, status)

        if status == 'open':
            logger.info("Order %s is open, cancelling it.", order_id)
            return self.cancel_order(symbol=symbol, order_id=order_id)
        elif status == 'closed':  # 'closed' in ccxt means filled
            logger.info("Order %s is filled (closed). Closing position for %s.", order_id, symbol)
            return self.close_position(symbol=symbol)
        else:  # e.g., 'canceled', or something else.
            logger.info("Order %s is already '%s'. No action taken.", order_id, status)
            return {"status": "no_action_needed", "reason": f"Order status was '{status}'"}

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

    def close_position(self, symbol: str, spread_ticks: int = 1) -> Dict[str, Any]:
        """
        Close an open position for a given symbol on Hyperliquid.
        Tries a post-only limit order first, falling back to a market order.
        """
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

        # 2. Get market info for precision
        market_info = self._client.market(symbol)
        tick_size = Decimal(str(market_info['precision']['price']))

        # --- 3. Attempt Post-Only Limit Order ---
        try:
            logger.info("Attempting to close with post-only limit order.")
            order_book = self._client.fetch_order_book(symbol)
            if not order_book.get("bids") or not order_book.get("asks"):
                raise DexAPIError(f"Order book for {symbol} is empty, cannot place limit order.")

            best_bid = Decimal(str(order_book["bids"][0][0]))
            best_ask = Decimal(str(order_book["asks"][0][0]))

            # Place order inside the spread to capture it
            limit_price = (best_ask - (tick_size * spread_ticks)) if close_side == "sell" else (best_bid + (tick_size * spread_ticks))

            logger.info(f"Placing post-only LIMIT {close_side} order for {size_to_close} of {symbol} at {limit_price}")
            order_response = self._client.create_order(
                symbol=symbol,
                type="limit",
                side=close_side.lower(),
                amount=size_to_close,
                price=float(limit_price),
                params={"postOnly": True, "reduceOnly": True},
            )
            logger.info(f"Successfully placed post-only limit order for {symbol}.")
            return order_response
        except Exception as exc:
            err_msg = str(exc).lower()
            if "post-only" in err_msg or "would cross" in err_msg or "fill immediately" in err_msg:
                logger.warning(
                    f"Post-only order for {symbol} failed as it would cross the book. Falling back. Error: {exc}"
                )
            else:
                logger.warning(f"Failed to place post-only order for {symbol}, falling back. Error: {exc}")

        # --- 4. Fallback to Market Order ---
        logger.info(f"Fallback: Closing {symbol} with a market order.")
        try:
            current_price = self.get_price(symbol)
            logger.info(
                "Placing reduce-only MARKET %s order for %s of %s to close position",
                close_side, size_to_close, symbol
            )
            return self._client.create_order(
                symbol=symbol,
                type="market",
                side=close_side.lower(),
                amount=size_to_close,
                price=float(current_price),
                params={"reduceOnly": True},
            )
        except Exception as exc:
            raise DexAPIError(f"Failed to place fallback closing market order for {symbol}") from exc

