"""
futures_screener.py — Scan all NSE stock futures (FUTSTK) and rank them by
backtested performance of the same 15-min Supertrend + MA strategy used live.

This is a full-universe wrapper around backtest.py — it does NOT re-implement
the strategy. Each stock is run through the exact same fetch_historical_data /
simulate_strategy / compute_stats pipeline that `python backtest.py
--instruments X` uses, so results for any single stock match exactly.

Usage:
    python futures_screener.py                          # 1 year, all FUTSTK stocks, rank by sharpe
    python futures_screener.py --months 6                # 6-month lookback instead of 1 year
    python futures_screener.py --rank-by pnl --top 20
    python futures_screener.py --min-trades 15            # require more trades before ranking
    python futures_screener.py --limit 20                 # quick test on first 20 stocks
    python futures_screener.py --exclude POLYCAB,TCS

Walk-forward validation mode (--walk-forward):
    python futures_screener.py --walk-forward
    python futures_screener.py --walk-forward --is-frac 0.6 --oos-folds 2

    Splits each stock's backtest window into an in-sample (IS) period used for
    ranking and a later, never-seen-during-ranking out-of-sample (OOS) period
    used to check whether the ranking actually held up. This directly tests
    the concern that screening 200+ stocks with one fixed strategy and taking
    the best backtested performers is a multiple-comparisons problem: some
    stocks will look great purely from noise. If the top IS performers are
    NOT also good OOS, and the IS/OOS rank correlation is near zero, the
    "edge" you're seeing is probably noise, not something to trade on.

    Indicators are computed ONCE on the full continuous price history (so
    warmup/continuity is correct, exactly like live) and the resulting trade
    list is then split by entry date into IS/OOS buckets — this is cheaper
    and more accurate than re-fetching and re-simulating per period, which
    would introduce a fake indicator warmup discontinuity at the split point
    that live trading would never actually see.

    --is-frac 0.6     : first 60% of the date range is in-sample (default)
    --oos-folds 2     : split the remaining 40% into 2 consecutive OOS
                        sub-periods, reported separately (default: 1)
    --min-oos-trades  : minimum OOS trades for a stock's OOS stats to be
                        trusted (default: 5) — separate from --min-trades,
                        which gates IS eligibility

Ranking:
    All metrics (win rate, P&L, profit factor, Sharpe, return on notional) are
    computed for every stock and saved to CSV so you can re-sort however you
    like in Excel. --rank-by only controls which metric drives the console's
    top-N table.

    "return_pct" = total P&L / (lot_size x average close price) x 100 — this
    normalizes P&L per stock's notional value, since raw P&L is not comparable
    across stocks with very different lot sizes / prices.

Notes:
    - Angel's getCandleData is rate-limited and this runs strictly sequential
      (one call at a time) to respect it — a full 1-year scan of ~180-200
      FUTSTK stocks can take 30-60+ minutes.
    - Results are written to CSV incrementally after every stock so an
      interrupted run (Ctrl+C, rate-limit exhaustion) doesn't lose progress.
    - Only stocks with a currently-active (not-yet-expired) NFO futures
      contract are included — this is a screener for what to trade *next*,
      not a historical curiosity report.
"""

import argparse
import bisect
import logging
import sys
from datetime import date, datetime, timedelta

import pandas as pd

import config
import scrip_master as sm
from backtest import compute_stats, fetch_historical_data, simulate_strategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("futures_screener")

for _mod in ("angel_login", "scrip_master", "urllib3", "requests"):
    logging.getLogger(_mod).setLevel(logging.WARNING)

RANK_METRICS = ["sharpe", "return_pct", "pnl", "profit_factor", "win_rate", "avg_pnl"]
_METRIC_COL = {
    "sharpe":        "sharpe",
    "return_pct":    "return_pct",
    "pnl":           "total_pnl",
    "profit_factor": "profit_factor",
    "win_rate":      "win_rate",
    "avg_pnl":       "avg_pnl",
}


# ══════════════════════════════════════════════════════════════════════════════
# Universe
# ══════════════════════════════════════════════════════════════════════════════

def get_futstk_universe(exclude: set[str]) -> list[str]:
    """
    All NSE stock names with a currently-active (not-yet-expired) FUTSTK
    contract on NFO, per the Angel+Kite merged scrip master.
    """
    merged = sm._get_merged()
    today = date.today()
    mask = (
        (merged["exchange"] == "NFO")
        & (merged["instrumenttype"] == "FUTSTK")
        & (merged["expiry_date"] > today)
    )
    names = sorted(merged[mask]["name"].str.upper().unique().tolist())
    return [n for n in names if n not in exclude]


def build_inst_cfg(name: str, qty: int, product: str) -> dict:
    return {
        "name":        name,
        "exchange":    "NFO",
        "qty":         qty,
        "product":     product,
        "trade_start": "09:15",
        "trade_end":   "15:30",
        "timeframe":   "FIFTEEN_MINUTE",
    }


# ══════════════════════════════════════════════════════════════════════════════
# Per-stock screening
# ══════════════════════════════════════════════════════════════════════════════

def screen_one(inst_cfg: dict, from_date: date, to_date: date) -> dict:
    """Run the full backtest pipeline for one stock and return a result row."""
    df = fetch_historical_data(inst_cfg, from_date, to_date)
    trades = simulate_strategy(inst_cfg, df)
    stats = compute_stats(trades)

    lot_size = int(df["lot_size"].iloc[0]) if "lot_size" in df.columns and not df.empty else None
    avg_close = float(df["close"].mean()) if not df.empty else None
    notional = (lot_size * avg_close) if (lot_size and avg_close) else None
    return_pct = round(stats["total_pnl"] / notional * 100, 2) if notional else 0.0

    return {
        "symbol":       inst_cfg["name"],
        "lot_size":     lot_size,
        "candles":      len(df),
        "return_pct":   return_pct,
        **stats,
        "error":        "",
    }


def _blank_row(name: str, error: str) -> dict:
    return {
        "symbol":     name,
        "lot_size":   None,
        "candles":    0,
        "return_pct": 0.0,
        **compute_stats([]),
        "error":      error,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Walk-forward validation
# ══════════════════════════════════════════════════════════════════════════════

def _fold_boundaries(from_date: date, to_date: date, is_frac: float, oos_folds: int) -> list[date]:
    """
    Return len(oos_folds)+2 sorted date boundaries:
        [from_date, is_end, oos_fold_1_end, ..., to_date+1]
    The trailing +1 day makes the last boundary exclusive-safe so a trade
    entered exactly on to_date still falls inside the last fold.
    """
    total_days = (to_date - from_date).days
    is_end = from_date + timedelta(days=int(total_days * is_frac))
    oos_span = (to_date - is_end).days

    boundaries = [from_date, is_end]
    for i in range(1, oos_folds + 1):
        boundaries.append(is_end + timedelta(days=round(oos_span * i / oos_folds)))
    boundaries[-1] = to_date + timedelta(days=1)
    return boundaries


def _bucket_trades(trades: list[dict], boundaries: list[date]) -> list[list[dict]]:
    """Bucket trades by entry_time.date() into len(boundaries)-1 sequential folds."""
    n = len(boundaries) - 1
    folds: list[list[dict]] = [[] for _ in range(n)]
    for t in trades:
        d = t["entry_time"].date()
        idx = bisect.bisect_right(boundaries, d) - 1
        idx = max(0, min(idx, n - 1))
        folds[idx].append(t)
    return folds


def _prefixed_stats(prefix: str, trades: list[dict], lot_size, avg_close) -> dict:
    stats = compute_stats(trades)
    notional = (lot_size * avg_close) if (lot_size and avg_close) else None
    return_pct = round(stats["total_pnl"] / notional * 100, 2) if notional else 0.0
    out = {f"{prefix}_return_pct": return_pct}
    out.update({f"{prefix}_{k}": v for k, v in stats.items()})
    return out


def screen_one_walkforward(inst_cfg: dict, from_date: date, to_date: date,
                            is_frac: float, oos_folds: int) -> dict:
    """
    Fetch + simulate ONCE over the full continuous window (correct indicator
    warmup/continuity), then split the resulting trades by entry date into an
    in-sample bucket and one-or-more out-of-sample buckets.
    """
    df = fetch_historical_data(inst_cfg, from_date, to_date)
    trades = simulate_strategy(inst_cfg, df)

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


def _blank_walkforward_row(name: str, error: str, oos_folds: int) -> dict:
    row = {"symbol": name, "lot_size": None, "candles": 0, "error": error}
    row.update(_prefixed_stats("is", [], None, None))
    row.update(_prefixed_stats("oos", [], None, None))
    for i in range(1, oos_folds + 1):
        row.update(_prefixed_stats(f"oos{i}", [], None, None))
    return row


def print_walkforward_report(df_all: pd.DataFrame, args, from_date, to_date, boundaries, out_path):
    WIDE = "=" * 130
    metric_col = _METRIC_COL[args.rank_by]
    is_col, oos_col = f"is_{metric_col}", f"oos_{metric_col}"

    print(f"\n{WIDE}")
    print(f"  WALK-FORWARD SCREENER   |   Full window: {from_date} -> {to_date}")
    print(f"  IS:  {boundaries[0]} -> {boundaries[1]}   ({args.is_frac*100:.0f}% of range)")
    for i in range(args.oos_folds):
        print(f"  OOS{i+1}: {boundaries[1+i]} -> {boundaries[2+i]}")
    print(f"  Strategy: Supertrend(ST1={config.ST1_PERIOD}/{config.ST1_FACTOR}, "
          f"ST2={config.ST2_PERIOD}/{config.ST2_FACTOR}) + SMA({config.MA_LENGTH})  |  "
          f"{args.qty} lot(s), product={args.product}")
    print(WIDE)

    eligible = df_all[(df_all["error"] == "") & (df_all["is_total_trades"] >= args.min_trades)].copy()
    reliable = eligible[eligible["oos_total_trades"] >= args.min_oos_trades].copy()

    print(f"\n  Universe: {len(df_all)} scanned  |  {len(eligible)} IS-eligible "
          f"(>= {args.min_trades} IS trades)  |  {len(reliable)} also OOS-eligible "
          f"(>= {args.min_oos_trades} OOS trades)")

    top = eligible.sort_values(is_col, ascending=False).head(args.top)
    cols = ["symbol", "lot_size", "is_total_trades", "is_win_rate", "is_return_pct", "is_sharpe",
            "oos_total_trades", "oos_win_rate", "oos_return_pct", "oos_sharpe"]
    display = top[cols].copy()
    display.columns = ["Symbol", "Lot", "IS Trd", "IS Win%", "IS Ret%", "IS Sharpe",
                        "OOS Trd", "OOS Win%", "OOS Ret%", "OOS Sharpe"]

    print(f"\n  TOP {args.top} BY IN-SAMPLE {args.rank_by.upper()} - does it hold up out-of-sample?")
    print(f"  {'-' * 120}")
    print(display.to_string(index=False))

    held_up = top[
        (top["oos_total_trades"] >= args.min_oos_trades)
        & (top["oos_sharpe"] > 0)
        & (top["oos_total_pnl"] > 0)
    ]
    print(f"\n  {len(held_up)}/{len(top)} of the IS top {args.top} remained profitable "
          f"(Sharpe>0 and P&L>0) out-of-sample.")

    if len(reliable) >= 5:
        rank_corr = reliable[[is_col, oos_col]].corr(method="spearman").iloc[0, 1]
        print(f"\n  Spearman rank correlation (IS {args.rank_by} vs OOS {args.rank_by}) "
              f"across {len(reliable)} stocks: {rank_corr:+.2f}")
        print("    Near 0 or negative => historical ranking has little/no predictive power;")
        print("    the 'top' stocks in a single-period screen are likely noise, not a real edge.")
        print("    Meaningfully positive (say > 0.3) => the ranking has some persistence worth trusting.")
    else:
        print(f"\n  Only {len(reliable)} stocks had enough OOS trades to compute a rank "
              f"correlation reliably - widen --months or lower --min-oos-trades.")

    failed = df_all[df_all["error"] != ""]
    if not failed.empty:
        print(f"\n  {len(failed)} stock(s) skipped/failed - see 'error' column in CSV")

    print(f"\n  Full IS/OOS results (all {len(df_all)} stocks) saved to: {out_path}")
    print(f"{WIDE}\n")


# ══════════════════════════════════════════════════════════════════════════════
# Report
# ══════════════════════════════════════════════════════════════════════════════

def print_report(df_all: pd.DataFrame, df_ranked: pd.DataFrame, args, from_date, to_date, out_path):
    WIDE = "=" * 120
    print(f"\n{WIDE}")
    print(f"  STOCK FUTURES SCREENER   |   Period: {from_date} -> {to_date}  ({args.months} months)")
    print(f"  Strategy: Supertrend(ST1={config.ST1_PERIOD}/{config.ST1_FACTOR}, "
          f"ST2={config.ST2_PERIOD}/{config.ST2_FACTOR}) + SMA({config.MA_LENGTH})  |  "
          f"{args.qty} lot(s), product={args.product}")
    print(f"  Universe: {len(df_all)} stocks scanned  |  {len(df_ranked)} eligible "
          f"(>= {args.min_trades} trades)  |  Ranked by: {args.rank_by}")
    print(WIDE)

    top = df_ranked.head(args.top)
    cols = ["symbol", "lot_size", "total_trades", "win_rate", "total_pnl",
            "return_pct", "max_drawdown", "profit_factor", "sharpe"]
    display = top[cols].copy()
    display.columns = ["Symbol", "Lot", "Trades", "Win%", "Total P&L (Rs)",
                        "Return%", "Max DD (Rs)", "PF", "Sharpe"]

    print(f"\n  TOP {args.top} (by {args.rank_by})")
    print(f"  {'-'*110}")
    print(display.to_string(index=False))

    failed = df_all[df_all["error"] != ""]
    if not failed.empty:
        print(f"\n  {len(failed)} stock(s) skipped/failed - see 'error' column in CSV for details")
        for _, r in failed.head(10).iterrows():
            print(f"    - {r['symbol']}: {r['error']}")
        if len(failed) > 10:
            print(f"    ... and {len(failed) - 10} more")

    print(f"\n  Full results (all {len(df_all)} stocks, all metrics) saved to: {out_path}")
    print(f"{WIDE}\n")


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Rank NSE stock futures by backtested 15-min Supertrend+MA performance"
    )
    parser.add_argument("--months",     type=int, default=12,
                         help="Lookback in months (default: 12 = 1 year; use 6 for 6 months)")
    parser.add_argument("--rank-by",    type=str, default="sharpe", choices=RANK_METRICS,
                         help="Metric driving the console top-N table (default: sharpe)")
    parser.add_argument("--min-trades", type=int, default=10,
                         help="Minimum trades required to be eligible for ranking (default: 10)")
    parser.add_argument("--top",        type=int, default=10, help="How many stocks to show (default: 10)")
    parser.add_argument("--qty",        type=int, default=1, help="Lots per stock for the backtest (default: 1, for fair comparison)")
    parser.add_argument("--product",    type=str, default=config.STOCK_FUTURES_PRODUCT,
                         help=f"NRML or MIS (default: {config.STOCK_FUTURES_PRODUCT})")
    parser.add_argument("--limit",      type=int, default=None,
                         help="Only test the first N stocks in the universe (for a quick smoke test)")
    parser.add_argument("--exclude",    type=str, default="",
                         help="Comma-separated stock names to skip, e.g. POLYCAB,TCS")
    parser.add_argument("--out",        type=str, default=None, help="CSV output path (default: auto-generated)")
    parser.add_argument("--walk-forward", action="store_true",
                         help="Split each stock's window into in-sample/out-of-sample periods "
                              "and check whether the IS ranking holds up OOS (see module docstring)")
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
    sm.refresh_masters()

    exclude = {s.strip().upper() for s in args.exclude.split(",") if s.strip()}
    universe = get_futstk_universe(exclude)
    if args.limit:
        universe = universe[: args.limit]

    if not universe:
        logger.error("No FUTSTK stocks found in the current universe. Check scrip master / --exclude.")
        sys.exit(1)

    default_prefix = "walkforward_results" if args.walk_forward else "screener_results"
    out_path = args.out or f"{default_prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    logger.info("=" * 60)
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
                row = screen_one_walkforward(inst_cfg, from_date, to_date, args.is_frac, args.oos_folds)
            else:
                row = screen_one(inst_cfg, from_date, to_date)
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
