"""
futures_screener_v2.py — Experimental variant of futures_screener.py testing
two strategy tweaks discussed during backtest review. Does NOT modify any
production code (indicators.py, backtest.py, main.py, config.py, order_manager.py
are all untouched and unaffected) — this is a standalone what-if sandbox.

Changes vs. the live/production strategy:

  1. Wider exit band. Entry already requires ALL THREE of st1/st2/ma to align
     (strict), but the production exit only needs the TIGHT Supertrend (ST1,
     period 10 factor 2.0) to be crossed — an asymmetric design that likely
     causes premature exits on the first pullback after a valid entry. Here,
     EXIT_LONG/EXIT_SHORT trigger off ST2 (period 10, factor 3.0 — the wider
     band) instead, to match the entry's strictness. Entry logic is untouched.

  2. No new entries within the last N minutes of the session (--no-entry-minutes,
     default 30). EXITS are still allowed at any time — only fresh entries are
     blocked. A position opened right before close has had zero time to prove
     itself before facing the overnight/day-boundary gap risk that backtest.py's
     day-boundary gate models (see backtest.py's simulate_strategy docstring).

Everything else — data fetching, contract rollover stitching, the day-boundary
gate, P&L accounting, stats, universe selection, walk-forward mode, CLI — is
identical to futures_screener.py / backtest.py, reused directly by import so
results are apples-to-apples comparable against a baseline futures_screener.py
run over the same period.

Usage: identical to futures_screener.py, plus:
    --no-entry-minutes 30   # block new entries in the last N minutes of the
                             # session (default 30); 0 disables this gate

    python futures_screener_v2.py --months 12
    python futures_screener_v2.py --walk-forward --is-frac 0.6 --oos-folds 2
    python futures_screener_v2.py --no-entry-minutes 45 --limit 20
"""

import argparse
import logging
import sys
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

import config
from backtest import _make_trade, _pnl_multiplier, compute_stats, fetch_historical_data
from indicators import sma, supertrend
from futures_screener import (
    RANK_METRICS,
    _METRIC_COL,
    _blank_row,
    _blank_walkforward_row,
    _bucket_trades,
    _fold_boundaries,
    _prefixed_stats,
    build_inst_cfg,
    get_futstk_universe,
    print_report,
    print_walkforward_report,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("futures_screener_v2")

for _mod in ("angel_login", "scrip_master", "urllib3", "requests"):
    logging.getLogger(_mod).setLevel(logging.WARNING)


# ══════════════════════════════════════════════════════════════════════════════
# V2 signal logic (change #1: wider exit band)
# ══════════════════════════════════════════════════════════════════════════════

def compute_signals_v2(df: pd.DataFrame, st1_period: int, st1_factor: float,
                        st2_period: int, st2_factor: float, ma_length: int) -> pd.DataFrame:
    """
    Same entry logic as indicators.compute_signals (all three of st1/st2/ma
    must align). EXIT_LONG/EXIT_SHORT trigger off ST2 (the wider band)
    instead of ST1, so the exit isn't more trigger-happy than the entry.
    """
    df = df.copy()
    df["st1"] = supertrend(df, st1_period, st1_factor)
    df["st2"] = supertrend(df, st2_period, st2_factor)
    df["ma"]  = sma(df["close"], ma_length)

    c, st1, st2, ma = df["close"], df["st1"], df["st2"], df["ma"]

    buy_cond   = (c > st1) & (c > st2) & (c > ma)
    sell_cond  = (c < st1) & (c < st2) & (c < ma)
    exit_long  = c < st2      # v2: was c < st1 in indicators.compute_signals
    exit_short = c > st2      # v2: was c > st1 in indicators.compute_signals

    conditions = [buy_cond, sell_cond, exit_long, exit_short]
    choices    = ["BUY",    "SELL",    "EXIT_LONG", "EXIT_SHORT"]
    df["signal"] = np.select(conditions, choices, default=None)
    return df


# ══════════════════════════════════════════════════════════════════════════════
# V2 simulation (change #2: no new entries near close)
# ══════════════════════════════════════════════════════════════════════════════

def simulate_strategy_v2(inst_cfg: dict, df: pd.DataFrame, no_entry_minutes: int) -> list[dict]:
    """
    Identical to backtest.simulate_strategy except:
      - uses compute_signals_v2 (wider exit band)
      - blocks NEW entries whose candle opens within the last
        `no_entry_minutes` of the session; exits are never gated by this.
    """
    long_only   = inst_cfg.get("long_only", False)
    trade_start = datetime.strptime(inst_cfg["trade_start"], "%H:%M").time()
    trade_end   = datetime.strptime(inst_cfg["trade_end"],   "%H:%M").time()

    if inst_cfg["exchange"] == "NFO" and "lot_size" in df.columns:
        inst_cfg = {**inst_cfg, "_lot_size": int(df["lot_size"].iloc[0])}

    entry_cutoff = (
        datetime.combine(date.today(), trade_end) - timedelta(minutes=no_entry_minutes)
    ).time()

    df_s = compute_signals_v2(
        df,
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
        close  = float(row["close"])
        exp    = row.get("contract_expiry")
        in_hrs = trade_start <= ts.time() <= trade_end
        # v2: fresh entries blocked once the candle opens inside the no-entry window
        entry_allowed = in_hrs and ts.time() < entry_cutoff

        # ── Contract rollover: force-close and re-evaluate ─────────────────
        if position != 0 and entry_exp is not None and exp != entry_exp:
            pnl = (close - entry_px) * position * mult
            trades.append(_make_trade(inst_cfg["name"], position, entry_ts, entry_px,
                                      ts, close, pnl, "ROLLOVER", entry_exp))
            position = 0
            entry_px = 0.0
            entry_ts = None
            entry_exp = None

        # ── Day-boundary gate (same as backtest.py — mirrors live exactly) ──
        is_last_of_day = (
            idx == len(df_s) - 1
            or df_s.index[idx + 1].date() != ts.date()
        )
        if is_last_of_day:
            continue

        # ── Signal processing ──────────────────────────────────────────────
        if position == 0:
            if entry_allowed and signal == "BUY":
                position  = +1
                entry_px  = close
                entry_ts  = ts
                entry_exp = exp
            elif entry_allowed and signal == "SELL" and not long_only:
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


# ══════════════════════════════════════════════════════════════════════════════
# Per-stock screening (V2)
# ══════════════════════════════════════════════════════════════════════════════

def screen_one_v2(inst_cfg: dict, from_date: date, to_date: date, no_entry_minutes: int) -> dict:
    df = fetch_historical_data(inst_cfg, from_date, to_date)
    trades = simulate_strategy_v2(inst_cfg, df, no_entry_minutes)
    stats = compute_stats(trades)

    lot_size = int(df["lot_size"].iloc[0]) if "lot_size" in df.columns and not df.empty else None
    avg_close = float(df["close"].mean()) if not df.empty else None
    notional = (lot_size * avg_close) if (lot_size and avg_close) else None
    return_pct = round(stats["total_pnl"] / notional * 100, 2) if notional else 0.0

    return {
        "symbol":     inst_cfg["name"],
        "lot_size":   lot_size,
        "candles":    len(df),
        "return_pct": return_pct,
        **stats,
        "error":      "",
    }


def screen_one_walkforward_v2(inst_cfg: dict, from_date: date, to_date: date,
                               is_frac: float, oos_folds: int, no_entry_minutes: int) -> dict:
    df = fetch_historical_data(inst_cfg, from_date, to_date)
    trades = simulate_strategy_v2(inst_cfg, df, no_entry_minutes)

    boundaries = _fold_boundaries(from_date, to_date, is_frac, oos_folds)
    fold_trades = _bucket_trades(trades, boundaries)
    is_trades = fold_trades[0]
    oos_fold_trades = fold_trades[1:]
    oos_trades_all = [t for fold in oos_fold_trades for t in fold]

    lot_size = int(df["lot_size"].iloc[0]) if "lot_size" in df.columns and not df.empty else None
    avg_close = float(df["close"].mean()) if not df.empty else None

    row = {"symbol": inst_cfg["name"], "lot_size": lot_size, "candles": len(df), "error": ""}
    row.update(_prefixed_stats("is", is_trades, lot_size, avg_close))
    row.update(_prefixed_stats("oos", oos_trades_all, lot_size, avg_close))
    for i, fold in enumerate(oos_fold_trades, 1):
        row.update(_prefixed_stats(f"oos{i}", fold, lot_size, avg_close))
    return row


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="V2 experimental screener: wider exit band (ST2) + no entries near close"
    )
    parser.add_argument("--months",     type=int, default=12,
                         help="Lookback in months (default: 12 = 1 year; use 6 for 6 months)")
    parser.add_argument("--rank-by",    type=str, default="sharpe", choices=RANK_METRICS,
                         help="Metric driving the console top-N table (default: sharpe)")
    parser.add_argument("--min-trades", type=int, default=10,
                         help="Minimum trades required to be eligible for ranking (default: 10)")
    parser.add_argument("--top",        type=int, default=10, help="How many stocks to show (default: 10)")
    parser.add_argument("--qty",        type=int, default=1, help="Lots per stock for the backtest (default: 1)")
    parser.add_argument("--product",    type=str, default=config.STOCK_FUTURES_PRODUCT,
                         help=f"NRML or MIS (default: {config.STOCK_FUTURES_PRODUCT})")
    parser.add_argument("--limit",      type=int, default=None,
                         help="Only test the first N stocks in the universe (for a quick smoke test)")
    parser.add_argument("--exclude",    type=str, default="",
                         help="Comma-separated stock names to skip, e.g. POLYCAB,TCS")
    parser.add_argument("--out",        type=str, default=None, help="CSV output path (default: auto-generated)")
    parser.add_argument("--no-entry-minutes", type=int, default=30,
                         help="Block new entries within the last N minutes of the session "
                              "(default: 30; use 0 to disable this gate)")
    parser.add_argument("--walk-forward", action="store_true",
                         help="Split each stock's window into in-sample/out-of-sample periods "
                              "and check whether the IS ranking holds up OOS")
    parser.add_argument("--is-frac",      type=float, default=0.6,
                         help="Fraction of the date range used as in-sample (default: 0.6)")
    parser.add_argument("--oos-folds",    type=int, default=1,
                         help="Number of consecutive out-of-sample sub-periods (default: 1)")
    parser.add_argument("--min-oos-trades", type=int, default=5,
                         help="Minimum OOS trades required to trust a stock's OOS stats (default: 5)")
    args = parser.parse_args()

    if args.walk_forward and not (0.2 <= args.is_frac <= 0.9):
        logger.error("--is-frac must be between 0.2 and 0.9 (got %.2f)", args.is_frac)
        sys.exit(1)

    to_date = date.today()
    from_date = (datetime.now() - timedelta(days=args.months * 30)).date()

    logger.info("Refreshing scrip master ...")
    import scrip_master as sm
    sm.refresh_masters()

    exclude = {s.strip().upper() for s in args.exclude.split(",") if s.strip()}
    universe = get_futstk_universe(exclude)
    if args.limit:
        universe = universe[: args.limit]

    if not universe:
        logger.error("No FUTSTK stocks found in the current universe. Check scrip master / --exclude.")
        sys.exit(1)

    default_prefix = "walkforward_v2_results" if args.walk_forward else "screener_v2_results"
    out_path = args.out or f"{default_prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    logger.info("=" * 60)
    logger.info("V2 VARIANT: exit uses ST2 (wide band) | no entries in last %d min of session",
                args.no_entry_minutes)
    logger.info("Screening %d stock(s) | %s -> %s | qty=%d lot(s), product=%s%s",
                len(universe), from_date, to_date, args.qty, args.product,
                "  [WALK-FORWARD]" if args.walk_forward else "")
    logger.info("=" * 60)

    rows = []
    start_time = datetime.now()

    for i, name in enumerate(universe, 1):
        inst_cfg = build_inst_cfg(name, args.qty, args.product)
        try:
            if args.walk_forward:
                row = screen_one_walkforward_v2(
                    inst_cfg, from_date, to_date, args.is_frac, args.oos_folds, args.no_entry_minutes
                )
            else:
                row = screen_one_v2(inst_cfg, from_date, to_date, args.no_entry_minutes)
        except Exception as exc:
            logger.warning("  %s failed: %s", name, exc)
            row = (
                _blank_walkforward_row(name, str(exc), args.oos_folds)
                if args.walk_forward else _blank_row(name, str(exc))
            )

        rows.append(row)
        pd.DataFrame(rows).to_csv(out_path, index=False)  # checkpoint after every stock

        elapsed = (datetime.now() - start_time).total_seconds()
        eta = elapsed / i * (len(universe) - i)
        if args.walk_forward:
            logger.info(
                "[%d/%d] %-12s IS trades=%-4s IS sharpe=%-6s | OOS trades=%-4s OOS sharpe=%-6s "
                "| elapsed=%.0fs ETA=%.0fs",
                i, len(universe), name,
                row["is_total_trades"], row["is_sharpe"],
                row["oos_total_trades"], row["oos_sharpe"],
                elapsed, eta,
            )
        else:
            logger.info(
                "[%d/%d] %-12s trades=%-4s pnl=Rs%-12s sharpe=%-6s | elapsed=%.0fs ETA=%.0fs",
                i, len(universe), name,
                row["total_trades"], f"{row['total_pnl']:+.2f}", row["sharpe"],
                elapsed, eta,
            )

    df_all = pd.DataFrame(rows)

    print(f"\n{'#' * 100}")
    print(f"# V2 VARIANT vs. production strategy:")
    print(f"#   1. EXIT_LONG/EXIT_SHORT trigger off ST2 (wide band) instead of ST1 (tight band)")
    print(f"#   2. No new entries within the last {args.no_entry_minutes} minute(s) of the session")
    print(f"# indicators.py / backtest.py / main.py are unmodified - compare this CSV against a")
    print(f"# baseline futures_screener.py run over the same period to see the effect.")
    print(f"{'#' * 100}")

    if args.walk_forward:
        boundaries = _fold_boundaries(from_date, to_date, args.is_frac, args.oos_folds)
        print_walkforward_report(df_all, args, from_date, to_date, boundaries, out_path)
    else:
        eligible = df_all[(df_all["error"] == "") & (df_all["total_trades"] >= args.min_trades)].copy()
        metric_col = _METRIC_COL[args.rank_by]
        df_ranked = eligible.sort_values(metric_col, ascending=False)
        print_report(df_all, df_ranked, args, from_date, to_date, out_path)


if __name__ == "__main__":
    main()
