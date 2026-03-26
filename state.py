"""
Persist position state across restarts using a simple JSON file.
State tracks what position (if any) is open per instrument.

position_size:
  > 0  → long
  < 0  → short
  = 0  → flat
"""
import json
import logging
import os
from pathlib import Path

import config

logger = logging.getLogger(__name__)


def load_state() -> dict:
    path = Path(config.STATE_FILE)
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    """Write atomically: write to .tmp then rename, so a crash never corrupts the file."""
    target = config.STATE_FILE
    tmp    = target + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, target)


def get_position(state: dict, instrument_name: str) -> int:
    """Return current position size for the instrument (default 0 = flat)."""
    return state.get(instrument_name, {}).get("position_size", 0)


def set_position(
    state: dict, instrument_name: str, position_size: int,
    entry_price: float = 0.0,
    kite_tradingsymbol: str = "",
    exchange: str = "",
    *,
    is_synthetic: bool = False,
    ce_tradingsymbol: str = "",
    pe_tradingsymbol: str = "",
    entry_ce_price: float | None = None,
    entry_pe_price: float | None = None,
) -> None:
    """
    Persist position state for one instrument.

    On exit calls (position_size=0) the existing record's fields are preserved
    via fallback so the rollover checker can still read the old tradingsymbol.
    Synthetic leg fields (ce/pe tradingsymbols) are preserved the same way.
    """
    existing = state.get(instrument_name, {})
    state[instrument_name] = {
        "position_size":      position_size,
        "entry_price":        entry_price,
        # Preserve from existing if not re-supplied (e.g. on exit calls)
        "kite_tradingsymbol": kite_tradingsymbol or existing.get("kite_tradingsymbol", ""),
        "exchange":           exchange           or existing.get("exchange", ""),
        # Synthetic futures leg data
        "is_synthetic":       is_synthetic       or existing.get("is_synthetic", False),
        "ce_tradingsymbol":   ce_tradingsymbol   or existing.get("ce_tradingsymbol", ""),
        "pe_tradingsymbol":   pe_tradingsymbol   or existing.get("pe_tradingsymbol", ""),
        "entry_ce_price":     entry_ce_price if entry_ce_price is not None else existing.get("entry_ce_price", 0.0),
        "entry_pe_price":     entry_pe_price if entry_pe_price is not None else existing.get("entry_pe_price", 0.0),
    }
    save_state(state)
    logger.info("State updated: %s -> position_size=%d", instrument_name, position_size)
