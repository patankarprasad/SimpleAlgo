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
import time

from kiteconnect import KiteConnect
from kiteconnect.exceptions import InputException, PermissionException, TokenException

logger = logging.getLogger(__name__)


class OrderFailed(RuntimeError):
    """The order definitively did NOT execute (REJECTED/CANCELLED, zero filled).

    Safe to treat as "nothing happened": state must not change, and re-sending
    the same order later cannot double a position.
    """
    def __init__(self, message: str, order_id: str | None = None):
        super().__init__(message)
        self.order_id = order_id


class OrderStatusUnknown(RuntimeError):
    """The order's outcome could NOT be confirmed — it MAY have executed.

    Neither "assume filled" nor "assume not filled" is safe: assuming not-filled
    re-enters and doubles the position; assuming filled fabricates a position
    that a later exit would turn into a naked opposite trade. Callers must stop
    trading the instrument and require a human to reconcile against Kite.
    """
    def __init__(self, message: str, order_id: str | None = None,
                 last_status: str = "", filled_quantity: int = 0):
        super().__init__(message)
        self.order_id        = order_id
        self.last_status     = last_status
        self.filled_quantity = filled_quantity


class SyntheticPartialFill(RuntimeError):
    """One leg of a two-leg synthetic order executed but the other did not
    (or its outcome is unknown). The instrument must be halted: re-running the
    whole entry/exit re-fires the already-executed leg every tick."""
    def __init__(self, message: str, *, filled_leg: str = "",
                 failed_leg: str = "", outcome_unknown: bool = False):
        super().__init__(message)
        self.filled_leg      = filled_leg
        self.failed_leg      = failed_leg
        self.outcome_unknown = outcome_unknown


# Exceptions from the placement call itself that guarantee the order never
# reached the exchange (validation / auth / permission rejections). Anything
# else (network timeout, 5xx, parse error) leaves the outcome ambiguous — the
# request may have been accepted even though the response never arrived.
_DEFINITE_PLACEMENT_FAILURES = (InputException, TokenException, PermissionException)


def _is_circuit_limit_error(exc: Exception) -> bool:
    """Return True if the exception is a circuit-limit rejection from the exchange."""
    return "circuit" in str(exc).lower()


def _place_order(kite: KiteConnect, **kwargs) -> str:
    """
    Call the Kite order placement API with market_protection=-1 injected.

    The kiteconnect SDK's place_order() doesn't expose market_protection as a
    named parameter, so we build the params dict manually and call _post directly,
    mirroring exactly what the SDK does internally.
    """
    params = {k: v for k, v in kwargs.items() if v is not None}
    params["market_protection"] = -1
    variety = params["variety"]
    return kite._post("order.place", url_args={"variety": variety}, params=params)["order_id"]


def _place_order_checked(kite: KiteConnect, **kwargs) -> str:
    """
    _place_order, but with error outcomes classified.

    Validation/auth rejections re-raise unchanged (definitively not placed —
    callers like the tender-period fallback rely on seeing InputException).
    Any other error becomes OrderStatusUnknown: the POST may have gone through
    even though we never saw the order_id, so the order could be live on Kite.
    """
    try:
        return _place_order(kite, **kwargs)
    except _DEFINITE_PLACEMENT_FAILURES:
        raise
    except Exception as exc:
        raise OrderStatusUnknown(
            f"order placement outcome unknown ({kwargs.get('transaction_type')} "
            f"{kwargs.get('tradingsymbol')}): {exc} — the order may be live on "
            f"Kite without a confirmed order_id",
        ) from exc


def _order_qty(instrument: dict) -> int:
    """Total units = lots × lot_size (both come from the resolved instrument dict)."""
    return instrument["qty"] * instrument["lot_size"]


def _await_order_complete(
    kite: KiteConnect,
    order_id: str,
    label: str,
    *,
    timeout: float = 45.0,
) -> None:
    """
    Poll Kite order history until the order reaches a terminal status.

    MARKET orders normally resolve in < 1 s; the long timeout only matters when
    the order stays OPEN or order_history itself keeps erroring. Polling starts
    at 0.5 s intervals and backs off to 5 s.

    Raises:
      OrderFailed        — REJECTED/CANCELLED with nothing filled (definitive:
                           the order did not execute, state must not change).
      OrderStatusUnknown — anything else: COMPLETE never observed within the
                           window, polls kept failing, or a terminal status
                           arrived with a partial fill. The order MAY have
                           executed; the caller must halt the instrument
                           rather than guess.
    """
    deadline    = time.monotonic() + timeout
    delay       = 0.5
    last_status = ""
    filled      = 0

    while True:
        try:
            history = kite.order_history(order_id)
            if history:
                last        = history[-1]
                last_status = last.get("status", "")
                filled      = int(last.get("filled_quantity") or 0)
                if last_status == "COMPLETE":
                    logger.info(
                        "Order COMPLETE | %s | order_id=%s | avg_price=%.4f | filled=%d",
                        label, order_id,
                        last.get("average_price", 0.0), filled,
                    )
                    return
                if last_status in ("REJECTED", "CANCELLED"):
                    reason = (
                        last.get("status_message")
                        or last.get("status_message_raw")
                        or last_status
                    )
                    if filled > 0:
                        # e.g. CANCELLED after a partial fill: `filled` units
                        # ARE held at the broker but state assumes all-or-nothing
                        raise OrderStatusUnknown(
                            f"Order {last_status} for {label} (id={order_id}) "
                            f"after PARTIAL fill of {filled} units: {reason} — "
                            f"broker position differs from expected, reconcile on Kite",
                            order_id=order_id, last_status=last_status,
                            filled_quantity=filled,
                        )
                    raise OrderFailed(
                        f"Order {last_status} for {label} (id={order_id}): {reason}",
                        order_id=order_id,
                    )
                # Still OPEN / TRIGGER PENDING / VALIDATION PENDING — keep polling
                logger.debug("Order status=%s, waiting… | %s", last_status, label)
        except (OrderFailed, OrderStatusUnknown):
            raise
        except Exception as exc:
            logger.warning(
                "order_history poll failed for %s [id=%s]: %s",
                label, order_id, exc,
            )

        if time.monotonic() + delay > deadline:
            break
        time.sleep(delay)
        delay = min(delay * 1.6, 5.0)

    raise OrderStatusUnknown(
        f"Order {order_id} for {label} could not be confirmed within {timeout:.0f}s "
        f"(last status={last_status or 'unavailable'!r}, filled={filled}) — "
        f"it may still execute; check Kite manually",
        order_id=order_id, last_status=last_status, filled_quantity=filled,
    )


def _fetch_ltp(kite: KiteConnect, instrument: dict) -> float:
    """Fetch current LTP for an instrument; returns 0.0 on failure."""
    key = f"{instrument['exchange']}:{instrument['kite_tradingsymbol']}"
    try:
        resp = kite.ltp([key])
        ltp = resp.get(key, {}).get("last_price", 0.0)
        if ltp == 0.0:
            logger.warning("LTP returned 0.0 for %s — full response: %s", key, resp)
        return ltp
    except Exception as exc:
        logger.warning("LTP fetch failed for %s: %s", key, exc)
        return 0.0


def _leg_params(kite: KiteConnect, *, exchange: str, tradingsymbol: str,
                transaction_type: str, quantity: int, product: str) -> dict:
    """Build the _place_order kwargs for one MARKET order leg."""
    return dict(
        variety          = kite.VARIETY_REGULAR,
        exchange         = exchange,
        tradingsymbol    = tradingsymbol,
        transaction_type = transaction_type,
        quantity         = quantity,
        order_type       = kite.ORDER_TYPE_MARKET,
        product          = product,
    )


def _place_leg(kite: KiteConnect, label: str, *, allow_retry: bool = True,
               **params) -> str:
    """
    Place one option leg and confirm it filled.

    A DEFINITIVE no-fill failure (rejected/cancelled with nothing filled, or a
    validation error before the exchange) is retried once — safe, because
    nothing executed. An UNKNOWN outcome is never retried: the first order may
    have filled, and re-sending it is exactly how legs get doubled (as on the
    2026-08-27 NIFTY_H partial exit).
    """
    try:
        order_id = _place_order_checked(kite, **params)
        logger.info("%s placed | order_id=%s | qty=%s",
                    label, order_id, params.get("quantity"))
        _await_order_complete(kite, order_id, label)
        return order_id
    except OrderStatusUnknown:
        raise
    except (OrderFailed, *_DEFINITE_PLACEMENT_FAILURES) as exc:
        if not allow_retry:
            raise
        logger.warning("%s: definitive failure (%s) — retrying leg once in 2s",
                       label, exc)
        time.sleep(2)
        order_id = _place_order_checked(kite, **params)
        logger.info("%s placed on retry | order_id=%s", label, order_id)
        _await_order_complete(kite, order_id, label)
        return order_id


def place_buy(kite: KiteConnect, instrument: dict) -> str:
    """Open a long (BUY) position. Retries once on circuit-limit rejection."""
    if kite is None:
        raise RuntimeError(f"Kite session unavailable — BUY order not placed for {instrument['name']}")
    qty   = _order_qty(instrument)
    label = f"{instrument['name']} BUY {instrument['kite_tradingsymbol']}"
    ltp   = _fetch_ltp(kite, instrument)

    logger.info(
        "Placing BUY | %s | symbol=%s | exchange=%s | product=%s | qty=%d | ltp=%.2f",
        instrument["name"], instrument["kite_tradingsymbol"],
        instrument["exchange"], instrument["product"], qty, ltp,
    )

    for attempt in range(2):
        order_id = _place_order_checked(kite,
            variety           = kite.VARIETY_REGULAR,
            exchange          = instrument["exchange"],
            tradingsymbol     = instrument["kite_tradingsymbol"],
            transaction_type  = kite.TRANSACTION_TYPE_BUY,
            quantity          = qty,
            order_type        = kite.ORDER_TYPE_MARKET,
            product           = instrument["product"],
        )
        logger.info(
            "BUY order placed | %s | symbol=%s | qty=%d | order_id=%s",
            instrument["name"], instrument["kite_tradingsymbol"], qty, order_id,
        )
        try:
            _await_order_complete(kite, order_id, label)
            return order_id
        except OrderStatusUnknown:
            # The order may have filled (including a partial fill whose reason
            # text could mention circuit limits) — never re-send it.
            raise
        except RuntimeError as exc:
            if attempt == 0 and _is_circuit_limit_error(exc):
                ltp = _fetch_ltp(kite, instrument)
                logger.warning(
                    "%s: BUY rejected due to circuit limits (ltp=%.2f) — retrying once in 2s: %s",
                    instrument["name"], ltp, exc,
                )
                time.sleep(2)
                continue
            raise


def place_sell(kite: KiteConnect, instrument: dict) -> str:
    """Open a short (SELL) position. Retries once on circuit-limit rejection."""
    if kite is None:
        raise RuntimeError(f"Kite session unavailable — SELL order not placed for {instrument['name']}")
    qty   = _order_qty(instrument)
    label = f"{instrument['name']} SELL {instrument['kite_tradingsymbol']}"
    ltp   = _fetch_ltp(kite, instrument)

    logger.info(
        "Placing SELL | %s | symbol=%s | exchange=%s | product=%s | qty=%d | ltp=%.2f",
        instrument["name"], instrument["kite_tradingsymbol"],
        instrument["exchange"], instrument["product"], qty, ltp,
    )

    for attempt in range(2):
        order_id = _place_order_checked(kite,
            variety           = kite.VARIETY_REGULAR,
            exchange          = instrument["exchange"],
            tradingsymbol     = instrument["kite_tradingsymbol"],
            transaction_type  = kite.TRANSACTION_TYPE_SELL,
            quantity          = qty,
            order_type        = kite.ORDER_TYPE_MARKET,
            product           = instrument["product"],
        )
        logger.info(
            "SELL order placed | %s | symbol=%s | qty=%d | order_id=%s",
            instrument["name"], instrument["kite_tradingsymbol"], qty, order_id,
        )
        try:
            _await_order_complete(kite, order_id, label)
            return order_id
        except OrderStatusUnknown:
            # The order may have filled (including a partial fill whose reason
            # text could mention circuit limits) — never re-send it.
            raise
        except RuntimeError as exc:
            if attempt == 0 and _is_circuit_limit_error(exc):
                ltp = _fetch_ltp(kite, instrument)
                logger.warning(
                    "%s: SELL rejected due to circuit limits (ltp=%.2f) — retrying once in 2s: %s",
                    instrument["name"], ltp, exc,
                )
                time.sleep(2)
                continue
            raise


def close_long(kite: KiteConnect, instrument: dict) -> str:
    """Close an existing long position (sell to exit)."""
    if kite is None:
        raise RuntimeError(f"Kite session unavailable — EXIT LONG order not placed for {instrument['name']}")
    qty      = _order_qty(instrument)
    order_id = _place_order_checked(kite,
        variety           = kite.VARIETY_REGULAR,
        exchange          = instrument["exchange"],
        tradingsymbol     = instrument["kite_tradingsymbol"],
        transaction_type  = kite.TRANSACTION_TYPE_SELL,
        quantity          = qty,
        order_type        = kite.ORDER_TYPE_MARKET,
        product           = instrument["product"],
    )
    logger.info(
        "EXIT LONG order placed | %s | symbol=%s | qty=%d | order_id=%s",
        instrument["name"], instrument["kite_tradingsymbol"], qty, order_id,
    )
    _await_order_complete(kite, order_id, f"{instrument['name']} EXIT_LONG {instrument['kite_tradingsymbol']}")
    return order_id


def close_short(kite: KiteConnect, instrument: dict) -> str:
    """Close an existing short position (buy to exit)."""
    if kite is None:
        raise RuntimeError(f"Kite session unavailable — EXIT SHORT order not placed for {instrument['name']}")
    qty      = _order_qty(instrument)
    order_id = _place_order_checked(kite,
        variety           = kite.VARIETY_REGULAR,
        exchange          = instrument["exchange"],
        tradingsymbol     = instrument["kite_tradingsymbol"],
        transaction_type  = kite.TRANSACTION_TYPE_BUY,
        quantity          = qty,
        order_type        = kite.ORDER_TYPE_MARKET,
        product           = instrument["product"],
    )
    logger.info(
        "EXIT SHORT order placed | %s | symbol=%s | qty=%d | order_id=%s",
        instrument["name"], instrument["kite_tradingsymbol"], qty, order_id,
    )
    _await_order_complete(kite, order_id, f"{instrument['name']} EXIT_SHORT {instrument['kite_tradingsymbol']}")
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
    Leg 1 is confirmed filled before Leg 2 is placed. If Leg 2 (SELL PE)
    definitively fails it is retried once; if it still fails (or its outcome
    is unknown), SyntheticPartialFill is raised so the caller HALTS the
    instrument — re-running the entry would buy another CE every tick.
    """
    if kite is None:
        raise RuntimeError(
            f"Kite session unavailable — synthetic BUY not placed for {instrument['name']}"
        )
    qty      = instrument["qty"] * instrument["lot_size"]
    exchange = ce_info.get("exchange", "NFO")
    ce_sym   = ce_info["kite_tradingsymbol"]
    pe_sym   = pe_info["kite_tradingsymbol"]

    # Leg 1: BUY CE — confirmed filled before risking a partial fill on leg 2
    ce_order_id = _place_leg(
        kite, f"{instrument['name']} Synthetic BUY CE {ce_sym}",
        **_leg_params(kite, exchange=exchange, tradingsymbol=ce_sym,
                      transaction_type=kite.TRANSACTION_TYPE_BUY,
                      quantity=qty, product=instrument["product"]),
    )

    # Leg 2: SELL PE
    try:
        pe_order_id = _place_leg(
            kite, f"{instrument['name']} Synthetic SELL PE {pe_sym}",
            **_leg_params(kite, exchange=exchange, tradingsymbol=pe_sym,
                          transaction_type=kite.TRANSACTION_TYPE_SELL,
                          quantity=qty, product=instrument["product"]),
        )
    except Exception as exc:
        unknown = isinstance(exc, OrderStatusUnknown)
        logger.critical(
            "PARTIAL SYNTHETIC FILL — %s: CE leg FILLED (id=%s, symbol=%s) "
            "but SELL PE %s (%s: %s). MANUAL INTERVENTION REQUIRED on Kite.",
            instrument["name"], ce_order_id, ce_sym,
            "outcome UNKNOWN" if unknown else "FAILED", pe_sym, exc,
        )
        raise SyntheticPartialFill(
            f"Synthetic BUY partial fill for {instrument['name']}: "
            f"CE {ce_sym} filled (id={ce_order_id}) but PE SELL "
            f"{'outcome unknown' if unknown else 'failed'}: {exc}",
            filled_leg=ce_sym, failed_leg=pe_sym, outcome_unknown=unknown,
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
    ce_sym   = ce_info["kite_tradingsymbol"]
    pe_sym   = pe_info["kite_tradingsymbol"]

    # Leg 1: BUY PE — confirmed filled before risking a partial fill on leg 2
    pe_order_id = _place_leg(
        kite, f"{instrument['name']} Synthetic BUY PE {pe_sym}",
        **_leg_params(kite, exchange=exchange, tradingsymbol=pe_sym,
                      transaction_type=kite.TRANSACTION_TYPE_BUY,
                      quantity=qty, product=instrument["product"]),
    )

    # Leg 2: SELL CE
    try:
        ce_order_id = _place_leg(
            kite, f"{instrument['name']} Synthetic SELL CE {ce_sym}",
            **_leg_params(kite, exchange=exchange, tradingsymbol=ce_sym,
                          transaction_type=kite.TRANSACTION_TYPE_SELL,
                          quantity=qty, product=instrument["product"]),
        )
    except Exception as exc:
        unknown = isinstance(exc, OrderStatusUnknown)
        logger.critical(
            "PARTIAL SYNTHETIC FILL — %s: PE leg FILLED (id=%s, symbol=%s) "
            "but SELL CE %s (%s: %s). MANUAL INTERVENTION REQUIRED on Kite.",
            instrument["name"], pe_order_id, pe_sym,
            "outcome UNKNOWN" if unknown else "FAILED", ce_sym, exc,
        )
        raise SyntheticPartialFill(
            f"Synthetic SELL partial fill for {instrument['name']}: "
            f"PE {pe_sym} filled (id={pe_order_id}) but CE SELL "
            f"{'outcome unknown' if unknown else 'failed'}: {exc}",
            filled_leg=pe_sym, failed_leg=ce_sym, outcome_unknown=unknown,
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
    If BUY PE fails after SELL CE it is retried once (only on a definitive
    no-fill); otherwise SyntheticPartialFill is raised so the caller HALTS the
    instrument — re-running the whole exit would sell the CE a second time
    (that is exactly what happened to NIFTY_H on 2026-08-27).
    """
    if kite is None:
        raise RuntimeError(
            f"Kite session unavailable — synthetic EXIT LONG not placed for {instrument['name']}"
        )
    qty = instrument["qty"] * instrument["lot_size"]

    # Leg 1: SELL CE — confirmed filled before risking a partial fill on leg 2
    ce_order_id = _place_leg(
        kite, f"{instrument['name']} Synthetic EXIT_LONG SELL CE {ce_symbol}",
        **_leg_params(kite, exchange=exchange, tradingsymbol=ce_symbol,
                      transaction_type=kite.TRANSACTION_TYPE_SELL,
                      quantity=qty, product=instrument["product"]),
    )

    # Leg 2: BUY PE
    try:
        pe_order_id = _place_leg(
            kite, f"{instrument['name']} Synthetic EXIT_LONG BUY PE {pe_symbol}",
            **_leg_params(kite, exchange=exchange, tradingsymbol=pe_symbol,
                          transaction_type=kite.TRANSACTION_TYPE_BUY,
                          quantity=qty, product=instrument["product"]),
        )
    except Exception as exc:
        unknown = isinstance(exc, OrderStatusUnknown)
        logger.critical(
            "PARTIAL SYNTHETIC EXIT — %s: SELL CE FILLED (id=%s, symbol=%s) "
            "but BUY PE %s (%s: %s). MANUAL INTERVENTION REQUIRED on Kite.",
            instrument["name"], ce_order_id, ce_symbol,
            "outcome UNKNOWN" if unknown else "FAILED", pe_symbol, exc,
        )
        raise SyntheticPartialFill(
            f"Synthetic EXIT LONG partial fail for {instrument['name']}: "
            f"CE {ce_symbol} closed (id={ce_order_id}) but PE BUY "
            f"{'outcome unknown' if unknown else 'failed'}: {exc}",
            filled_leg=ce_symbol, failed_leg=pe_symbol, outcome_unknown=unknown,
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

    # Leg 1: SELL PE — confirmed filled before risking a partial fill on leg 2
    pe_order_id = _place_leg(
        kite, f"{instrument['name']} Synthetic EXIT_SHORT SELL PE {pe_symbol}",
        **_leg_params(kite, exchange=exchange, tradingsymbol=pe_symbol,
                      transaction_type=kite.TRANSACTION_TYPE_SELL,
                      quantity=qty, product=instrument["product"]),
    )

    # Leg 2: BUY CE
    try:
        ce_order_id = _place_leg(
            kite, f"{instrument['name']} Synthetic EXIT_SHORT BUY CE {ce_symbol}",
            **_leg_params(kite, exchange=exchange, tradingsymbol=ce_symbol,
                          transaction_type=kite.TRANSACTION_TYPE_BUY,
                          quantity=qty, product=instrument["product"]),
        )
    except Exception as exc:
        unknown = isinstance(exc, OrderStatusUnknown)
        logger.critical(
            "PARTIAL SYNTHETIC EXIT — %s: SELL PE FILLED (id=%s, symbol=%s) "
            "but BUY CE %s (%s: %s). MANUAL INTERVENTION REQUIRED on Kite.",
            instrument["name"], pe_order_id, pe_symbol,
            "outcome UNKNOWN" if unknown else "FAILED", ce_symbol, exc,
        )
        raise SyntheticPartialFill(
            f"Synthetic EXIT SHORT partial fail for {instrument['name']}: "
            f"PE {pe_symbol} closed (id={pe_order_id}) but CE BUY "
            f"{'outcome unknown' if unknown else 'failed'}: {exc}",
            filled_leg=pe_symbol, failed_leg=ce_symbol, outcome_unknown=unknown,
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

    order_id = _place_leg(
        kite,
        f"{instrument['name']} Short CE SELL {ce_info['kite_tradingsymbol']} "
        f"(strike={ce_info.get('strike', '?')})",
        **_leg_params(kite, exchange=exchange,
                      tradingsymbol=ce_info["kite_tradingsymbol"],
                      transaction_type=kite.TRANSACTION_TYPE_SELL,
                      quantity=qty, product=instrument["product"]),
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

    order_id = _place_leg(
        kite, f"{instrument['name']} Short CE BUY-back {ce_symbol}",
        **_leg_params(kite, exchange=exchange, tradingsymbol=ce_symbol,
                      transaction_type=kite.TRANSACTION_TYPE_BUY,
                      quantity=qty, product=instrument["product"]),
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
    import strategy_config
    from state import get_position, refresh_position, set_position, instrument_lock
    for instrument in resolved_instruments:
        name = instrument["name"]
        # A halted instrument's saved state may not match the broker (that is
        # why it was halted) — firing a close off it could open a naked
        # position instead. It must be reconciled and closed manually on Kite.
        if strategy_config.get_halted(name):
            logger.critical(
                "square_off_all: %s is HALTED (state may not match broker) — "
                "skipped; verify and close it manually on Kite", name,
            )
            continue
        # Hold the instrument lock across check+close+save so this can't race
        # a concurrent strategy tick (or the 14:45 expiry square-off job)
        # deciding to act on the same instrument at the same time.
        with instrument_lock(name):
            refresh_position(state, name)
            pos = get_position(state, name)
            if pos == 0:
                continue
            saved = state.get(name, {})
            # Contain failures per instrument so one bad close doesn't abort
            # the square-off of everything after it in the list.
            try:
                if pos > 0:
                    close_long(kite, instrument)
                elif saved.get("is_short_ce"):
                    ce_sym = saved.get("ce_tradingsymbol", "")
                    if ce_sym:
                        close_short_ce(kite, instrument, ce_sym)
                    else:
                        logger.warning("square_off_all: %s is_short_ce but no ce_tradingsymbol in state", name)
                        continue
                else:
                    close_short(kite, instrument)
                set_position(state, name, 0)
            except OrderStatusUnknown as exc:
                logger.critical(
                    "square_off_all: %s close outcome UNKNOWN (%s) — instrument "
                    "halted, reconcile on Kite manually", name, exc,
                )
                strategy_config.set_halted(name, f"square_off_all close: {exc}")
            except Exception as exc:
                logger.error(
                    "square_off_all: closing %s failed (%s) — continuing with "
                    "remaining instruments", name, exc, exc_info=True,
                )
