"""
In-memory paper trading state for dry-run mode.

Mirrors the real position lifecycle (open → hold → close) without touching Kite.
State is persisted to PAPER_STATE_FILE so open positions survive server restarts.
The file is written atomically (write-to-tmp then os.replace) on every change.

Thread-safe: the scheduler thread writes positions; Flask threads read them
for the dashboard display.
"""
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

import pytz

import config

logger = logging.getLogger(__name__)

_lock      = threading.Lock()
_positions: dict[str, dict] = {}   # keyed by instrument name
IST        = pytz.timezone("Asia/Kolkata")


# ══════════════════════════════════════════════════════════════════════════════
# Persistence helpers
# ══════════════════════════════════════════════════════════════════════════════

def _save() -> None:
    """
    Atomically persist the current _positions snapshot to disk.
    Holds the lock for the entire snapshot + write so two concurrent
    callers cannot interleave writes to the same .tmp file.
    """
    target = config.PAPER_STATE_FILE
    tmp    = target + ".tmp"
    with _lock:
        snapshot = {k: dict(v) for k, v in _positions.items()}
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, indent=2)
            os.replace(tmp, target)
            logger.debug("Paper positions saved (%d open)", len(snapshot))
        except Exception as exc:
            logger.warning("Failed to save paper positions: %s", exc)


def load() -> None:
    """
    Load persisted paper positions from disk into memory.
    Called once at startup (bottom of this module).
    Silently no-ops if the file does not exist yet.
    """
    path = Path(config.PAPER_STATE_FILE)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        with _lock:
            _positions.clear()
            _positions.update(data)
        logger.info(
            "Loaded %d paper position(s) from %s", len(data), path
        )
    except Exception as exc:
        logger.warning("Failed to load paper positions from %s: %s", path, exc)


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def open_position(
    name: str, action: str, symbol: str, qty: int, price: float,
    *,
    ce_symbol: str = "",
    pe_symbol: str = "",
    entry_ce_price: float = 0.0,
    entry_pe_price: float = 0.0,
    ce_angel_token: str = "",
    ce_angel_symbol: str = "",
    pe_angel_token: str = "",
    pe_angel_symbol: str = "",
    is_short_ce: bool = False,
) -> None:
    """
    Record a paper trade entry.
    action: "BUY" (long) or "SELL" (short)

    For synthetic futures (NIFTY/BANKNIFTY LONG) pass the keyword-only CE/PE fields.
    For short CE (NIFTY/BANKNIFTY SHORT) pass is_short_ce=True and ce_symbol/entry_ce_price.
    All keyword args default to empty/zero so existing callers are unaffected.
    """
    size = qty if action == "BUY" else -qty
    with _lock:
        _positions[name] = {
            "position_size":   size,
            "entry_price":     price,
            "entry_date":      datetime.now(IST).strftime("%Y-%m-%d"),
            "entry_time":      datetime.now(IST).strftime("%H:%M:%S"),
            "symbol":          symbol,
            "qty":             qty,
            # synthetic future (2-leg) fields:
            "is_synthetic":    bool(ce_symbol and pe_symbol),
            # single-leg short CE flag:
            "is_short_ce":     is_short_ce,
            "ce_symbol":       ce_symbol,
            "pe_symbol":       pe_symbol,
            "entry_ce_price":  entry_ce_price,
            "entry_pe_price":  entry_pe_price,
            # Angel tokens — used by Angel getLtpData for live PnL
            "ce_angel_token":  ce_angel_token,
            "ce_angel_symbol": ce_angel_symbol,
            "pe_angel_token":  pe_angel_token,
            "pe_angel_symbol": pe_angel_symbol,
        }
    _save()


def close_position(
    name: str, exit_price: float,
    *,
    exit_ce_price: float | None = None,
    exit_pe_price: float | None = None,
) -> dict | None:
    """
    Close a paper position and return a trade summary dict.
    Returns None if no open position exists for this instrument.

    For synthetic positions pass exit_ce_price and exit_pe_price (live LTPs).
    PnL formula for synthetic:
        position_size × ((exit_ce - entry_ce) - (exit_pe - entry_pe))
    This works for both LONG (positive size) and SHORT (negative size).
    """
    with _lock:
        pos = _positions.pop(name, None)
    if pos is None:
        return None

    _save()   # persist the removal immediately

    if pos.get("is_short_ce") and exit_ce_price is not None:
        # Single-leg CE short: P&L = premium received − premium paid to close
        pnl = (pos["entry_ce_price"] - exit_ce_price) * pos["qty"]
    elif (pos.get("is_synthetic")
            and exit_ce_price is not None
            and exit_pe_price is not None):
        pnl = pos["position_size"] * (
            (exit_ce_price - pos["entry_ce_price"]) -
            (exit_pe_price - pos["entry_pe_price"])
        )
    else:
        pnl = (exit_price - pos["entry_price"]) * pos["position_size"]

    return {
        "name":         name,
        "symbol":       pos["symbol"],
        "direction":    "LONG" if pos["position_size"] > 0 else "SHORT",
        "qty":          pos["qty"],
        "entry_price":  pos["entry_price"],
        "entry_time":   pos["entry_time"],
        "exit_price":   exit_price,
        "exit_time":    datetime.now(IST).strftime("%H:%M:%S"),
        "pnl":          pnl,
        "is_synthetic": pos.get("is_synthetic", False),
    }


def get_position(name: str) -> dict | None:
    """Return the open paper position for an instrument, or None if flat."""
    with _lock:
        return dict(_positions[name]) if name in _positions else None


def get_position_size(name: str) -> int:
    """Return position_size (positive=long, negative=short, 0=flat)."""
    with _lock:
        return _positions.get(name, {}).get("position_size", 0)


def get_unrealized_pnl(
    name: str, current_price: float,
    *,
    ce_ltp: float | None = None,
    pe_ltp: float | None = None,
) -> float | None:
    """
    Unrealized P&L for an open paper position, or None if flat.

    For synthetic positions pass live ce_ltp and pe_ltp for accurate PnL.
    Falls back to futures-price formula when option LTPs are unavailable.
    """
    with _lock:
        pos = _positions.get(name)
    if pos is None:
        return None
    if pos.get("is_short_ce") and ce_ltp is not None:
        # Single-leg CE short: unrealized P&L = entry premium − current premium
        return (pos["entry_ce_price"] - ce_ltp) * pos["qty"]
    if pos.get("is_synthetic") and ce_ltp is not None and pe_ltp is not None:
        return pos["position_size"] * (
            (ce_ltp - pos["entry_ce_price"]) -
            (pe_ltp - pos["entry_pe_price"])
        )
    return (current_price - pos["entry_price"]) * pos["position_size"]


def get_all_positions() -> dict[str, dict]:
    """Return a snapshot of all open paper positions (for the web dashboard)."""
    with _lock:
        return {k: dict(v) for k, v in _positions.items()}


def clear_all() -> None:
    """
    Wipe all open paper positions and remove the persisted state file.
    Called from the web dashboard 'Clear Paper State' action.
    """
    with _lock:
        _positions.clear()
    _save()
    logger.info("All paper positions cleared.")


# ── Restore persisted positions on import ─────────────────────────────────────
load()
