"""
Persist position state across restarts using a simple JSON file.
State tracks what position (if any) is open per instrument.

position_size:
  > 0  → long
  < 0  → short
  = 0  → flat

Concurrency
-----------
Several independent scheduler jobs (the 15-min strategy tick, the hourly
tick, the 14:45 expiry square-off — which is scheduled a single second
apart from the 14:45 strategy tick) and webapp routes can all read/act/write
position state for instruments at effectively the same time. Two hazards
follow from that:

1. Whichever job saves last wins. If job A loads state, job B loads state,
   A updates instrument X and saves, then B updates instrument Y and saves
   its own (older) in-memory copy, B's save silently reverts A's update to
   X even though B never touched X. `set_position()`/`clear_position()`
   avoid this by always re-reading the latest on-disk state and merging
   just their own key into it immediately before writing, instead of
   writing back whatever full snapshot the caller happened to be holding.
2. Two jobs deciding to act on the *same* instrument at the same time (e.g.
   expiry square-off closing CRUDEOIL while a strategy tick is also mid-way
   through processing CRUDEOIL) need to be fully serialized — checking the
   position, placing an order, and persisting the result must happen as one
   atomic unit. `instrument_lock()` hands out a process-wide lock per
   instrument name for callers to hold across that whole sequence.
"""
import json
import logging
import os
import threading
from collections import defaultdict
from pathlib import Path

import config

logger = logging.getLogger(__name__)

_state_write_lock = threading.Lock()

_locks_guard      = threading.Lock()
_instrument_locks: dict[str, threading.RLock] = defaultdict(threading.RLock)


def instrument_lock(instrument_name: str) -> threading.RLock:
    """
    Return a process-wide re-entrant lock scoped to one instrument name.

    Callers must hold this for the full check-position -> place-order ->
    save-position sequence so concurrent scheduler jobs / webapp actions can
    never race on the same instrument.
    """
    with _locks_guard:
        return _instrument_locks[instrument_name]


def load_state() -> dict:
    path = Path(config.STATE_FILE)
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def _write_atomic(state: dict) -> None:
    """Write to .tmp then rename, so a crash never corrupts the file."""
    target = config.STATE_FILE
    tmp    = target + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, target)


def save_state(state: dict):
    """
    Write the given snapshot verbatim.

    Prefer set_position() / clear_position() for per-instrument changes —
    they merge onto the latest on-disk state instead of overwriting the
    whole file with a possibly-stale in-memory copy, so concurrent jobs
    can't clobber each other's updates to other instruments.
    """
    with _state_write_lock:
        _write_atomic(state)


def get_position(state: dict, instrument_name: str) -> int:
    """Return current position size for the instrument (default 0 = flat)."""
    return state.get(instrument_name, {}).get("position_size", 0)


def refresh_position(state: dict, instrument_name: str) -> None:
    """
    Overwrite state[instrument_name] in-place with the latest on-disk record.

    Call this right before reading a position to act on it — the caller's
    `state` dict may have been loaded well before another concurrent job
    updated this instrument on disk (e.g. a long strategy tick loads state
    once at the top but processes instruments one by one over many
    seconds/minutes).
    """
    fresh = load_state()
    if instrument_name in fresh:
        state[instrument_name] = fresh[instrument_name]
    else:
        state.pop(instrument_name, None)


def set_position(
    state: dict, instrument_name: str, position_size: int,
    entry_price: float = 0.0,
    kite_tradingsymbol: str = "",
    exchange: str = "",
    *,
    is_synthetic: bool = False,
    is_short_ce: bool = False,
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

    is_short_ce=True  → single-leg CE short (SELL CE, no PE leg).
    is_synthetic=True → two-leg synthetic future (BUY CE + SELL PE for long,
                        or the older BUY PE + SELL CE for short).

    Always merges onto a freshly-loaded on-disk snapshot (not the possibly
    stale `state` dict the caller is holding) so a concurrent job's update to
    a *different* instrument can never be wiped out by this save. The
    caller's `state` dict is updated in place for convenience.
    """
    with _state_write_lock:
        fresh    = load_state()
        existing = fresh.get(instrument_name, {})
        record = {
            "position_size":      position_size,
            "entry_price":        entry_price,
            # Preserve from existing if not re-supplied (e.g. on exit calls)
            "kite_tradingsymbol": kite_tradingsymbol or existing.get("kite_tradingsymbol", ""),
            "exchange":           exchange           or existing.get("exchange", ""),
            # Synthetic futures / short-CE leg data.
            # On exit (position_size=0) clear the type flags so stale values from the
            # previous trade don't bleed into the next entry via the `or existing` fallback.
            "is_synthetic":       is_synthetic       if position_size != 0 else False,
            "is_short_ce":        is_short_ce        if position_size != 0 else False,
            "ce_tradingsymbol":   ce_tradingsymbol   or existing.get("ce_tradingsymbol", ""),
            "pe_tradingsymbol":   pe_tradingsymbol   or existing.get("pe_tradingsymbol", ""),
            "entry_ce_price":     entry_ce_price if entry_ce_price is not None else existing.get("entry_ce_price", 0.0),
            "entry_pe_price":     entry_pe_price if entry_pe_price is not None else existing.get("entry_pe_price", 0.0),
        }
        fresh[instrument_name] = record
        _write_atomic(fresh)
    state[instrument_name] = record
    logger.info("State updated: %s -> position_size=%d", instrument_name, position_size)


def clear_position(state: dict, instrument_name: str) -> bool:
    """
    Remove an instrument's record entirely (manual admin action).

    Merges onto the latest on-disk state so it can't clobber concurrent
    updates to other instruments. Returns True if a record existed and was
    removed.
    """
    with _state_write_lock:
        fresh   = load_state()
        existed = instrument_name in fresh
        if existed:
            del fresh[instrument_name]
            _write_atomic(fresh)
    state.pop(instrument_name, None)
    return existed
