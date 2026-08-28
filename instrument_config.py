"""
Runtime-editable instrument settings — lot counts and the stock futures list.

Stored in instrument_config.json (runtime-writable), edited from the dashboard
Settings page.  Same rationale as strategy_config.py: .env cannot be rewritten
at runtime without destroying its comments and formatting, and config.py is
source code.

Schema
------
{
  "lots":                  {"GOLDM": 2, "NIFTY": 1},   # per-instrument overrides
  "stock_futures":         ["RELIANCE", "HDFCBANK"],   # NSE stock futures traded
  "stock_futures_qty":     1,                          # default lots for new stocks
  "stock_futures_product": "NRML"                      # NRML | MIS
}

Every key is optional: whatever is absent falls back to the config.py / .env
default, so a missing or empty file reproduces the pre-existing behaviour
exactly.  The "stock_futures" key is seeded once from the STOCK_FUTURES env var
(see seed_stock_futures) so an existing deployment keeps its list on upgrade.

This module deliberately does NOT import config — config imports it.
"""
import json
import logging
import os
import re
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

if getattr(sys, "frozen", False):
    _BASE_DIR = os.path.dirname(sys.executable)
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = Path(_BASE_DIR) / "instrument_config.json"

_lock = threading.Lock()

# Stock/underlying names as they appear in the broker scrip masters:
# upper-case letters, digits, & (M&M) and - (BAJAJ-AUTO).
_STOCK_RE = re.compile(r"^[A-Z0-9&\-]{1,30}$")
# Instrument names additionally allow _, used by the hourly variants (GOLDM_H).
_NAME_RE  = re.compile(r"^[A-Z0-9&\-_]{1,30}$")
_MAX_LOTS = 1000          # sanity ceiling — a fat-finger guard, not a margin check
_PRODUCTS = ("NRML", "MIS")


# ── Internal helpers ───────────────────────────────────────────────────────────

def _load() -> dict:
    """Load the config from disk. Returns {} on missing or corrupt file."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        data = json.loads(CONFIG_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("instrument_config: could not read %s: %s", CONFIG_FILE, exc)
        return {}


def _save(cfg: dict) -> None:
    """Atomically write the config to disk."""
    tmp = str(CONFIG_FILE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_FILE)


# ── Validation ─────────────────────────────────────────────────────────────────

def normalise_name(name: str) -> str:
    """
    Upper-case and validate any configured instrument name — including the
    hourly variants (GOLDM_H), which are not scrip-master symbols.
    """
    clean = (name or "").strip().upper()
    if not _NAME_RE.match(clean):
        raise ValueError(
            f"Invalid instrument name '{name}'. Use letters, digits, "
            f"&, - and _ only."
        )
    return clean


def normalise_stock_name(name: str) -> str:
    """
    Upper-case and validate a stock name that must match a scrip-master symbol.
    Stricter than normalise_name: no underscore, which no NSE symbol contains.
    """
    clean = (name or "").strip().upper()
    if not _STOCK_RE.match(clean):
        raise ValueError(
            f"Invalid stock name '{name}'. Use the NSE symbol only "
            f"(letters, digits, & and -), e.g. RELIANCE, M&M, BAJAJ-AUTO."
        )
    return clean


def normalise_lots(lots) -> int:
    """Validate a lot count. Raises ValueError if not a sane positive integer."""
    try:
        val = int(str(lots).strip())
    except (TypeError, ValueError):
        raise ValueError(f"Lots must be a whole number, got '{lots}'.")
    if val < 1 or val > _MAX_LOTS:
        raise ValueError(f"Lots must be between 1 and {_MAX_LOTS}, got {val}.")
    return val


def normalise_product(product: str) -> str:
    """Validate a Kite product type. Raises ValueError if unknown."""
    clean = (product or "").strip().upper()
    if clean not in _PRODUCTS:
        raise ValueError(f"Product must be one of {', '.join(_PRODUCTS)}, got '{product}'.")
    return clean


# ── Lots (per-instrument qty override) ─────────────────────────────────────────

def get_all_lots() -> dict[str, int]:
    """Return {instrument_name: lots} for every instrument with an override."""
    with _lock:
        raw = _load().get("lots", {})
    out = {}
    for name, lots in (raw or {}).items():
        try:
            out[str(name).upper()] = normalise_lots(lots)
        except ValueError:
            logger.warning("instrument_config: ignoring invalid lots for %s: %r", name, lots)
    return out


def set_lots(name: str, lots: int) -> int:
    """Override the lot count for one instrument. Returns the stored value."""
    name  = normalise_name(name)
    value = normalise_lots(lots)
    with _lock:
        cfg = _load()
        cfg.setdefault("lots", {})[name] = value
        _save(cfg)
    logger.info("instrument_config: %s lots set to %d", name, value)
    return value


def clear_lots(name: str) -> None:
    """Drop the override for one instrument, reverting to the config.py default."""
    name = normalise_name(name)
    with _lock:
        cfg = _load()
        cfg.get("lots", {}).pop(name, None)
        _save(cfg)
    logger.info("instrument_config: %s lots override cleared", name)


# ── Stock futures list ─────────────────────────────────────────────────────────

def seed_stock_futures(env_names: list[str], env_qty: int, env_product: str) -> None:
    """
    First-run migration: if the store has no stock-futures list yet, copy the
    values from .env so an existing deployment keeps trading the same stocks
    after the upgrade.  Called once by config at import time; a no-op after
    that, which is what makes the dashboard — not .env — the source of truth.
    """
    with _lock:
        cfg = _load()
        if "stock_futures" in cfg:
            return

        # Validate before freezing: a malformed .env value is written to disk
        # exactly once here, so a bad name would otherwise stick permanently.
        clean, dropped = [], []
        for name in env_names:
            try:
                clean.append(normalise_stock_name(name))
            except ValueError:
                dropped.append(name)

        cfg["stock_futures"]         = clean
        cfg["stock_futures_qty"]     = env_qty
        cfg["stock_futures_product"] = env_product
        _save(cfg)

    if dropped:
        logger.error(
            "instrument_config: dropped %d malformed name(s) while seeding from "
            ".env: %s. Check STOCK_FUTURES for stray text (systemd's "
            "EnvironmentFile keeps inline '# comments' as part of the value). "
            "Add them from the dashboard Settings page.",
            len(dropped), ", ".join(repr(d) for d in dropped),
        )
    logger.info(
        "instrument_config: seeded stock futures from .env → %s (qty=%d, product=%s). "
        "The dashboard Settings page is now the source of truth; STOCK_FUTURES in "
        ".env is no longer read.",
        ", ".join(clean) or "(none)", env_qty, env_product,
    )


def get_stock_futures(default: list[str] | None = None) -> list[str]:
    """Return the list of stock futures to trade (falls back to `default`)."""
    with _lock:
        cfg = _load()
    if "stock_futures" not in cfg:
        return list(default or [])
    out = []
    for name in cfg.get("stock_futures") or []:
        try:
            clean = normalise_name(name)
        except ValueError:
            logger.warning("instrument_config: ignoring invalid stock name %r", name)
            continue
        if clean not in out:
            out.append(clean)
    return out


def get_stock_defaults(default_qty: int, default_product: str) -> tuple[int, str]:
    """Return (default lots, product) applied to stock futures."""
    with _lock:
        cfg = _load()
    try:
        qty = normalise_lots(cfg.get("stock_futures_qty", default_qty))
    except ValueError:
        qty = default_qty
    try:
        product = normalise_product(cfg.get("stock_futures_product", default_product))
    except ValueError:
        product = default_product
    return qty, product


def set_stock_defaults(qty: int, product: str) -> tuple[int, str]:
    """Set the default lots and product used for stock futures."""
    qty     = normalise_lots(qty)
    product = normalise_product(product)
    with _lock:
        cfg = _load()
        cfg["stock_futures_qty"]     = qty
        cfg["stock_futures_product"] = product
        _save(cfg)
    logger.info("instrument_config: stock futures defaults set to qty=%d product=%s",
                qty, product)
    return qty, product


def add_stock(name: str, lots: int | None = None) -> str:
    """
    Add a stock to the traded futures list (idempotent).
    Returns the normalised name. Raises ValueError on a malformed name — the
    scrip-master lookup that actually proves the symbol exists happens later,
    in main.reload_instruments().
    """
    name = normalise_stock_name(name)
    with _lock:
        cfg     = _load()
        current = list(cfg.get("stock_futures") or [])
        if name not in current:
            current.append(name)
        cfg["stock_futures"] = current
        if lots is not None:
            cfg.setdefault("lots", {})[name] = normalise_lots(lots)
        _save(cfg)
    logger.info("instrument_config: stock future %s added", name)
    return name


def remove_stock(name: str) -> str:
    """Remove a stock from the traded futures list and drop its lots override."""
    name = normalise_name(name)
    with _lock:
        cfg = _load()
        cfg["stock_futures"] = [
            n for n in (cfg.get("stock_futures") or []) if str(n).upper() != name
        ]
        cfg.get("lots", {}).pop(name, None)
        _save(cfg)
    logger.info("instrument_config: stock future %s removed", name)
    return name
