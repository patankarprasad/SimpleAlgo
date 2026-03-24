"""
In-memory paper trading state for dry-run mode.

Mirrors the real position lifecycle (open → hold → close) without touching Kite.
State is intentionally NOT persisted — resets on every server restart.

Thread-safe: the scheduler thread writes positions; Flask threads read them
for the dashboard display.
"""
import threading
from datetime import datetime

import pytz

_lock      = threading.Lock()
_positions: dict[str, dict] = {}   # keyed by instrument name
IST        = pytz.timezone("Asia/Kolkata")


def open_position(name: str, action: str, symbol: str, qty: int, price: float) -> None:
    """
    Record a paper trade entry.
    action: "BUY" (long) or "SELL" (short)
    """
    size = qty if action == "BUY" else -qty
    with _lock:
        _positions[name] = {
            "position_size": size,
            "entry_price":   price,
            "entry_time":    datetime.now(IST).strftime("%H:%M:%S"),
            "symbol":        symbol,
            "qty":           qty,
        }


def close_position(name: str, exit_price: float) -> dict | None:
    """
    Close a paper position and return a trade summary dict.
    Returns None if no open position exists for this instrument.
    """
    with _lock:
        pos = _positions.pop(name, None)
    if pos is None:
        return None
    pnl = (exit_price - pos["entry_price"]) * pos["position_size"]
    return {
        "name":        name,
        "symbol":      pos["symbol"],
        "direction":   "LONG" if pos["position_size"] > 0 else "SHORT",
        "qty":         pos["qty"],
        "entry_price": pos["entry_price"],
        "entry_time":  pos["entry_time"],
        "exit_price":  exit_price,
        "exit_time":   datetime.now(IST).strftime("%H:%M:%S"),
        "pnl":         pnl,
    }


def get_position(name: str) -> dict | None:
    """Return the open paper position for an instrument, or None if flat."""
    with _lock:
        return dict(_positions[name]) if name in _positions else None


def get_position_size(name: str) -> int:
    """Return position_size (positive=long, negative=short, 0=flat)."""
    with _lock:
        return _positions.get(name, {}).get("position_size", 0)


def get_unrealized_pnl(name: str, current_price: float) -> float | None:
    """Unrealized P&L at current_price for an open paper position, or None if flat."""
    with _lock:
        pos = _positions.get(name)
    if pos is None:
        return None
    return (current_price - pos["entry_price"]) * pos["position_size"]


def get_all_positions() -> dict[str, dict]:
    """Return a snapshot of all open paper positions (for the web dashboard)."""
    with _lock:
        return {k: dict(v) for k, v in _positions.items()}
