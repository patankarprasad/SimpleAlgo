"""
ha_screener.py — Compare Normal-candle vs Heikin-Ashi-signal backtests.

Same Supertrend + MA strategy and same trade-simulation logic as backtest.py
(rollover handling, day-boundary gate, entry/exit rules) — run three times
per instrument:

  NORMAL   : signals generated from real candles, fills at real close.
             Identical to backtest.py.
  HA       : signals generated from Heikin Ashi candles (smoothed
             O/H/L/C), but fills happen at the REAL close of the signal
             bar. HA prices only decide entry/exit timing — this is what
             makes the backtest behave like a real trade would.
  HA_NAIVE : signals AND fills both use the synthetic HA close. This
             reproduces the default (misleading) behaviour you get on
             TradingView when you switch the chart to Heikin Ashi and run
             Strategy Tester on it without overriding fills to real price.
             HA close lags/smooths real price, so filling here is filling
             at a price you could never actually get in the market —
             compare HA_NAIVE vs HA to see how much of "HA looks better"
             is a real timing edge vs a fill-price artifact.

No walk-forward split — this is a single in-sample backtest over the full
lookback window, run for all three signal/fill combinations so they can be
compared directly.

Usage:
    python ha_screener.py                               # all 15-min instruments, 6 months
    python ha_screener.py --months 3
    python ha_screener.py --instruments GOLDM,NIFTY
    python ha_screener.py --hourly
    python ha_screener.py --all
"""

import argparse
import logging
import sys
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

import config
from indicators import compute_signals, heikin_ashi
import scrip_master as sm
from backtest import fetch_historical_data, _pnl_multiplier, _make_trade, compute_stats

# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ha_screener")

for _mod in ("angel_login", "scrip_master", "urllib3", "requests"):
    logging.getLogger(_mod).setLevel(logging.WARNING)


# ══════════════════════════════════════════════════════════════════════════════
# Strategy simulation (mirrors backtest.simulate_strategy, generalized so
# signals can come from a transformed df while fills use real close)
# ══════════════════════════════════════════════════════════════════════════════

def simulate_strategy(inst_cfg: dict, df: pd.DataFrame, mode: str) -> list[dict]:
    """
    Walk through every bar and simulate the strategy in live-system order.

    mode="normal"   : signals + fills both from real candles (identical to
                       backtest.simulate_strategy).
    mode="ha"       : signals computed on Heikin Ashi candles, fills use
                       that bar's REAL close — HA never used as a
                       tradable price, only for entry/exit timing.
    mode="ha_naive" : signals AND fills both use the synthetic HA close —
                       reproduces TradingView's default (misleading)
                       Heikin-Ashi-chart backtest behaviour.
    """
    long_only   = inst_cfg.get("long_only", False)
    trade_start = datetime.strptime(inst_cfg["trade_start"], "%H:%M").time()
    trade_end   = datetime.strptime(inst_cfg["trade_end"],   "%H:%M").time()

    if inst_cfg["exchange"] == "NFO" and "lot_size" in df.columns:
        inst_cfg = {**inst_cfg, "_lot_size": int(df["lot_size"].iloc[0])}

    if mode == "normal":
        sig_df = df.copy()
        sig_df["real_close"] = df["close"]
        fill_col = "real_close"
    elif mode == "ha":
        sig_df = heikin_ashi(df)          # open/high/low/close -> HA values, real close kept in 'real_close'
        fill_col = "real_close"
    elif mode == "ha_naive":
        sig_df = heikin_ashi(df)
        fill_col = "close"                # fill at the synthetic HA close on purpose — this is the artifact
    else:
        raise ValueError(f"Unknown mode: {mode!r}")

    df_s = compute_signals(
        sig_df,
        config.ST1_PERIOD, config.ST1_FACTOR,
        config.ST2_PERIOD, config.ST2_FACTOR,
        config.MA_LENGTH,
    )

    mult      = _pnl_multiplier(inst_cfg)
    position  = 0      # 0=flat, +1=long, -1=short
    entry_px  = 0.0
    entry_ts  = None
    entry_exp = None
    trades    = []

    for idx, (ts, row) in enumerate(df_s.iterrows()):
        if idx < config.MA_LENGTH:
            continue

        signal = row.get("signal")
        fill   = float(row[fill_col])          # always the REAL tradable price
        exp    = row.get("contract_expiry")
        in_hrs = trade_start <= ts.time() <= trade_end

        # ── Contract rollover: force-close and re-evaluate ─────────────────
        if position != 0 and entry_exp is not None and exp != entry_exp:
            pnl = (fill - entry_px) * position * mult
            trades.append(_make_trade(inst_cfg["name"], position, entry_ts, entry_px,
                                      ts, fill, pnl, "ROLLOVER", entry_exp))
            position = 0
            entry_px = 0.0
            entry_ts = None
            entry_exp = None

        # ── Day-boundary gate (mirrors live scheduler, see backtest.py) ────
        is_last_of_day = (
            idx == len(df_s) - 1
            or df_s.index[idx + 1].date() != ts.date()
        )
        if is_last_of_day:
            continue

        # ── Signal processing ──────────────────────────────────────────────
        if position == 0:
            if in_hrs and signal == "BUY":
                position, entry_px, entry_ts, entry_exp = +1, fill, ts, exp
            elif in_hrs and signal == "SELL" and not long_only:
                position, entry_px, entry_ts, entry_exp = -1, fill, ts, exp

        elif position > 0:
            if signal == "EXIT_LONG":
                pnl = (fill - entry_px) * mult
                trades.append(_make_trade(inst_cfg["name"], +1, entry_ts, entry_px,
                                          ts, fill, pnl, signal, entry_exp))
                position, entry_px, entry_ts, entry_exp = 0, 0.0, None, None

        else:  # position < 0
            if signal == "EXIT_SHORT":
                pnl = (entry_px - fill) * mult
                trades.append(_make_trade(inst_cfg["name"], -1, entry_ts, entry_px,
                                          ts, fill, pnl, signal, entry_exp))
                position, entry_px, entry_ts, entry_exp = 0, 0.0, None, None

    # ── Close any position open at end of data ─────────────────────────────
    if position != 0:
        ts   = df_s.index[-1]
        fill = float(df_s.iloc[-1][fill_col])
        exp  = df_s.iloc[-1].get("contract_expiry")
        pnl  = (fill - entry_px) * position * mult
        trades.append(_make_trade(inst_cfg["name"], position, entry_ts, entry_px,
                                  ts, fill, pnl, "END_OF_DATA", entry_exp))

    return trades


# ══════════════════════════════════════════════════════════════════════════════
# Report printing
# ══════════════════════════════════════════════════════════════════════════════

def _w(label, value, width=22):
    return f"  {label:<{width}}: {value}"


MODES = (("NORMAL", "normal"), ("HEIKIN ASHI", "ha"), ("HA NAIVE (TradingView-style)", "ha_naive"))


def print_comparison(results: dict, from_date: date, to_date: date):
    WIDE = "=" * 130
    THIN = "-" * 130

    print(f"\n{WIDE}")
    print(f"  NORMAL vs HEIKIN ASHI vs HA-NAIVE COMPARISON   |   Period: {from_date}  ->  {to_date}")
    print(f"  Strategy: Supertrend (ST1={config.ST1_PERIOD}/{config.ST1_FACTOR}, "
          f"ST2={config.ST2_PERIOD}/{config.ST2_FACTOR}) + SMA({config.MA_LENGTH})")
    print("  NORMAL/HA fill at REAL candle close. HA_NAIVE fills at the synthetic HA close")
    print("  (reproduces TradingView's default Heikin-Ashi-chart backtest artifact).")
    print(WIDE)

    summary_rows = []

    for inst_name, data in results.items():
        err = data.get("error")
        print(f"\n{'-'*50}  {inst_name}  {'-'*50}")

        if err:
            print(f"  ERROR: {err}\n")
            continue

        for mode_label, key in MODES:
            s = data[key]["stats"]
            print(f"\n  [{mode_label}]")
            print(_w("Total Trades",    s["total_trades"]))
            print(_w("Wins / Losses",   f"{s['wins']} / {s['losses']}"))
            print(_w("Win Rate",        f"{s['win_rate']}%"))
            print(_w("Total P&L",       f"Rs {s['total_pnl']:>+,.2f}"))
            print(_w("Avg P&L / Trade", f"Rs {s['avg_pnl']:>+,.2f}"))
            print(_w("Avg Win / Loss",  f"Rs {s['avg_win']:>+,.2f} / Rs {s['avg_loss']:>+,.2f}"))
            print(_w("Max Drawdown",    f"Rs {s['max_drawdown']:>+,.2f}"))
            print(_w("Profit Factor",   s["profit_factor"]))
            print(_w("Sharpe Ratio",    s["sharpe"]))

        n, h, hn = data["normal"]["stats"], data["ha"]["stats"], data["ha_naive"]["stats"]
        summary_rows.append({
            "Instrument":   inst_name,
            "N_Trades":     n["total_trades"],
            "N_PnL":        f"{n['total_pnl']:>+,.0f}",
            "HA_Trades":    h["total_trades"],
            "HA_PnL":       f"{h['total_pnl']:>+,.0f}",
            "HAnaive_Trds": hn["total_trades"],
            "HAnaive_PnL":  f"{hn['total_pnl']:>+,.0f}",
            "Artifact_Rs":  f"{hn['total_pnl'] - h['total_pnl']:>+,.0f}",
        })

    print(f"\n{WIDE}")
    print("  SUMMARY  (N_=Normal, HA_=Heikin Ashi signal/real fill, HAnaive_=HA signal/HA fill)")
    print("  Artifact_Rs = HAnaive P&L minus HA P&L -> how much of TradingView's edge is fake fill price")
    print(WIDE)

    if summary_rows:
        sdf = pd.DataFrame(summary_rows)
        print(sdf.to_string(index=False))

    totals = {}
    for _, key in MODES:
        totals[key] = {
            "pnl":    sum(d[key]["stats"]["total_pnl"]    for d in results.values() if not d.get("error")),
            "trades": sum(d[key]["stats"]["total_trades"] for d in results.values() if not d.get("error")),
        }

    print(f"\n  {'Combined Normal':28s}: {totals['normal']['trades']} trades | Rs {totals['normal']['pnl']:>+,.2f}")
    print(f"  {'Combined Heikin Ashi':28s}: {totals['ha']['trades']} trades | Rs {totals['ha']['pnl']:>+,.2f}")
    print(f"  {'Combined HA Naive (TV-style)':28s}: {totals['ha_naive']['trades']} trades | Rs {totals['ha_naive']['pnl']:>+,.2f}")
    print(f"  {'Fill-price artifact (Rs)':28s}: Rs {totals['ha_naive']['pnl'] - totals['ha']['pnl']:>+,.2f}")
    print(f"\n{WIDE}\n")


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Compare Normal vs Heikin Ashi signal backtests")
    parser.add_argument("--months",      type=int, default=6,  help="Lookback in months (default: 6)")
    parser.add_argument("--instruments", type=str, default="", help="Comma-separated names e.g. GOLDM,NIFTY")
    parser.add_argument("--hourly",      action="store_true",  help="Include hourly instruments")
    parser.add_argument("--all",         action="store_true",  help="Include both 15-min and hourly instruments")
    parser.add_argument("--no-save",     action="store_true",  help="Skip saving results to CSV")
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

    logger.info("=" * 60)
    logger.info("Normal vs HA screener | %d instrument(s) | %s -> %s", len(instruments), from_date, to_date)
    logger.info("=" * 60)

    sm.refresh_masters()

    results = {}
    for inst_cfg in instruments:
        name = inst_cfg["name"]
        logger.info("-- %s --------------------------------", name)
        try:
            df = fetch_historical_data(inst_cfg, from_date, to_date)

            normal_trades   = simulate_strategy(inst_cfg, df, mode="normal")
            ha_trades       = simulate_strategy(inst_cfg, df, mode="ha")
            ha_naive_trades = simulate_strategy(inst_cfg, df, mode="ha_naive")

            results[name] = {
                "normal":   {"trades": normal_trades,   "stats": compute_stats(normal_trades)},
                "ha":       {"trades": ha_trades,       "stats": compute_stats(ha_trades)},
                "ha_naive": {"trades": ha_naive_trades, "stats": compute_stats(ha_naive_trades)},
            }
            ns, hs, hns = (results[name]["normal"]["stats"], results[name]["ha"]["stats"],
                          results[name]["ha_naive"]["stats"])
            logger.info("  %s: NORMAL %d trades / Rs%.2f  |  HA %d trades / Rs%.2f  |  HA_NAIVE %d trades / Rs%.2f",
                        name, ns["total_trades"], ns["total_pnl"], hs["total_trades"], hs["total_pnl"],
                        hns["total_trades"], hns["total_pnl"])
        except Exception as exc:
            logger.error("  %s failed: %s", name, exc, exc_info=False)
            results[name] = {
                "normal":   {"trades": [], "stats": compute_stats([])},
                "ha":       {"trades": [], "stats": compute_stats([])},
                "ha_naive": {"trades": [], "stats": compute_stats([])},
                "error":    str(exc),
            }

    print_comparison(results, from_date, to_date)

    if not args.no_save:
        rows = []
        for inst_name, data in results.items():
            for _, mode in MODES:
                for t in data[mode]["trades"]:
                    rows.append({"mode": mode, **t})
        if rows:
            out_path = f"ha_screener_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            pd.DataFrame(rows).to_csv(out_path, index=False)
            logger.info("Trade log saved -> %s", out_path)


if __name__ == "__main__":
    main()
