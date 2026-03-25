"""
Main entry point — starts the algo scheduler AND the web server in one process.

Startup flow
------------
1. Refresh scrip masters (Angel + Kite) if not already cached today.
2. Resolve each instrument in config.INSTRUMENTS → adds angel_token,
   angel_symbol, kite_tradingsymbol, kite_instrument_token, expiry, lot_size.
3. Start APScheduler (BackgroundScheduler) — fires run_strategy() at candle close.
4. Start Flask web server (main thread) — dashboard, positions, trades, log, Kite login.

Usage
-----
  python main.py              # normal mode (scheduler + web server)
  python main.py --run-once   # run strategy once and exit (no web server)

  Set DRY_RUN=true in .env for paper trading (no real orders placed).
"""
import logging
import sys
from datetime import datetime

import pytz

import config
import notifier
import paper_trading
import scrip_master
import strategy_config
import trade_log
import web_state
from angel_data import get_candles, get_option_ltps
from indicators import compute_signals
from kite_login import get_kite_session
from order_manager import (
    place_buy, place_sell, close_long, close_short,
    place_synthetic_buy, place_synthetic_sell,
    close_synthetic_long, close_synthetic_short,
)
from state import load_state, set_position, get_position

# ── Logging setup ──────────────────────────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("algo.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)
IST    = pytz.timezone("Asia/Kolkata")

# ── Globals ────────────────────────────────────────────────────────────────────
RESOLVED_INSTRUMENTS: list[dict] = []
DRY_RUN: bool = False


# ── Initialisation ─────────────────────────────────────────────────────────────

def initialise():
    """
    Run once at startup:
    - Refresh scrip masters (downloads if not yet cached today).
    - Resolve each config instrument → fills in live tokens and symbols.
    - Log the active contracts so you can verify expiry dates.
    """
    global RESOLVED_INSTRUMENTS

    logger.info("Refreshing scrip masters ...")
    scrip_master.refresh_masters()

    RESOLVED_INSTRUMENTS = []
    for inst_def in config.INSTRUMENTS:
        try:
            resolved = scrip_master.resolve_instrument(inst_def)
            RESOLVED_INSTRUMENTS.append(resolved)
            logger.info(
                "  %-12s | angel=%-22s (token %s) | kite=%-22s | expiry=%s | lot=%d",
                resolved["name"],
                resolved["angel_symbol"],
                resolved["angel_token"],
                resolved["kite_tradingsymbol"],
                resolved["expiry"],
                resolved["lot_size"],
            )
        except Exception as exc:
            logger.error("Failed to resolve %s: %s", inst_def["name"], exc)

    if not RESOLVED_INSTRUMENTS:
        raise RuntimeError("No instruments could be resolved. Check scrip masters.")

    web_state.set_resolved_instruments(RESOLVED_INSTRUMENTS)


# ── Contract rollover check ────────────────────────────────────────────────────

def _check_rollover():
    """
    Compare each resolved instrument's live symbol against positions_state.json.
    If a contract has rolled over while a position is open, log a critical warning
    and send a Telegram alert so the trader can intervene manually.

    The algo does NOT auto-close rolled positions — doing so blindly
    risks trading the wrong contract. Human confirmation is required.
    """
    state = load_state()
    for inst in RESOLVED_INSTRUMENTS:
        name    = inst["name"]
        new_sym = inst["kite_tradingsymbol"]
        saved   = state.get(name, {})
        old_sym = saved.get("kite_tradingsymbol", "")
        pos     = saved.get("position_size", 0)
        if pos != 0 and old_sym and old_sym != new_sym:
            logger.critical(
                "ROLLOVER DETECTED: %s — open position in %s but contract is now %s. "
                "Manual intervention required before trading resumes.",
                name, old_sym, new_sym,
            )
            notifier.notify_rollover_warning(name, old_sym, new_sym, pos)


# ── Core strategy tick ─────────────────────────────────────────────────────────

def run_strategy():
    """Called at every candle close. Processes all resolved instruments."""
    web_state.record_run()

    now_ist = datetime.now(IST)
    logger.info("=" * 64)
    logger.info("Strategy run at %s IST", now_ist.strftime("%Y-%m-%d %H:%M:%S"))

    # Kite session is fetched once and shared; None if token missing/expired.
    # Instruments that need to place orders will fail gracefully when kite is None.
    kite = None
    try:
        kite = get_kite_session()
    except RuntimeError as exc:
        logger.warning("Kite session unavailable (%s) — signal calc continues, orders disabled", exc)

    state = load_state()

    for instrument in RESOLVED_INSTRUMENTS:
        try:
            _process_instrument(kite, state, instrument, now_ist)
        except Exception as exc:
            logger.error("Error processing %s: %s", instrument["name"], exc, exc_info=True)


def _fetch_option_ltps_safe(ce_opt: dict, pe_opt: dict) -> tuple[float, float]:
    """
    Fetch live LTPs for a CE/PE pair from Angel SmartAPI (getLtpData).
    Uses Angel instead of Kite to avoid Kite API permission restrictions on options.

    ce_opt / pe_opt must have: angel_token, angel_symbol, angel_exchange,
                               kite_tradingsymbol  (used as result key)
    Returns (0.0, 0.0) on any failure.
    """
    try:
        results = get_option_ltps([ce_opt, pe_opt])
        return (
            results.get(ce_opt["kite_tradingsymbol"], 0.0),
            results.get(pe_opt["kite_tradingsymbol"], 0.0),
        )
    except Exception as exc:
        logger.warning(
            "Failed to fetch option LTPs (%s / %s): %s",
            ce_opt.get("kite_tradingsymbol"), pe_opt.get("kite_tradingsymbol"), exc,
        )
        return 0.0, 0.0


def _process_instrument(kite, state: dict, instrument: dict, now_ist):
    name         = instrument["name"]
    trade_start  = _parse_time(instrument["trade_start"])
    trade_end    = _parse_time(instrument["trade_end"])
    current_time = now_ist.time()

    if not strategy_config.is_enabled(name):
        logger.info("%s: strategy disabled — skipping", name)
        return

    # ── Outside market hours — skip candle fetch, hold position as-is ──────────
    # This is a positional strategy: open trades carry overnight/multi-day.
    # Exit happens only when the strategy signal fires, never on a time basis.
    if current_time > trade_end or current_time < trade_start:
        logger.info("%s: outside market hours (%s–%s), skipping",
                    name, instrument["trade_start"], instrument["trade_end"])
        return

    # 1. Fetch OHLCV candles from Angel using the resolved token
    df = get_candles(instrument)
    if len(df) < config.MA_LENGTH + 10:
        logger.warning("%s: not enough candles (%d). Skipping.", name, len(df))
        return

    # 2. Compute indicators; use iloc[-2] = last CLOSED candle
    df_sig = compute_signals(
        df,
        st1_period = config.ST1_PERIOD,
        st1_factor = config.ST1_FACTOR,
        st2_period = config.ST2_PERIOD,
        st2_factor = config.ST2_FACTOR,
        ma_length  = config.MA_LENGTH,
    )

    last        = df_sig.iloc[-2]
    signal      = last["signal"]
    close_price = last["close"]
    pos         = get_position(state, name)

    logger.info(
        "%s | kite=%-20s | close=%.2f  st1=%.2f  st2=%.2f  ma=%.2f | signal=%-12s pos=%d",
        name, instrument["kite_tradingsymbol"],
        close_price, last["st1"], last["st2"], last["ma"], signal, pos,
    )

    # 3. Push latest snapshot to the web dashboard
    web_state.update_instrument(name, {
        "kite_tradingsymbol": instrument["kite_tradingsymbol"],
        "interval":           config.CANDLE_INTERVAL,
        "close":  float(close_price),
        "st1":    float(last["st1"]),
        "st2":    float(last["st2"]),
        "ma":     float(last["ma"]),
        "signal": str(signal),
    })

    # order_qty: lots × kite lot_size — used for live order logs/notifications
    # pnl_qty:   lots × contract_size — used only for paper PnL (MCX contract_size > lot_size)
    order_qty = instrument["qty"] * instrument["lot_size"]
    pnl_qty   = instrument["qty"] * instrument.get("contract_size", instrument["lot_size"])

    sym          = instrument["kite_tradingsymbol"]
    exchange     = instrument["exchange"]
    is_synthetic = instrument.get("mode") == "SYNTHETIC"
    strike_step  = instrument.get("strike_step", 50)
    expiry_date  = instrument.get("expiry")          # datetime.date from resolve_instrument

    price = float(close_price)

    # 4. Act on signal
    if DRY_RUN:
        paper_size = paper_trading.get_position_size(name)

        if signal == "BUY" and paper_size == 0:
            if is_synthetic:
                try:
                    ce_info, pe_info = scrip_master.get_atm_options(
                        name, exchange, price, strike_step, expiry_date
                    )
                    ce_ltp, pe_ltp = _fetch_option_ltps_safe(ce_info, pe_info)
                    paper_trading.open_position(
                        name, "BUY", ce_info["kite_tradingsymbol"], pnl_qty, price,
                        ce_symbol=ce_info["kite_tradingsymbol"],
                        pe_symbol=pe_info["kite_tradingsymbol"],
                        entry_ce_price=ce_ltp,
                        entry_pe_price=pe_ltp,
                        ce_angel_token=ce_info["angel_token"],
                        ce_angel_symbol=ce_info["angel_symbol"],
                        pe_angel_token=pe_info["angel_token"],
                        pe_angel_symbol=pe_info["angel_symbol"],
                    )
                    leg_sym = f"{ce_info['kite_tradingsymbol']}+{pe_info['kite_tradingsymbol']}"
                    trade_log.log_trade(name, "BUY_SYNTHETIC", leg_sym, order_qty, dry_run=True)
                    notifier.notify_paper_open(name, "BUY", ce_info["kite_tradingsymbol"], order_qty, price)
                    logger.info(
                        "%s: [PAPER] SYNTHETIC BUY | strike=%d | CE=%s (%.2f) | PE=%s (%.2f)",
                        name, ce_info["strike"],
                        ce_info["kite_tradingsymbol"], ce_ltp,
                        pe_info["kite_tradingsymbol"], pe_ltp,
                    )
                except Exception as exc:
                    logger.error("%s: Failed to open synthetic paper BUY: %s", name, exc)
            else:
                paper_trading.open_position(name, "BUY", sym, pnl_qty, price)
                trade_log.log_trade(name, "BUY", sym, order_qty, dry_run=True)
                notifier.notify_paper_open(name, "BUY", sym, order_qty, price)
                logger.info("%s: [PAPER] BUY at %.2f qty=%d", name, price, order_qty)

        elif signal == "SELL" and paper_size == 0:
            if is_synthetic:
                try:
                    ce_info, pe_info = scrip_master.get_atm_options(
                        name, exchange, price, strike_step, expiry_date
                    )
                    ce_ltp, pe_ltp = _fetch_option_ltps_safe(ce_info, pe_info)
                    paper_trading.open_position(
                        name, "SELL", pe_info["kite_tradingsymbol"], pnl_qty, price,
                        ce_symbol=ce_info["kite_tradingsymbol"],
                        pe_symbol=pe_info["kite_tradingsymbol"],
                        entry_ce_price=ce_ltp,
                        entry_pe_price=pe_ltp,
                        ce_angel_token=ce_info["angel_token"],
                        ce_angel_symbol=ce_info["angel_symbol"],
                        pe_angel_token=pe_info["angel_token"],
                        pe_angel_symbol=pe_info["angel_symbol"],
                    )
                    leg_sym = f"{ce_info['kite_tradingsymbol']}+{pe_info['kite_tradingsymbol']}"
                    trade_log.log_trade(name, "SELL_SYNTHETIC", leg_sym, order_qty, dry_run=True)
                    notifier.notify_paper_open(name, "SELL", pe_info["kite_tradingsymbol"], order_qty, price)
                    logger.info(
                        "%s: [PAPER] SYNTHETIC SELL | strike=%d | CE=%s (%.2f) | PE=%s (%.2f)",
                        name, pe_info["strike"],
                        ce_info["kite_tradingsymbol"], ce_ltp,
                        pe_info["kite_tradingsymbol"], pe_ltp,
                    )
                except Exception as exc:
                    logger.error("%s: Failed to open synthetic paper SELL: %s", name, exc)
            else:
                paper_trading.open_position(name, "SELL", sym, pnl_qty, price)
                trade_log.log_trade(name, "SELL", sym, order_qty, dry_run=True)
                notifier.notify_paper_open(name, "SELL", sym, order_qty, price)
                logger.info("%s: [PAPER] SELL at %.2f qty=%d", name, price, order_qty)

        elif signal in ("EXIT_LONG", "SELL") and paper_size > 0:
            if is_synthetic:
                paper_pos = paper_trading.get_position(name)
                if paper_pos and paper_pos.get("is_synthetic"):
                    ce_opt = {"kite_tradingsymbol": paper_pos["ce_symbol"], "angel_token": paper_pos["ce_angel_token"], "angel_symbol": paper_pos["ce_angel_symbol"], "angel_exchange": "NFO"}
                    pe_opt = {"kite_tradingsymbol": paper_pos["pe_symbol"], "angel_token": paper_pos["pe_angel_token"], "angel_symbol": paper_pos["pe_angel_symbol"], "angel_exchange": "NFO"}
                    ce_ltp, pe_ltp = _fetch_option_ltps_safe(ce_opt, pe_opt)
                    result = paper_trading.close_position(
                        name, price,
                        exit_ce_price=ce_ltp if ce_ltp else None,
                        exit_pe_price=pe_ltp if pe_ltp else None,
                    )
                else:
                    result = paper_trading.close_position(name, price)
            else:
                result = paper_trading.close_position(name, price)
            trade_log.log_trade(name, "EXIT_LONG", sym, order_qty, dry_run=True)
            notifier.notify_paper_close(result)
            logger.info("%s: [PAPER] EXIT LONG at %.2f | P&L=%.2f", name, price, result["pnl"])

        elif signal in ("EXIT_SHORT", "BUY") and paper_size < 0:
            if is_synthetic:
                paper_pos = paper_trading.get_position(name)
                if paper_pos and paper_pos.get("is_synthetic"):
                    ce_opt = {"kite_tradingsymbol": paper_pos["ce_symbol"], "angel_token": paper_pos["ce_angel_token"], "angel_symbol": paper_pos["ce_angel_symbol"], "angel_exchange": "NFO"}
                    pe_opt = {"kite_tradingsymbol": paper_pos["pe_symbol"], "angel_token": paper_pos["pe_angel_token"], "angel_symbol": paper_pos["pe_angel_symbol"], "angel_exchange": "NFO"}
                    ce_ltp, pe_ltp = _fetch_option_ltps_safe(ce_opt, pe_opt)
                    result = paper_trading.close_position(
                        name, price,
                        exit_ce_price=ce_ltp if ce_ltp else None,
                        exit_pe_price=pe_ltp if pe_ltp else None,
                    )
                else:
                    result = paper_trading.close_position(name, price)
            else:
                result = paper_trading.close_position(name, price)
            trade_log.log_trade(name, "EXIT_SHORT", sym, order_qty, dry_run=True)
            notifier.notify_paper_close(result)
            logger.info("%s: [PAPER] EXIT SHORT at %.2f | P&L=%.2f", name, price, result["pnl"])

        else:
            paper_pos = paper_trading.get_position(name)
            if paper_pos:
                if is_synthetic and paper_pos.get("is_synthetic"):
                    ce_opt = {"kite_tradingsymbol": paper_pos["ce_symbol"], "angel_token": paper_pos["ce_angel_token"], "angel_symbol": paper_pos["ce_angel_symbol"], "angel_exchange": "NFO"}
                    pe_opt = {"kite_tradingsymbol": paper_pos["pe_symbol"], "angel_token": paper_pos["pe_angel_token"], "angel_symbol": paper_pos["pe_angel_symbol"], "angel_exchange": "NFO"}
                    ce_ltp, pe_ltp = _fetch_option_ltps_safe(ce_opt, pe_opt)
                    upnl = paper_trading.get_unrealized_pnl(
                        name, price, ce_ltp=ce_ltp or None, pe_ltp=pe_ltp or None
                    )
                else:
                    upnl = paper_trading.get_unrealized_pnl(name, price)
                logger.info(
                    "%s: [PAPER] holding %s | close=%.2f  entry=%.2f  unrealised P&L=%.2f",
                    name,
                    "LONG" if paper_size > 0 else "SHORT",
                    price, paper_pos["entry_price"], upnl,
                )
            else:
                logger.info("%s: [PAPER] no action (signal=%s)", name, signal)
        return

    # ── Live trading ────────────────────────────────────────────────────────────

    if signal == "BUY" and pos == 0:
        if is_synthetic:
            try:
                ce_info, pe_info = scrip_master.get_atm_options(
                    name, exchange, price, strike_step, expiry_date
                )
                ce_oid, pe_oid, ce_ltp, pe_ltp = place_synthetic_buy(
                    kite, instrument, ce_info, pe_info
                )
                leg_sym = f"{ce_info['kite_tradingsymbol']}+{pe_info['kite_tradingsymbol']}"
                trade_log.log_trade(name, "BUY_SYNTHETIC", leg_sym, order_qty)
                notifier.notify_trade(name, "BUY", ce_info["kite_tradingsymbol"], order_qty, price)
                set_position(
                    state, name, instrument["qty"], close_price,
                    ce_info["kite_tradingsymbol"], exchange,
                    is_synthetic=True,
                    ce_tradingsymbol=ce_info["kite_tradingsymbol"],
                    pe_tradingsymbol=pe_info["kite_tradingsymbol"],
                    entry_ce_price=ce_ltp,
                    entry_pe_price=pe_ltp,
                )
            except RuntimeError as exc:
                logger.error("%s: Synthetic BUY failed: %s", name, exc, exc_info=True)
                notifier.notify_synthetic_partial_fill(
                    name, str(exc).split("CE placed")[-1][:60], "", str(exc)
                )
        else:
            place_buy(kite, instrument)
            trade_log.log_trade(name, "BUY", sym, order_qty)
            notifier.notify_trade(name, "BUY", sym, order_qty, price)
            set_position(state, name, instrument["qty"], close_price, sym, exchange)

    elif signal == "SELL" and pos == 0:
        if is_synthetic:
            try:
                ce_info, pe_info = scrip_master.get_atm_options(
                    name, exchange, price, strike_step, expiry_date
                )
                ce_oid, pe_oid, ce_ltp, pe_ltp = place_synthetic_sell(
                    kite, instrument, ce_info, pe_info
                )
                leg_sym = f"{ce_info['kite_tradingsymbol']}+{pe_info['kite_tradingsymbol']}"
                trade_log.log_trade(name, "SELL_SYNTHETIC", leg_sym, order_qty)
                notifier.notify_trade(name, "SELL", pe_info["kite_tradingsymbol"], order_qty, price)
                set_position(
                    state, name, -instrument["qty"], close_price,
                    pe_info["kite_tradingsymbol"], exchange,
                    is_synthetic=True,
                    ce_tradingsymbol=ce_info["kite_tradingsymbol"],
                    pe_tradingsymbol=pe_info["kite_tradingsymbol"],
                    entry_ce_price=ce_ltp,
                    entry_pe_price=pe_ltp,
                )
            except RuntimeError as exc:
                logger.error("%s: Synthetic SELL failed: %s", name, exc, exc_info=True)
                notifier.notify_synthetic_partial_fill(
                    name, str(exc).split("PE placed")[-1][:60], "", str(exc)
                )
        else:
            place_sell(kite, instrument)
            trade_log.log_trade(name, "SELL", sym, order_qty)
            notifier.notify_trade(name, "SELL", sym, order_qty, price)
            set_position(state, name, -instrument["qty"], close_price, sym, exchange)

    # A strong SELL signal (all three bearish) while long is still an exit.
    # np.select returns "SELL" before "EXIT_LONG" when sell_cond is True,
    # so we must handle both signals here to avoid missing the exit.
    elif signal in ("EXIT_LONG", "SELL") and pos > 0:
        if signal == "SELL":
            logger.warning("%s: SELL fired while long — treating as EXIT_LONG", name)
        if is_synthetic:
            saved   = state.get(name, {})
            ce_sym  = saved.get("ce_tradingsymbol", "")
            pe_sym  = saved.get("pe_tradingsymbol", "")
            try:
                ce_oid, pe_oid, exit_ce_ltp, exit_pe_ltp = close_synthetic_long(
                    kite, instrument, ce_sym, pe_sym
                )
                trade_log.log_trade(name, "EXIT_LONG", f"{ce_sym}+{pe_sym}", order_qty)
                notifier.notify_trade(name, "EXIT_LONG", ce_sym, order_qty, price)
                set_position(state, name, 0)
            except RuntimeError as exc:
                logger.error("%s: Synthetic EXIT LONG failed: %s", name, exc, exc_info=True)
                notifier.notify_synthetic_partial_fill(name, ce_sym, pe_sym, str(exc))
        else:
            close_long(kite, instrument)
            trade_log.log_trade(name, "EXIT_LONG", sym, order_qty)
            notifier.notify_trade(name, "EXIT_LONG", sym, order_qty, price)
            set_position(state, name, 0)

    # Symmetric: strong BUY while short is still an exit.
    elif signal in ("EXIT_SHORT", "BUY") and pos < 0:
        if signal == "BUY":
            logger.warning("%s: BUY fired while short — treating as EXIT_SHORT", name)
        if is_synthetic:
            saved   = state.get(name, {})
            ce_sym  = saved.get("ce_tradingsymbol", "")
            pe_sym  = saved.get("pe_tradingsymbol", "")
            try:
                ce_oid, pe_oid, exit_ce_ltp, exit_pe_ltp = close_synthetic_short(
                    kite, instrument, ce_sym, pe_sym
                )
                trade_log.log_trade(name, "EXIT_SHORT", f"{ce_sym}+{pe_sym}", order_qty)
                notifier.notify_trade(name, "EXIT_SHORT", pe_sym, order_qty, price)
                set_position(state, name, 0)
            except RuntimeError as exc:
                logger.error("%s: Synthetic EXIT SHORT failed: %s", name, exc, exc_info=True)
                notifier.notify_synthetic_partial_fill(name, pe_sym, ce_sym, str(exc))
        else:
            close_short(kite, instrument)
            trade_log.log_trade(name, "EXIT_SHORT", sym, order_qty)
            notifier.notify_trade(name, "EXIT_SHORT", sym, order_qty, price)
            set_position(state, name, 0)

    else:
        logger.info("%s: no action (signal=%s, pos=%d)", name, signal, pos)


# ── Scheduler ─────────────────────────────────────────────────────────────────

def _candle_cron() -> dict:
    """APScheduler cron kwargs that fire 1s after each candle close."""
    interval = config.CANDLE_INTERVAL.upper()
    if "ONE_MINUTE"   in interval: return dict(minute="*",          second=1)
    if "THREE"        in interval: return dict(minute="*/3",        second=1)
    if "FIVE"         in interval: return dict(minute="*/5",        second=1)
    if "TEN"          in interval: return dict(minute="*/10",       second=1)
    if "FIFTEEN"      in interval: return dict(minute="0,15,30,45", second=1)
    if "THIRTY"       in interval: return dict(minute="0,30",       second=1)
    if "ONE_HOUR"     in interval: return dict(minute=1, second=1)
    if "ONE_DAY"      in interval: return dict(hour=15, minute=31)
    return dict(minute="0,15,30,45", second=1)


def _parse_time(hhmm: str):
    h, m = map(int, hhmm.split(":"))
    return datetime.now(IST).replace(hour=h, minute=m, second=0, microsecond=0).time()


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Supertrend + MA Algo")
    parser.add_argument("--run-once", action="store_true",
                        help="Run the strategy once immediately then exit (no web server)")
    args = parser.parse_args()

    DRY_RUN = config.DRY_RUN
    if DRY_RUN:
        logger.info("*** DRY RUN MODE (set in .env) — paper trading only, no real orders ***")

    initialise()
    _check_rollover()
    notifier.notify_strategy_start(RESOLVED_INSTRUMENTS, startup=True, dry_run=config.DRY_RUN)

    if args.run_once:
        run_strategy()
        sys.exit(0)

    # ── Normal mode: scheduler (background) + web server (main thread) ─────────
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    cron_kwargs = _candle_cron()
    scheduler   = BackgroundScheduler(timezone=IST)
    scheduler.add_job(run_strategy, CronTrigger(timezone=IST, **cron_kwargs))

    # ── Daily Telegram notifications ───────────────────────────────────────────
    # 08:30 — login reminder with auth server link
    scheduler.add_job(
        notifier.notify_login_reminder,
        CronTrigger(timezone=IST, hour=8, minute=30),
    )
    # 09:00 — token status + active strategies
    scheduler.add_job(
        lambda: notifier.notify_strategy_start(RESOLVED_INSTRUMENTS, dry_run=config.DRY_RUN),
        CronTrigger(timezone=IST, hour=9, minute=0),
    )

    scheduler.start()
    web_state.set_scheduler_running(True)
    logger.info("Scheduler started. Interval=%s  Cron=%s", config.CANDLE_INTERVAL, cron_kwargs)

    # Waitress (production WSGI server) runs in main thread — blocks until Ctrl+C.
    # Single process keeps the APScheduler thread and Flask in the same memory space
    # so web_state shared state works without IPC.
    from waitress import serve
    from webapp import app
    port = config.KITE_AUTH_PORT
    logger.info("Web server starting on http://0.0.0.0:%d", port)
    logger.info("  Dashboard : http://localhost:%d/", port)
    logger.info("  Kite login: http://localhost:%d/kite/login", port)
    logger.info("  Redirect URL for Kite app: http://<VPS-IP>:%d/callback", port)
    try:
        serve(app, host="0.0.0.0", port=port, threads=4)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown()
        web_state.set_scheduler_running(False)
        logger.info("Shutdown complete.")
