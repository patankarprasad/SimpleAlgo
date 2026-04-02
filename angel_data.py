"""
Fetch OHLCV candle data from Angel SmartAPI.

The instrument dict passed to get_candles() must already be resolved by
scrip_master.resolve_instrument() so it contains:
  angel_token    – numeric token string from the scrip master
  angel_exchange – exchange string (MCX / NFO)

Angel interval strings:
  ONE_MINUTE, THREE_MINUTE, FIVE_MINUTE, TEN_MINUTE,
  FIFTEEN_MINUTE, THIRTY_MINUTE, ONE_HOUR, ONE_DAY
"""
import logging
import time
from datetime import datetime, timedelta

import pandas as pd

import config
from angel_login import force_relogin, get_angel_session

logger = logging.getLogger(__name__)

# ── Candle retry configuration ─────────────────────────────────────────────────
_CANDLE_MAX_RETRIES   = 5     # total attempts (1 initial + 4 retries)
_CANDLE_RETRY_BASE    = 2.0   # seconds to wait after first failure
_CANDLE_RETRY_BACKOFF = 2.0   # multiplier applied on each subsequent retry
_CANDLE_RETRY_MAX     = 30.0  # cap on wait time (seconds)

# ── Interval string normalisation ──────────────────────────────────────────────
INTERVAL_MAP = {
    # Short aliases (used in .env)
    "1minute":        "ONE_MINUTE",
    "3minute":        "THREE_MINUTE",
    "5minute":        "FIVE_MINUTE",
    "10minute":       "TEN_MINUTE",
    "15minute":       "FIFTEEN_MINUTE",
    "30minute":       "THIRTY_MINUTE",
    "60minute":       "ONE_HOUR",
    "day":            "ONE_DAY",
    # Native Angel strings (accepted as-is)
    "ONE_MINUTE":     "ONE_MINUTE",
    "THREE_MINUTE":   "THREE_MINUTE",
    "FIVE_MINUTE":    "FIVE_MINUTE",
    "TEN_MINUTE":     "TEN_MINUTE",
    "FIFTEEN_MINUTE": "FIFTEEN_MINUTE",
    "THIRTY_MINUTE":  "THIRTY_MINUTE",
    "ONE_HOUR":       "ONE_HOUR",
    "ONE_DAY":        "ONE_DAY",
}


def get_candles(instrument: dict, n_candles: int = None, interval: str = None) -> pd.DataFrame:
    """
    Fetch the last `n_candles` OHLCV candles for an instrument.

    `instrument` must be a resolved dict (from scrip_master.resolve_instrument)
    containing at minimum:
        angel_token    – token string from Angel scrip master
        angel_exchange – exchange (MCX / NFO)
        name           – instrument name (for logging)

    `interval` overrides the candle timeframe for this call; falls back to the
    instrument's own ``timeframe`` field.

    Returns a DataFrame indexed by datetime (candle OPEN time) with columns:
        open, high, low, close, volume

    Only fully-closed candles are returned. Angel sometimes includes the
    currently-forming candle (e.g. at 17:30:01 it may return a partial 17:30
    candle). We strip any row whose open timestamp falls within the current
    candle interval so callers can always safely use ``df.iloc[-1]``.
    """
    n_candles = n_candles or config.CANDLE_LOOKBACK
    raw_interval = interval or instrument["timeframe"]
    interval  = INTERVAL_MAP.get(raw_interval, raw_interval)

    # Determine a from_date that is wide enough to contain n_candles bars,
    # accounting for weekends, holidays, and MCX evening gaps.
    # Cap at 400 days — Angel SmartAPI returns at most ~2 years of history
    # and the daily-interval formula would otherwise request 750+ days.
    minutes_per_bar = _interval_minutes(interval)
    buffer_days     = max(7, int(n_candles * minutes_per_bar / 375) + 5)
    buffer_days     = min(buffer_days, 400)
    from_date = datetime.now() - timedelta(days=buffer_days)
    to_date   = datetime.now()

    angel  = get_angel_session()
    params = {
        "exchange":    instrument["angel_exchange"],
        "symboltoken": instrument["angel_token"],       # from scrip master – no lookup needed
        "interval":    interval,
        "fromdate":    from_date.strftime("%Y-%m-%d %H:%M"),
        "todate":      to_date.strftime("%Y-%m-%d %H:%M"),
    }

    logger.info(
        "Fetching candles: %s | exchange=%s | token=%s | interval=%s",
        instrument["name"], params["exchange"], params["symboltoken"], interval,
    )

    # Fetch with retry: relogin once on first failure, then exponential backoff
    resp         = None
    relogin_done = False
    retry_delay  = _CANDLE_RETRY_BASE

    for attempt in range(1, _CANDLE_MAX_RETRIES + 1):
        time.sleep(config.ANGEL_BASE_DELAY)  # always honour rate limit before each call
        try:
            resp = angel.getCandleData(params)
        except Exception as exc:
            resp = {"status": False, "message": str(exc)}

        if "status" in resp and resp.get("status") is not False and resp.get("data"):
            break  # success

        logger.warning(
            "Angel getCandleData bad response for %s (attempt %d/%d): %s",
            instrument["name"], attempt, _CANDLE_MAX_RETRIES, resp,
        )

        if attempt == _CANDLE_MAX_RETRIES:
            raise RuntimeError(
                f"Angel getCandleData failed for {instrument['name']} after "
                f"{_CANDLE_MAX_RETRIES} attempts: {resp}"
            )

        if not relogin_done:
            logger.info("Angel: forcing re-login before next retry ...")
            angel        = force_relogin()
            relogin_done = True
        else:
            wait = min(retry_delay, _CANDLE_RETRY_MAX)
            logger.info(
                "Angel: waiting %.1fs before retry %d/%d ...",
                wait, attempt + 1, _CANDLE_MAX_RETRIES,
            )
            time.sleep(wait)
            retry_delay *= _CANDLE_RETRY_BACKOFF

    df = pd.DataFrame(
        resp["data"],
        columns=["datetime", "open", "high", "low", "close", "volume"],
    )
    df["datetime"] = pd.to_datetime(df["datetime"])
    # Angel may return ISO timestamps with a +05:30 offset (tz-aware).
    # Normalise to tz-naive IST so all downstream comparisons work uniformly.
    if df["datetime"].dt.tz is not None:
        df["datetime"] = df["datetime"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    df = df.set_index("datetime").sort_index()
    df = df.astype({c: float for c in ["open", "high", "low", "close", "volume"]})

    nan_cols = [c for c in ["open", "high", "low", "close"] if df[c].isna().any()]
    if nan_cols:
        logger.warning(
            "NaN values in OHLCV data for %s (columns: %s) — data may be corrupted",
            instrument["name"], nan_cols,
        )

    # ── Strip the forming (incomplete) candle ─────────────────────────────────
    # Angel timestamps candles by their OPEN time. At e.g. 17:30:01 the API may
    # return a partial 17:30 candle (only 1 s of data). We drop any row whose
    # open-time falls inside the currently-forming interval so that callers can
    # always treat df.iloc[-1] as the last fully-closed candle.
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
        "Fetched %d candles for %s | latest closed candle: %s",
        len(df), instrument["name"],
        df.index[-1].strftime("%Y-%m-%d %H:%M") if not df.empty else "N/A",
    )
    return df


# ── Interval helpers ───────────────────────────────────────────────────────────

def _interval_minutes(interval: str) -> int:
    return {
        "ONE_MINUTE":     1,
        "THREE_MINUTE":   3,
        "FIVE_MINUTE":    5,
        "TEN_MINUTE":     10,
        "FIFTEEN_MINUTE": 15,
        "THIRTY_MINUTE":  30,
        "ONE_HOUR":       60,
        "ONE_DAY":        1440,
    }.get(interval, 15)


def _floor_to_interval(dt: datetime, interval_minutes: int) -> datetime:
    """
    Floor a naive datetime to the nearest lower interval boundary.

    Examples (15-min interval):
        17:30:01  →  17:30:00
        17:15:01  →  17:15:00
        17:29:59  →  17:15:00
    """
    total_minutes = dt.hour * 60 + dt.minute
    floored       = (total_minutes // interval_minutes) * interval_minutes
    return dt.replace(
        hour        = floored // 60,
        minute      = floored % 60,
        second      = 0,
        microsecond = 0,
    )


# ── Live LTP fetcher for options (used by synthetic futures) ───────────────────

def get_option_ltps(options: list[dict]) -> dict[str, float]:
    """
    Fetch LTP for a list of option contracts via Angel getMarketData (bulk call).

    All tokens are sent in a single API request regardless of count, so fetching
    CE + PE for multiple instruments costs just one call instead of N calls.

    Each dict in `options` must contain:
        angel_token          – Angel numeric token string (e.g. "54518")
        angel_exchange       – Exchange (e.g. "NFO")
        kite_tradingsymbol   – Used as the key in the returned dict

    Returns {kite_tradingsymbol: ltp} for each successfully fetched contract.
    Items with missing tokens or unfetched by the API are absent from the result.
    """
    # Group tokens by exchange and build reverse map token -> kite_tradingsymbol
    exchange_tokens: dict[str, list[str]] = {}
    token_to_kite:   dict[str, str]       = {}

    for opt in options:
        token    = (opt.get("angel_token") or "").strip()
        exch     = (opt.get("angel_exchange") or "NFO").strip()
        kite_sym = (opt.get("kite_tradingsymbol") or "").strip()

        if not token or not kite_sym:
            logger.debug("get_option_ltps: skipping %s — missing angel_token", kite_sym)
            continue

        exchange_tokens.setdefault(exch, []).append(token)
        token_to_kite[token] = kite_sym

    if not exchange_tokens:
        return {}

    # Angel getMarketData accepts at most 50 tokens per exchange per call.
    # Split each exchange's list into chunks and merge results.
    _ANGEL_LTP_CHUNK = 50

    def _chunks(lst, n):
        for i in range(0, len(lst), n):
            yield lst[i : i + n]

    angel  = get_angel_session()
    result = {}
    for exch, tokens in exchange_tokens.items():
        for chunk in _chunks(tokens, _ANGEL_LTP_CHUNK):
            try:
                time.sleep(config.ANGEL_LTP_DELAY)
                resp = angel.getMarketData("LTP", {exch: chunk})
                if resp.get("status") and resp.get("data"):
                    for item in resp["data"].get("fetched", []):
                        token    = str(item.get("symbolToken", ""))
                        ltp      = item.get("ltp")
                        kite_sym = token_to_kite.get(token)
                        if kite_sym and ltp is not None:
                            result[kite_sym] = float(ltp)
                            logger.debug("getMarketData LTP: %s = %.2f", kite_sym, float(ltp))
                    for item in resp["data"].get("unfetched", []):
                        token    = str(item.get("symbolToken", ""))
                        kite_sym = token_to_kite.get(token, token)
                        logger.warning("getMarketData: could not fetch LTP for %s (token=%s)", kite_sym, token)
                else:
                    logger.warning("getMarketData LTP failed: %s", resp.get("message", "unknown error"))
            except Exception as exc:
                logger.warning("getMarketData LTP exception: %s", exc)

    return result


# ── Search helper (kept for manual token lookup / debugging) ───────────────────

def search_token(symbol: str, exchange: str) -> None:
    """
    Print FUT contracts matching `symbol` on `exchange`.
    Uses the Angel searchScrip API – prefer scrip_master for production use.
    """
    import logzero

    time.sleep(config.ANGEL_SEARCH_DELAY)
    _prev = logzero.logger.level
    logzero.loglevel(logging.WARNING)
    try:
        angel = get_angel_session()
        resp  = angel.searchScrip(exchange, symbol)
    finally:
        logzero.loglevel(_prev if _prev != 0 else logging.DEBUG)

    if resp["status"] and resp.get("data"):
        futures = [r for r in resp["data"] if r["tradingsymbol"].endswith("FUT")]
        rows = futures if futures else resp["data"][:10]
        for item in rows:
            print(f"  symbol={item['tradingsymbol']}  token={item['symboltoken']}")
    else:
        print(f"  No results for {symbol} on {exchange}")
