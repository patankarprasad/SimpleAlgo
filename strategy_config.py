"""
Per-instrument strategy enable/disable state.

Stored in strategy_config.json (runtime-writable).
Note: .env is not used because Python apps cannot reliably write back to .env
at runtime without corrupting comments and formatting.

Default: all instruments enabled. Missing key = enabled.
"""
import json
import threading
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
