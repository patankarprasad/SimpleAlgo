#!/usr/bin/env python3
"""
Single-instrument backtest with user-selectable candle interval.

Strategy (mirrors the live algo exactly):
  LONG  entry : candle close > ST(10,2)  AND  > ST(10,3)  AND  > MA(50)
  LONG  SL    : candle close < ST(10,2)
  SHORT entry : candle close < ST(10,2)  AND  < ST(10,3)  AND  < MA(50)
  SHORT SL    : candle close > ST(10,2)

A SELL signal while long exits and immediately enters short (same candle),
and a BUY signal while short exits and immediately enters long.

Usage:
  python backtest_single.py --instrument GOLDM      --interval FIFTEEN_MINUTE
  python backtest_single.py --instrument CRUDE      --interval ONE_HOUR
  python backtest_single.py --instrument RELIANCE   --interval FIFTEEN_MINUTE
  python backtest_single.py --instrument TATASTEEL  --interval ONE_HOUR --from 2025-01-01
  python backtest_single.py --instrument NIFTY      --interval FIFTEEN_MINUTE --from 2025-01-01 --to 2025-12-31
  python backtest_single.py --instrument BANKNIFTY  --interval ONE_HOUR --save-trades

Any instrument that has an active futures contract in the Angel/Kite scrip
master can be used — NIFTY, BANKNIFTY, GOLDM, CRUDEOIL, SILVERM, RELIANCE,
TATAMOTORS, NIFTYBANK, MIDCPNIFTY, etc.

Supported intervals : ONE_MINUTE  THREE_MINUTE  FIVE_MINUTE  TEN_MINUTE
                      FIFTEEN_MINUTE  THIRTY_MINUTE  ONE_HOUR  ONE_DAY

Notes:
  - contract_size for P&L calculation:
      MCX known instruments (GOLDM/CRUDE/SILVERM) use physical contract sizes.
      All other instruments use lot_size from the scrip master.
  - For NIFTY/BANKNIFTY, candle data is fetched for the nearest futures
    contract. The live algo uses NSE spot-index candles for signals —
    so backtest signals may differ very slightly from live signals.
"""
import argparse
import itertools
import logging
import sys
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import config
import scrip_master
from angel_login import force_relogin, get_angel_session
from indicators import sma, supertrend

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Quieten noisy sub-module loggers during backtest runs
for _mod in ("angel_login", "scrip_master", "urllib3", "requests"):
    logging.getLogger(_mod).setLevel(logging.WARNING)


# ── Strategy parameters (match config.py) ─────────────────────────────────────
ST1_PERIOD = config.ST1_PERIOD    # 10
ST1_FACTOR = config.ST1_FACTOR    # 2.0  → ST(10,2)  used for SL
ST2_PERIOD = config.ST2_PERIOD    # 10
ST2_FACTOR = config.ST2_FACTOR    # 3.0  → ST(10,3)  used for entry confirmation
MA_LENGTH  = config.MA_LENGTH     # 50

# ── Angel interval string → minutes per bar ────────────────────────────────────
INTERVAL_MINUTES = {
    "ONE_MINUTE":     1,
    "THREE_MINUTE":   3,
    "FIVE_MINUTE":    5,
    "TEN_MINUTE":     10,
    "FIFTEEN_MINUTE": 15,
    "THIRTY_MINUTE":  30,
    "ONE_HOUR":       60,
    "ONE_DAY":        1440,
}

# ── MCX physical contract sizes (PnL multiplier per price point) ──────────────
# For all other instruments, lot_size from the scrip master is used directly.
MCX_CONTRACT_SIZES = {
    "GOLDM":    10,     # 10 grams/lot
    "CRUDEOIL": 100,    # 100 barrels/lot
    "SILVERM":  5,      # 5 kg/lot
    "GOLD":     1,      # 1 gram/lot (GOLD Mini)
    "CRUDE":    100,    # alias
    "SILVER":   30,     # 30 kg/lot
    "COPPER":   250,    # 250 kg/lot
    "NATURALGAS": 1250, # 1250 mmBtu/lot
    "ZINC":     5000,   # 5000 kg/lot
    "ALUMINIUM":5000,
    "LEAD":     5000,
    "NICKEL":   250,
}

# ── Instrument aliases (common shorthand → canonical scrip master name) ────────
INSTRUMENT_ALIASES = {
    "CRUDE":     "CRUDEOIL",
    "BNF":       "BANKNIFTY",
    "NF":        "NIFTY",
    "FINNIFTY":  "FINNIFTY",
    "MIDCAP":    "MIDCPNIFTY",
}

# Max days Angel API returns per call (conservative values)
_CHUNK_DAYS = {
    1:    60,
    3:    60,
    5:    120,
    10:   120,
    15:   120,
    30:   200,
    60:   400,
    1440: 400,
}


# ══════════════════════════════════════════════════════════════════════════════
# Dynamic instrument resolver
# ══════════════════════════════════════════════════════════════════════════════

def resolve_instrument_dynamic(name: str) -> tuple[dict, int]:
    """
    Resolve any futures instrument by name from the scrip master.

    Resolution order:
      1. Apply alias (e.g. CRUDE → CRUDEOIL, BNF → BANKNIFTY).
      2. Search the merged scrip master for active futures contracts whose
         `name` field matches exactly (case-insensitive), across all exchanges.
      3. Pick the nearest-expiry contract.
      4. Derive contract_size:
           - MCX instruments  → use MCX_CONTRACT_SIZES if available, else lot_size
           - All others       → lot_size from scrip master

    Returns:
        (contract_dict, contract_size)

    Raises ValueError with spelling suggestions if no match is found.
    """
    from datetime import date as _date

    canonical = INSTRUMENT_ALIASES.get(name.upper(), name.upper())

    scrip_master.refresh_masters()
    merged = scrip_master._get_merged()
    today  = _date.today()

    mask = (
        (merged["name"].str.upper() == canonical) &
        (merged["expiry_date"] > today)
    )
    subset = merged[mask].sort_values("expiry_date")

    if subset.empty:
        # Suggest close matches from the scrip master
        all_names   = sorted(merged["name"].str.upper().unique())
        suggestions = [n for n in all_names if canonical in n or n in canonical][:8]
        hint = (
            f"\n  Did you mean one of: {', '.join(suggestions)}"
            if suggestions else ""
        )
        raise ValueError(
            f"No active futures contract found for '{name}' in the scrip master.{hint}\n"
            f"  Tip: names must match exactly as listed by the broker "
            f"(e.g. RELIANCE, TATAMOTORS, HDFCBANK, NIFTY, BANKNIFTY)."
        )

    row      = subset.iloc[0]
    contract = scrip_master._row_to_dict(row)

    # Determine contract_size (P&L multiplier per 1-point move)
    if row["exchange"] == "MCX":
        contract_size = MCX_CONTRACT_SIZES.get(canonical, int(row["lot_size"]))
    else:
        contract_size = int(row["lot_size"])

    return contract, contract_size


# ══════════════════════════════════════════════════════════════════════════════
# Historical data fetcher
# ══════════════════════════════════════════════════════════════════════════════

def fetch_history(
    instrument: dict,
    interval: str,
    from_dt: datetime,
    to_dt: datetime,
) -> pd.DataFrame:
    """
    Fetch OHLCV candles for an instrument between from_dt and to_dt.

    Requests are automatically chunked to stay within Angel API limits.
    Returns a DataFrame indexed by IST datetime (tz-naive) with columns:
        open, high, low, close, volume
    """
    minutes_per_bar = INTERVAL_MINUTES[interval]
    chunk_days = _CHUNK_DAYS.get(minutes_per_bar, 120)

    all_chunks: list[pd.DataFrame] = []
    cursor = from_dt
    angel  = get_angel_session()

    while cursor < to_dt:
        chunk_end = min(cursor + timedelta(days=chunk_days), to_dt)

        params = {
            "exchange":    instrument["angel_exchange"],
            "symboltoken": instrument["angel_token"],
            "interval":    interval,
            "fromdate":    cursor.strftime("%Y-%m-%d %H:%M"),
            "todate":      chunk_end.strftime("%Y-%m-%d %H:%M"),
        }

        logger.info(
            "Fetching %s [%s]  %s → %s",
            instrument["name"], interval,
            cursor.strftime("%Y-%m-%d"),
            chunk_end.strftime("%Y-%m-%d"),
        )

        resp         = None
        relogin_done = False
        retry_delay  = 2.0

        for attempt in range(1, 6):
            time.sleep(config.ANGEL_BASE_DELAY)
            try:
                resp = angel.getCandleData(params)
            except Exception as exc:
                resp = {"status": False, "message": str(exc)}

            if resp.get("status") is not False and resp.get("data"):
                break

            logger.warning(
                "Attempt %d/5 failed: %s",
                attempt, resp.get("message", "unknown error"),
            )
            if attempt == 5:
                raise RuntimeError(
                    f"Angel getCandleData failed after 5 attempts for "
                    f"{instrument['name']}: {resp}"
                )
            if not relogin_done:
                logger.info("Forcing re-login before retry ...")
                angel        = force_relogin()
                relogin_done = True
            else:
                wait = min(retry_delay, 30.0)
                logger.info("Waiting %.1fs before retry ...", wait)
                time.sleep(wait)
                retry_delay *= 2.0

        if resp and resp.get("data"):
            chunk_df = pd.DataFrame(
                resp["data"],
                columns=["datetime", "open", "high", "low", "close", "volume"],
            )
            chunk_df["datetime"] = pd.to_datetime(chunk_df["datetime"])
            if chunk_df["datetime"].dt.tz is not None:
                chunk_df["datetime"] = (
                    chunk_df["datetime"]
                    .dt.tz_convert("Asia/Kolkata")
                    .dt.tz_localize(None)
                )
            all_chunks.append(chunk_df)

        cursor = chunk_end + timedelta(minutes=1)

    if not all_chunks:
        raise RuntimeError("No candle data returned from Angel API.")

    df = pd.concat(all_chunks, ignore_index=True)
    df = df.set_index("datetime").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df = df.astype({c: float for c in ["open", "high", "low", "close", "volume"]})

    logger.info(
        "Loaded %d candles  |  %s → %s",
        len(df),
        df.index[0].strftime("%Y-%m-%d %H:%M"),
        df.index[-1].strftime("%Y-%m-%d %H:%M"),
    )
    return df


# ══════════════════════════════════════════════════════════════════════════════
# Indicator + signal computation
# ══════════════════════════════════════════════════════════════════════════════

def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute SuperTrend and MA indicators, then derive a signal for each candle.

    Indicators:
        st1  – SuperTrend(10, 2)  →  entry/SL line
        st2  – SuperTrend(10, 3)  →  entry confirmation line
        ma   – SMA(50)

    Signals (first matching rule wins):
        'BUY'        → close > st1 AND close > st2 AND close > ma
        'SELL'       → close < st1 AND close < st2 AND close < ma
        'EXIT_LONG'  → close < st1   (SL hit for longs, but not a full SELL)
        'EXIT_SHORT' → close > st1   (SL hit for shorts, but not a full BUY)
        None         → indicator still in warm-up (NaN)
    """
    df = df.copy()
    df["st1"] = supertrend(df, ST1_PERIOD, ST1_FACTOR)
    df["st2"] = supertrend(df, ST2_PERIOD, ST2_FACTOR)
    df["ma"]  = sma(df["close"], MA_LENGTH)

    c   = df["close"]
    st1 = df["st1"]
    st2 = df["st2"]
    ma  = df["ma"]

    buy_cond   = (c > st1) & (c > st2) & (c > ma)
    sell_cond  = (c < st1) & (c < st2) & (c < ma)
    exit_long  = c < st1
    exit_short = c > st1

    df["signal"] = np.select(
        [buy_cond,  sell_cond, exit_long,   exit_short],
        ["BUY",     "SELL",    "EXIT_LONG", "EXIT_SHORT"],
        default=None,
    )
    return df


# ══════════════════════════════════════════════════════════════════════════════
# Backtest simulation
# ══════════════════════════════════════════════════════════════════════════════

def run_backtest(df: pd.DataFrame, contract_size: int, interval_minutes: int) -> list[dict]:
    """
    Walk through each candle in df and simulate trades on closed-candle signals.

    Entry (only when flat):
        BUY  signal → go long  at candle close
        SELL signal → go short at candle close

    Exit (evaluated before entry on the same candle):
        Long  position: close on EXIT_LONG (SL) or SELL (reverse to short)
        Short position: close on EXIT_SHORT (SL) or BUY  (reverse to long)

    After closing on a reversal signal the reverse trade is opened immediately
    on the same candle — matching the live system's behaviour.

    Timestamps stored are candle CLOSE times (open + interval), since that is
    when the signal fires and the trade executes in the live system.

    Returns a list of completed trade dicts.
    """
    warm_up   = max(ST1_PERIOD, ST2_PERIOD, MA_LENGTH) + 5
    bar_delta = timedelta(minutes=interval_minutes)

    trades: list[dict] = []
    position    = 0        # 0 = flat, +1 = long, -1 = short
    entry_price = 0.0
    entry_bar   = 0
    entry_time  = None

    for i in range(warm_up, len(df)):
        row    = df.iloc[i]
        signal = row["signal"]
        close  = float(row["close"])
        # Candle close time = open timestamp + one interval
        ts     = df.index[i] + bar_delta

        # Skip candles where indicators have not yet seeded (warm-up NaNs)
        if pd.isna(row.get("st1")) or pd.isna(row.get("ma")):
            continue

        # ── Exit current position ─────────────────────────────────────────────
        if position == 1 and signal in ("EXIT_LONG", "SELL"):
            pnl = (close - entry_price) * contract_size
            trades.append({
                "direction":   "LONG",
                "entry_time":  entry_time,
                "exit_time":   ts,
                "entry_price": round(entry_price, 2),
                "exit_price":  round(close, 2),
                "pnl":         round(pnl, 2),
                "bars_held":   i - entry_bar,
                "exit_reason": "SL" if signal == "EXIT_LONG" else "REVERSE",
                "st1_at_exit": round(float(row["st1"]), 2),
            })
            position = 0

        elif position == -1 and signal in ("EXIT_SHORT", "BUY"):
            pnl = (entry_price - close) * contract_size
            trades.append({
                "direction":   "SHORT",
                "entry_time":  entry_time,
                "exit_time":   ts,
                "entry_price": round(entry_price, 2),
                "exit_price":  round(close, 2),
                "pnl":         round(pnl, 2),
                "bars_held":   i - entry_bar,
                "exit_reason": "SL" if signal == "EXIT_SHORT" else "REVERSE",
                "st1_at_exit": round(float(row["st1"]), 2),
            })
            position = 0

        # ── Enter new position (only when flat) ───────────────────────────────
        if position == 0:
            if signal == "BUY":
                position    = 1
                entry_price = close
                entry_bar   = i
                entry_time  = ts
            elif signal == "SELL":
                position    = -1
                entry_price = close
                entry_bar   = i
                entry_time  = ts

    # Note: any open position at end of data is left open (not force-closed)
    if position != 0:
        logger.info(
            "Open %s position at end of data: entry=%.2f | last close=%.2f",
            "LONG" if position == 1 else "SHORT",
            entry_price,
            float(df.iloc[-1]["close"]),
        )

    return trades


# ══════════════════════════════════════════════════════════════════════════════
# Results display
# ══════════════════════════════════════════════════════════════════════════════

def print_results(
    trades: list[dict],
    instrument_name: str,
    interval: str,
    contract_size: int,
) -> None:
    W = 58

    if not trades:
        print(f"\n{'═' * W}")
        print(f"  BACKTEST  ·  {instrument_name}  [{interval}]")
        print(f"{'═' * W}")
        print("  No completed trades found in the selected period.")
        print(f"{'═' * W}\n")
        return

    df_t = pd.DataFrame(trades)
    df_t["entry_time"] = pd.to_datetime(df_t["entry_time"])
    df_t["exit_time"]  = pd.to_datetime(df_t["exit_time"])

    total  = len(df_t)
    wins   = int((df_t["pnl"] > 0).sum())
    losses = int((df_t["pnl"] <= 0).sum())
    win_rate = wins / total * 100

    gross_pnl   = df_t["pnl"].sum()
    avg_win     = df_t.loc[df_t["pnl"] > 0,  "pnl"].mean() if wins   > 0 else 0.0
    avg_loss    = df_t.loc[df_t["pnl"] <= 0, "pnl"].mean() if losses > 0 else 0.0
    best_trade  = df_t["pnl"].max()
    worst_trade = df_t["pnl"].min()
    avg_bars    = df_t["bars_held"].mean()

    # Long / Short split
    long_df   = df_t[df_t["direction"] == "LONG"]
    short_df  = df_t[df_t["direction"] == "SHORT"]
    long_cnt  = len(long_df)
    short_cnt = len(short_df)
    long_pnl  = long_df["pnl"].sum()
    short_pnl = short_df["pnl"].sum()
    long_wins  = int((long_df["pnl"] > 0).sum())
    short_wins = int((short_df["pnl"] > 0).sum())
    long_wr   = (long_wins  / long_cnt  * 100) if long_cnt  > 0 else 0.0
    short_wr  = (short_wins / short_cnt * 100) if short_cnt > 0 else 0.0
    # Percentage contribution to gross P&L (based on absolute share)
    long_pct  = (long_pnl  / gross_pnl * 100) if gross_pnl != 0 else 0.0
    short_pct = (short_pnl / gross_pnl * 100) if gross_pnl != 0 else 0.0

    # Max drawdown on cumulative P&L curve
    cum_pnl = df_t["pnl"].cumsum()
    peak    = cum_pnl.cummax()
    max_dd  = (cum_pnl - peak).min()

    # Profit factor
    total_profit = df_t.loc[df_t["pnl"] > 0, "pnl"].sum()
    total_loss   = abs(df_t.loc[df_t["pnl"] < 0, "pnl"].sum())
    profit_factor = (total_profit / total_loss) if total_loss > 0 else float("inf")

    # Consecutive wins / losses
    pnl_signs = (df_t["pnl"] > 0).astype(int).tolist()
    max_consec_wins = max(
        (sum(1 for _ in g) for k, g in itertools.groupby(pnl_signs) if k == 1),
        default=0,
    )
    max_consec_loss = max(
        (sum(1 for _ in g) for k, g in itertools.groupby(pnl_signs) if k == 0),
        default=0,
    )

    sep = "─" * W

    print(f"\n{'═' * W}")
    print(f"  BACKTEST  ·  {instrument_name}  [{interval}]")
    print(f"{'═' * W}")
    print(f"  Period          :  {df_t['entry_time'].min().date()}  →  {df_t['exit_time'].max().date()}")
    print(f"  Strategy        :  ST({ST1_PERIOD},{ST1_FACTOR}) + ST({ST2_PERIOD},{ST2_FACTOR}) + MA({MA_LENGTH})")
    print(f"  Contract size   :  {contract_size}")
    print(sep)
    print(f"  Total trades    : {total:>6}")
    print(f"  Long  trades    : {long_cnt:>6}  ({long_wins}W / {long_cnt - long_wins}L)  WR: {long_wr:>5.1f}%")
    print(f"  Short trades    : {short_cnt:>6}  ({short_wins}W / {short_cnt - short_wins}L)  WR: {short_wr:>5.1f}%")
    print(f"  Overall win rate: {win_rate:>5.1f}%  ({wins}W / {losses}L)")
    print(f"  Avg bars held   : {avg_bars:>6.1f}")
    print(sep)
    print(f"  Gross P&L       : {gross_pnl:>+14,.2f}  (100.0%)")
    print(f"  Long  P&L       : {long_pnl:>+14,.2f}  ({long_pct:>+6.1f}%)")
    print(f"  Short P&L       : {short_pnl:>+14,.2f}  ({short_pct:>+6.1f}%)")
    print(sep)
    print(f"  Profit factor   : {profit_factor:>14.2f}")
    print(f"  Avg win         : {avg_win:>+14,.2f}")
    print(f"  Avg loss        : {avg_loss:>+14,.2f}")
    print(f"  Best trade      : {best_trade:>+14,.2f}")
    print(f"  Worst trade     : {worst_trade:>+14,.2f}")
    print(f"  Max drawdown    : {max_dd:>+14,.2f}")
    print(sep)
    print(f"  Max consec wins : {max_consec_wins}")
    print(f"  Max consec loss : {max_consec_loss}")
    print(f"{'═' * W}")

    # ── Exit reason breakdown ──────────────────────────────────────────────────
    print("\n  Exit reason breakdown:")
    by_reason = df_t.groupby("exit_reason")["pnl"].agg(["count", "sum", "mean"])
    for reason, row in by_reason.iterrows():
        print(
            f"    {reason:<12} {int(row['count']):>4} trades  "
            f"P&L: {row['sum']:>+10,.2f}  Avg: {row['mean']:>+8,.2f}"
        )

    # ── Monthly P&L bar chart ──────────────────────────────────────────────────
    df_t["month"] = df_t["exit_time"].dt.to_period("M")
    monthly = df_t.groupby("month")["pnl"].sum()
    max_abs = monthly.abs().max() or 1
    print("\n  Monthly P&L:")
    for month, pnl in monthly.items():
        bar_len = int(abs(pnl) / max_abs * 24)
        bar  = ("█" * bar_len).ljust(24)
        sign = "▲" if pnl >= 0 else "▼"
        print(f"    {month}  {sign}  {pnl:>+10,.2f}  {bar}")

    # ── Last 20 trades table ────────────────────────────────────────────────────
    n_show = min(20, total)
    print(f"\n  Last {n_show} trades:")
    print(
        f"  {'#':<4} {'Dir':<6} {'Entry Time':<20} "
        f"{'Exit Time':<20} {'Entry':>9} {'Exit':>9} "
        f"{'P&L':>10}  Reason"
    )
    print("  " + "─" * 85)
    tail = df_t.tail(n_show).reset_index(drop=True)
    for j, tr in tail.iterrows():
        print(
            f"  {j+1:<4} {tr['direction']:<6} "
            f"{str(tr['entry_time']):<20} {str(tr['exit_time']):<20} "
            f"{tr['entry_price']:>9.2f} {tr['exit_price']:>9.2f} "
            f"{tr['pnl']:>+10,.2f}  {tr['exit_reason']}"
        )
    print()


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__,
    )
    parser.add_argument(
        "--instrument", "-i",
        required=True,
        metavar="INSTRUMENT",
        help=(
            "Any futures instrument name as it appears in the scrip master. "
            "Examples: GOLDM, CRUDE, SILVERM, NIFTY, BANKNIFTY, RELIANCE, "
            "TATAMOTORS, HDFCBANK, INFY, TATASTEEL, MIDCPNIFTY ..."
        ),
    )
    parser.add_argument(
        "--interval", "-t",
        required=True,
        metavar="INTERVAL",
        help=(
            "ONE_MINUTE | THREE_MINUTE | FIVE_MINUTE | TEN_MINUTE | "
            "FIFTEEN_MINUTE | THIRTY_MINUTE | ONE_HOUR | ONE_DAY"
        ),
    )
    parser.add_argument(
        "--from", "-f",
        dest="from_date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Backtest start date (default: 365 days ago)",
    )
    parser.add_argument(
        "--to",
        dest="to_date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Backtest end date (default: today)",
    )
    parser.add_argument(
        "--save-trades",
        action="store_true",
        help="Save full trade log to a CSV file",
    )
    args = parser.parse_args()

    # ── Validate interval ──────────────────────────────────────────────────────
    interval = args.interval.upper()
    if interval not in INTERVAL_MINUTES:
        valid = ", ".join(sorted(INTERVAL_MINUTES.keys()))
        print(f"ERROR: Unknown interval '{args.interval}'.\nValid: {valid}")
        sys.exit(1)

    # ── Date range ─────────────────────────────────────────────────────────────
    to_dt = (
        datetime.strptime(args.to_date, "%Y-%m-%d").replace(hour=23, minute=59)
        if args.to_date
        else datetime.now().replace(hour=23, minute=59, second=0, microsecond=0)
    )
    from_dt = (
        datetime.strptime(args.from_date, "%Y-%m-%d")
        if args.from_date
        else to_dt - timedelta(days=365)
    )

    if from_dt >= to_dt:
        print("ERROR: --from date must be earlier than --to date.")
        sys.exit(1)

    # ── Resolve instrument dynamically from scrip master ───────────────────────
    inst_key = args.instrument.upper()
    try:
        contract, contract_size = resolve_instrument_dynamic(inst_key)
    except ValueError as exc:
        print(f"\nERROR: {exc}")
        sys.exit(1)

    instrument = {
        "name":           contract["name"],
        "angel_token":    contract["angel_token"],
        "angel_exchange": contract["angel_exchange"],
    }

    logger.info(
        "Resolved  : %s  |  exchange=%s  |  expiry=%s  |  lot_size=%s  |  contract_size=%d",
        contract["kite_tradingsymbol"],
        contract["exchange"],
        contract["expiry"],
        contract["lot_size"],
        contract_size,
    )
    logger.info("Interval  : %s", interval)

    # ── Fetch historical candles ───────────────────────────────────────────────
    logger.info("Fetching candles: %s → %s ...", from_dt.date(), to_dt.date())
    df = fetch_history(instrument, interval, from_dt, to_dt)

    min_required = MA_LENGTH + max(ST1_PERIOD, ST2_PERIOD) + 20
    if len(df) < min_required:
        print(
            f"ERROR: Only {len(df)} candles available — need at least {min_required} "
            "for indicator warm-up. Widen the date range or use a shorter interval."
        )
        sys.exit(1)

    # ── Compute indicators & signals ───────────────────────────────────────────
    logger.info(
        "Computing indicators: ST(%d,%.1f)  ST(%d,%.1f)  MA(%d) ...",
        ST1_PERIOD, ST1_FACTOR,
        ST2_PERIOD, ST2_FACTOR,
        MA_LENGTH,
    )
    df = compute_signals(df)

    dist = df["signal"].value_counts(dropna=True).to_dict()
    logger.info("Signal distribution: %s", dist)

    # ── Run simulation ─────────────────────────────────────────────────────────
    logger.info("Simulating trades on %d candles ...", len(df))
    trades = run_backtest(df, contract_size, INTERVAL_MINUTES[interval])
    logger.info("Simulation complete — %d completed trade(s).", len(trades))

    # ── Print results ──────────────────────────────────────────────────────────
    print_results(trades, contract["kite_tradingsymbol"], interval, contract_size)

    # ── Optionally save trade log ──────────────────────────────────────────────
    if args.save_trades:
        if trades:
            fname = (
                f"backtest_{inst_key}_{interval}_"
                f"{from_dt.date()}_{to_dt.date()}.csv"
            )
            pd.DataFrame(trades).to_csv(fname, index=False)
            print(f"  Trade log saved to: {fname}\n")
        else:
            print("  No trades to save.\n")


if __name__ == "__main__":
    main()
