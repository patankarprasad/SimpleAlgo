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


# ══════════════════════════════════════════════════════════════════════════════
# Synthetic futures helpers (NIFTY / BANKNIFTY)
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_option_ltps(
    kite: KiteConnect, ce_symbol: str, pe_symbol: str, exchange: str = "NFO"
) -> tuple[float, float]:
    """Fetch live last-traded prices for the two synthetic legs from Kite."""
    ce_key = f"{exchange}:{ce_symbol}"
    pe_key = f"{exchange}:{pe_symbol}"
    try:
        ltps = kite.ltp([ce_key, pe_key])
    except Exception as exc:
        logger.warning("kite.ltp() failed for %s / %s: %s", ce_symbol, pe_symbol, exc)
        return 0.0, 0.0
    ce_ltp = ltps.get(ce_key, {}).get("last_price", 0.0)
    pe_ltp = ltps.get(pe_key, {}).get("last_price", 0.0)
    if ce_ltp == 0.0 or pe_ltp == 0.0:
        logger.warning(
            "LTP missing from Kite response for %s (%.2f) / %s (%.2f)",
            ce_symbol, ce_ltp, pe_symbol, pe_ltp,
        )
    return ce_ltp, pe_ltp


def place_synthetic_buy(
    kite: KiteConnect,
    instrument: dict,
    ce_info: dict,
    pe_info: dict,
) -> tuple[str, str, float, float]:
    """
    Open a long synthetic future: BUY CE + SELL PE.

    Returns (ce_order_id, pe_order_id, entry_ce_ltp, entry_pe_ltp).
    If Leg 2 (SELL PE) fails after Leg 1 (BUY CE) has been placed, a CRITICAL
    log is emitted and RuntimeError is raised so the caller does NOT persist
    position state (preventing phantom positions). Manual Kite intervention
    is required to close the orphaned CE leg.
    """
    if kite is None:
        raise RuntimeError(
            f"Kite session unavailable — synthetic BUY not placed for {instrument['name']}"
        )
    qty      = instrument["qty"] * instrument["lot_size"]
    exchange = ce_info.get("exchange", "NFO")

    # Leg 1: BUY CE
    ce_order_id = kite.place_order(
        variety          = kite.VARIETY_REGULAR,
        exchange         = exchange,
        tradingsymbol    = ce_info["kite_tradingsymbol"],
        transaction_type = kite.TRANSACTION_TYPE_BUY,
        quantity         = qty,
        order_type       = kite.ORDER_TYPE_MARKET,
        product          = instrument["product"],
    )
    logger.info(
        "Synthetic BUY CE placed | %s | symbol=%s | qty=%d | order_id=%s",
        instrument["name"], ce_info["kite_tradingsymbol"], qty, ce_order_id,
    )

    # Leg 2: SELL PE
    try:
        pe_order_id = kite.place_order(
            variety          = kite.VARIETY_REGULAR,
            exchange         = exchange,
            tradingsymbol    = pe_info["kite_tradingsymbol"],
            transaction_type = kite.TRANSACTION_TYPE_SELL,
            quantity         = qty,
            order_type       = kite.ORDER_TYPE_MARKET,
            product          = instrument["product"],
        )
        logger.info(
            "Synthetic BUY PE (SELL) placed | %s | symbol=%s | qty=%d | order_id=%s",
            instrument["name"], pe_info["kite_tradingsymbol"], qty, pe_order_id,
        )
    except Exception as exc:
        logger.critical(
            "PARTIAL SYNTHETIC FILL — %s: CE leg placed (id=%s, symbol=%s) "
            "but SELL PE FAILED (%s: %s). MANUAL INTERVENTION REQUIRED on Kite.",
            instrument["name"], ce_order_id, ce_info["kite_tradingsymbol"],
            pe_info["kite_tradingsymbol"], exc,
        )
        raise RuntimeError(
            f"Synthetic BUY partial fill for {instrument['name']}: "
            f"CE placed (id={ce_order_id}) but PE SELL failed: {exc}"
        ) from exc

    ce_ltp, pe_ltp = _fetch_option_ltps(
        kite, ce_info["kite_tradingsymbol"], pe_info["kite_tradingsymbol"], exchange
    )
    return ce_order_id, pe_order_id, ce_ltp, pe_ltp


def place_synthetic_sell(
    kite: KiteConnect,
    instrument: dict,
    ce_info: dict,
    pe_info: dict,
) -> tuple[str, str, float, float]:
    """
    Open a short synthetic future: BUY PE + SELL CE.

    Returns (ce_order_id, pe_order_id, entry_ce_ltp, entry_pe_ltp).
    Same partial-fill safety as place_synthetic_buy.
    """
    if kite is None:
        raise RuntimeError(
            f"Kite session unavailable — synthetic SELL not placed for {instrument['name']}"
        )
    qty      = instrument["qty"] * instrument["lot_size"]
    exchange = pe_info.get("exchange", "NFO")

    # Leg 1: BUY PE
    pe_order_id = kite.place_order(
        variety          = kite.VARIETY_REGULAR,
        exchange         = exchange,
        tradingsymbol    = pe_info["kite_tradingsymbol"],
        transaction_type = kite.TRANSACTION_TYPE_BUY,
        quantity         = qty,
        order_type       = kite.ORDER_TYPE_MARKET,
        product          = instrument["product"],
    )
    logger.info(
        "Synthetic SELL PE (BUY) placed | %s | symbol=%s | qty=%d | order_id=%s",
        instrument["name"], pe_info["kite_tradingsymbol"], qty, pe_order_id,
    )

    # Leg 2: SELL CE
    ce_order_id = None
    try:
        ce_order_id = kite.place_order(
            variety          = kite.VARIETY_REGULAR,
            exchange         = exchange,
            tradingsymbol    = ce_info["kite_tradingsymbol"],
            transaction_type = kite.TRANSACTION_TYPE_SELL,
            quantity         = qty,
            order_type       = kite.ORDER_TYPE_MARKET,
            product          = instrument["product"],
        )
        logger.info(
            "Synthetic SELL CE (SELL) placed | %s | symbol=%s | qty=%d | order_id=%s",
            instrument["name"], ce_info["kite_tradingsymbol"], qty, ce_order_id,
        )
    except Exception as exc:
        logger.critical(
            "PARTIAL SYNTHETIC FILL — %s: PE leg placed (id=%s, symbol=%s) "
            "but SELL CE FAILED (%s: %s). MANUAL INTERVENTION REQUIRED on Kite.",
            instrument["name"], pe_order_id, pe_info["kite_tradingsymbol"],
            ce_info["kite_tradingsymbol"], exc,
        )
        raise RuntimeError(
            f"Synthetic SELL partial fill for {instrument['name']}: "
            f"PE placed (id={pe_order_id}) but CE SELL failed: {exc}"
        ) from exc

    ce_ltp, pe_ltp = _fetch_option_ltps(
        kite, ce_info["kite_tradingsymbol"], pe_info["kite_tradingsymbol"], exchange
    )
    return ce_order_id, pe_order_id, ce_ltp, pe_ltp


def close_synthetic_long(
    kite: KiteConnect,
    instrument: dict,
    ce_symbol: str,
    pe_symbol: str,
    exchange: str = "NFO",
) -> tuple[str, str, float, float]:
    """
    Close a long synthetic future: SELL CE + BUY PE.

    Returns (ce_order_id, pe_order_id, exit_ce_ltp, exit_pe_ltp).
    Same partial-fill safety: if BUY PE fails after SELL CE, raises RuntimeError.
    """
    if kite is None:
        raise RuntimeError(
            f"Kite session unavailable — synthetic EXIT LONG not placed for {instrument['name']}"
        )
    qty = instrument["qty"] * instrument["lot_size"]

    # Leg 1: SELL CE
    ce_order_id = kite.place_order(
        variety          = kite.VARIETY_REGULAR,
        exchange         = exchange,
        tradingsymbol    = ce_symbol,
        transaction_type = kite.TRANSACTION_TYPE_SELL,
        quantity         = qty,
        order_type       = kite.ORDER_TYPE_MARKET,
        product          = instrument["product"],
    )
    logger.info(
        "Synthetic EXIT LONG SELL CE | %s | symbol=%s | order_id=%s",
        instrument["name"], ce_symbol, ce_order_id,
    )

    # Leg 2: BUY PE
    try:
        pe_order_id = kite.place_order(
            variety          = kite.VARIETY_REGULAR,
            exchange         = exchange,
            tradingsymbol    = pe_symbol,
            transaction_type = kite.TRANSACTION_TYPE_BUY,
            quantity         = qty,
            order_type       = kite.ORDER_TYPE_MARKET,
            product          = instrument["product"],
        )
        logger.info(
            "Synthetic EXIT LONG BUY PE | %s | symbol=%s | order_id=%s",
            instrument["name"], pe_symbol, pe_order_id,
        )
    except Exception as exc:
        logger.critical(
            "PARTIAL SYNTHETIC EXIT — %s: SELL CE placed (id=%s, symbol=%s) "
            "but BUY PE FAILED (%s: %s). MANUAL INTERVENTION REQUIRED on Kite.",
            instrument["name"], ce_order_id, ce_symbol, pe_symbol, exc,
        )
        raise RuntimeError(
            f"Synthetic EXIT LONG partial fail for {instrument['name']}: "
            f"CE closed (id={ce_order_id}) but PE BUY failed: {exc}"
        ) from exc

    exit_ce_ltp, exit_pe_ltp = _fetch_option_ltps(kite, ce_symbol, pe_symbol, exchange)
    return ce_order_id, pe_order_id, exit_ce_ltp, exit_pe_ltp


def close_synthetic_short(
    kite: KiteConnect,
    instrument: dict,
    ce_symbol: str,
    pe_symbol: str,
    exchange: str = "NFO",
) -> tuple[str, str, float, float]:
    """
    Close a short synthetic future: SELL PE + BUY CE.

    Returns (ce_order_id, pe_order_id, exit_ce_ltp, exit_pe_ltp).
    Same partial-fill safety.
    """
    if kite is None:
        raise RuntimeError(
            f"Kite session unavailable — synthetic EXIT SHORT not placed for {instrument['name']}"
        )
    qty = instrument["qty"] * instrument["lot_size"]

    # Leg 1: SELL PE
    pe_order_id = kite.place_order(
        variety          = kite.VARIETY_REGULAR,
        exchange         = exchange,
        tradingsymbol    = pe_symbol,
        transaction_type = kite.TRANSACTION_TYPE_SELL,
        quantity         = qty,
        order_type       = kite.ORDER_TYPE_MARKET,
        product          = instrument["product"],
    )
    logger.info(
        "Synthetic EXIT SHORT SELL PE | %s | symbol=%s | order_id=%s",
        instrument["name"], pe_symbol, pe_order_id,
    )

    # Leg 2: BUY CE
    try:
        ce_order_id = kite.place_order(
            variety          = kite.VARIETY_REGULAR,
            exchange         = exchange,
            tradingsymbol    = ce_symbol,
            transaction_type = kite.TRANSACTION_TYPE_BUY,
            quantity         = qty,
            order_type       = kite.ORDER_TYPE_MARKET,
            product          = instrument["product"],
        )
        logger.info(
            "Synthetic EXIT SHORT BUY CE | %s | symbol=%s | order_id=%s",
            instrument["name"], ce_symbol, ce_order_id,
        )
    except Exception as exc:
        logger.critical(
            "PARTIAL SYNTHETIC EXIT — %s: SELL PE placed (id=%s, symbol=%s) "
            "but BUY CE FAILED (%s: %s). MANUAL INTERVENTION REQUIRED on Kite.",
            instrument["name"], pe_order_id, pe_symbol, ce_symbol, exc,
        )
        raise RuntimeError(
            f"Synthetic EXIT SHORT partial fail for {instrument['name']}: "
            f"PE closed (id={pe_order_id}) but CE BUY failed: {exc}"
        ) from exc

    exit_ce_ltp, exit_pe_ltp = _fetch_option_ltps(kite, ce_symbol, pe_symbol, exchange)
    return ce_order_id, pe_order_id, exit_ce_ltp, exit_pe_ltp


def place_short_ce(
    kite: KiteConnect,
    instrument: dict,
    ce_info: dict,
) -> tuple[str, float]:
    """
    Open a short by SELLING a single CE option (premium collection strategy).

    Returns (order_id, entry_ltp).
    Used for NIFTY/BANKNIFTY SHORT signals instead of a 2-leg synthetic short.
    """
    if kite is None:
        raise RuntimeError(
            f"Kite session unavailable — short CE SELL not placed for {instrument['name']}"
        )
    qty      = instrument["qty"] * instrument["lot_size"]
    exchange = ce_info.get("exchange", "NFO")

    order_id = kite.place_order(
        variety          = kite.VARIETY_REGULAR,
        exchange         = exchange,
        tradingsymbol    = ce_info["kite_tradingsymbol"],
        transaction_type = kite.TRANSACTION_TYPE_SELL,
        quantity         = qty,
        order_type       = kite.ORDER_TYPE_MARKET,
        product          = instrument["product"],
    )
    logger.info(
        "Short CE SELL placed | %s | symbol=%s | strike=%s | qty=%d | order_id=%s",
        instrument["name"], ce_info["kite_tradingsymbol"],
        ce_info.get("strike", "?"), qty, order_id,
    )

    # Fetch entry LTP immediately after order placement
    ce_key = f"{exchange}:{ce_info['kite_tradingsymbol']}"
    try:
        ltps = kite.ltp([ce_key])
        entry_ltp = ltps.get(ce_key, {}).get("last_price", 0.0)
    except Exception as exc:
        logger.warning("Could not fetch CE LTP after short CE SELL (%s): %s",
                       ce_info["kite_tradingsymbol"], exc)
        entry_ltp = 0.0

    return order_id, entry_ltp


def close_short_ce(
    kite: KiteConnect,
    instrument: dict,
    ce_symbol: str,
    exchange: str = "NFO",
) -> tuple[str, float]:
    """
    Close a short CE position by BUYING back the CE.

    Returns (order_id, exit_ltp).
    """
    if kite is None:
        raise RuntimeError(
            f"Kite session unavailable — short CE BUY-back not placed for {instrument['name']}"
        )
    qty = instrument["qty"] * instrument["lot_size"]

    order_id = kite.place_order(
        variety          = kite.VARIETY_REGULAR,
        exchange         = exchange,
        tradingsymbol    = ce_symbol,
        transaction_type = kite.TRANSACTION_TYPE_BUY,
        quantity         = qty,
        order_type       = kite.ORDER_TYPE_MARKET,
        product          = instrument["product"],
    )
    logger.info(
        "Short CE BUY-back placed | %s | symbol=%s | qty=%d | order_id=%s",
        instrument["name"], ce_symbol, qty, order_id,
    )

    ce_key = f"{exchange}:{ce_symbol}"
    try:
        ltps = kite.ltp([ce_key])
        exit_ltp = ltps.get(ce_key, {}).get("last_price", 0.0)
    except Exception as exc:
        logger.warning("Could not fetch CE LTP after short CE BUY-back (%s): %s", ce_symbol, exc)
        exit_ltp = 0.0

    return order_id, exit_ltp


def square_off_all(kite: KiteConnect, resolved_instruments: list, state: dict):
    """Emergency / EOD square-off of every tracked open position."""
    from state import get_position
    for instrument in resolved_instruments:
        name = instrument["name"]
        pos  = get_position(state, name)
        if pos > 0:
            close_long(kite, instrument)
        elif pos < 0:
            saved = state.get(name, {})
            if saved.get("is_short_ce"):
                ce_sym = saved.get("ce_tradingsymbol", "")
                if ce_sym:
                    close_short_ce(kite, instrument, ce_sym)
                else:
                    logger.warning("square_off_all: %s is_short_ce but no ce_tradingsymbol in state", name)
            else:
                close_short(kite, instrument)
