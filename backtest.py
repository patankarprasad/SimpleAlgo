"""
backtest.py — Backtest SimpleAlgo strategies using Angel API historical data.

Fetches 6 months of OHLCV data (stitching multiple contract expiries),
runs the same Supertrend + MA strategy, and prints a full performance report.

Usage:
    python backtest.py                               # all 15-min instruments, 6 months
    python backtest.py --months 3                    # 3 months
    python backtest.py --instruments GOLDM,NIFTY     # specific instruments
    python backtest.py --hourly                      # include hourly instruments too
    python backtest.py --all                         # 15-min + hourly instruments

Notes:
  - NIFTY/BANKNIFTY (SYNTHETIC mode): futures prices are used for both signal
    generation and P&L. This approximates the real options-based P&L for an
    ATM synthetic future.
  - Entry/exit price = close of the signal bar (market order at close).
  - Trade logic mirrors the live system exactly:
      flat   + BUY  → enter long
      flat   + SELL → enter short  (unless long_only)
      long   + EXIT_LONG  → close long
      short  + EXIT_SHORT → close short
  - Day-boundary gate: mirrors main.py's scheduler exactly. The live tick that
    would evaluate a trading day's FINAL candle fires 1s after that candle
    closes, which is already past trade_end (market closed) — so live never
    acts on it; the position just carries into the next trading day, which is
    evaluated fresh on its own first candle. This backtest skips signals on
    the last candle of each day for the same reason, instead of (incorrectly)
    filling them at that candle's close as if the market were still open.
"""

import argparse
import logging
import sys
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

import config
from angel_login import force_relogin, get_angel_session
from indicators import compute_signals
import scrip_master as sm

# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("backtest")

# Suppress noisy sub-module loggers
for _mod in ("angel_login", "scrip_master", "urllib3", "requests"):
    logging.getLogger(_mod).setLevel(logging.WARNING)


# ══════════════════════════════════════════════════════════════════════════════
# Data fetching
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_candles_range(angel_token: str, angel_exchange: str, interval: str,
                         from_dt: datetime, to_dt: datetime, label: str = "") -> pd.DataFrame:
    """Fetch OHLCV candles for a specific token and date range from Angel API."""
    angel = get_angel_session()
    params = {
        "exchange":    angel_exchange,
        "symboltoken": angel_token,
        "interval":    interval,
        "fromdate":    from_dt.strftime("%Y-%m-%d %H:%M"),
        "todate":      to_dt.strftime("%Y-%m-%d %H:%M"),
    }
    logger.info("  Fetching %-30s | %s -> %s", label, from_dt.strftime("%Y-%m-%d"), to_dt.strftime("%Y-%m-%d"))

    time.sleep(config.ANGEL_BASE_DELAY)
    resp = angel.getCandleData(params)

    if not resp.get("status") or not resp.get("data"):
        logger.warning("  Bad response for %s — retrying after re-login …", label)
        angel = force_relogin()
        time.sleep(config.ANGEL_BASE_DELAY)
        resp = angel.getCandleData(params)
        if not resp.get("status") or not resp.get("data"):
            logger.error("  Still no data for %s after re-login. Skipping.", label)
            return pd.DataFrame()

    df = pd.DataFrame(resp["data"], columns=["datetime", "open", "high", "low", "close", "volume"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    if df["datetime"].dt.tz is not None:
        df["datetime"] = df["datetime"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    df = df.set_index("datetime").sort_index()
    df = df.astype({c: float for c in ["open", "high", "low", "close", "volume"]})
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df


def _get_all_contracts(name: str, exchange: str) -> list[dict]:
    """
    Return ALL futures contracts for this instrument from the scrip master,
    including expired ones, sorted by expiry (oldest first).
    """
    merged = sm._get_merged()
    mask = (
        (merged["name"].str.upper() == name.upper()) &
        (merged["exchange"] == exchange.upper())
    )
    subset = merged[mask].sort_values("expiry_date")
    return [sm._row_to_dict(r) for _, r in subset.iterrows()]


def fetch_historical_data(inst_cfg: dict, from_date: date, to_date: date) -> pd.DataFrame:
    """
    Fetch and stitch OHLCV data for an instrument, handling contract rollovers.

    Finds all relevant futures contracts covering the date range, fetches
    each in order, and concatenates into a single DataFrame. Each row gets a
    `contract_expiry` column to detect rollover points during simulation.
    """
    # For hourly variants, the underlying name is used for contract lookup
    base_name = inst_cfg.get("underlying", inst_cfg["name"])
    exchange  = inst_cfg["exchange"]
    interval  = inst_cfg["timeframe"]

    all_contracts = _get_all_contracts(base_name, exchange)
    if not all_contracts:
        raise ValueError(f"No contracts found for {base_name} on {exchange} in scrip master.")

    # Keep contracts that expire on or after from_date (they were active during our window)
    relevant = [c for c in all_contracts if c["expiry"] >= from_date]
    if not relevant:
        raise ValueError(
            f"No contracts covering {from_date} found for {base_name}. "
            f"Earliest available expiry: {all_contracts[-1]['expiry']}"
        )

    logger.info("  %s: %d contract(s) cover the backtest window", base_name, len(relevant))

    frames = []
    for i, contract in enumerate(relevant):
        # Data start: beginning of backtest (first contract) or day after previous expiry
        if i == 0:
            fetch_from = datetime.combine(from_date, datetime.min.time())
        else:
            fetch_from = datetime.combine(relevant[i - 1]["expiry"] + timedelta(days=1),
                                          datetime.min.time())

        # Data end: contract expiry or backtest end (whichever is sooner)
        fetch_to = datetime.combine(
            min(contract["expiry"], to_date),
            datetime.strptime("23:59", "%H:%M").time()
        )

        if fetch_from > fetch_to:
            continue

        label = f"{base_name} (exp {contract['expiry']})"
        df_c = _fetch_candles_range(
            angel_token=contract["angel_token"],
            angel_exchange=contract["angel_exchange"],
            interval=interval,
            from_dt=fetch_from,
            to_dt=fetch_to,
            label=label,
        )

        if not df_c.empty:
            df_c["contract_expiry"] = contract["expiry"]
            df_c["lot_size"]        = contract["lot_size"]
            frames.append(df_c)

        if contract["expiry"] >= to_date:
            break

    if not frames:
        raise ValueError(f"No candle data returned for {base_name} — check Angel login and market hours.")

    full_df = pd.concat(frames).sort_index()
    full_df  = full_df[~full_df.index.duplicated(keep="first")]

    logger.info(
        "  %s: %d candles total  (%s -> %s)",
        base_name, len(full_df),
        full_df.index[0].strftime("%Y-%m-%d"),
        full_df.index[-1].strftime("%Y-%m-%d"),
    )
    return full_df


# ══════════════════════════════════════════════════════════════════════════════
# Strategy simulation
# ══════════════════════════════════════════════════════════════════════════════

def _pnl_multiplier(inst_cfg: dict) -> float:
    """
    Return the INR P&L per 1-point move in the instrument price.

    MCX  → contract_size from config (e.g. GOLDM=10 grams/lot, CRUDEOIL=100 bbl/lot)
    NFO  → lot_size from scrip master (resolved at data-fetch time)
    """
    if inst_cfg["exchange"] == "MCX":
        return inst_cfg.get("contract_size", 1) * inst_cfg.get("qty", 1)
    # NFO: lot_size stored per-row; caller passes it explicitly
    return inst_cfg.get("_lot_size", 1) * inst_cfg.get("qty", 1)


def simulate_strategy(inst_cfg: dict, df: pd.DataFrame) -> list[dict]:
    """
    Walk through every bar and simulate the strategy in live-system order:

        flat   + BUY        → enter long
        flat   + SELL       → enter short  (skipped if long_only=True)
        long   + EXIT_LONG  → close long
        short  + EXIT_SHORT → close short

    On contract rollover (contract_expiry changes), any open position is
    force-closed at that bar's close price and re-opened if signal warrants.

    Returns a list of trade dicts.
    """
    long_only   = inst_cfg.get("long_only", False)
    trade_start = datetime.strptime(inst_cfg["trade_start"], "%H:%M").time()
    trade_end   = datetime.strptime(inst_cfg["trade_end"],   "%H:%M").time()

    # For NFO: grab lot_size from the first data row (scrip master value)
    if inst_cfg["exchange"] == "NFO" and "lot_size" in df.columns:
        inst_cfg = {**inst_cfg, "_lot_size": int(df["lot_size"].iloc[0])}

    # Compute signals on full history
    df_s = compute_signals(
        df,
        config.ST1_PERIOD, config.ST1_FACTOR,
        config.ST2_PERIOD, config.ST2_FACTOR,
        config.MA_LENGTH,
    )

    mult     = _pnl_multiplier(inst_cfg)
    position = 0      # 0=flat, +1=long, -1=short
    entry_px = 0.0
    entry_ts = None
    entry_exp = None
    trades   = []

    for idx, (ts, row) in enumerate(df_s.iterrows()):
        # Skip warmup: need at least MA_LENGTH bars for valid signals
        if idx < config.MA_LENGTH:
            continue

        signal  = row.get("signal")
        close   = float(row["close"])
        exp     = row.get("contract_expiry")
        in_hrs  = trade_start <= ts.time() <= trade_end

        # ── Contract rollover: force-close and re-evaluate ─────────────────
        if position != 0 and entry_exp is not None and exp != entry_exp:
            pnl = (close - entry_px) * position * mult
            trades.append(_make_trade(inst_cfg["name"], position, entry_ts, entry_px,
                                      ts, close, pnl, "ROLLOVER", entry_exp))
            position = 0
            entry_px = 0.0
            entry_ts = None
            entry_exp = None

        # ── Day-boundary gate ────────────────────────────────────────────────
        # ts is the candle's OPEN time. If the next candle falls on a different
        # calendar date (or this is the last row in the data), this bar is the
        # final candle of its trading day. Live's scheduler tick for this bar
        # fires 1s after it closes — already past trade_end — so live never
        # evaluates it; the position just carries into tomorrow's first candle.
        # Skip signal processing here so the backtest doesn't fill a trade at
        # a price the live system could never actually have gotten.
        is_last_of_day = (
            idx == len(df_s) - 1
            or df_s.index[idx + 1].date() != ts.date()
        )
        if is_last_of_day:
            continue

        # ── Signal processing ──────────────────────────────────────────────
        if position == 0:
            if in_hrs and signal == "BUY":
                position  = +1
                entry_px  = close
                entry_ts  = ts
                entry_exp = exp
            elif in_hrs and signal == "SELL" and not long_only:
                position  = -1
                entry_px  = close
                entry_ts  = ts
                entry_exp = exp

        elif position > 0:
            if signal == "EXIT_LONG":
                pnl = (close - entry_px) * mult
                trades.append(_make_trade(inst_cfg["name"], +1, entry_ts, entry_px,
                                          ts, close, pnl, signal, entry_exp))
                position = 0
                entry_px = 0.0
                entry_ts = None
                entry_exp = None

        else:  # position < 0
            if signal == "EXIT_SHORT":
                pnl = (entry_px - close) * mult
                trades.append(_make_trade(inst_cfg["name"], -1, entry_ts, entry_px,
                                          ts, close, pnl, signal, entry_exp))
                position = 0
                entry_px = 0.0
                entry_ts = None
                entry_exp = None

    # ── Close any position open at end of data ─────────────────────────────
    if position != 0:
        ts    = df_s.index[-1]
        close = float(df_s.iloc[-1]["close"])
        exp   = df_s.iloc[-1].get("contract_expiry")
        pnl   = (close - entry_px) * position * mult
        trades.append(_make_trade(inst_cfg["name"], position, entry_ts, entry_px,
                                  ts, close, pnl, "END_OF_DATA", entry_exp))

    return trades


def _make_trade(name, direction, entry_ts, entry_px, exit_ts, exit_px, pnl, reason, contract) -> dict:
    return {
        "instrument":  name,
        "direction":   "LONG" if direction > 0 else "SHORT",
        "entry_time":  entry_ts,
        "entry_price": round(entry_px, 2),
        "exit_time":   exit_ts,
        "exit_price":  round(exit_px, 2),
        "pnl":         round(pnl, 2),
        "exit_reason": reason,
        "contract":    str(contract),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Performance statistics
# ══════════════════════════════════════════════════════════════════════════════

def compute_stats(trades: list[dict]) -> dict:
    if not trades:
        return {k: 0 for k in ("total_trades","wins","losses","win_rate","total_pnl",
                                "avg_pnl","avg_win","avg_loss","max_win","max_loss",
                                "max_drawdown","profit_factor","sharpe")}

    pnls   = np.array([t["pnl"] for t in trades])
    wins   = pnls[pnls > 0]
    losses = pnls[pnls <= 0]

    cumulative = np.cumsum(pnls)
    running_max = np.maximum.accumulate(cumulative)
    max_drawdown = float((cumulative - running_max).min())

    profit_factor = (float(wins.sum()) / abs(float(losses.sum()))
                     if len(losses) > 0 and losses.sum() != 0 else float("inf"))

    sharpe = float(pnls.mean() / pnls.std() * np.sqrt(252)) if pnls.std() > 0 else 0.0

    return {
        "total_trades":  int(len(pnls)),
        "wins":          int(len(wins)),
        "losses":        int(len(losses)),
        "win_rate":      round(len(wins) / len(pnls) * 100, 1),
        "total_pnl":     round(float(pnls.sum()), 2),
        "avg_pnl":       round(float(pnls.mean()), 2),
        "avg_win":       round(float(wins.mean()),   2) if len(wins)   > 0 else 0,
        "avg_loss":      round(float(losses.mean()), 2) if len(losses) > 0 else 0,
        "max_win":       round(float(pnls.max()), 2),
        "max_loss":      round(float(pnls.min()), 2),
        "max_drawdown":  round(max_drawdown, 2),
        "profit_factor": round(profit_factor, 2),
        "sharpe":        round(sharpe, 2),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Report printing
# ══════════════════════════════════════════════════════════════════════════════

def _w(label, value, width=28):
    return f"  {label:<{width}}: {value}"


def print_report(results: dict, from_date: date, to_date: date):
    WIDE = "=" * 110
    THIN = "-" * 110

    print(f"\n{WIDE}")
    print(f"  BACKTEST REPORT   |   Period: {from_date}  →  {to_date}")
    print(f"  Strategy: Supertrend (ST1={config.ST1_PERIOD}/{config.ST1_FACTOR}, "
          f"ST2={config.ST2_PERIOD}/{config.ST2_FACTOR}) + SMA({config.MA_LENGTH})")
    print(WIDE)

    summary_rows = []

    for inst_name, data in results.items():
        trades = data["trades"]
        stats  = data["stats"]
        err    = data.get("error")

        print(f"\n{'─'*50}  {inst_name}  {'─'*50}")

        if err:
            print(f"  ERROR: {err}\n")
            continue

        if not trades:
            print("  No trades generated in this period.\n")
            continue

        # ── Trade log ─────────────────────────────────────────────────────
        print("\n  TRADE LOG")
        print(f"  {'#':>3}  {'Dir':6}  {'Entry Time':17}  {'Entry':>10}  "
              f"{'Exit Time':17}  {'Exit':>10}  {'P&L (₹)':>12}  {'Reason':15}  Contract")
        print(f"  {'─'*3}  {'─'*6}  {'─'*17}  {'─'*10}  {'─'*17}  {'─'*10}  {'─'*12}  {'─'*15}  {'─'*12}")

        for n, t in enumerate(trades, 1):
            pnl_str = f"₹{t['pnl']:>+,.2f}"
            print(
                f"  {n:>3}  {t['direction']:6}  "
                f"{t['entry_time'].strftime('%Y-%m-%d %H:%M'):17}  "
                f"{t['entry_price']:>10.2f}  "
                f"{t['exit_time'].strftime('%Y-%m-%d %H:%M'):17}  "
                f"{t['exit_price']:>10.2f}  "
                f"{pnl_str:>12}  "
                f"{t['exit_reason']:15}  "
                f"{t['contract']}"
            )

        # ── Per-instrument stats ───────────────────────────────────────────
        s = stats
        print(f"\n  PERFORMANCE STATS")
        print(f"  {THIN[2:]}")
        print(_w("Total Trades",    s["total_trades"]))
        print(_w("Wins / Losses",   f"{s['wins']} / {s['losses']}"))
        print(_w("Win Rate",        f"{s['win_rate']}%"))
        print(_w("Total P&L",       f"₹{s['total_pnl']:>+,.2f}"))
        print(_w("Avg P&L / Trade", f"₹{s['avg_pnl']:>+,.2f}"))
        print(_w("Avg Win",         f"₹{s['avg_win']:>+,.2f}"))
        print(_w("Avg Loss",        f"₹{s['avg_loss']:>+,.2f}"))
        print(_w("Best Trade",      f"₹{s['max_win']:>+,.2f}"))
        print(_w("Worst Trade",     f"₹{s['max_loss']:>+,.2f}"))
        print(_w("Max Drawdown",    f"₹{s['max_drawdown']:>+,.2f}"))
        print(_w("Profit Factor",   s["profit_factor"]))
        print(_w("Sharpe Ratio",    s["sharpe"]))

        summary_rows.append({
            "Instrument":    inst_name,
            "Trades":        s["total_trades"],
            "Win%":          f"{s['win_rate']}%",
            "Total P&L":     f"₹{s['total_pnl']:>+,.2f}",
            "Avg/Trade":     f"₹{s['avg_pnl']:>+,.2f}",
            "Max DD":        f"₹{s['max_drawdown']:>+,.2f}",
            "Profit Factor": s["profit_factor"],
            "Sharpe":        s["sharpe"],
        })

    # ── Overall summary table ──────────────────────────────────────────────
    print(f"\n{WIDE}")
    print("  SUMMARY")
    print(WIDE)

    if summary_rows:
        sdf = pd.DataFrame(summary_rows)
        print(sdf.to_string(index=False))

    overall_pnl    = sum(d["stats"]["total_pnl"]    for d in results.values())
    overall_trades = sum(d["stats"]["total_trades"] for d in results.values())
    print(f"\n  {'Overall Instruments':25s}: {len(results)}")
    print(f"  {'Total Trades':25s}: {overall_trades}")
    print(f"  {'Combined Net P&L':25s}: ₹{overall_pnl:>+,.2f}")
    print(f"\n{WIDE}\n")


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Backtest SimpleAlgo strategies via Angel API")
    parser.add_argument("--months",      type=int,   default=6,  help="Lookback in months (default: 6)")
    parser.add_argument("--instruments", type=str,   default="", help="Comma-separated names e.g. GOLDM,NIFTY")
    parser.add_argument("--hourly",      action="store_true",    help="Include hourly instruments")
    parser.add_argument("--all",         action="store_true",    help="Include both 15-min and hourly instruments")
    parser.add_argument("--no-save",     action="store_true",    help="Skip saving results to CSV")
    args = parser.parse_args()

    to_date   = date.today()
    from_date = (datetime.now() - timedelta(days=args.months * 30)).date()

    # Build instrument list
    instruments = list(config.INSTRUMENTS)
    if args.all or args.hourly:
        instruments += config.HOURLY_INSTRUMENTS

    if args.instruments:
        names = {n.strip().upper() for n in args.instruments.split(",")}
        instruments = [i for i in instruments if i["name"].upper() in names]

    if not instruments:
        logger.error("No instruments selected. Check --instruments filter.")
        sys.exit(1)

    logger.info("═" * 60)
    logger.info("Backtesting %d instrument(s) | %s → %s", len(instruments), from_date, to_date)
    logger.info("═" * 60)

    # Refresh scrip master once
    sm.refresh_masters()

    results = {}
    for inst_cfg in instruments:
        name = inst_cfg["name"]
        logger.info("── %s ─────────────────────────────────", name)
        try:
            df     = fetch_historical_data(inst_cfg, from_date, to_date)
            trades = simulate_strategy(inst_cfg, df)
            stats  = compute_stats(trades)
            results[name] = {"trades": trades, "stats": stats}
            logger.info(
                "  %s: %d trade(s) | P&L = ₹%.2f",
                name, stats["total_trades"], stats["total_pnl"],
            )
        except Exception as exc:
            logger.error("  %s failed: %s", name, exc, exc_info=False)
            results[name] = {
                "trades": [],
                "stats":  compute_stats([]),
                "error":  str(exc),
            }

    # Print full report
    print_report(results, from_date, to_date)

    # Save trade log to CSV
    if not args.no_save:
        all_trades = [t for d in results.values() for t in d["trades"]]
        if all_trades:
            out_path = f"backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            pd.DataFrame(all_trades).to_csv(out_path, index=False)
            logger.info("Trade log saved → %s", out_path)


if __name__ == "__main__":
    main()
