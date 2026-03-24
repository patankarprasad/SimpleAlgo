"""
Thread-safe shared state between the APScheduler algo loop and the Flask web server.
Both run in the same process (separate threads).  All mutations go through this module.

Trade history is NOT stored here — see trade_log.py for date-wise persistent files.
"""
import threading
from copy import deepcopy
from datetime import datetime

_lock = threading.Lock()

# Latest per-instrument snapshot, keyed by instrument name.
# Populated by main._process_instrument() after each candle evaluation.
_instruments: dict[str, dict] = {}

# Resolved instrument list (set once at startup by main.initialise()).
# Needed by Flask routes that must call order_manager functions.
_resolved_instruments: list[dict] = []

# Scheduler heartbeat info.
_scheduler: dict = {
    "running":   False,
    "last_run":  None,   # datetime | None
    "run_count": 0,
}


def update_instrument(name: str, data: dict) -> None:
    """Called by main.py after each candle evaluation."""
    with _lock:
        _instruments[name] = {**data, "updated_at": datetime.now()}


def set_resolved_instruments(instruments: list[dict]) -> None:
    with _lock:
        global _resolved_instruments
        _resolved_instruments = list(instruments)


def get_resolved_instruments() -> list[dict]:
    with _lock:
        return list(_resolved_instruments)


def set_scheduler_running(running: bool) -> None:
    with _lock:
        _scheduler["running"] = running


def record_run() -> None:
    """Call at the start of each strategy run tick."""
    with _lock:
        _scheduler["last_run"]   = datetime.now()
        _scheduler["run_count"] += 1


def snapshot() -> dict:
    """Return a deep copy of all shared state — safe to read from any Flask thread."""
    import paper_trading
    with _lock:
        return {
            "instruments":   deepcopy(_instruments),
            "scheduler":     deepcopy(_scheduler),
            "paper_positions": paper_trading.get_all_positions(),
        }
