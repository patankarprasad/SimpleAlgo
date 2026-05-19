"""
Fetch OHLCV candle data and option LTPs from Zerodha Kite.

Used when DATA_PROVIDER=kite in .env.  The functions here mirror the
interface of angel_data.py so that angel_data.get_candles() / get_option_ltps()
can transparently delegate to this module without any changes to callers.

Requires a paid Kite Connect Historical Data API subscription.

The instrument dict passed to get_candles_kite() must already be resolved by
scrip_master.resolve_instrument() so it contains:
  kite_instrument_token – integer token from the Kite instrument master
  name                  – instrument name (for logging)
  timeframe             – Angel-format interval string (e.g. FIFTEEN_MINUTE)

Kite interval strings:
  minute, 3minute, 5minute, 10minute, 15minute, 30minute, 60minute, day
"""
import logging
import threading
import time
from datetime import datetime, timedelta

import pandas as pd

import config
from kite_login import get_kite_session

logger = logging.getLogger(__name__)

# ── Kite rate limiters ────────────────────────────────────────────────────────
# Historical candle data: 3 req/s  → min gap 0.334s → use 0.4s (headroom)
# Quote / ltp():          1 req/s  → min gap 1.0s   → use 1.1s (headroom)
_KITE_HIST_DELAY     = 0.4
_kite_hist_lock      = threading.Lock()
_kite_hist_last_call = 0.0

_KITE_LTP_DELAY     = 1.1
_kite_ltp_lock      = threading.Lock()
_kite_ltp_last_call = 0.0


def _kite_rate_limit() -> None:
    global _kite_hist_last_call
    with _kite_hist_lock:
        elapsed = time.monotonic() - _kite_hist_last_call
        if elapsed < _KITE_HIST_DELAY:
            time.sleep(_KITE_HIST_DELAY - elapsed)
        _kite_hist_last_call = time.monotonic()


def _kite_ltp_rate_limit() -> None:
    global _kite_ltp_last_call
    with _kite_ltp_lock:
        elapsed = time.monotonic() - _kite_ltp_last_call
        if elapsed < _KITE_LTP_DELAY:
            time.sleep(_KITE_LTP_DELAY - elapsed)
        _kite_ltp_last_call = time.monotonic()


# ── Interval mappings ──────────────────────────────────────────────────────────

# Angel interval string → Kite interval string
ANGEL_TO_KITE_INTERVAL = {
    "ONE_MINUTE":     "minute",
    "THREE_MINUTE":   "3minute",
    "FIVE_MINUTE":    "5minute",
    "TEN_MINUTE":     "10minute",
    "FIFTEEN_MINUTE": "15minute",
    "THIRTY_MINUTE":  "30minute",
    "ONE_HOUR":       "60minute",
    "ONE_DAY":        "day",
}

# Kite enforces a maximum date-range per request depending on interval.
_KITE_MAX_DAYS = {
    "minute":   60,
    "3minute":  100,
    "5minute":  100,
    "10minute": 100,
    "15minute": 200,
    "30minute": 200,
    "60minute": 400,
    "day":      2000,
}

# Minutes per Kite interval (used to compute buffer_days and strip forming candle)
_KITE_INTERVAL_MINUTES = {
    "minute":   1,
    "3minute":  3,
    "5minute":  5,
    "10minute": 10,
    "15minute": 15,
    "30minute": 30,
    "60minute": 60,
    "day":      1440,
}

# ── Retry configuration ────────────────────────────────────────────────────────
_MAX_RETRIES   = 5
_RETRY_BASE    = 2.0
_RETRY_BACKOFF = 2.0
_RETRY_MAX     = 30.0


def get_candles_kite(instrument: dict, n_candles: int, angel_interval: str) -> pd.DataFrame:
    """
    Fetch the last `n_candles` OHLCV candles from Kite historical data API.

    `angel_interval` is already normalised to an Angel-format string
    (e.g. FIFTEEN_MINUTE) by angel_data.get_candles() before calling here.

    Returns a DataFrame indexed by datetime (candle OPEN time) with columns:
        open, high, low, close, volume
    Only fully-closed candles are returned.
    """
    # Resolve Kite interval
    kite_interval = ANGEL_TO_KITE_INTERVAL.get(angel_interval)
    if kite_interval is None:
        raise ValueError(
            f"Unsupported interval '{angel_interval}' for Kite data provider. "
            f"Supported: {list(ANGEL_TO_KITE_INTERVAL)}"
        )

    minutes_per_bar = _KITE_INTERVAL_MINUTES[kite_interval]
    max_days        = _KITE_MAX_DAYS[kite_interval]

    buffer_days = max(7, int(n_candles * minutes_per_bar / 375) + 5)
    buffer_days = min(buffer_days, max_days)

    from_date = datetime.now() - timedelta(days=buffer_days)
    to_date   = datetime.now()

    token = instrument["kite_instrument_token"]

    logger.info(
        "Fetching candles (Kite): %s | token=%s | interval=%s",
        instrument["name"], token, kite_interval,
    )

    resp         = None
    retry_delay  = _RETRY_BASE

    for attempt in range(1, _MAX_RETRIES + 1):
        _kite_rate_limit()
        try:
            kite = get_kite_session()
            resp = kite.historical_data(token, from_date, to_date, kite_interval)
            if resp:
                break
        except Exception as exc:
            msg = str(exc)
            logger.warning(
                "Kite historical_data failed for %s (attempt %d/%d): %s",
                instrument["name"], attempt, _MAX_RETRIES, msg,
            )
            if attempt == _MAX_RETRIES:
                raise RuntimeError(
                    f"Kite historical_data failed for {instrument['name']} "
                    f"after {_MAX_RETRIES} attempts: {msg}"
                ) from exc
            wait = min(retry_delay, _RETRY_MAX)
            logger.info(
                "Kite: waiting %.1fs before retry %d/%d ...",
                wait, attempt + 1, _MAX_RETRIES,
            )
            time.sleep(wait)
            retry_delay *= _RETRY_BACKOFF

    if not resp:
        raise RuntimeError(
            f"Kite historical_data returned empty response for {instrument['name']}"
        )

    # Kite returns list of dicts: {date: datetime, open, high, low, close, volume}
    df = pd.DataFrame(resp)
    df = df.rename(columns={"date": "datetime"})
    df["datetime"] = pd.to_datetime(df["datetime"])
    # Kite returns tz-aware datetimes (IST / +05:30) — normalise to tz-naive
    if df["datetime"].dt.tz is not None:
        df["datetime"] = df["datetime"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    df = df.set_index("datetime").sort_index()
    df = df[["open", "high", "low", "close", "volume"]]
    df = df.astype(float)

    nan_cols = [c for c in ["open", "high", "low", "close"] if df[c].isna().any()]
    if nan_cols:
        logger.warning(
            "NaN values in Kite OHLCV data for %s (columns: %s)",
            instrument["name"], nan_cols,
        )

    # Strip the currently-forming (incomplete) candle using the same logic as
    # angel_data — Kite may include the open candle in the response.
    from angel_data import _floor_to_interval
    current_candle_open = pd.Timestamp(_floor_to_interval(datetime.now(), minutes_per_bar))
    if not df.empty and df.index[-1] >= current_candle_open:
        logger.debug(
            "%s: dropping forming candle (open=%s, current interval started %s)",
            instrument["name"],
            df.index[-1].strftime("%H:%M"),
            current_candle_open.strftime("%H:%M"),
        )
        df = df[df.index < current_candle_open]

    df = df.tail(n_candles)
    logger.info(
        "Fetched %d candles for %s via Kite | latest closed candle: %s",
        len(df), instrument["name"],
        df.index[-1].strftime("%Y-%m-%d %H:%M") if not df.empty else "N/A",
    )
    return df


def get_option_ltps_kite(options: list[dict]) -> dict[str, float]:
    """
    Fetch LTP for a list of option contracts via Kite ltp().

    Each dict in `options` must contain:
        kite_tradingsymbol  – Kite trading symbol (e.g. NIFTY25JUN26000CE)
        angel_exchange      – Exchange string (NFO / MCX) used to build Kite key

    Returns {kite_tradingsymbol: ltp} — same shape as angel_data.get_option_ltps().
    """
    # Build "EXCHANGE:SYMBOL" keys that Kite's ltp() expects
    kite_keys  = []
    key_to_sym = {}  # "NFO:NIFTY25JUN26000CE" -> "NIFTY25JUN26000CE"

    for opt in options:
        sym  = (opt.get("kite_tradingsymbol") or "").strip()
        exch = (opt.get("angel_exchange") or "NFO").strip()
        if not sym:
            continue
        key = f"{exch}:{sym}"
        kite_keys.append(key)
        key_to_sym[key] = sym

    if not kite_keys:
        return {}

    try:
        _kite_ltp_rate_limit()
        kite = get_kite_session()
        resp = kite.ltp(kite_keys)
    except Exception as exc:
        logger.warning("Kite ltp() failed: %s", exc)
        return {}

    result = {}
    for key, data in resp.items():
        sym = key_to_sym.get(key)
        ltp = data.get("last_price") if data else None
        if sym and ltp is not None:
            result[sym] = float(ltp)
            logger.debug("Kite LTP: %s = %.2f", sym, float(ltp))

    return result
