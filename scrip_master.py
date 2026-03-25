"""
Scrip master correlation – Angel SmartAPI  ↔  Zerodha Kite.

Key insight
-----------
Kite's `exchange_token` field == Angel's `token` field (same numeric ID).
This makes the join a single integer equality – no symbol-string parsing,
no date-format gymnastics.

What this module provides
--------------------------
1. Downloads both masters once per day (cached to disk).
2. Builds a merged DataFrame: one row per futures contract with all fields
   needed for both data-fetching (Angel) and order-placement (Kite).
3. `get_nearest_future(name, exchange)` – returns the nearest-expiry active
   contract dict for a given underlying + exchange.
4. `resolve_instrument(inst_def)` – enriches a bare instrument config dict
   (name, exchanges, qty, product) with all resolved runtime fields.

Angel `instrumenttype` values we care about
--------------------------------------------
  MCX  → FUTCOM  (commodity futures, e.g. GOLDM, CRUDEOIL)
         FUTCUR  (currency futures – not used here but included)
  NFO  → FUTIDX  (index futures, e.g. NIFTY, BANKNIFTY)
         FUTSTK  (stock futures)

Kite `instrument_type` = "FUT" for all futures.
"""
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# ── Cache file paths ───────────────────────────────────────────────────────────
_CACHE_DIR    = Path("cache")
_ANGEL_CACHE  = _CACHE_DIR / "angel_scrip_master.json"
_KITE_CACHE   = _CACHE_DIR / "kite_instruments.csv"
_META_FILE    = _CACHE_DIR / "scrip_master_meta.json"

# ── Remote URLs ────────────────────────────────────────────────────────────────
_ANGEL_URL = (
    "https://margincalculator.angelbroking.com"
    "/OpenAPI_File/files/OpenAPIScripMaster.json"
)
_KITE_URL  = "https://api.kite.trade/instruments"

# ── In-memory cached DataFrames (module-level) ────────────────────────────────
_merged_df:  pd.DataFrame | None = None   # futures (FUT) — Angel + Kite join
_options_df: pd.DataFrame | None = None   # NFO options (CE/PE) — Kite only


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def get_nearest_future(name: str, exchange: str) -> dict:
    """
    Return a dict with all Angel + Kite fields for the nearest-expiry
    active futures contract of `name` on `exchange`.

    Example:
        get_nearest_future("GOLDM", "MCX")
        get_nearest_future("BANKNIFTY", "NFO")

    Returned keys:
        angel_token, angel_symbol, angel_exchange,
        kite_tradingsymbol, kite_instrument_token, kite_exchange,
        name, expiry (datetime.date), lot_size, tick_size
    """
    df = _get_merged()
    today = date.today()

    mask = (
        (df["name"].str.upper() == name.upper()) &
        (df["exchange"] == exchange.upper()) &
        (df["expiry_date"] > today)          # strictly future
    )
    subset = df[mask].sort_values("expiry_date")

    if subset.empty:
        raise ValueError(
            f"No active futures found for {name} on {exchange}. "
            "Is the scrip master up-to-date?"
        )

    row = subset.iloc[0]
    return _row_to_dict(row)


def get_all_futures(name: str, exchange: str) -> list[dict]:
    """Return all active futures contracts (sorted nearest-first)."""
    df  = _get_merged()
    today = date.today()

    mask = (
        (df["name"].str.upper() == name.upper()) &
        (df["exchange"] == exchange.upper()) &
        (df["expiry_date"] > today)
    )
    return [_row_to_dict(r) for _, r in df[mask].sort_values("expiry_date").iterrows()]


def get_atm_options(
    name: str,
    exchange: str,
    futures_price: float,
    strike_step: int,
    expiry_date,            # datetime.date — use instrument["expiry"] from resolve_instrument()
) -> tuple[dict, dict]:
    """
    Return (ce_dict, pe_dict) for the ATM synthetic future legs.

    Strike = nearest to futures_price rounded to strike_step.
    Expiry  = monthly contract date already resolved via get_nearest_future()
              (monthly futures and monthly options share the same expiry date).

    Each returned dict contains:
        kite_tradingsymbol, kite_instrument_token,
        strike, expiry, lot_size, tick_size,
        option_type ("CE" or "PE"), exchange

    Raises ValueError if no matching option rows are found.
    """
    df = _get_options()

    # Use arithmetic rounding (avoid Python's banker's rounding for .5 cases)
    atm_strike = int(futures_price / strike_step + 0.5) * strike_step

    mask = (
        (df["name"].str.upper() == name.upper()) &
        (df["expiry_date"] == expiry_date) &
        (df["strike"] == atm_strike)
    )
    subset = df[mask]

    ce_rows = subset[subset["instrument_type"] == "CE"]
    pe_rows = subset[subset["instrument_type"] == "PE"]

    if ce_rows.empty or pe_rows.empty:
        available = sorted(
            df[df["name"].str.upper() == name.upper()]["strike"].unique()
        )
        raise ValueError(
            f"ATM options not found for {name} strike={atm_strike} expiry={expiry_date}. "
            f"Available strikes (first 10): {available[:10]}"
        )

    def _opt_dict(row, opt_type: str) -> dict:
        return {
            "kite_tradingsymbol":    row["tradingsymbol"],
            "kite_instrument_token": int(row["instrument_token"]),
            "strike":                int(row["strike"]),
            "expiry":                row["expiry_date"],
            "lot_size":              int(row["lot_size"]),
            "tick_size":             float(row["tick_size"]),
            "option_type":           opt_type,
            "exchange":              exchange.upper(),
            # Angel fields — needed for getLtpData calls
            "angel_token":           str(row.get("angel_token", "")),
            "angel_symbol":          str(row.get("angel_symbol", "")),
            "angel_exchange":        str(row.get("angel_exchange", "NFO")),
        }

    return (
        _opt_dict(ce_rows.iloc[0], "CE"),
        _opt_dict(pe_rows.iloc[0], "PE"),
    )


def resolve_instrument(inst_def: dict) -> dict:
    """
    Enrich a bare instrument config dict with live scrip-master data.

    Input (from config.py):
        {"name": "GOLDM", "angel_exchange": "MCX", "kite_exchange": "MCX",
         "qty": 1, "product": "NRML"}

    Output (adds):
        angel_token, angel_symbol,
        kite_tradingsymbol, kite_instrument_token,
        expiry, lot_size, tick_size
    """
    contract = get_nearest_future(
        inst_def["name"],
        inst_def["exchange"],           # both brokers share the same exchange name
    )
    return {**inst_def, **contract}


def refresh_masters(force: bool = False):
    """
    Download and cache both scrip masters.
    Skips download if already cached today (unless force=True).
    """
    _CACHE_DIR.mkdir(exist_ok=True)
    if not force and _is_fresh():
        logger.info("Scrip masters are up-to-date (cached today).")
        return

    logger.info("Downloading Angel scrip master …")
    _download_angel()

    logger.info("Downloading Kite instruments …")
    _download_kite()

    # Write today's date to the meta file
    _META_FILE.write_text(json.dumps({"date": str(date.today())}))

    # Invalidate in-memory caches so they are rebuilt on next access
    global _merged_df, _options_df
    _merged_df  = None
    _options_df = None
    logger.info("Scrip masters refreshed and cached.")


# ══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ══════════════════════════════════════════════════════════════════════════════

def _is_fresh() -> bool:
    if not _META_FILE.exists():
        return False
    meta = json.loads(_META_FILE.read_text())
    return meta.get("date") == str(date.today())


def _download_angel():
    r = requests.get(_ANGEL_URL, timeout=60)
    r.raise_for_status()
    _ANGEL_CACHE.write_text(r.text, encoding="utf-8")
    logger.info("Angel master saved (%d bytes)", len(r.content))


def _download_kite():
    r = requests.get(_KITE_URL, timeout=60)
    r.raise_for_status()
    _KITE_CACHE.write_text(r.text, encoding="utf-8")
    logger.info("Kite instruments saved (%d bytes)", len(r.content))


def _get_merged() -> pd.DataFrame:
    """Return (building if needed) the in-memory merged DataFrame."""
    global _merged_df
    if _merged_df is None:
        refresh_masters()
        _merged_df = _build_merged()
    return _merged_df


def _get_options() -> pd.DataFrame:
    """Return (building if needed) the in-memory NFO options DataFrame."""
    global _options_df
    if _options_df is None:
        refresh_masters()
        _options_df = _build_options()
    return _options_df


def _build_options() -> pd.DataFrame:
    """
    Build a DataFrame of all NFO index options (CE/PE).

    Joins the Kite CSV with the Angel OPTIDX master on the shared numeric token
    so that angel_token and angel_symbol are available for getLtpData calls.
    Both fields are needed because Angel's getLtpData requires all three of:
        exchange, symboltoken, tradingsymbol
    """
    # ── Kite options rows ─────────────────────────────────────────────────────
    kite_df = pd.read_csv(_KITE_CACHE, dtype={"exchange_token": int})
    opt_df  = kite_df[
        kite_df["instrument_type"].isin(["CE", "PE"]) &
        (kite_df["exchange"] == "NFO")
    ].copy()
    opt_df["expiry_date"] = pd.to_datetime(opt_df["expiry"], errors="coerce").dt.date
    opt_df["strike"]      = pd.to_numeric(opt_df["strike"], errors="coerce")
    opt_df                = opt_df.dropna(subset=["expiry_date", "strike"])
    opt_df["strike"]      = opt_df["strike"].astype(int)

    # ── Angel OPTIDX join — adds angel_token + angel_symbol ───────────────────
    angel_raw = json.loads(_ANGEL_CACHE.read_text(encoding="utf-8"))
    angel_df  = pd.DataFrame(angel_raw)
    angel_df  = angel_df[angel_df["instrumenttype"] == "OPTIDX"].copy()
    angel_df["token_int"] = pd.to_numeric(angel_df["token"], errors="coerce")
    angel_df  = angel_df.dropna(subset=["token_int"])
    angel_df["token_int"] = angel_df["token_int"].astype(int)

    opt_df = opt_df.merge(
        angel_df[["token_int", "token", "symbol", "exch_seg"]],
        left_on  = "exchange_token",
        right_on = "token_int",
        how      = "left",      # keep all Kite rows even if Angel join misses
    )
    opt_df = opt_df.rename(columns={
        "token":    "angel_token",
        "symbol":   "angel_symbol",
        "exch_seg": "angel_exchange",
    })
    # Fill NaN for any rows that didn't match Angel (e.g. very new contracts)
    opt_df["angel_token"]    = opt_df["angel_token"].fillna("").astype(str)
    opt_df["angel_symbol"]   = opt_df["angel_symbol"].fillna("").astype(str)
    opt_df["angel_exchange"] = opt_df["angel_exchange"].fillna("NFO").astype(str)

    matched = (opt_df["angel_token"] != "").sum()
    logger.info(
        "Options master built: %d NFO CE/PE contracts (%d with Angel token)",
        len(opt_df), matched,
    )
    return opt_df


def _build_merged() -> pd.DataFrame:
    """
    Join Angel + Kite on the shared numeric token field.

    Angel: token (str)  →  cast to int
    Kite:  exchange_token (int)
    """
    # ── Load Angel master ──────────────────────────────────────────────────────
    angel_raw = json.loads(_ANGEL_CACHE.read_text(encoding="utf-8"))
    angel_df  = pd.DataFrame(angel_raw)

    # Keep only futures contracts we care about
    fut_types = {"FUTCOM", "FUTCUR", "FUTIDX", "FUTSTK"}
    angel_df  = angel_df[angel_df["instrumenttype"].isin(fut_types)].copy()
    angel_df["token_int"] = pd.to_numeric(angel_df["token"], errors="coerce")
    angel_df = angel_df.dropna(subset=["token_int"])
    angel_df["token_int"] = angel_df["token_int"].astype(int)

    # Parse Angel expiry: format is "DDMMMYYYY", e.g. "03APR2026"
    angel_df["angel_expiry_date"] = pd.to_datetime(
        angel_df["expiry"], format="%d%b%Y", errors="coerce"
    ).dt.date

    # ── Load Kite master ───────────────────────────────────────────────────────
    kite_df = pd.read_csv(_KITE_CACHE, dtype={"exchange_token": int})
    kite_df = kite_df[kite_df["instrument_type"] == "FUT"].copy()
    kite_df["expiry_date"] = pd.to_datetime(
        kite_df["expiry"], errors="coerce"
    ).dt.date
    kite_df = kite_df.dropna(subset=["expiry_date"])

    # ── Merge on shared token ─────────────────────────────────────────────────
    merged = kite_df.merge(
        angel_df[["token_int", "token", "symbol", "exch_seg", "instrumenttype"]],
        left_on  = "exchange_token",
        right_on = "token_int",
        how      = "inner",      # only keep rows present in BOTH masters
    )

    # ── Rename columns for clarity ────────────────────────────────────────────
    merged = merged.rename(columns={
        "token":           "angel_token",
        "symbol":          "angel_symbol",
        "exch_seg":        "angel_exchange",
        "instrument_token":"kite_instrument_token",
        "tradingsymbol":   "kite_tradingsymbol",
        "exchange":        "exchange",      # shared exchange name (MCX / NFO)
    })

    # Keep only the columns we actually use
    keep = [
        "name", "exchange", "expiry_date",
        "angel_token", "angel_symbol", "angel_exchange",
        "kite_tradingsymbol", "kite_instrument_token",
        "lot_size", "tick_size", "instrumenttype",
    ]
    merged = merged[keep].copy()

    logger.info(
        "Scrip master merged: %d futures contracts across all exchanges",
        len(merged),
    )
    return merged


def _row_to_dict(row) -> dict:
    return {
        "name":                  row["name"],
        "exchange":              row["exchange"],
        "expiry":                row["expiry_date"],
        "angel_token":           str(row["angel_token"]),
        "angel_symbol":          row["angel_symbol"],
        "angel_exchange":        row["angel_exchange"],
        "kite_tradingsymbol":    row["kite_tradingsymbol"],
        "kite_instrument_token": int(row["kite_instrument_token"]),
        "lot_size":              int(row["lot_size"]),
        "tick_size":             float(row["tick_size"]),
    }
