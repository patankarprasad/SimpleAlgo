"""
backtest_2ind.py — Backtest with only 2 indicators: Supertrend(10,2) + MA(50).

Strategy vs backtest.py:
  - Removes ST2 (10,3) from entry conditions
  - Entry : BUY  when Close > ST(10,2) AND Close > MA(50)
            SELL when Close < ST(10,2) AND Close < MA(50)
  - Stoploss / Exit: Supertrend(10,2) — same as entry indicator

Usage:
    python backtest_2ind.py                               # all 15-min instruments, 6 months
    python backtest_2ind.py --months 3                    # 3 months
    python backtest_2ind.py --instruments GOLDM,NIFTY     # specific instruments
    python backtest_2ind.py --hourly                      # include hourly instruments too
    python backtest_2ind.py --all                         # 15-min + hourly instruments
    python backtest_2ind.py --no-save                     # skip CSV export
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
from indicators import supertrend, sma          # reuse existing indicator math
import scrip_master as sm

# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("backtest_2ind")

for _mod in ("angel_login", "scrip_master", "urllib3", "requests"):
    logging.getLogger(_mod).setLevel(logging.WARNING)

ST_PERIOD  = 10
ST_FACTOR  = 2.0
MA_LENGTH  = 50


# ══════════════════════════════════════════════════════════════════════════════
# 2-indicator signal computation  (ST 10/2 + MA 50 only)
# ══════════════════════════════════════════════════════════════════════════════

def compute_signals_2ind(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute signals using only Supertrend(10,2) and SMA(50).

    signal values:
      'BUY'        — close > ST and close > MA
      'SELL'       — close < ST and close < MA
      'EXIT_LONG'  — close < ST
      'EXIT_SHORT' — close > ST
    """
    df = df.copy()
    df["st1"] = supertrend(df, ST_PERIOD, ST_FACTOR)
    df["ma"]  = sma(df["close"], MA_LENGTH)

    c   = df["close"]
    st1 = df["st1"]
    ma  = df["ma"]

    buy_cond   = (c > st1) & (c > ma)
    sell_cond  = (c < st1) & (c < ma)
    exit_long  = c < st1
    exit_short = c > st1

    conditions = [buy_cond, sell_cond, exit_long, exit_short]
    choices    = ["BUY",    "SELL",    "EXIT_LONG", "EXIT_SHORT"]
    df["signal"] = np.select(conditions, choices, default=None)

    return df


# ══════════════════════════════════════════════════════════════════════════════
# Data fetching  (identical to backtest.py)
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_candles_range(angel_token: str, angel_exchange: str, interval: str,
                         from_dt: datetime, to_dt: datetime, label: str = "") -> pd.DataFrame:
    angel = get_angel_session()
    params = {
        "exchange":    angel_exchange,
        "symboltoken": angel_token,
        "interval":    interval,
        "fromdate":    from_dt.strftime("%Y-%m-%d %H:%M"),
        "todate":      to_dt.strftime("%Y-%m-%d %H:%M"),
    }
    logger.info("  Fetching %-30s | %s → %s", label, from_dt.strftime("%Y-%m-%d"), to_dt.strftime("%Y-%m-%d"))

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
    merged = sm._get_merged()
    mask = (
        (merged["name"].str.upper() == name.upper()) &
        (merged["exchange"] == exchange.upper())
    )
    subset = merged[mask].sort_values("expiry_date")
    return [sm._row_to_dict(r) for _, r in subset.iterrows()]


def fetch_historical_data(inst_cfg: dict, from_date: date, to_date: date) -> pd.DataFrame:
    base_name = inst_cfg.get("underlying", inst_cfg["name"])
    exchange  = inst_cfg["exchange"]
    interval  = inst_cfg["timeframe"]

    all_contracts = _get_all_contracts(base_name, exchange)
    if not all_contracts:
        raise ValueError(f"No contracts found for {base_name} on {exchange} in scrip master.")

    relevant = [c for c in all_contracts if c["expiry"] >= from_date]
    if not relevant:
        raise ValueError(
            f"No contracts covering {from_date} found for {base_name}. "
            f"Earliest available expiry: {all_contracts[-1]['expiry']}"
        )

    logger.info("  %s: %d contract(s) cover the backtest window", base_name, len(relevant))

    frames = []
    for i, contract in enumerate(relevant):
        if i == 0:
            fetch_from = datetime.combine(from_date, datetime.min.time())
        else:
            fetch_from = datetime.combine(relevant[i - 1]["expiry"] + timedelta(days=1),
                                          datetime.min.time())

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
        "  %s: %d candles total  (%s → %s)",
        base_name, len(full_df),
        full_df.index[0].strftime("%Y-%m-%d"),
        full_df.index[-1].strftime("%Y-%m-%d"),
    )
    return full_df


# ══════════════════════════════════════════════════════════════════════════════
# Strategy simulation
# ══════════════════════════════════════════════════════════════════════════════

def _pnl_multiplier(inst_cfg: dict) -> float:
    if inst_cfg["exchange"] == "MCX":
        return inst_cfg.get("contract_size", 1) * inst_cfg.get("qty", 1)
    return inst_cfg.get("_lot_size", 1) * inst_cfg.get("qty", 1)


def simulate_strategy(inst_cfg: dict, df: pd.DataFrame) -> list[dict]:
    long_only   = inst_cfg.get("long_only", False)
    trade_start = datetime.strptime(inst_cfg["trade_start"], "%H:%M").time()
    trade_end   = datetime.strptime(inst_cfg["trade_end"],   "%H:%M").time()

    if inst_cfg["exchange"] == "NFO" and "lot_size" in df.columns:
        inst_cfg = {**inst_cfg, "_lot_size": int(df["lot_size"].iloc[0])}

    # Use 2-indicator signals (ST 10/2 + MA 50 only)
    df_s = compute_signals_2ind(df)

    mult     = _pnl_multiplier(inst_cfg)
    position = 0
    entry_px = 0.0
    entry_ts = None
    entry_exp = None
    trades   = []

    for idx, (ts, row) in enumerate(df_s.iterrows()):
        if idx < MA_LENGTH:
            continue

        signal  = row.get("signal")
        close   = float(row["close"])
        exp     = row.get("contract_expiry")
        in_hrs  = trade_start <= ts.time() <= trade_end

        # ── Contract rollover ──────────────────────────────────────────────
        if position != 0 and entry_exp is not None and exp != entry_exp:
            pnl = (close - entry_px) * position * mult
            trades.append(_make_trade(inst_cfg["name"], position, entry_ts, entry_px,
                                      ts, close, pnl, "ROLLOVER", entry_exp))
            position = 0
            entry_px = 0.0
            entry_ts = None
            entry_exp = None

        # ── Signal processing ──────────────────────────────────────────────
        # Exit check uses raw indicator value (close vs st1) so that a SELL
        # signal (which also has close < st) reliably closes an open long,
        # and a BUY signal reliably closes an open short.  This prevents
        # positions from getting stuck when SELL/BUY takes np.select priority
        # over EXIT_LONG/EXIT_SHORT.
        st_value = float(row["st1"])
        below_st = close < st_value
        above_st = close > st_value

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
            if below_st:                            # stoploss: close < ST(10,2)
                pnl = (close - entry_px) * mult
                trades.append(_make_trade(inst_cfg["name"], +1, entry_ts, entry_px,
                                          ts, close, pnl, "EXIT_LONG", entry_exp))
                position = 0
                entry_px = 0.0
                entry_ts = None
                entry_exp = None
                # If the signal is also SELL, flip to short immediately
                if in_hrs and signal == "SELL" and not long_only:
                    position  = -1
                    entry_px  = close
                    entry_ts  = ts
                    entry_exp = exp

        else:  # position < 0
            if above_st:                            # stoploss: close > ST(10,2)
                pnl = (entry_px - close) * mult
                trades.append(_make_trade(inst_cfg["name"], -1, entry_ts, entry_px,
                                          ts, close, pnl, "EXIT_SHORT", entry_exp))
                position = 0
                entry_px = 0.0
                entry_ts = None
                entry_exp = None
                # If the signal is also BUY, flip to long immediately
                if in_hrs and signal == "BUY":
                    position  = +1
                    entry_px  = close
                    entry_ts  = ts
                    entry_exp = exp

    # ── Close any open position at end of data ─────────────────────────────
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

    cumulative   = np.cumsum(pnls)
    running_max  = np.maximum.accumulate(cumulative)
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
    print(f"  BACKTEST REPORT (2-INDICATOR)   |   Period: {from_date}  →  {to_date}")
    print(f"  Strategy: Supertrend({ST_PERIOD}/{ST_FACTOR}) + SMA({MA_LENGTH})   "
          f"|  Stoploss: Supertrend({ST_PERIOD}/{ST_FACTOR})")
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
    parser = argparse.ArgumentParser(description="Backtest 2-indicator strategy: ST(10,2) + MA(50)")
    parser.add_argument("--months",      type=int,   default=6,  help="Lookback in months (default: 6)")
    parser.add_argument("--instruments", type=str,   default="", help="Comma-separated names e.g. GOLDM,NIFTY")
    parser.add_argument("--hourly",      action="store_true",    help="Include hourly instruments")
    parser.add_argument("--all",         action="store_true",    help="Include both 15-min and hourly instruments")
    parser.add_argument("--no-save",     action="store_true",    help="Skip saving results to CSV")
    args = parser.parse_args()

    to_date   = date.today()
    from_date = (datetime.now() - timedelta(days=args.months * 30)).date()

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
    logger.info("Strategy: ST(%d/%.1f) + MA(%d)  |  Stoploss: ST(%d/%.1f)",
                ST_PERIOD, ST_FACTOR, MA_LENGTH, ST_PERIOD, ST_FACTOR)
    logger.info("═" * 60)

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

    print_report(results, from_date, to_date)

    if not args.no_save:
        all_trades = [t for d in results.values() for t in d["trades"]]
        if all_trades:
            out_path = f"backtest_2ind_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            pd.DataFrame(all_trades).to_csv(out_path, index=False)
            logger.info("Trade log saved → %s", out_path)


if __name__ == "__main__":
    main()
