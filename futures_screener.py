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
        print(f"\n  {len(failed)} stock(s) skipped/failed — see 'error' column in CSV for details")
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
    args = parser.parse_args()

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

    out_path = args.out or f"screener_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    logger.info("=" * 60)
    logger.info("Screening %d stock(s) | %s -> %s | qty=%d lot(s), product=%s",
                len(universe), from_date, to_date, args.qty, args.product)
    logger.info("=" * 60)

    rows = []
    start_time = datetime.now()

    for i, name in enumerate(universe, 1):
        inst_cfg = build_inst_cfg(name, args.qty, args.product)
        try:
            row = screen_one(inst_cfg, from_date, to_date)
        except Exception as exc:
            logger.warning("  %s failed: %s", name, exc)
            row = _blank_row(name, str(exc))

        rows.append(row)
        pd.DataFrame(rows).to_csv(out_path, index=False)  # checkpoint after every stock

        elapsed = (datetime.now() - start_time).total_seconds()
        eta = elapsed / i * (len(universe) - i)
        logger.info(
            "[%d/%d] %-12s trades=%-4s pnl=Rs%-12s sharpe=%-6s | elapsed=%.0fs ETA=%.0fs",
            i, len(universe), name,
            row["total_trades"], f"{row['total_pnl']:+.2f}", row["sharpe"],
            elapsed, eta,
        )

    df_all = pd.DataFrame(rows)
    eligible = df_all[(df_all["error"] == "") & (df_all["total_trades"] >= args.min_trades)].copy()

    metric_col = _METRIC_COL[args.rank_by]
    df_ranked = eligible.sort_values(metric_col, ascending=False)

    print_report(df_all, df_ranked, args, from_date, to_date, out_path)


if __name__ == "__main__":
    main()
