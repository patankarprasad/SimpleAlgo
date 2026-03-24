"""
Place and close orders on Zerodha Kite.

Instrument dict must be resolved (scrip_master.resolve_instrument) and contains:
    kite_tradingsymbol  – e.g. "GOLDM26APRFUT"
    kite_exchange       – e.g. "MCX"
    lot_size            – from scrip master (e.g. 1 for GOLDM)
    qty                 – number of lots (from config)
    product             – "NRML" or "MIS"

Order quantity sent to Kite = qty × lot_size
All orders are MARKET orders.
"""
import logging

from kiteconnect import KiteConnect

logger = logging.getLogger(__name__)


def _order_qty(instrument: dict) -> int:
    """Total units = lots × lot_size (both come from the resolved instrument dict)."""
    return instrument["qty"] * instrument["lot_size"]


def place_buy(kite: KiteConnect, instrument: dict) -> str:
    """Open a long (BUY) position."""
    if kite is None:
        raise RuntimeError(f"Kite session unavailable — BUY order not placed for {instrument['name']}")
    qty      = _order_qty(instrument)
    order_id = kite.place_order(
        variety          = kite.VARIETY_REGULAR,
        exchange         = instrument["exchange"],
        tradingsymbol    = instrument["kite_tradingsymbol"],
        transaction_type = kite.TRANSACTION_TYPE_BUY,
        quantity         = qty,
        order_type       = kite.ORDER_TYPE_MARKET,
        product          = instrument["product"],
    )
    logger.info(
        "BUY order placed | %s | symbol=%s | qty=%d | order_id=%s",
        instrument["name"], instrument["kite_tradingsymbol"], qty, order_id,
    )
    return order_id


def place_sell(kite: KiteConnect, instrument: dict) -> str:
    """Open a short (SELL) position."""
    if kite is None:
        raise RuntimeError(f"Kite session unavailable — SELL order not placed for {instrument['name']}")
    qty      = _order_qty(instrument)
    order_id = kite.place_order(
        variety          = kite.VARIETY_REGULAR,
        exchange         = instrument["exchange"],
        tradingsymbol    = instrument["kite_tradingsymbol"],
        transaction_type = kite.TRANSACTION_TYPE_SELL,
        quantity         = qty,
        order_type       = kite.ORDER_TYPE_MARKET,
        product          = instrument["product"],
    )
    logger.info(
        "SELL order placed | %s | symbol=%s | qty=%d | order_id=%s",
        instrument["name"], instrument["kite_tradingsymbol"], qty, order_id,
    )
    return order_id


def close_long(kite: KiteConnect, instrument: dict) -> str:
    """Close an existing long position (sell to exit)."""
    if kite is None:
        raise RuntimeError(f"Kite session unavailable — EXIT LONG order not placed for {instrument['name']}")
    qty      = _order_qty(instrument)
    order_id = kite.place_order(
        variety          = kite.VARIETY_REGULAR,
        exchange         = instrument["exchange"],
        tradingsymbol    = instrument["kite_tradingsymbol"],
        transaction_type = kite.TRANSACTION_TYPE_SELL,
        quantity         = qty,
        order_type       = kite.ORDER_TYPE_MARKET,
        product          = instrument["product"],
    )
    logger.info(
        "EXIT LONG order placed | %s | symbol=%s | qty=%d | order_id=%s",
        instrument["name"], instrument["kite_tradingsymbol"], qty, order_id,
    )
    return order_id


def close_short(kite: KiteConnect, instrument: dict) -> str:
    """Close an existing short position (buy to exit)."""
    if kite is None:
        raise RuntimeError(f"Kite session unavailable — EXIT SHORT order not placed for {instrument['name']}")
    qty      = _order_qty(instrument)
    order_id = kite.place_order(
        variety          = kite.VARIETY_REGULAR,
        exchange         = instrument["exchange"],
        tradingsymbol    = instrument["kite_tradingsymbol"],
        transaction_type = kite.TRANSACTION_TYPE_BUY,
        quantity         = qty,
        order_type       = kite.ORDER_TYPE_MARKET,
        product          = instrument["product"],
    )
    logger.info(
        "EXIT SHORT order placed | %s | symbol=%s | qty=%d | order_id=%s",
        instrument["name"], instrument["kite_tradingsymbol"], qty, order_id,
    )
    return order_id


def square_off_all(kite: KiteConnect, resolved_instruments: list, state: dict):
    """Emergency / EOD square-off of every tracked open position."""
    from state import get_position
    for instrument in resolved_instruments:
        pos = get_position(state, instrument["name"])
        if pos > 0:
            close_long(kite, instrument)
        elif pos < 0:
            close_short(kite, instrument)
