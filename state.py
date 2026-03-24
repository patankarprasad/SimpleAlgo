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


def set_position(state: dict, instrument_name: str, position_size: int,
                 entry_price: float = 0.0,
                 kite_tradingsymbol: str = "",
                 exchange: str = "") -> None:
    existing = state.get(instrument_name, {})
    state[instrument_name] = {
        "position_size":      position_size,
        "entry_price":        entry_price,
        # Preserve symbol fields from the existing record if not supplied
        # (e.g. when calling set_position(state, name, 0) on exit).
        "kite_tradingsymbol": kite_tradingsymbol or existing.get("kite_tradingsymbol", ""),
        "exchange":           exchange           or existing.get("exchange", ""),
    }
    save_state(state)
    logger.info("State updated: %s -> position_size=%d", instrument_name, position_size)
