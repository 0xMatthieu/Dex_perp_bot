"""Aster exchange connector."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
import urllib.parse
import uuid
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

import requests

from .base import (
    BalanceParsingError,
    DexAPIError,
    WalletBalance,
    find_first_key,
    get_from_path,
    to_decimal,
)
from ..config import AsterConfig, AsterCredentials

KeyVals = Sequence[Tuple[str, Any]]

logger = logging.getLogger(__name__)


class AsterClient:
    """HTTP client for interacting with the Aster API (fapi.*)."""

    def __init__(
        self,
        credentials: AsterCredentials,
        config: AsterConfig,
        *,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._credentials = credentials
        self._config = config
        self._session = session or requests.Session()
        self._time_offset_ms = 0  # serverTime - local
        self._markets: Dict[str, Any] = {}

        try:
            logger.info("Loading markets for Aster...")
            self.load_markets()
            logger.info("Aster markets loaded.")
        except Exception as exc:
            raise DexAPIError("Failed to load Aster markets") from exc

    def load_markets(self) -> None:
        """Fetch exchange info and cache it."""
        exchange_info = self._get_public("/fapi/v1/exchangeInfo")
        symbols_data = exchange_info.get("symbols", [])
        if not isinstance(symbols_data, list):
            raise DexAPIError("Invalid 'symbols' data in Aster exchange info")

        self._markets = {s["symbol"]: s for s in symbols_data if isinstance(s, dict) and "symbol" in s}

    # -------- Time sync (fix -1021) --------
    def sync_time(self) -> None:
        """GET /fapi/v1/time and cache the offset (serverTime - now)."""
        url = f"{self._config.base_url.rstrip('/')}/fapi/v1/time"
        try:
            r = self._session.get(url, timeout=self._config.request_timeout)
            r.raise_for_status()
            server = int(r.json()["serverTime"])
            self._time_offset_ms = server - int(time.time() * 1000)
        except (requests.RequestException, ValueError) as exc:
            raise DexAPIError("Failed to sync time with Aster") from exc

    def _now_ms(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    def _get_funding_time_range_ms(self) -> Tuple[int, int]:
        """Calculates the last and next funding timestamps for Aster."""
        now = datetime.now(timezone.utc)
        # Funding is every 4 hours.
        funding_hours = [0, 4, 8, 12, 16, 20]

        next_funding_dt = None
        for hour in funding_hours:
            potential_dt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if potential_dt > now:
                next_funding_dt = potential_dt
                break

        if next_funding_dt is None:  # all of today's funding times have passed
            tomorrow = now + timedelta(days=1)
            next_funding_dt = tomorrow.replace(hour=funding_hours[0], minute=0, second=0, microsecond=0)

        # The previous funding time is 4 hours before the next one.
        previous_funding_dt = next_funding_dt - timedelta(hours=4)

        return int(previous_funding_dt.timestamp() * 1000), int(next_funding_dt.timestamp() * 1000)

    # -------- Signing primitives (Binance-style) --------
    def _urlencode(self, items: KeyVals) -> str:
        # EXACT encoding used to sign & send
        return urllib.parse.urlencode(items, doseq=True)

    def _sign(self, total_params: str) -> str:
        secret = self._credentials.api_secret.encode("utf-8")
        return hmac.new(secret, total_params.encode("utf-8"), hashlib.sha256).hexdigest()

    def _headers(self) -> Dict[str, str]:
        return {"X-MBX-APIKEY": self._credentials.api_key}

    def get_wallet_balance(self) -> WalletBalance:
        """
        Fetch account info and return parsed balance.

        Uses GET /fapi/v4/account (SIGNED):
        - total fields: totalWalletBalance / totalMarginBalance
        - available fields: availableBalance / maxWithdrawAmount / totalMarginBalance
        """
        response = self._get_signed(self._config.balance_endpoint, params=[])
        account_data = self._extract_account_data(response)

        total = to_decimal(find_first_key(account_data, self._config.total_fields))
        available = to_decimal(find_first_key(account_data, self._config.available_fields))

        if total is None and available is None:
            raise BalanceParsingError(
                "Aster account payload missing both total and available fields"
            )

        return WalletBalance(total=total, available=available, raw=response)

    def get_funding_rate(
        self,
        symbol: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch current funding rate from GET /fapi/v1/premiumIndex."""
        endpoint = "/fapi/v1/premiumIndex"
        params: Dict[str, Any] = {}
        if symbol is not None:
            params["symbol"] = symbol

        data = self._get_public(endpoint, params)
        # The endpoint returns a single dict for a symbol, and a list for all symbols.
        # We normalize to always return a list.
        if isinstance(data, dict):
            return [data]
        return data

    def get_max_leverage(self, symbol: str) -> int:
        """Fetch maximum leverage for a symbol from leverage brackets."""
        logger.debug("Fetching leverage brackets for %s", symbol)
        try:
            # This is a standard endpoint on Binance-like exchanges.
            # It's a list containing one dict for the symbol.
            brackets_info_list = self._get_signed("/fapi/v1/leverageBracket", params=[("symbol", symbol)])
            if not isinstance(brackets_info_list, list) or not brackets_info_list:
                raise DexAPIError("Invalid response from leverageBracket endpoint")

            brackets_info = brackets_info_list[0]
            # Find the bracket with the highest leverage.
            max_leverage = 0
            if isinstance(brackets_info, dict) and "brackets" in brackets_info:
                for b in brackets_info["brackets"]:
                    if b.get("initialLeverage", 0) > max_leverage:
                        max_leverage = b["initialLeverage"]

            if max_leverage == 0:
                logger.warning("Could not parse max leverage for %s, falling back to 50x", symbol)
                return 50
            return int(max_leverage)
        except DexAPIError as exc:
            logger.warning("Failed to fetch leverage brackets for %s from Aster, falling back to 50x. Error: %s", symbol, exc)
            return 50  # Fallback

    def get_price(self, symbol: str) -> Decimal:
        """Fetch the latest mark price for a symbol."""
        logger.debug("Fetching ticker price for %s", symbol)
        price_info = self._get_public("/fapi/v1/ticker/price", params={"symbol": symbol})
        return Decimal(price_info["price"])

    def _get_order_book(self, symbol: str, limit: int = 5) -> Dict[str, Any]:
        """Fetch order book for a symbol."""
        logger.debug("Fetching order book for %s", symbol)
        return self._get_public("/fapi/v1/depth", params={"symbol": symbol, "limit": limit})

    def get_symbol_filters(self, symbol: str) -> Dict[str, Decimal]:
        """Fetch and return price/lot/notional filters for a symbol from cached markets."""
        if not self._markets:  # Should be loaded at init, but as a safeguard
            self.load_markets()

        symbol_info = self._markets.get(symbol)
        if not symbol_info:
            raise DexAPIError(f"Could not find symbol info for {symbol}")

        filters = {f["filterType"]: f for f in symbol_info.get("filters", [])}
        price_filter = filters.get("PRICE_FILTER")
        lot_size_filter = filters.get("LOT_SIZE")
        market_lot_size_filter = filters.get("MARKET_LOT_SIZE")
        min_notional_filter = filters.get("MIN_NOTIONAL")

        if not all([price_filter, lot_size_filter, min_notional_filter]):
            raise DexAPIError(f"Missing required filters for {symbol}")

        return {
            "tick_size": Decimal(price_filter["tickSize"]),
            "step_size": Decimal(lot_size_filter["stepSize"]),
            "min_notional": Decimal(min_notional_filter["notional"]),
            "limit_min_quantity": Decimal(lot_size_filter["minQty"]),
            "market_min_quantity": Decimal(market_lot_size_filter["minQty"]),
            "limit_max_quantity": Decimal(lot_size_filter["maxQty"]),
            "market_max_quantity": Decimal(market_lot_size_filter["maxQty"]),
        }

    def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        """Set leverage for a given symbol."""
        logger.info("Setting leverage for %s to %sx", symbol, leverage)
        return self._post_signed(
            "/fapi/v1/leverage",
            query=[("symbol", symbol), ("leverage", leverage)],
            signature_location="query",
        )

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Union[float, Decimal],
        price: Optional[Union[float, Decimal]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Place an order on Aster.
        If order_type is MAKER_TAKER, attempts a post-only limit order before
        falling back to a market order.
        """
        if params is None:
            params = {}

        filters = self.get_symbol_filters(symbol)
        tick_size, step_size = filters['tick_size'], filters['step_size']

        qty_decimal = Decimal(str(quantity))
        qty_rounded = (qty_decimal // step_size) * step_size
        if qty_rounded == 0:
            raise ValueError(f"Quantity {quantity} for {symbol} is zero after rounding to step size {step_size}")

        if order_type.upper() == "MAKER_TAKER":
            # --- 1. Attempt Post-Only Limit Order ---
            try:
                logger.info(f"Attempting to open {symbol} with post-only limit order.")
                order_book = self._get_order_book(symbol)
                if not order_book.get("bids") or not order_book.get("asks"):
                    raise DexAPIError(f"Order book for {symbol} is empty, cannot place limit order.")

                best_bid = Decimal(order_book["bids"][0][0])
                best_ask = Decimal(order_book["asks"][0][0])

                # Place one tick past the passive side of the book to be a maker
                limit_price = (best_bid - tick_size) if side.upper() == "BUY" else (best_ask + tick_size)

                price_precision = -tick_size.normalize().as_tuple().exponent
                price_str = f"{limit_price:.{price_precision}f}"
                qty_precision = -step_size.normalize().as_tuple().exponent
                qty_str = f"{qty_rounded:.{qty_precision}f}"

                payload: List[Tuple[str, Any]] = [
                    ("symbol", symbol), ("side", side.upper()), ("type", "LIMIT"),
                    ("quantity", qty_str), ("price", price_str), ("timeInForce", "GTX"),
                ]
                if params.get("reduceOnly"):
                    payload.append(("reduceOnly", "true"))

                response = self._post_signed("/fapi/v1/order", body=payload)
                logger.info(f"Successfully placed post-only limit order for {symbol}.")
                return response
            except DexAPIError as exc:
                if "-2026" in str(exc) or "Order would immediately trigger" in str(exc):
                    logger.warning(f"Post-only order for {symbol} failed as it would cross. Falling back.")
                else:
                    logger.error(f"Unexpected API error on post-only order: {exc}. Falling back.")
            except (requests.RequestException, KeyError, IndexError) as exc:
                logger.warning(f"Failed to place post-only order for {symbol}: {exc}. Falling back.")

            # --- 2. Fallback to Market Order ---
            logger.info(f"Fallback: Opening {symbol} with a market order.")
            market_params = params.copy()
            market_params.pop("timeInForce", None)
            return self.place_order(symbol, side, "MARKET", quantity, None, market_params)

        # --- Standard Order Logic ---
        order_payload: List[Tuple[str, Any]] = [
            ("symbol", symbol), ("side", side.upper()), ("type", order_type.upper()),
        ]

        if "newClientOrderId" not in params:
            order_payload.append(("newClientOrderId", f"dxp-{uuid.uuid4().hex}"))

        qty_precision = -step_size.normalize().as_tuple().exponent
        qty_str = f"{qty_rounded:.{qty_precision}f}"
        order_payload.append(("quantity", qty_str))

        if order_type.upper() == "LIMIT":
            if price is None:
                raise ValueError("Price must be provided for LIMIT orders")
            price_precision = -tick_size.normalize().as_tuple().exponent
            price_str = f"{Decimal(str(price)):.{price_precision}f}"
            order_payload.extend([("price", price_str), ("timeInForce", params.get("timeInForce", "GTC"))])

        for key, value in params.items():
            if key not in ['timeInForce', 'newClientOrderId']:
                order_payload.append((key, value))

        logger.info("Placing order with payload: %s", dict(order_payload))
        return self._post_signed("/fapi/v1/order", body=order_payload)

    def _place_single_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        current_price: Decimal,
        tick_size: Decimal,
        step_size: Decimal,
    ) -> Dict[str, Any]:
        """Helper to place a single order on Aster."""
        client_order_id = f"dxp-{uuid.uuid4().hex}"
        order_payload: List[Tuple[str, Any]] = [
            ("symbol", symbol),
            ("side", side.upper()),
            ("type", order_type.upper()),
            ("newClientOrderId", client_order_id),
        ]

        # Format quantity according to stepSize precision
        qty_precision = -step_size.normalize().as_tuple().exponent
        qty_str = f"{quantity:.{qty_precision}f}"
        order_payload.append(("quantity", qty_str))

        if order_type.upper() == "LIMIT":
            # For testing cancellation, place the order far from the current price
            # to ensure it is not filled immediately.
            if side.upper() == "BUY":
                limit_price = current_price * Decimal("0.8")
            else:
                limit_price = current_price * Decimal("1.2")
            rounded_price = round(limit_price / tick_size) * tick_size
            price_precision = -tick_size.normalize().as_tuple().exponent
            price_str = f"{rounded_price:.{price_precision}f}"
            order_payload.extend([
                ("price", price_str),
                ("timeInForce", "GTC"),
            ])

        logger.info("Placing order with payload: %s", dict(order_payload))
        order_response = self._post_signed("/fapi/v1/order", body=order_payload)

        print("Final order payload:", dict(order_payload))
        return order_response

    def _get_public(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Generic public GET request helper."""
        url = f"{self._config.base_url.rstrip('/')}{endpoint}"
        try:
            response = self._session.get(url, params=params, timeout=self._config.request_timeout)
            self._raise_for_json(response)
            return response.json()
        except requests.RequestException as exc:  # pragma: no cover - network failure
            raise DexAPIError(f"Aster public request to {endpoint} failed") from exc

    def create_order(
        self,
        side: str,
        order_type: str,
        leverage: int,
        margin_usd: float,
    ) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
        """Create an order on Aster for BTCUSDT."""
        symbol = "BTCUSDT"

        # 1. Query exchangeInfo for filters
        logger.info("Fetching symbol filters for %s", symbol)
        filters = self.get_symbol_filters(symbol)
        tick_size = filters["tick_size"]
        step_size = filters["step_size"]
        min_notional = filters["min_notional"]
        max_quantity = filters["limit_max_quantity"] if order_type == "LIMIT" else filters["market_max_quantity"]
        min_quantity = filters["limit_min_quantity"] if order_type == "LIMIT" else filters["market_min_quantity"]

        # 2. Query for current price
        logger.info("Fetching ticker price for %s", symbol)
        price_info = self._get_public("/fapi/v1/ticker/price", params={"symbol": symbol})
        current_price = Decimal(price_info["price"])

        # 3. Query for available balance (withdrawable)
        logger.info("Fetching account balance to log available funds")
        account_info_response = self._get_signed(self._config.balance_endpoint, params=[])
        account_data = self._extract_account_data(account_info_response)
        available_balance = to_decimal(find_first_key(account_data, self._config.available_fields))
        logger.info("Available balance: %s", available_balance)

        # 4. Compute order quantity
        qty = (Decimal(str(margin_usd)) * Decimal(leverage)) / current_price
        # Round down to stepSize
        qty = (qty // step_size) * step_size

        if qty == 0:
            raise ValueError(
                f"Calculated quantity is zero for margin {margin_usd}, leverage {leverage}. "
                f"This may be due to low margin or high price. stepSize is {step_size}"
            )

        # Use current_price for notional check, even for LIMIT orders, as it's a pre-check.
        notional_value = qty * current_price
        if notional_value < min_notional:
            raise ValueError(
                f"Notional value {notional_value} is less than minNotional {min_notional}. "
                f"Increase margin or leverage."
            )

        # 5. Set leverage
        logger.info("Setting leverage for %s to %sx", symbol, leverage)
        self._post_signed(
            "/fapi/v1/leverage",
            query=[("symbol", symbol), ("leverage", leverage)],
            signature_location="query",
        )

        # 6. Place order(s)
        if qty <= max_quantity:
            # Place a single order
            return self._place_single_order(symbol, side, order_type, qty, current_price, tick_size, step_size)

        # Split into chunks if qty > max_quantity
        logger.info(f"Quantity {qty} exceeds max {max_quantity}, splitting into multiple orders.")
        results = []
        remaining_qty = qty
        while remaining_qty > 0:
            chunk_qty = min(remaining_qty, max_quantity)
            # Round down to stepSize
            chunk_qty = (chunk_qty // step_size) * step_size

            if chunk_qty < min_quantity:
                logger.warning(f"Remaining quantity {remaining_qty} is less than min quantity {min_quantity}, stopping.")
                break

            try:
                res = self._place_single_order(
                    symbol, side, order_type, chunk_qty, current_price, tick_size, step_size
                )
                results.append(res)
                remaining_qty -= chunk_qty
                time.sleep(0.1)  # Small delay to avoid rate limiting issues
            except DexAPIError as exc:
                logger.error(f"Error placing chunk of size {chunk_qty}: {exc}. Stopping further chunks.")
                break

        return results

    def get_all_open_orders(self) -> List[Dict[str, Any]]:
        """Query all open orders."""
        logger.debug("Fetching all open orders from Aster")
        try:
            # Empty params will fetch for all symbols
            return self._get_signed("/fapi/v1/openOrders", params=[])
        except DexAPIError as exc:
            logger.error("Failed to fetch all open orders from Aster: %s", exc)
            return []

    def get_open_order(
        self,
        symbol: str,
        order_id: Optional[int] = None,
        orig_client_order_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Query a specific open order by ID or client ID."""
        if order_id is None and orig_client_order_id is None:
            raise ValueError("Either order_id or orig_client_order_id must be provided")

        params: List[Tuple[str, Any]] = [("symbol", symbol)]
        if order_id is not None:
            params.append(("orderId", order_id))
        if orig_client_order_id is not None:
            params.append(("origClientOrderId", orig_client_order_id))

        try:
            return self._get_signed("/fapi/v1/openOrder", params=params)
        except DexAPIError as exc:
            # Per docs, "Order does not exist" is returned for filled/cancelled orders.
            if "Order does not exist" in str(exc):
                return None
            raise  # re-raise other errors

    def cancel_or_close(self, symbol: str, client_order_id: str) -> Dict[str, Any]:
        """Cancels an open order or closes the position if the order was filled."""
        logger.info("Checking status of order %s for %s to cancel or close.", client_order_id, symbol)

        open_order = self.get_open_order(symbol=symbol, orig_client_order_id=client_order_id)

        if open_order:
            logger.info("Order %s is still open, cancelling it.", client_order_id)
            return self.cancel_order(symbol=symbol, orig_client_order_id=client_order_id)
        else:
            logger.info("Order %s not found or already filled. Closing position for %s.", client_order_id, symbol)
            return self.close_position(symbol=symbol)

    def get_all_positions(self) -> List[Dict[str, Any]]:
        """Fetch all open positions from the account endpoint."""
        logger.debug("Fetching account info to find all open positions")
        account_info = self._get_signed("/fapi/v4/account")
        positions = account_info.get("positions")
        if not isinstance(positions, list):
            raise DexAPIError("Account response did not contain a 'positions' list")

        open_positions = []
        for position in positions:
            if isinstance(position, dict):
                try:
                    position_amt = Decimal(position.get("positionAmt", "0"))
                    if not position_amt.is_zero():
                        open_positions.append(position)
                except InvalidOperation:
                    logger.warning(f"Could not parse positionAmt for position: {position}")
                    continue
        return open_positions

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Fetch position information for a single symbol from the account endpoint."""
        logger.debug("Fetching account info to find position for %s", symbol)
        account_info = self._get_signed("/fapi/v4/account")
        positions = account_info.get("positions")
        if not isinstance(positions, list):
            raise DexAPIError("Account response did not contain a 'positions' list")

        for position in positions:
            if isinstance(position, dict) and position.get("symbol") == symbol:
                return position
        return None

    def close_position(self, symbol: str) -> Dict[str, Any]:
        """
        Close an open position for a given symbol on Aster.
        Tries a post-only limit order first, falling back to a stop-market order.
        """
        logger.info("Attempting to close position for %s", symbol)

        position = self.get_position(symbol)
        if position is None:
            raise DexAPIError(f"Could not find position information for {symbol}")

        try:
            position_amt = Decimal(position.get("positionAmt", "0"))
        except InvalidOperation:
            raise DexAPIError(f"Invalid position amount '{position.get('positionAmt')}' for {symbol}")

        if position_amt.is_zero():
            logger.info("No open position found for %s", symbol)
            return {"status": "no_position"}

        close_side = "SELL" if position_amt > 0 else "BUY"
        quantity_to_close = abs(position_amt)

        filters = self.get_symbol_filters(symbol)
        tick_size = filters['tick_size']
        step_size = filters['step_size']

        # --- 1. Attempt Post-Only Limit Order ---
        try:
            logger.info("Attempting to close with post-only limit order.")
            order_book = self._get_order_book(symbol)
            if not order_book.get("bids") or not order_book.get("asks"):
                raise DexAPIError(f"Order book for {symbol} is empty, cannot place limit order.")

            best_bid = Decimal(order_book["bids"][0][0])
            best_ask = Decimal(order_book["asks"][0][0])

            # Place one tick inside the passive side of the book
            limit_price = (best_ask - tick_size) if close_side == "SELL" else (best_bid + tick_size)

            price_precision = -tick_size.normalize().as_tuple().exponent
            price_str = f"{limit_price:.{price_precision}f}"
            qty_precision = -step_size.normalize().as_tuple().exponent
            qty_str = f"{quantity_to_close:.{qty_precision}f}"

            payload: List[Tuple[str, Any]] = [
                ("symbol", symbol),
                ("side", close_side),
                ("type", "LIMIT"),
                ("quantity", qty_str),
                ("price", price_str),
                ("timeInForce", "GTX"),  # Post-Only
                ("reduceOnly", "true"),
            ]
            logger.info("Placing post-only limit order with payload: %s", dict(payload))
            response = self._post_signed("/fapi/v1/order", body=payload)
            logger.info(f"Successfully placed post-only limit order for {symbol}.")
            return response
        except DexAPIError as exc:
            # Error -2026: Order would immediately trigger. This is expected for post-only failures.
            if "-2026" in str(exc) or "Order would immediately trigger" in str(exc):
                logger.warning(
                    f"Post-only order for {symbol} failed as it would cross the book. Falling back."
                )
            else:
                logger.error(f"Unexpected API error on post-only order for {symbol}, falling back anyway: {exc}")
        except (requests.RequestException, KeyError, IndexError) as exc:
            logger.warning(
                f"Failed to place post-only order for {symbol} due to connection or data issue. Falling back. Error: {exc}"
            )

        # --- 2. Fallback to Stop-Market Order ---
        logger.info(f"Fallback: Closing {symbol} with a stop-market order.")
        current_price = self.get_price(symbol)

        # To trigger the stop order immediately, set stopPrice through the current mark price.
        if close_side == "SELL":  # Closing a long, stop triggers when price <= stopPrice
            stop_price = current_price * Decimal("0.995")
        else:  # Closing a short, stop triggers when price >= stopPrice
            stop_price = current_price * Decimal("1.005")

        price_precision = -tick_size.normalize().as_tuple().exponent
        stop_price_rounded = round(stop_price / tick_size) * tick_size
        stop_price_str = f"{stop_price_rounded:.{price_precision}f}"

        payload: List[Tuple[str, Any]] = [
            ("symbol", symbol),
            ("side", close_side),
            ("type", "STOP_MARKET"),
            ("stopPrice", stop_price_str),
            ("closePosition", "true"),
            ("priceProtect", "FALSE"),
        ]
        logger.info("Placing closePosition STOP_MARKET order with payload: %s", dict(payload))
        return self._post_signed("/fapi/v1/order", body=payload)

    def cancel_order(
        self,
        symbol: str,
        order_id: Optional[int] = None,
        orig_client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Cancel an active order on Aster for a given symbol."""
        if order_id is None and orig_client_order_id is None:
            raise ValueError("Either order_id or orig_client_order_id must be provided")

        params: List[Tuple[str, Any]] = [("symbol", symbol)]
        if order_id is not None:
            params.append(("orderId", order_id))
        if orig_client_order_id is not None:
            params.append(("origClientOrderId", orig_client_order_id))

        logger.info("Canceling order with params: %s", dict(params))
        return self._delete_signed("/fapi/v1/order", params=params)

    # -------- GET SIGNED (query only) --------
    def _get_signed(self, endpoint: str, params: Optional[KeyVals] = None) -> Mapping[str, Any]:
        base_items: List[Tuple[str, Any]] = []
        # Put recvWindow first or last � order must match what you sign & send. We keep it first.
        base_items.append(("recvWindow", 5000))
        base_items.append(("timestamp", self._now_ms()))

        if params:
            base_items.extend(list(params))  # preserve caller order

        query_str = self._urlencode(base_items)
        #logger.info("Signing payload: %s", query_str)
        sig = self._sign(query_str)

        # signature MUST be last
        url = f"{self._config.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        full_query = f"{query_str}&signature={sig}"

        full_url = f"{url}?{full_query}"
        #logger.info("Aster GET: %s", full_url)
        r = self._session.get(full_url, headers=self._headers(), timeout=self._config.request_timeout)
        self._raise_for_json(r)
        return r.json()

    # -------- DELETE SIGNED (query only) --------
    def _delete_signed(self, endpoint: str, params: Optional[KeyVals] = None) -> Mapping[str, Any]:
        base_items: List[Tuple[str, Any]] = []
        base_items.append(("recvWindow", 5000))
        base_items.append(("timestamp", self._now_ms()))

        if params:
            base_items.extend(list(params))

        query_str = self._urlencode(base_items)
        #logger.info("Signing payload for DELETE: %s", query_str)
        sig = self._sign(query_str)

        url = f"{self._config.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        full_query = f"{query_str}&signature={sig}"

        full_url = f"{url}?{full_query}"
        #logger.info("Aster DELETE: %s", full_url)
        r = self._session.delete(full_url, headers=self._headers(), timeout=self._config.request_timeout)
        self._raise_for_json(r)
        return r.json()

    # -------- POST SIGNED (supports body, query, or mixed per docs) --------
    def _post_signed(
        self,
        endpoint: str,
        *,
        query: Optional[KeyVals] = None,
        body: Optional[KeyVals] = None,
        signature_location: str = "body",  # "body" | "query"
    ) -> Mapping[str, Any]:
        """
        Implements: totalParams = urlencode(query) + ('&' if both) + urlencode(body)
        Signs totalParams. Sends url-encoded (not JSON) like Binance/Aster examples.
        Ensures signature is the LAST param in the chosen location.
        """
        q_items: List[Tuple[str, Any]] = list(query) if query else []
        b_items: List[Tuple[str, Any]] = list(body) if body else []

        # Common parameters should go in the body if a body is present, otherwise in the query.
        common_params = [("recvWindow", 5000), ("timestamp", self._now_ms())]
        if body is not None:
            b_items.extend(common_params)
        else:
            q_items.extend(common_params)

        query_str = self._urlencode(q_items)
        body_str = self._urlencode(b_items) if b_items else ""

        if query_str and body_str:
            total_params = f"{query_str}&{body_str}"
        else:
            total_params = query_str or body_str  # exactly one side

        #logger.info("Signing payload: %s", total_params)
        signature = self._sign(total_params)

        url = f"{self._config.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        headers = {
            **self._headers(),
            "Content-Type": "application/x-www-form-urlencoded",
        }

        if signature_location == "query":
            # signature last in query
            q_to_send = query_str + ("&signature=" + signature if query_str else "signature=" + signature)
            r = self._session.post(
                f"{url}?{q_to_send}",
                data=body_str,  # may be empty; still form-encoded
                headers=headers,
                timeout=self._config.request_timeout,
            )
        else:
            # signature last in body
            if body_str:
                body_to_send = f"{body_str}&signature={signature}"
            else:
                body_to_send = f"signature={signature}"
            r = self._session.post(
                f"{url}?{query_str}" if query_str else url,
                data=body_to_send,
                headers=headers,
                timeout=self._config.request_timeout,
            )

        self._raise_for_json(r)
        return r.json()

    def _raise_for_json(self, r: requests.Response) -> None:
        try:
            r.raise_for_status()
        except requests.HTTPError as exc:
            try:
                err = r.json()
            except ValueError:
                err = {"raw": r.text}
            raise DexAPIError(f"Aster HTTP {r.status_code}: {err}") from exc

    def _extract_account_data(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        # If response_path is empty, use the whole payload (v4/account is top-level)
        if not self._config.response_path:
            if not isinstance(payload, Mapping):
                raise BalanceParsingError("Aster response is not a JSON object")
            return payload

        account_data = get_from_path(payload, self._config.response_path)
        if not isinstance(account_data, Mapping):
            raise BalanceParsingError("Aster response did not contain expected account data")
        return account_data

if __name__ == "__main__":  # pragma: no cover
    # Test signature generation against the example from Aster's API documentation.
    dummy_creds = AsterCredentials(
        api_key="dummy",
        api_secret="2b5eb11e18796d12d88f13dc27dbbd02c2cc51ff7059765ed9821957d82bb4d9",
    )
    # Config is not used by _sign, but required for instantiation.
    dummy_config = AsterConfig(
        account_id=None, base_url="", balance_endpoint="", response_path=(),
        available_fields=(), total_fields=(),
    )
    client = AsterClient(credentials=dummy_creds, config=dummy_config)

    total_params = "symbol=BTCUSDT&side=BUY&type=LIMIT&quantity=1&price=9000&timeInForce=GTC&recvWindow=5000&timestamp=1591702613943"
    expected_sig = "3c661234138461fcc7a7d8746c6558c9842d4e10870d2ecbedf7777cad694af9"
    actual_sig = client._sign(total_params)

    print("--- Testing Aster Signature Generation ---")
    print(f"Test Payload: {total_params}")
    print(f"Expected Signature: {expected_sig}")
    print(f"Actual Signature:   {actual_sig}")
    assert actual_sig == expected_sig, "Signature does not match!"
    print("✅ Signature matches the example.")
