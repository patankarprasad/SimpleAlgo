"""
Contract pin — persist an explicit futures-month override for an instrument.

When you decide to roll over to the next expiry (e.g. GOLDM May instead of
GOLDM April), call `pin_next_month("GOLDM", "MCX")`.  The algo will then use
that contract for candle fetching AND order placement until the pin expires
(day after its expiry date) or is manually cleared.

Pin data is stored in CONTRACT_PIN_FILE (default: contract_pin.json).

Typical usage
-------------
  from contract_pin import pin_next_month, get_pinned_contract, clear_pin

  # Declare rollover to next month:
  pin_next_month("GOLDM", "MCX")

  # scrip_master.resolve_instrument() calls get_pinned_contract() internally,
  # so the algo picks up the new contract automatically on the next run.
"""
import json
import logging
import os
from datetime import date
from pathlib import Path

import config

logger = logging.getLogger(__name__)

_PIN_FILE = Path(config.CONTRACT_PIN_FILE)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _load() -> dict:
    """Load all pins from disk. Returns {} on missing or corrupt file."""
    if not _PIN_FILE.exists():
        return {}
    try:
        with open(_PIN_FILE) as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("contract_pin: could not read %s: %s", _PIN_FILE, exc)
        return {}


def _save(pins: dict) -> None:
    """Atomically write pins to disk."""
    tmp = str(_PIN_FILE) + ".tmp"
    with open(tmp, "w") as f:
        # Serialize dates as ISO strings so JSON round-trips cleanly
        json.dump(pins, f, indent=2, default=str)
    os.replace(tmp, _PIN_FILE)


# ── Public API ─────────────────────────────────────────────────────────────────

def pin_next_month(name: str, exchange: str) -> dict:
    """
    Pin the NEXT-nearest futures contract for ``name`` on ``exchange``.

    Looks up the second-nearest active contract from the scrip master (i.e.
    next month's expiry) and persists the choice.  Subsequent calls to
    ``scrip_master.resolve_instrument()`` will use this contract instead of
    the auto-selected nearest one.

    Returns the pinned contract dict (same keys as ``get_nearest_future()``).
    Raises ``RuntimeError`` if fewer than 2 active contracts exist.
    """
    import scrip_master  # late import to avoid circular dependency at module level

    contracts = scrip_master.get_all_futures(name, exchange)
    if len(contracts) < 2:
        raise RuntimeError(
            f"No next-month contract available for {name} on {exchange}. "
            "Only one active contract found in the scrip master."
        )

    contract = contracts[1]  # second-nearest = next month's expiry
    pins = _load()
    pins[name.upper()] = {
        # Spread all contract fields; convert any date objects to ISO strings for JSON
        **{k: str(v) if isinstance(v, date) else v for k, v in contract.items()},
        "pinned_at": str(date.today()),
    }
    _save(pins)
    logger.info(
        "Contract pin set: %s → %s (expires %s)",
        name, contract["kite_tradingsymbol"], contract["expiry"],
    )
    return contract


def get_pinned_contract(name: str) -> dict | None:
    """
    Return the pinned contract dict for ``name``, or ``None`` if:
      - no pin has been set, or
      - the pinned contract's expiry has already passed (pin is auto-cleared).

    The returned dict has the same keys as ``get_nearest_future()`` output,
    with ``expiry`` restored to a ``datetime.date`` object.
    """
    pins = _load()
    pin  = pins.get(name.upper())
    if not pin:
        return None

    try:
        expiry = date.fromisoformat(pin["expiry"])
    except (KeyError, ValueError) as exc:
        logger.warning(
            "contract_pin: invalid expiry in pin for %s (%s) — clearing", name, exc
        )
        clear_pin(name)
        return None

    if expiry <= date.today():
        logger.info(
            "Contract pin for %s (%s) has expired — auto-clearing",
            name, pin.get("kite_tradingsymbol"),
        )
        clear_pin(name)
        return None

    # Re-hydrate expiry back to datetime.date (was serialised as a string)
    pin = dict(pin)
    pin["expiry"] = expiry
    return pin


def clear_pin(name: str) -> bool:
    """
    Remove the contract pin for ``name``.

    Returns ``True`` if a pin existed and was removed, ``False`` if no pin
    was set.
    """
    pins = _load()
    key  = name.upper()
    if key not in pins:
        return False
    del pins[key]
    _save(pins)
    logger.info("Contract pin cleared for %s", name)
    return True


def list_pins() -> dict:
    """Return all current pins as a dict keyed by instrument name (upper-case)."""
    return _load()
