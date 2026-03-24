"""
Date-wise persistent trade log.

Each trading day gets its own file: trades/YYYY-MM-DD.json
Entries survive process restarts. The web UI reads these files directly.
"""
import json
import logging
from datetime import date, datetime
from pathlib import Path

import pytz

logger = logging.getLogger(__name__)
_IST = pytz.timezone("Asia/Kolkata")

TRADES_DIR = Path("trades")


def log_trade(name: str, action: str, symbol: str, qty: int,
              dry_run: bool = False) -> None:
    """Append one trade entry to today's log file."""
    TRADES_DIR.mkdir(exist_ok=True)
    path = _path_for(str(date.today()))

    entries = _load(path)
    entries.append({
        "time":    datetime.now(_IST).strftime("%H:%M:%S"),
        "name":    name,
        "action":  action,
        "symbol":  symbol,
        "qty":     qty,
        "dry_run": dry_run,
    })
    path.write_text(json.dumps(entries, indent=2))
    logger.info("Trade logged: %s %s %s qty=%d dry=%s", name, action, symbol, qty, dry_run)


def get_trades(trade_date: str = None) -> list[dict]:
    """Return all trades for the given date (YYYY-MM-DD), defaulting to today."""
    if trade_date is None:
        trade_date = str(date.today())
    return _load(_path_for(trade_date))


def list_dates() -> list[str]:
    """Return all dates that have a trade log file, newest first."""
    if not TRADES_DIR.exists():
        return []
    return sorted(
        (p.stem for p in TRADES_DIR.glob("????.??.??.json")),
        reverse=True,
    )


def _path_for(trade_date: str) -> Path:
    return TRADES_DIR / f"{trade_date}.json"


def _load(path: Path) -> list:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return []
    return []
