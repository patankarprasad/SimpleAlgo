"""
Standalone debug script: fetch candles for a scrip and print Supertrend + MA values.

Usage:
    python debug_indicators.py GOLDM
    python debug_indicators.py NIFTY --exchange NFO
    python debug_indicators.py CRUDEOIL --timeframe ONE_HOUR --tail 30
    python debug_indicators.py RELIANCE --exchange NFO --n-candles 100

The script reuses the same Angel login, scrip-master resolution, candle fetch,
and indicator logic as the main program so any discrepancy will show up here.
"""
import argparse
import logging
import os
import sys

# ── Logging (simple console output, no file) ──────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,          # suppress internal INFO noise by default
    format="%(levelname)s  %(name)s  %(message)s",
)
# Keep angel_login / angel_data quiet unless -v is requested
_VERBOSE = "--verbose" in sys.argv or "-v" in sys.argv
if _VERBOSE:
    logging.getLogger().setLevel(logging.DEBUG)

import pandas as pd

# ── Import project modules (script must be run from the SimpleAlgo directory) ─
try:
    import config                  # loads .env, sets ST1_PERIOD etc.
    from angel_login import get_angel_session
    import scrip_master
    from angel_data import get_candles, INTERVAL_MAP
    from indicators import supertrend, sma, compute_signals
except ImportError as e:
    sys.exit(
        f"[ERROR] Could not import project module: {e}\n"
        "Make sure you run this script from the SimpleAlgo directory:\n"
        "  cd D:\\Prasad\\Trade\\SimpleAlgo && python debug_indicators.py GOLDM"
    )

# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Debug: print candles + Supertrend + MA for a scrip",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("scrip", help="Underlying name, e.g. GOLDM, NIFTY, RELIANCE")
    p.add_argument(
        "--exchange", "-e",
        default=None,
        help="Exchange: MCX or NFO. Auto-detected from config if omitted.",
    )
    p.add_argument(
        "--timeframe", "-t",
        default=None,
        help="Angel interval string (e.g. FIFTEEN_MINUTE, ONE_HOUR). "
             "Defaults to instrument's configured timeframe.",
    )
    p.add_argument(
        "--n-candles", "-n",
        type=int,
        default=config.CANDLE_LOOKBACK,
        help=f"Number of candles to fetch (default: {config.CANDLE_LOOKBACK})",
    )
    p.add_argument(
        "--tail",
        type=int,
        default=None,
        help="Print only the last N rows of candles (default: print all)",
    )
    p.add_argument(
        "--st1-period",  type=int,   default=config.ST1_PERIOD,
        help=f"ST1 period (default: {config.ST1_PERIOD})",
    )
    p.add_argument(
        "--st1-factor",  type=float, default=config.ST1_FACTOR,
        help=f"ST1 multiplier (default: {config.ST1_FACTOR})",
    )
    p.add_argument(
        "--st2-period",  type=int,   default=config.ST2_PERIOD,
        help=f"ST2 period (default: {config.ST2_PERIOD})",
    )
    p.add_argument(
        "--st2-factor",  type=float, default=config.ST2_FACTOR,
        help=f"ST2 multiplier (default: {config.ST2_FACTOR})",
    )
    p.add_argument(
        "--ma-length",   type=int,   default=config.MA_LENGTH,
        help=f"MA period (default: {config.MA_LENGTH})",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed Angel API logs",
    )
    return p.parse_args()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_exchange(scrip: str) -> str:
    """Guess exchange from config; fall back to NFO."""
    name_upper = scrip.upper()
    all_instruments = config.INSTRUMENTS + config.HOURLY_INSTRUMENTS + config.STOCK_INSTRUMENTS
    for inst in all_instruments:
        if inst["name"].upper() == name_upper or inst.get("underlying", "").upper() == name_upper:
            return inst["exchange"]
    # MCX commodities are typically 4-6 chars and don't end with familiar NFO names
    nfo_names = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"}
    return "MCX" if name_upper not in nfo_names else "NFO"


def _find_timeframe(scrip: str) -> str:
    """Find configured timeframe for scrip, default FIFTEEN_MINUTE."""
    name_upper = scrip.upper()
    all_instruments = config.INSTRUMENTS + config.HOURLY_INSTRUMENTS
    for inst in all_instruments:
        if inst["name"].upper() == name_upper:
            return inst["timeframe"]
    return "FIFTEEN_MINUTE"


def _resolve(scrip: str, exchange: str, timeframe: str) -> dict:
    """Resolve the scrip to a full instrument dict via scrip_master."""
    print(f"\n[1/4] Refreshing scrip master for {scrip} on {exchange} ...")
    scrip_master.refresh_masters()

    try:
        contract = scrip_master.get_nearest_future(scrip, exchange)
    except ValueError as e:
        sys.exit(f"[ERROR] {e}")

    # Merge with minimal instrument structure so get_candles() is happy
    instrument = {
        "name":            scrip.upper(),
        "exchange":        exchange,
        "angel_exchange":  contract["angel_exchange"],
        "angel_token":     contract["angel_token"],
        "timeframe":       timeframe,
    }
    return instrument, contract


def _print_separator(char="─", width=120):
    print(char * width)


def _print_instrument_info(scrip, exchange, contract, timeframe, args):
    _print_separator("═")
    print(f"  INSTRUMENT DEBUG  —  {scrip.upper()} / {exchange}")
    _print_separator("═")
    print(f"  Angel token     : {contract['angel_token']}")
    print(f"  Angel symbol    : {contract['angel_symbol']}")
    print(f"  Angel exchange  : {contract['angel_exchange']}")
    print(f"  Kite symbol     : {contract['kite_tradingsymbol']}")
    print(f"  Expiry          : {contract['expiry']}")
    print(f"  Lot size        : {contract['lot_size']}")
    print(f"  Timeframe       : {timeframe}")
    print()
    print(f"  ST1  : period={args.st1_period}  factor={args.st1_factor}")
    print(f"  ST2  : period={args.st2_period}  factor={args.st2_factor}")
    print(f"  MA   : period={args.ma_length}")
    _print_separator()


def _print_candles(df: pd.DataFrame, tail: int | None):
    """Print the OHLCV + indicator table."""
    display = df.tail(tail) if tail else df

    # Determine column widths
    col_fmt = {
        "open":   ">10.2f",
        "high":   ">10.2f",
        "low":    ">10.2f",
        "close":  ">10.2f",
        "volume": ">12.0f",
        "st1":    ">10.2f",
        "st2":    ">10.2f",
        "ma":     ">10.2f",
        "signal": "<12",
    }
    headers = ["datetime", "open", "high", "low", "close", "volume", "st1", "st2", "ma", "signal"]

    header_line = (
        f"{'datetime':<20}"
        f"{'open':>10}  {'high':>10}  {'low':>10}  {'close':>10}  "
        f"{'volume':>12}  {'st1':>10}  {'st2':>10}  {'ma':>10}  {'signal':<12}"
    )
    print(header_line)
    _print_separator("-", len(header_line))

    for ts, row in display.iterrows():
        def _fmt(col, fmt):
            v = row.get(col)
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return f"{'—':>{fmt.strip('<>').rstrip('f.0123456789')}}"
            try:
                return format(v, fmt.strip("<>"))
            except Exception:
                return str(v)

        signal = row.get("signal") or ""
        if signal == "BUY":
            signal_str = f"\033[92m{'BUY':<12}\033[0m"      # green
        elif signal == "SELL":
            signal_str = f"\033[91m{'SELL':<12}\033[0m"     # red
        elif signal == "EXIT_LONG":
            signal_str = f"\033[93m{'EXIT_LONG':<12}\033[0m"   # yellow
        elif signal == "EXIT_SHORT":
            signal_str = f"\033[93m{'EXIT_SHORT':<12}\033[0m"
        else:
            signal_str = f"{'':<12}"

        # Colour last close vs ST1 to show trend direction
        close_val  = row.get("close", float("nan"))
        st1_val    = row.get("st1",   float("nan"))
        if not pd.isna(close_val) and not pd.isna(st1_val):
            if close_val > st1_val:
                close_str = f"\033[92m{close_val:>10.2f}\033[0m"   # green = above ST1
            else:
                close_str = f"\033[91m{close_val:>10.2f}\033[0m"   # red   = below ST1
        else:
            close_str = f"{close_val:>10.2f}" if not pd.isna(close_val) else f"{'—':>10}"

        def fv(col, w=10, dp=2):
            v = row.get(col)
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return f"{'—':>{w}}"
            return f"{v:>{w}.{dp}f}"

        print(
            f"{str(ts):<20}"
            f"{fv('open'):>10}  {fv('high'):>10}  {fv('low'):>10}  {close_str}  "
            f"{fv('volume', 12, 0):>12}  {fv('st1'):>10}  {fv('st2'):>10}  {fv('ma'):>10}  "
            f"{signal_str}"
        )


def _print_last_candle_summary(df: pd.DataFrame):
    """Print a concise summary of the last closed candle."""
    last_ts  = df.index[-1]
    row      = df.iloc[-1]
    prev_row = df.iloc[-2] if len(df) > 1 else None

    _print_separator()
    print(f"\n  LAST CLOSED CANDLE  —  {last_ts}\n")

    def val(col):
        v = row.get(col)
        return f"{v:.4f}" if v is not None and not (isinstance(v, float) and pd.isna(v)) else "—"

    def arrow(col):
        if prev_row is None:
            return ""
        curr = row.get(col)
        prev = prev_row.get(col)
        if curr is None or prev is None:
            return ""
        if pd.isna(curr) or pd.isna(prev):
            return ""
        return " ▲" if curr > prev else (" ▼" if curr < prev else " ─")

    close = row.get("close")
    st1   = row.get("st1")
    st2   = row.get("st2")
    ma    = row.get("ma")

    print(f"  {'Close':<12}: {val('close')}{arrow('close')}")
    print(f"  {'ST1':<12}: {val('st1')}{arrow('st1')}    ({'above' if not pd.isna(close) and not pd.isna(st1) and close > st1 else 'below'})")
    print(f"  {'ST2':<12}: {val('st2')}{arrow('st2')}    ({'above' if not pd.isna(close) and not pd.isna(st2) and close > st2 else 'below'})")
    print(f"  {'MA':<12}: {val('ma')}{arrow('ma')}    ({'above' if not pd.isna(close) and not pd.isna(ma) and close > ma else 'below'})")
    print()
    signal = row.get("signal") or "—"
    print(f"  {'Signal':<12}: {signal}")
    print()
    _print_separator("═")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    scrip    = args.scrip.upper()
    exchange = (args.exchange or _find_exchange(scrip)).upper()
    timeframe = INTERVAL_MAP.get(
        args.timeframe or _find_timeframe(scrip),
        args.timeframe or _find_timeframe(scrip),
    )

    # Step 1: resolve instrument
    instrument, contract = _resolve(scrip, exchange, timeframe)
    _print_instrument_info(scrip, exchange, contract, timeframe, args)

    # Step 2: Angel login
    print(f"[2/4] Logging in to Angel SmartAPI ...")
    try:
        get_angel_session()
        print("       Login OK.\n")
    except Exception as e:
        sys.exit(f"[ERROR] Angel login failed: {e}")

    # Step 3: fetch candles
    print(f"[3/4] Fetching {args.n_candles} candles ({timeframe}) ...")
    try:
        df = get_candles(instrument, n_candles=args.n_candles, interval=timeframe)
    except Exception as e:
        sys.exit(f"[ERROR] Candle fetch failed: {e}")
    print(f"       Got {len(df)} candles.  "
          f"Range: {df.index[0].strftime('%Y-%m-%d %H:%M')} → {df.index[-1].strftime('%Y-%m-%d %H:%M')}\n")

    # Step 4: compute indicators
    print(f"[4/4] Computing Supertrend & MA ...")
    df = compute_signals(
        df,
        st1_period=args.st1_period,
        st1_factor=args.st1_factor,
        st2_period=args.st2_period,
        st2_factor=args.st2_factor,
        ma_length=args.ma_length,
    )
    print("       Done.\n")

    # Print candle table
    tail_label = f" (last {args.tail})" if args.tail else f" (all {len(df)})"
    print(f"  CANDLES{tail_label}  —  green close = above ST1, red = below ST1\n")
    _print_candles(df, args.tail)

    # Print last-candle summary
    _print_last_candle_summary(df)


if __name__ == "__main__":
    main()
