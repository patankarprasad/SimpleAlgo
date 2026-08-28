"""
Per-instrument strategy enable/disable state.

Stored in strategy_config.json (runtime-writable).
Note: .env is not used because Python apps cannot reliably write back to .env
at runtime without corrupting comments and formatting.

Default: all instruments enabled. Missing key = enabled.
"""
import json
import threading
from datetime import datetime
from pathlib import Path

_FILE = Path("strategy_config.json")
_lock = threading.Lock()


def _load() -> dict:
    if _FILE.exists():
        try:
            return json.loads(_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save(cfg: dict) -> None:
    _FILE.write_text(json.dumps(cfg, indent=2))


def is_enabled(name: str) -> bool:
    """Return True if the strategy for this instrument is enabled (default True)."""
    with _lock:
        return _load().get(name, True)


def set_enabled(name: str, enabled: bool) -> None:
    with _lock:
        cfg = _load()
        cfg[name] = enabled
        _save(cfg)


def get_all(names: list[str]) -> dict[str, bool]:
    """Return {name: enabled} for the given instrument names."""
    with _lock:
        cfg = _load()
        return {name: cfg.get(name, True) for name in names}


# ── Booked-manually state ─────────────────────────────────────────────────────
# When a user manually exits via the broker and marks the strategy "Booked
# Manually", new entries are blocked until:
#   Phase 1 — the natural SL/exit signal fires (sl_fired goes False → True)
#   Phase 2 — the next valid entry signal (BUY/SELL) fires → flag cleared

def get_booked_manually(name: str) -> dict | None:
    """Return {direction, sl_fired} if this strategy is booked manually, else None."""
    with _lock:
        return _load().get(f"_bm_{name}")


def set_booked_manually(name: str, direction: str) -> None:
    """Mark strategy as booked manually. direction='LONG' or 'SHORT'."""
    with _lock:
        cfg = _load()
        cfg[f"_bm_{name}"] = {"direction": direction, "sl_fired": False}
        _save(cfg)


def mark_booked_manually_sl_fired(name: str) -> None:
    """Advance from phase 1 to phase 2: SL signal has now fired."""
    with _lock:
        cfg = _load()
        key = f"_bm_{name}"
        if key in cfg:
            cfg[key]["sl_fired"] = True
            _save(cfg)


def clear_booked_manually(name: str) -> None:
    """Remove the booked-manually flag, resuming normal strategy behaviour."""
    with _lock:
        cfg = _load()
        cfg.pop(f"_bm_{name}", None)
        _save(cfg)


def get_all_booked_manually(names: list[str]) -> dict[str, dict | None]:
    """Return {name: bm_state_or_None} for the given instrument names."""
    with _lock:
        cfg = _load()
        return {name: cfg.get(f"_bm_{name}") for name in names}


# ── Halted state ──────────────────────────────────────────────────────────────
# Set automatically when an order's outcome could not be confirmed or a
# synthetic order partially filled — i.e. whenever the algo's saved position
# may no longer match the broker's. While halted, the strategy loop places NO
# orders for the instrument (and the expiry square-off / square-off-all skip
# it). Cleared from the dashboard once the operator has verified and, if
# needed, fixed the position on Kite.

def get_halted(name: str) -> dict | None:
    """Return {reason, time} if this instrument is halted, else None."""
    with _lock:
        return _load().get(f"_halt_{name}")


def set_halted(name: str, reason: str) -> None:
    """Halt all automated trading for one instrument until manually resumed."""
    with _lock:
        cfg = _load()
        cfg[f"_halt_{name}"] = {
            "reason": reason[:400],
            "time":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save(cfg)


def clear_halted(name: str) -> None:
    """Resume automated trading for a halted instrument (operator action)."""
    with _lock:
        cfg = _load()
        cfg.pop(f"_halt_{name}", None)
        _save(cfg)


def get_all_halted(names: list[str]) -> dict[str, dict | None]:
    """Return {name: halt_state_or_None} for the given instrument names."""
    with _lock:
        cfg = _load()
        return {name: cfg.get(f"_halt_{name}") for name in names}
