from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

from pathlib import Path

import json

from .basis import BasisTracker
from .config import ExecutionConfig
from .decision_log import log_event
from .execution import Leg, execute_pair
from .funding import FundingComparison, fetch_and_compare_funding_rates
from .exchanges.aster import AsterClient
from .exchanges.hyperliquid import HyperliquidClient
from .microstructure import (
    basis_bps, basis_gain_bps, breakeven_hours, expected_reversion_bps, favorable_z, funding_bps_per_hour,
    half_spread_bps,
)
from .trade_log import log_trade

if TYPE_CHECKING:
    from .notifier import DiscordNotifier

logger = logging.getLogger(__name__)

TRADE_LOG_PATH = Path("logs/trades.md")
POSITION_STATE_PATH = Path("logs/position_state.json")


def hl_symbol(base: str) -> str:
    return f"{base}/USDC:USDC"


def aster_symbol(base: str) -> str:
    return f"{base}USDT"


# ---------------------------------------------------------------------------
# Position state (what we hold and the basis at entry) - survives restarts
# ---------------------------------------------------------------------------

def load_position_state(path: Path = POSITION_STATE_PATH) -> Optional[Dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_position_state(state: Optional[Dict], path: Path = POSITION_STATE_PATH) -> None:
    try:
        if state is None:
            if path.exists():
                path.unlink()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write position state: %s", exc)


def market_snapshot(aster_client: AsterClient, hyperliquid_client: HyperliquidClient, base: str) -> Dict:
    """Basis plus what execution will cost on each venue: half-spreads and tick granularity, in bps."""
    a = aster_client.get_book(aster_symbol(base), depth=1)
    h = hyperliquid_client.get_book(hl_symbol(base), depth=1)
    mid_a = (a["bids"][0][0] + a["asks"][0][0]) / 2
    mid_h = (h["bids"][0][0] + h["asks"][0][0]) / 2
    tick_a, _ = aster_client.get_increments(aster_symbol(base))
    tick_h, _ = hyperliquid_client.get_increments(hl_symbol(base))
    return {
        "basis_bps": basis_bps(mid_a, mid_h),
        "half_spread_aster_bps": half_spread_bps(a["bids"], a["asks"]),
        "half_spread_hl_bps": half_spread_bps(h["bids"], h["asks"]),
        "tick_aster_bps": tick_a / mid_a * Decimal(10_000) if mid_a else Decimal(0),
        "tick_hl_bps": tick_h / mid_h * Decimal(10_000) if mid_h else Decimal(0),
    }


def reconcile_position_state(aster_client: AsterClient, hyperliquid_client: HyperliquidClient) -> Optional[Dict]:
    """Rebuild logs/position_state.json from live positions when it is missing or stale (e.g. the
    process was stopped mid-execution). Entry basis is adopted from the current market."""
    try:
        hl_positions = hyperliquid_client.get_all_positions()
        aster_positions = aster_client.get_all_positions()
    except Exception as exc:
        logger.warning("reconcile: cannot read positions: %s", exc)
        return load_position_state()
    state = load_position_state()
    if not hl_positions and not aster_positions:
        if state:
            logger.info("reconcile: no positions but a state file exists; clearing it")
            save_position_state(None)
        return None
    if len(hl_positions) != 1 or len(aster_positions) != 1:
        logger.warning("reconcile: unexpected position set (HL %d, Aster %d); leaving state as is",
                       len(hl_positions), len(aster_positions))
        return state
    hl_pos, a_pos = hl_positions[0], aster_positions[0]
    symbol = str(hl_pos.get("symbol", "")).split("/")[0]
    long_venue = "Hyperliquid" if hl_pos.get("side") == "long" else "Aster"
    if state and state.get("symbol") == symbol and state.get("long_venue") == long_venue:
        return state
    try:
        entry_basis = float(current_basis(aster_client, hyperliquid_client, symbol))
    except Exception:
        entry_basis = None
    state = {
        "symbol": symbol, "long_venue": long_venue,
        "short_venue": "Aster" if long_venue == "Hyperliquid" else "Hyperliquid",
        "entry_basis_bps": entry_basis, "entry_basis_note": "adopted at reconcile (state file was missing)",
        "net_apy_pct": None, "entered_at": datetime.now(timezone.utc).isoformat(),
        "legs": [{"venue": "Hyperliquid", "side": hl_pos.get("side"), "filled": str(hl_pos.get("contracts")), "avg_price": str(hl_pos.get("entryPrice"))},
                 {"venue": "Aster", "filled": str(a_pos.get("positionAmt")), "avg_price": str(a_pos.get("entryPrice"))}],
    }
    save_position_state(state)
    log_event("note", message="position state reconciled from live positions", **{k: v for k, v in state.items() if k != "legs"})
    return state


def current_basis(aster_client: AsterClient, hyperliquid_client: HyperliquidClient, base: str) -> Decimal:
    """Aster premium over HL in bps from both mid prices."""
    return market_snapshot(aster_client, hyperliquid_client, base)["basis_bps"]


def hedge_cross_bps(snapshot: Dict) -> Decimal:
    """The hedge leg crosses the tighter book: half its spread is paid on top of the taker fee."""
    return min(snapshot["half_spread_aster_bps"], snapshot["half_spread_hl_bps"])


def cancel_all_open_orders(aster_client: AsterClient, hyperliquid_client: HyperliquidClient, *, reason: str) -> int:
    """Cancel every resting order on both venues (orphans after a restart, or on shutdown)."""
    n = 0
    try:
        for o in aster_client.get_all_open_orders():
            if o.get("symbol") and o.get("orderId") is not None:
                aster_client.cancel_by_id(o["symbol"], str(o["orderId"])); n += 1
    except Exception as exc:
        logger.warning("Aster cancel-all failed: %s", exc)
    try:
        for o in hyperliquid_client.get_all_open_orders():
            if o.get("symbol") and o.get("id"):
                hyperliquid_client.cancel_by_id(o["symbol"], str(o["id"])); n += 1
    except Exception as exc:
        logger.warning("Hyperliquid cancel-all failed: %s", exc)
    if n:
        logger.warning("Cancelled %d resting order(s): %s", n, reason)
        log_event("note", message="cancelled resting orders", count=n, reason=reason)
    return n


# ---------------------------------------------------------------------------
# Gates: fee break-even (with basis reversion) and switch economics
# ---------------------------------------------------------------------------

def evaluate_entry_gate(
    opp: FundingComparison,
    exec_cfg: ExecutionConfig,
    tracker: BasisTracker,
    basis_now_bps: Decimal,
    snapshot: Optional[Dict] = None,
) -> Dict:
    """Return a dict with ``ok`` plus every input, and log it as a ``gate`` event.

    Cost of a round trip = maker+taker fees on both venues + crossing the tighter book twice
    (hedge on entry, hedge on exit) - expected basis reversion.
    """
    stats = tracker.stats(opp.symbol, basis_now_bps, exec_cfg.basis_window_hours, exec_cfg.basis_min_samples)
    reversion = expected_reversion_bps(stats, opp.long_venue)
    if stats.z is None:
        # No history yet: assume the cross-venue basis reverts to 0 (same index on both venues).
        # Long HL / short Aster profits when Aster-minus-HL falls, so a negative basis now is adverse.
        from .microstructure import favorable_sign
        reversion = Decimal(favorable_sign(opp.long_venue)) * (Decimal(0) - basis_now_bps)
    fz = favorable_z(stats, opp.long_venue)
    fee_cost = Decimal(str(exec_cfg.round_trip_cost_bps))
    crossing = 2 * hedge_cross_bps(snapshot) if snapshot else Decimal(0)
    tick_bps = max(snapshot["tick_aster_bps"], snapshot["tick_hl_bps"]) if snapshot else Decimal(0)
    effective_cost = fee_cost + crossing - reversion
    hours = breakeven_hours(effective_cost, opp.apy_difference)
    tick_ok = tick_bps <= Decimal(str(exec_cfg.max_tick_bps))
    ok = hours <= exec_cfg.max_breakeven_hours and tick_ok
    if not tick_ok:
        reason_code = "tick_too_coarse"
    elif ok:
        reason_code = "ok"
    elif reversion < 0 and breakeven_hours(fee_cost + crossing, opp.apy_difference) <= exec_cfg.max_breakeven_hours:
        reason_code = "adverse_basis"
    elif breakeven_hours(fee_cost, opp.apy_difference) <= exec_cfg.max_breakeven_hours:
        reason_code = "spread_too_wide"
    else:
        reason_code = "breakeven_too_long"
    info = {
        "symbol": opp.symbol, "long_venue": opp.long_venue, "net_apy_pct": opp.apy_difference,
        "funding_bps_per_hour": funding_bps_per_hour(opp.apy_difference),
        "fee_cost_bps": fee_cost, "hedge_crossing_bps": crossing, "tick_bps": tick_bps,
        "half_spread_aster_bps": snapshot["half_spread_aster_bps"] if snapshot else None,
        "half_spread_hl_bps": snapshot["half_spread_hl_bps"] if snapshot else None,
        "basis_now_bps": basis_now_bps, "basis_mean_bps": stats.mean_bps,
        "basis_std_bps": stats.std_bps, "basis_samples": stats.n, "z": stats.z, "favorable_z": fz,
        "expected_reversion_bps": reversion, "reversion_prior": "history" if stats.z is not None else "mean0",
        "effective_cost_bps": effective_cost,
        "breakeven_hours": hours, "max_breakeven_hours": exec_cfg.max_breakeven_hours,
        "ok": ok, "reason_code": reason_code,
    }
    log_event("gate", decision="enter" if ok else "skip", **info)
    return info


def evaluate_switch_gate(current_apy: Decimal, new_apy: Decimal, exec_cfg: ExecutionConfig, symbol: str) -> bool:
    """Switching pays a full round trip; the APY improvement must repay it within the expected hold."""
    improvement_bps = funding_bps_per_hour(new_apy - current_apy) * Decimal(str(exec_cfg.expected_hold_hours))
    cost = Decimal(str(exec_cfg.round_trip_cost_bps))
    ok = improvement_bps >= cost
    log_event("gate", decision="switch" if ok else "hold", reason_code="switch_economics", symbol=symbol,
              current_apy_pct=current_apy, new_apy_pct=new_apy, improvement_bps_over_hold=improvement_bps,
              switch_cost_bps=cost, expected_hold_hours=exec_cfg.expected_hold_hours, ok=ok)
    return ok


def report_portfolio_status(
    aster_client: AsterClient,
    hyperliquid_client: HyperliquidClient,
) -> None:
    """Fetches and logs PNL, size, and price spread for current positions."""
    logger.info("--- Current Portfolio Status ---")
    try:
        hl_positions = hyperliquid_client.get_all_positions()
        aster_positions = aster_client.get_all_positions()

        if not hl_positions and not aster_positions:
            logger.info("No open positions on either exchange.")
            return

        total_pnl = Decimal("0")

        # Process Hyperliquid positions
        for pos in hl_positions:
            symbol_base = pos.get("symbol", "").split('/')[0]
            pnl = Decimal(pos.get("unrealizedPnl", "0"))
            total_pnl += pnl
            logger.info(
                f"Hyperliquid Position: {pos.get('symbol')} | Size: {pos.get('contracts')} | "
                f"Side: {pos.get('side')} | PNL: ${pnl:.4f}"
            )

        # Process Aster positions
        for pos in aster_positions:
            symbol_base = pos.get("symbol", "").replace("USDT", "")
            try:
                pnl = Decimal(pos.get("unrealizedProfit", "0"))
                total_pnl += pnl
                logger.info(
                    f"Aster Position: {pos.get('symbol')} | Size: {pos.get('positionAmt')} | PNL: ${pnl:.4f}"
                )
            except InvalidOperation:
                logger.warning(f"Could not parse PNL for Aster position: {pos}")

        logger.info(f"Total Unrealized PNL: ${total_pnl:.4f}")

        # Calculate and log price spread if in a delta-neutral position
        if len(hl_positions) == 1 and len(aster_positions) == 1:
            hl_symbol = hl_positions[0].get('symbol')
            aster_symbol = aster_positions[0].get('symbol')
            if hl_symbol and aster_symbol:
                price_hl = hyperliquid_client.get_price(hl_symbol)
                price_aster = aster_client.get_price(aster_symbol)
                spread = price_aster - price_hl
                spread_pct = (spread / price_hl) * 100 if price_hl else Decimal("0")
                logger.info(
                    f"Price Spread ({aster_symbol}): Aster=${price_aster:.4f}, HL=${price_hl:.4f} | "
                    f"Delta: ${spread:.4f} ({spread_pct:.4f}%)"
                )

    except Exception as exc:
        logger.error(f"Failed to generate portfolio status report: {exc}")


@dataclass(frozen=True)
class StrategyDecision:
    """Represents a fully-formed delta-neutral trade."""
    opportunity: FundingComparison
    long_qty: Decimal
    short_qty: Decimal
    long_symbol: str
    short_symbol: str
    margin: Decimal
    leverage: int
    # NEW fields for spread-based entry
    long_limit_price: Decimal
    short_limit_price: Decimal
    prefer_post_only: bool
    spread_ticks: Optional[int]
    spread_bps: Optional[Decimal]


def _is_portfolio_matching_opportunity(
    hl_positions: List[Dict],
    aster_positions: List[Dict],
    opportunity: FundingComparison,
) -> bool:
    """Checks if the current open positions match the target opportunity."""
    if not opportunity:
        return not hl_positions and not aster_positions

    target_symbol_base = opportunity.symbol
    target_long_venue = opportunity.long_venue

    # For simplicity, assume only one position pair should be open for this strategy.
    if len(hl_positions) > 1 or len(aster_positions) > 1 or (len(hl_positions) != len(aster_positions)):
        return False  # Not in a clean delta-neutral state

    if not hl_positions:  # and not aster_positions
        return False  # No positions exist

    hl_pos = hl_positions[0]
    aster_pos = aster_positions[0]

    hl_symbol_base = hl_pos.get("symbol", "").split('/')[0]
    aster_symbol_base = aster_pos.get("symbol", "").replace("USDT", "")

    if not (hl_symbol_base == aster_symbol_base == target_symbol_base):
        return False  # Wrong symbol

    # Check sides
    hl_side = hl_pos.get("side")  # 'long' or 'short'
    aster_pos_amt = Decimal(aster_pos.get("positionAmt", "0"))
    aster_side = 'long' if aster_pos_amt > 0 else 'short'

    if target_long_venue == "Hyperliquid":
        return hl_side == 'long' and aster_side == 'short'
    else:  # Long on Aster
        return aster_side == 'long' and hl_side == 'short'


def round_qty_down(quantity: Decimal, step: Decimal) -> Decimal:
    """Rounds a quantity down to the nearest multiple of step_size."""
    if step.is_zero():
        return quantity
    return (quantity // step) * step


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Rounds a value down to the nearest multiple of step_size (e.g., tick_size)."""
    if step.is_zero():
        return value
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Rounds a value up to the nearest multiple of step_size."""
    if step.is_zero():
        return value
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def compute_spread_abs(
    price: Decimal,
    tick: Decimal,
    *,
    spread_ticks: Optional[int],
    spread_bps: Optional[Decimal],
) -> Decimal:
    """Computes the absolute spread amount from ticks and/or basis points."""
    spread_from_ticks = Decimal(spread_ticks or 0) * tick
    spread_from_bps = (spread_bps or Decimal(0)) * price
    return max(spread_from_ticks, spread_from_bps)


def _calculate_trade_decision(
    aster_client: AsterClient,
    hyperliquid_client: HyperliquidClient,
    best_opp: FundingComparison,
    leverage: int,
    capital_usd: Decimal,
    *,
    spread_ticks: Optional[int] = 1,
    spread_bps: Optional[Decimal] = None,
) -> Optional[StrategyDecision]:
    """Calculates quantities and post-only entry prices for a given opportunity."""
    # 1. Symbols and which venue is long/short
    symbol_base = best_opp.symbol
    symbol_hl = f"{symbol_base}/USDC:USDC"
    symbol_aster = f"{symbol_base}USDT"

    long_venue_client = aster_client if best_opp.long_venue == "Aster" else hyperliquid_client
    short_venue_client = hyperliquid_client if best_opp.long_venue == "Aster" else aster_client

    long_symbol = symbol_aster if isinstance(long_venue_client, AsterClient) else symbol_hl
    short_symbol = symbol_aster if isinstance(short_venue_client, AsterClient) else symbol_hl

    # 2. Prices and tick/lot sizes
    logger.info("--- Calculating Trade Decision ---")
    logger.info("Fetching prices and exchange info for sizing...")
    price_long = long_venue_client.get_price(long_symbol)
    price_short = short_venue_client.get_price(short_symbol)
    logger.info(f"[{long_symbol}] Mark Price (Long): {price_long}")
    logger.info(f"[{short_symbol}] Mark Price (Short): {price_short}")

    # Long side increments
    if isinstance(long_venue_client, AsterClient):
        lf = long_venue_client.get_symbol_filters(long_symbol)
        long_tick = lf["tick_size"]
        long_step = lf["step_size"]
        logger.info(f"[{long_symbol}] Aster Increments: Tick={long_tick}, Step={long_step}")
    else:  # Hyperliquid
        lmk = long_venue_client._client.market(long_symbol)
        long_step = Decimal(str(lmk["precision"]["amount"]))
        if "limits" in lmk and lmk["limits"].get("price", {}).get("min"):
            long_tick = Decimal(str(lmk["limits"]["price"]["min"]))
        else:
            long_tick = Decimal(str(lmk["precision"]["price"]))
        logger.info(f"[{long_symbol}] Hyperliquid Increments: Tick={long_tick}, Step={long_step} (from precision: {lmk.get('precision')})")

    # Short side increments
    if isinstance(short_venue_client, AsterClient):
        sf = short_venue_client.get_symbol_filters(short_symbol)
        short_tick = sf["tick_size"]
        short_step = sf["step_size"]
        logger.info(f"[{short_symbol}] Aster Increments: Tick={short_tick}, Step={short_step}")
    else:  # Hyperliquid
        smk = short_venue_client._client.market(short_symbol)
        short_step = Decimal(str(smk["precision"]["amount"]))
        if "limits" in smk and smk["limits"].get("price", {}).get("min"):
            short_tick = Decimal(str(smk["limits"]["price"]["min"]))
        else:
            short_tick = Decimal(str(smk["precision"]["price"]))
        logger.info(f"[{short_symbol}] Hyperliquid Increments: Tick={short_tick}, Step={short_step} (from precision: {smk.get('precision')})")

    # 3. Quantities based on capital & leverage
    notional_value = capital_usd * Decimal(leverage)
    qty_long = notional_value / price_long
    qty_short = notional_value / price_short

    # 4. Round quantities to lot steps
    qty_long_unrounded = qty_long
    qty_short_unrounded = qty_short
    qty_long = round_qty_down(qty_long, long_step)
    qty_short = round_qty_down(qty_short, short_step)
    logger.info(f"[{long_symbol}] Quantity: {qty_long_unrounded} -> Rounded: {qty_long}")
    logger.info(f"[{short_symbol}] Quantity: {qty_short_unrounded} -> Rounded: {qty_short}")

    if qty_long <= 0 or qty_short <= 0:
        logger.error("Calculated quantity is zero. Increase capital or leverage.")
        return None

    # 5. Compute post-only limit prices with spread bias
    long_spread_abs = compute_spread_abs(price_long, long_tick, spread_ticks=spread_ticks, spread_bps=spread_bps)
    short_spread_abs = compute_spread_abs(price_short, short_tick, spread_ticks=spread_ticks, spread_bps=spread_bps)

    # Long leg (BUY): place below current price -> floor
    long_limit_price = floor_to_step(price_long - long_spread_abs, long_tick)
    logger.info(f"[{long_symbol}] BUY limit price calc: {price_long} (mark) - {long_spread_abs} (spread) -> {long_limit_price}")

    # Short leg (SELL): place above current price -> ceil
    short_limit_price = ceil_to_step(price_short + short_spread_abs, short_tick)
    logger.info(f"[{short_symbol}] SELL limit price calc: {price_short} (mark) + {short_spread_abs} (spread) -> {short_limit_price}")

    # Safety: ensure prices moved at least 1 tick to the passive side
    if long_limit_price >= price_long:
        original_price = long_limit_price
        long_limit_price = floor_to_step(price_long - long_tick, long_tick)
        logger.warning(f"[{long_symbol}] Safety check triggered. BUY price {original_price} >= {price_long} (mark). Adjusted to {long_limit_price}")
    if short_limit_price <= price_short:
        original_price = short_limit_price
        short_limit_price = ceil_to_step(price_short + short_tick, short_tick)
        logger.warning(f"[{short_symbol}] Safety check triggered. SELL price {original_price} <= {price_short} (mark). Adjusted to {short_limit_price}")

    # 6. Return decision incl. post-only target prices
    return StrategyDecision(
        opportunity=best_opp,
        long_qty=qty_long,
        short_qty=qty_short,
        long_symbol=long_symbol,
        short_symbol=short_symbol,
        margin=capital_usd,
        leverage=leverage,
        long_limit_price=long_limit_price,
        short_limit_price=short_limit_price,
        prefer_post_only=True,
        spread_ticks=spread_ticks,
        spread_bps=spread_bps,
    )


def _get_current_position_apy(
    hl_positions: List[Dict],
    aster_positions: List[Dict],
    opportunities: List[FundingComparison],
) -> Optional[Decimal]:
    """Returns the APY of the current position if it matches any known opportunity."""
    if not hl_positions or not aster_positions:
        return None
    for opp in opportunities:
        if _is_portfolio_matching_opportunity(hl_positions, aster_positions, opp):
            return abs(opp.apy_difference)
    return None


def perform_hourly_rebalance(
    aster_client: AsterClient,
    hyperliquid_client: HyperliquidClient,
    leverage: int,
    capital_usd: Decimal,
    min_apy_diff_pct: Decimal,
    spread_ticks: int,
    cleanup_timeout_seconds: int,
    rebalance_hysteresis_pct: Decimal = Decimal("20"),
    notifier: Optional["DiscordNotifier"] = None,
    exec_cfg: Optional[ExecutionConfig] = None,
    basis_tracker: Optional[BasisTracker] = None,
    should_abort: Optional[Callable[[], bool]] = None,
    on_tick: Optional[Callable[[], None]] = None,
) -> None:
    """
    Main strategy function to rebalance the portfolio hourly to the best opportunity.
    It closes existing positions and opens a new one based on funding and spread.
    """
    logger.info("--- Performing Hourly Rebalance ---")
    if exec_cfg is None or basis_tracker is None:
        raise ValueError("exec_cfg and basis_tracker are required")

    # 1. Find all opportunities.
    opportunities = fetch_and_compare_funding_rates(
        aster_client, hyperliquid_client, imminent_funding_minutes=60  # Use a wide window
    )
    actionable_opportunities = [
        opp for opp in opportunities if opp.is_actionable and abs(opp.apy_difference) > min_apy_diff_pct
    ]
    log_event("scan", candidates=[(o.symbol, o.long_venue, o.apy_difference, o.is_actionable) for o in opportunities],
              actionable=len(actionable_opportunities), min_apy_diff_pct=min_apy_diff_pct)

    if not actionable_opportunities:
        logger.info(
            "No actionable opportunities found meeting the minimum APY difference of %s%%. Waiting for next cycle.",
            min_apy_diff_pct,
        )
        if notifier:
            notifier.notify_no_opportunity(min_apy_diff_pct)
        return

    # 1b. Fee break-even + basis gate: first candidate (best APY first) that passes wins.
    best_opp = None
    gate_info = None
    for opp in actionable_opportunities:
        try:
            snap = market_snapshot(aster_client, hyperliquid_client, opp.symbol)
            basis_now = snap["basis_bps"]
        except Exception as exc:
            logger.warning("Could not read books for %s: %s", opp.symbol, exc)
            log_event("gate", decision="skip", reason_code="books_unavailable", symbol=opp.symbol,
                      long_venue=opp.long_venue, net_apy_pct=opp.apy_difference, error=str(exc)[:200])
            continue
        basis_tracker.record_bps(opp.symbol, basis_now)  # keep history warm
        info = evaluate_entry_gate(opp, exec_cfg, basis_tracker, basis_now, snap)
        if info["ok"]:
            best_opp, gate_info = opp, info
            break
        logger.info("Gate skipped %s: %s (break-even %.1fh, effective cost %.1f bps)",
                    opp.symbol, info["reason_code"], info["breakeven_hours"], info["effective_cost_bps"])
    if best_opp is None:
        logger.info("No candidate passed the fee/basis gate. Waiting for next cycle.")
        if notifier:
            notifier.notify_no_opportunity(min_apy_diff_pct)
        return

    # 2. Determine effective leverage.
    effective_leverage = min(
        leverage, best_opp.long_max_leverage or 1, best_opp.short_max_leverage or 1
    )
    logger.info(f"Selected best opportunity: {best_opp}")
    logger.info(f"Effective leverage set to {effective_leverage}x.")

    # 3. Check if the current portfolio already matches the best opportunity.
    hl_positions = hyperliquid_client.get_all_positions()
    aster_positions = aster_client.get_all_positions()
    logger.info(f"Current positions: Hyperliquid={hl_positions}, Aster={aster_positions}")

    if _is_portfolio_matching_opportunity(hl_positions, aster_positions, best_opp):
        logger.info("Already in optimal position for imminent funding. Holding position.")
        log_event("gate", decision="hold", reason_code="already_in_best", symbol=best_opp.symbol)
        if notifier:
            notifier.notify_holding(best_opp.symbol, abs(best_opp.apy_difference))
        return

    # 3b. Hysteresis + switch economics: only rebalance if the improvement repays a round trip.
    current_apy = _get_current_position_apy(hl_positions, aster_positions, opportunities)
    if current_apy is not None:
        improvement = abs(best_opp.apy_difference) - current_apy
        if improvement < rebalance_hysteresis_pct:
            logger.info(
                "New opportunity (%.2f%% APY) is only %.2f%% better than current (%.2f%% APY). "
                "Hysteresis threshold is %.2f%%. Holding current position.",
                abs(best_opp.apy_difference), improvement, current_apy, rebalance_hysteresis_pct,
            )
            log_event("gate", decision="hold", reason_code="hysteresis", symbol=best_opp.symbol,
                      current_apy_pct=current_apy, new_apy_pct=abs(best_opp.apy_difference))
            return
        if not evaluate_switch_gate(current_apy, abs(best_opp.apy_difference), exec_cfg, best_opp.symbol):
            logger.info("Switch to %s does not repay its round-trip cost over %.0fh. Holding.",
                        best_opp.symbol, exec_cfg.expected_hold_hours)
            return
        logger.info(
            "New opportunity is %.2f%% APY better than current (%.2f%% -> %.2f%%). Rebalancing.",
            improvement, current_apy, abs(best_opp.apy_difference),
        )
    elif hl_positions or aster_positions:
        log_event("note", message="positions open but not matching any scanned opportunity; will close and re-enter",
                  hl=len(hl_positions), aster=len(aster_positions))

    # 4. Calculate the new trade.
    decision = _calculate_trade_decision(
        aster_client,
        hyperliquid_client,
        best_opp,
        effective_leverage,
        capital_usd,
        spread_ticks=spread_ticks,
        spread_bps=None,
    )
    if not decision:
        logger.error("Failed to calculate trade decision. Aborting rebalance.")
        return

    # 5. Close all open positions and orders
    if hl_positions or aster_positions:
        logger.info("Closing all existing positions and orders before finding new opportunity...")
        if notifier:
            notifier.notify_trade_closed(reason="rebalancing to better opportunity")
        close_positions_with_execution(aster_client, hyperliquid_client, exec_cfg, context="rebalance_close",
                                       fallback_timeout_seconds=cleanup_timeout_seconds, on_tick=on_tick)
        time.sleep(15)  # Allow time for balance updates after closing positions.

    # 6. Execute the trade.
    execute_strategy(aster_client, hyperliquid_client, decision, notifier=notifier, exec_cfg=exec_cfg,
                     entry_basis_bps=gate_info["basis_now_bps"] if gate_info else None,
                     time_budget_s=cleanup_timeout_seconds, should_abort=should_abort, on_tick=on_tick)


def execute_strategy(
    aster_client: AsterClient,
    hyperliquid_client: HyperliquidClient,
    decision: StrategyDecision,
    notifier: Optional["DiscordNotifier"] = None,
    exec_cfg: Optional[ExecutionConfig] = None,
    entry_basis_bps: Optional[Decimal] = None,
    time_budget_s: float = 300.0,
    should_abort: Optional[Callable[[], bool]] = None,
    on_tick: Optional[Callable[[], None]] = None,
) -> None:
    """
    Executes a pre-determined strategy: sets leverage, then runs both legs through
    ``execution.execute_pair`` (imbalance/queue-aware passive-or-cross per leg).
    """
    logger.info(f"--- Executing Delta-Neutral Strategy for {decision.opportunity.symbol} ---")
    if exec_cfg is None:
        raise ValueError("exec_cfg is required")

    long_venue_client = aster_client if decision.opportunity.long_venue == "Aster" else hyperliquid_client
    short_venue_client = hyperliquid_client if decision.opportunity.long_venue == "Aster" else aster_client

    # 1. Set leverage on both exchanges
    logger.info("Setting leverage to %sx on both venues...", decision.leverage)
    long_venue_client.set_leverage(decision.long_symbol, decision.leverage)
    short_venue_client.set_leverage(decision.short_symbol, decision.leverage)

    # 2. Execute both legs
    entry_started_at = datetime.now(timezone.utc).isoformat()  # fills/fees of this entry are dated from here
    legs = [
        Leg(long_venue_client, decision.long_symbol, "buy", decision.long_qty),
        Leg(short_venue_client, decision.short_symbol, "sell", decision.short_qty),
    ]
    result = execute_pair(legs, exec_cfg, context=f"entry:{decision.opportunity.symbol}", max_total_s=float(time_budget_s),
                          should_abort=should_abort, on_tick=on_tick)
    logger.info("Execution result: %s", result.summary())
    if all(leg.filled == 0 for leg in legs):
        logger.warning("Nothing filled (anchor never traded); no position, no fees. Waiting for the next window.")
        return
    if any(leg.filled > 0 for leg in legs):
        # Persist immediately: if the process dies during verification the basis exit still knows the entry.
        save_position_state({
            "symbol": decision.opportunity.symbol, "long_venue": decision.opportunity.long_venue,
            "short_venue": decision.opportunity.short_venue,
            "entry_basis_bps": float(entry_basis_bps) if entry_basis_bps is not None else None,
            "net_apy_pct": float(decision.opportunity.apy_difference),
            "entered_at": datetime.now(timezone.utc).isoformat(), "entry_started_at": entry_started_at, "verified": False,
            "legs": result.summary()["legs"],
        })

    for leg, side, venue in ((legs[0], "BUY", decision.opportunity.long_venue), (legs[1], "SELL", decision.opportunity.short_venue)):
        if leg.filled > 0:
            log_trade(
                TRADE_LOG_PATH,
                action="OPEN", symbol=decision.opportunity.symbol, side=side, venue=venue,
                quantity=leg.filled, price=leg.avg_price or Decimal(0), leverage=decision.leverage,
                funding_rate=decision.opportunity.rate_aster if venue == "Aster" else decision.opportunity.rate_hyperliquid,
                apy_difference=decision.opportunity.apy_difference,
                notes=f"basis={decision.opportunity.apy_difference_basis} | tactic={leg.plan.tactic if leg.plan else '?'}->{leg.current_tactic}",
            )

    # 3. Verify positions were opened successfully.
    logger.info("Verifying positions are open and match the strategy...")
    start_time = time.time()
    timeout_seconds = 30
    verified = False
    while time.time() - start_time < timeout_seconds:
        try:
            hl_positions = hyperliquid_client.get_all_positions()
            aster_positions = aster_client.get_all_positions()
            if _is_portfolio_matching_opportunity(hl_positions, aster_positions, decision.opportunity):
                logger.info("Successfully verified new positions are open.")
                verified = True
                save_position_state({
                    "symbol": decision.opportunity.symbol, "long_venue": decision.opportunity.long_venue,
                    "short_venue": decision.opportunity.short_venue,
                    "entry_basis_bps": float(entry_basis_bps) if entry_basis_bps is not None else None,
                    "net_apy_pct": float(decision.opportunity.apy_difference),
                    "entered_at": datetime.now(timezone.utc).isoformat(), "entry_started_at": entry_started_at,
                    "legs": result.summary()["legs"],
                })
                if notifier:
                    notifier.notify_trade_opened(
                        symbol=decision.opportunity.symbol,
                        long_venue=decision.opportunity.long_venue,
                        short_venue=decision.opportunity.short_venue,
                        apy_difference=abs(decision.opportunity.apy_difference),
                        leverage=decision.leverage,
                        capital=decision.margin,
                    )
                break

            logger.info(f"Waiting for positions to open. HL: {len(hl_positions)}, Aster: {len(aster_positions)}")
            time.sleep(2)
        except Exception as exc:
            logger.warning(f"Error during position opening verification, retrying: {exc}")
            time.sleep(2)

    if not verified:
        logger.error(f"Timeout: Positions not confirmed open after {timeout_seconds} seconds.")
        # Partial fill rollback: if only one side filled, close it to avoid naked exposure.
        try:
            hl_positions = hyperliquid_client.get_all_positions()
            aster_positions = aster_client.get_all_positions()
            hl_has_pos = len(hl_positions) > 0
            aster_has_pos = len(aster_positions) > 0

            if hl_has_pos != aster_has_pos:
                reason = (
                    f"PARTIAL FILL: HL has {len(hl_positions)} position(s), "
                    f"Aster has {len(aster_positions)}. Closing to avoid unhedged exposure."
                )
                logger.warning(reason)
                if notifier:
                    notifier.notify_rollback(reason)
                cleanup_all_open_positions_and_orders(
                    aster_client, hyperliquid_client, timeout_seconds=60, close_spread_ticks=1,
                )
            elif hl_has_pos and aster_has_pos:
                reason = (
                    "Both sides have positions but they don't match the expected opportunity. "
                    "Closing all to avoid mismatched exposure."
                )
                logger.warning(reason)
                if notifier:
                    notifier.notify_rollback(reason)
                cleanup_all_open_positions_and_orders(
                    aster_client, hyperliquid_client, timeout_seconds=60, close_spread_ticks=1,
                )
        except Exception as exc:
            logger.error(f"Error during partial fill rollback: {exc}")

    logger.info("Strategy execution complete.")


def close_positions_with_execution(
    aster_client: AsterClient,
    hyperliquid_client: HyperliquidClient,
    exec_cfg: ExecutionConfig,
    *,
    context: str,
    fallback_timeout_seconds: int = 300,
    on_tick: Optional[Callable[[], None]] = None,
) -> None:
    """Close every open position with reduce-only legs through execute_pair (anchor at the touch,
    short patience: an exit is time-sensitive); fall back to the robust cleanup routine if anything is left."""
    legs: List[Leg] = []
    try:
        for pos in hyperliquid_client.get_all_positions():
            qty = abs(Decimal(str(pos.get("contracts") or 0)))
            if qty > 0 and pos.get("symbol"):
                legs.append(Leg(hyperliquid_client, pos["symbol"], "sell" if pos.get("side") == "long" else "buy", qty, reduce_only=True))
        for pos in aster_client.get_all_positions():
            amt = Decimal(str(pos.get("positionAmt") or 0))
            if amt != 0 and pos.get("symbol"):
                legs.append(Leg(aster_client, pos["symbol"], "sell" if amt > 0 else "buy", abs(amt), reduce_only=True))
    except Exception as exc:
        logger.warning("Could not enumerate positions for execution close (%s); using cleanup", exc)
    if legs:
        # cancel resting orders first so reduce-only sizes are right
        try:
            for o in aster_client.get_all_open_orders():
                if o.get("symbol") and o.get("orderId") is not None:
                    aster_client.cancel_by_id(o["symbol"], str(o["orderId"]))
            for o in hyperliquid_client.get_all_open_orders():
                if o.get("symbol") and o.get("id"):
                    hyperliquid_client.cancel_by_id(o["symbol"], str(o["id"]))
        except Exception as exc:
            logger.warning("Cancelling open orders before close failed: %s", exc)
        result = execute_pair(legs, exec_cfg, context=context, ladder=False, anchor_wait_s=exec_cfg.exit_max_wait_s,
                              max_total_s=float(exec_cfg.exit_max_wait_s + 60), on_tick=on_tick)
        for leg in legs:
            if leg.filled > 0:
                base = leg.symbol.split("/")[0].replace("USDT", "")
                log_trade(TRADE_LOG_PATH, action="CLOSE", symbol=base, side=leg.side.upper(), venue=leg.venue_name,
                          quantity=leg.filled, price=leg.avg_price or Decimal(0),
                          notes=f"{context} | tactic={leg.plan.tactic if leg.plan else '?'}->{leg.current_tactic}")
        if result.hedged:
            save_position_state(None)
    # Whatever is left (partial, errors, no legs): robust path.
    cleanup_all_open_positions_and_orders(aster_client, hyperliquid_client, timeout_seconds=fallback_timeout_seconds, close_spread_ticks=1)
    save_position_state(None)


def check_basis_exit(
    aster_client: AsterClient,
    hyperliquid_client: HyperliquidClient,
    exec_cfg: ExecutionConfig,
    tracker: BasisTracker,
    notifier: Optional["DiscordNotifier"] = None,
) -> bool:
    """Close the pair when the basis has moved in our favour far enough to pay the exit and then some.

    Condition: realised basis gain since entry >= exit fees + margin AND the basis is now
    stretched against us (favorable z <= -z_exit), i.e. mean reversion would give the gain back.
    Returns True when an exit was executed.
    """
    state = load_position_state()
    if not state:
        return False
    symbol, long_venue = state["symbol"], state["long_venue"]
    try:
        now_bps = current_basis(aster_client, hyperliquid_client, symbol)
    except Exception as exc:
        logger.debug("basis exit check: cannot read basis for %s: %s", symbol, exc)
        return False
    entry = state.get("entry_basis_bps")
    if entry is None:  # position predates the tracker: adopt the first observation as entry
        state["entry_basis_bps"] = float(now_bps)
        state["entry_basis_note"] = "adopted after restart"
        save_position_state(state)
        return False
    gain = basis_gain_bps(Decimal(str(entry)), now_bps, long_venue)
    stats = tracker.stats(symbol, now_bps, exec_cfg.basis_window_hours, exec_cfg.basis_min_samples)
    fz = favorable_z(stats, long_venue)
    # Closing costs taker fees + crossing the tighter book, and gives up the funding this position
    # would earn over the expected hold. The basis must pay for all of that, plus a margin.
    try:
        snap = market_snapshot(aster_client, hyperliquid_client, symbol)
        crossing = hedge_cross_bps(snap)
    except Exception:
        crossing = Decimal(0)
    funding_forgone = funding_bps_per_hour(Decimal(str(state.get("net_apy_pct") or 0))) * Decimal(str(exec_cfg.expected_hold_hours))
    threshold = Decimal(str(exec_cfg.exit_cost_bps + exec_cfg.basis_exit_min_gain_bps)) + crossing + funding_forgone
    stretched = fz is not None and fz <= -Decimal(str(exec_cfg.z_exit))
    if not (gain >= threshold and stretched):
        return False
    log_event("basis_exit", symbol=symbol, long_venue=long_venue, entry_basis_bps=entry, basis_now_bps=now_bps,
              gain_bps=gain, threshold_bps=threshold, crossing_bps=crossing, funding_forgone_bps=funding_forgone,
              favorable_z=fz, z=stats.z, basis_mean_bps=stats.mean_bps,
              basis_std_bps=stats.std_bps, samples=stats.n, net_apy_pct=state.get("net_apy_pct"))
    logger.warning("BASIS EXIT %s: gain %.1f bps >= %.1f bps and favorable z %.2f <= -%.2f. Closing pair.",
                   symbol, gain, threshold, fz, exec_cfg.z_exit)
    if notifier:
        notifier.notify_trade_closed(reason=f"basis exit on {symbol}: +{gain:.1f} bps captured")
    close_positions_with_execution(aster_client, hyperliquid_client, exec_cfg, context=f"basis_exit:{symbol}")
    return True


def cleanup_all_open_positions_and_orders(
    aster_client: AsterClient,
    hyperliquid_client: HyperliquidClient,
    timeout_seconds: int = 900,
    close_spread_ticks: int = 1,
) -> None:
    """
    Cleans up by cancelling all open orders and closing all open positions.
    """
    logger.info("--- Starting cleanup: Cancelling all open orders and closing all positions ---")

    # 1. Cancel all open orders to prevent them from creating new positions
    logger.info("--- Cancelling open orders ---")
    try:
        aster_orders = aster_client.get_all_open_orders()
        if aster_orders:
            logger.info(f"Found {len(aster_orders)} open order(s) on Aster. Cancelling them...")
            for order in aster_orders:
                symbol = order.get("symbol")
                client_order_id = order.get("clientOrderId")
                if symbol and client_order_id:
                    try:
                        aster_client.cancel_order(symbol=symbol, orig_client_order_id=client_order_id)
                    except Exception as exc:
                        logger.error(f"Failed to cancel order {client_order_id} for {symbol} on Aster: {exc}")
        else:
            logger.info("No open orders found on Aster.")
    except Exception as exc:
        logger.error(f"Failed to get open orders from Aster: {exc}")

    try:
        hl_orders = hyperliquid_client.get_all_open_orders()
        if hl_orders:
            logger.info(f"Found {len(hl_orders)} open order(s) on Hyperliquid. Cancelling them...")
            for order in hl_orders:
                symbol = order.get("symbol")
                order_id = order.get("id")
                if symbol and order_id:
                    try:
                        hyperliquid_client.cancel_order(symbol=symbol, order_id=order_id)
                    except Exception as exc:
                        logger.error(f"Failed to cancel order {order_id} for {symbol} on Hyperliquid: {exc}")
        else:
            logger.info("No open orders found on Hyperliquid.")
    except Exception as exc:
        logger.error(f"Failed to get open orders from Hyperliquid: {exc}")

    # 2. Close all open positions
    logger.info("--- Closing open positions ---")
    try:
        aster_positions = aster_client.get_all_positions()
        if aster_positions:
            logger.info(f"Found {len(aster_positions)} open position(s) on Aster. Closing them...")
            for pos in aster_positions:
                symbol = pos.get("symbol")
                if symbol:
                    try:
                        aster_client.close_position(symbol, spread_ticks=close_spread_ticks)
                        pos_amt = abs(Decimal(pos.get("positionAmt", "0")))
                        price = aster_client.get_price(symbol)
                        side = "SELL" if Decimal(pos.get("positionAmt", "0")) > 0 else "BUY"
                        log_trade(
                            TRADE_LOG_PATH, action="CLOSE", symbol=symbol,
                            side=side, venue="Aster", quantity=pos_amt, price=price,
                        )
                    except Exception as exc:
                        logger.error(f"Failed to close position for {symbol} on Aster: {exc}")
        else:
            logger.info("No open positions found on Aster.")
    except Exception as exc:
        logger.error(f"Failed to get positions from Aster: {exc}")

    try:
        hl_positions = hyperliquid_client.get_all_positions()
        if hl_positions:
            logger.info(f"Found {len(hl_positions)} open position(s) on Hyperliquid. Closing them...")
            for pos in hl_positions:
                symbol = pos.get("symbol")
                if symbol:
                    try:
                        hyperliquid_client.close_position(symbol, spread_ticks=close_spread_ticks)
                        contracts = abs(Decimal(str(pos.get("contracts", "0"))))
                        price = hyperliquid_client.get_price(symbol)
                        side = "SELL" if pos.get("side") == "long" else "BUY"
                        log_trade(
                            TRADE_LOG_PATH, action="CLOSE", symbol=symbol,
                            side=side, venue="Hyperliquid", quantity=contracts, price=price,
                        )
                    except Exception as exc:
                        logger.error(f"Failed to close position for {symbol} on Hyperliquid: {exc}")
        else:
            logger.info("No open positions found on Hyperliquid.")
    except Exception as exc:
        logger.error(f"Failed to get positions from Hyperliquid: {exc}")

    # 3. Verify all positions are closed before proceeding.
    logger.info("Verifying all positions are closed...")
    start_time = time.time()
    while time.time() - start_time < timeout_seconds:
        try:
            hl_positions = hyperliquid_client.get_all_positions()
            aster_positions = aster_client.get_all_positions()
            if not hl_positions and not aster_positions:
                logger.info("Successfully verified all positions are closed.")
                break

            logger.info(f"Waiting for positions to close. HL: {len(hl_positions)}, Aster: {len(aster_positions)}")
            time.sleep(30)
        except Exception as exc:
            logger.warning(f"Error during position closure verification, retrying: {exc}")
            time.sleep(2)
    else:
        # This block runs if the while loop times out without a 'break'
        logger.error(f"Timeout: Positions not confirmed closed after {timeout_seconds} seconds.")
        # Depending on desired behavior, we could raise an exception here to halt operations.

    logger.info("--- Cleanup complete ---")
