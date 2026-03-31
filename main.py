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
from datetime import datetime, timedelta

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
from kiteconnect.exceptions import InputException
from order_manager import (
    place_buy, place_sell, close_long, close_short,
    place_synthetic_buy, place_synthetic_sell,
    close_synthetic_long, close_synthetic_short,
    place_short_ce, close_short_ce,
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
RESOLVED_HOURLY_INSTRUMENTS: list[dict] = []
DRY_RUN: bool = False


# ── Initialisation ─────────────────────────────────────────────────────────────

def initialise():
    """
    Run once at startup:
    - Refresh scrip masters (downloads if not yet cached today).
    - Resolve each config instrument → fills in live tokens and symbols.
    - Build hourly instrument variants by inheriting tokens from base instruments.
    - Log the active contracts so you can verify expiry dates.
    """
    global RESOLVED_INSTRUMENTS, RESOLVED_HOURLY_INSTRUMENTS

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

    # Build hourly variants by inheriting resolved tokens/symbols from base instruments.
    # No additional scrip-master lookup needed — same contract, different candle interval.
    RESOLVED_HOURLY_INSTRUMENTS = []
    for h_def in config.HOURLY_INSTRUMENTS:
        underlying = h_def["underlying"]
        base = next((i for i in RESOLVED_INSTRUMENTS if i["name"] == underlying), None)
        if base is None:
            logger.error(
                "Hourly instrument %s: base instrument %s not resolved — skipping",
                h_def["name"], underlying,
            )
            continue
        resolved = {**base, **h_def}   # inherit tokens; h_def overrides name/timeframe/etc.
        RESOLVED_HOURLY_INSTRUMENTS.append(resolved)
        logger.info(
            "  %-12s | 1H variant of %-10s | kite=%-22s | expiry=%s",
            resolved["name"], underlying,
            resolved["kite_tradingsymbol"], resolved["expiry"],
        )

    # Resolve spot index tokens for SYNTHETIC instruments (used for indicator candles)
    all_resolved = RESOLVED_INSTRUMENTS + RESOLVED_HOURLY_INSTRUMENTS
    for inst in all_resolved:
        spot_name = inst.get("spot_index_name")
        if inst.get("mode") == "SYNTHETIC" and spot_name:
            try:
                spot = scrip_master.get_spot_index(spot_name)
                inst["spot_angel_token"]    = spot["angel_token"]
                inst["spot_angel_exchange"] = spot["angel_exchange"]
                inst["spot_angel_symbol"]   = spot["angel_symbol"]
                logger.info(
                    "  %-12s | spot index: %s (token=%s, exchange=%s)",
                    inst["name"], spot["angel_symbol"],
                    spot["angel_token"], spot["angel_exchange"],
                )
            except Exception as exc:
                logger.warning(
                    "Could not resolve spot index '%s' for %s: %s — "
                    "will use futures prices for indicators",
                    spot_name, inst["name"], exc,
                )

    web_state.set_resolved_instruments(RESOLVED_INSTRUMENTS + RESOLVED_HOURLY_INSTRUMENTS)


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
    for inst in RESOLVED_INSTRUMENTS + RESOLVED_HOURLY_INSTRUMENTS:
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


def _is_tender_period_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "tender period" in msg or "physical delivery" in msg


def _next_month_instrument(instrument: dict) -> dict:
    """
    Return an instrument dict updated to the second-nearest futures contract.
    Used when the nearest contract is blocked due to entering tender period.
    The caller is responsible for saving the new kite_tradingsymbol to state.
    """
    contracts = scrip_master.get_all_futures(instrument["name"], instrument["exchange"])
    if len(contracts) < 2:
        raise RuntimeError(
            f"No next-month contract available for {instrument['name']} "
            f"on {instrument['exchange']}"
        )
    next_contract = contracts[1]  # second nearest, sorted by expiry
    logger.warning(
        "%s: tender-period — switching from %s to next-month %s",
        instrument["name"], instrument["kite_tradingsymbol"],
        next_contract["kite_tradingsymbol"],
    )
    return {**instrument, **next_contract}


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


def _fetch_ce_ltp_safe(ce_opt: dict) -> float:
    """Fetch live LTP for a single CE option via Angel. Returns 0.0 on failure."""
    try:
        results = get_option_ltps([ce_opt])
        return results.get(ce_opt["kite_tradingsymbol"], 0.0)
    except Exception as exc:
        logger.warning("Failed to fetch CE LTP (%s): %s",
                       ce_opt.get("kite_tradingsymbol"), exc)
        return 0.0


def _find_ce_for_short(
    name: str, exchange: str, expiry_date, spot_price: float,
    strike_step: int, target_premium: float,
) -> tuple[dict, float]:
    """
    Find the monthly CE option whose live LTP is closest to target_premium.

    Searches CE options within ±25 strikes of spot_price for the given expiry.
    Returns (ce_info_dict, ltp).  Raises ValueError if no valid LTP is found.
    """
    ce_options = scrip_master.get_ce_options_for_expiry(
        name, exchange, expiry_date, spot_price, strike_step
    )
    if not ce_options:
        raise ValueError(f"No CE options found for {name} expiry={expiry_date}")

    ltps = get_option_ltps(ce_options)

    best_ce  = None
    best_ltp = 0.0
    best_diff = float("inf")
    for ce in ce_options:
        ltp = ltps.get(ce["kite_tradingsymbol"], 0.0)
        if ltp <= 0:
            continue
        diff = abs(ltp - target_premium)
        if diff < best_diff:
            best_diff = diff
            best_ce   = ce
            best_ltp  = ltp

    if best_ce is None:
        raise ValueError(
            f"Could not fetch any CE LTP for {name} — "
            "check Angel tokens in options master"
        )

    logger.info(
        "%s: selected CE for short | strike=%d | LTP=%.2f | target=%.2f | diff=%.2f",
        name, best_ce["strike"], best_ltp, target_premium, best_diff,
    )
    return best_ce, best_ltp


def _get_spot_instrument(instrument: dict) -> dict:
    """
    Return a modified instrument dict that uses the NSE spot index token
    instead of the futures token, so that Angel candle calls return SPOT prices.
    Only applies when the instrument has spot_angel_token resolved at startup.
    """
    if instrument.get("spot_angel_token"):
        return {
            **instrument,
            "angel_token":    instrument["spot_angel_token"],
            "angel_exchange": instrument["spot_angel_exchange"],
            "angel_symbol":   instrument.get("spot_angel_symbol", ""),
        }
    return instrument


def _process_instrument(kite, state: dict, instrument: dict, now_ist):
    name         = instrument["name"]
    trade_start  = _parse_time(instrument["trade_start"])
    trade_end    = _parse_time(instrument["trade_end"])
    current_time = now_ist.time()
    timeframe    = instrument["timeframe"]
    long_only    = instrument.get("long_only", False)
    interval_min = _candle_interval_minutes(timeframe)

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

    # ── First-candle-close gate ────────────────────────────────────────────────
    # The scheduler fires 1 s after each candle boundary. The very first candle
    # of the session does not close until trade_start + one interval.
    #   MCX opens 09:00 → first 15-min candle closes 09:15 → first calc 09:15 ✓
    #   NSE opens 09:15 → first 15-min candle closes 09:30 → first calc 09:30
    #     (without this guard NSE would incorrectly calculate at 09:15:01 while
    #      the opening candle is still forming)
    first_calc_time = (
        datetime.combine(now_ist.date(), trade_start)
        + timedelta(minutes=interval_min)
    ).time()
    if current_time < first_calc_time:
        logger.info(
            "%s: first candle not yet closed (market opens %s, first calc at %s) — skipping",
            name, instrument["trade_start"], first_calc_time.strftime("%H:%M"),
        )
        return

    # 1. Fetch OHLCV candles from Angel.
    # For NIFTY/BANKNIFTY (SYNTHETIC mode) use the NSE spot index so that
    # indicators are calculated on SPOT prices, not futures prices.
    data_instrument = _get_spot_instrument(instrument)
    df = get_candles(data_instrument, interval=timeframe)
    if len(df) < config.MA_LENGTH + 10:
        logger.warning("%s: not enough candles (%d). Skipping.", name, len(df))
        return

    # ── Market-open guard — latest candle must be from today (IST) ────────────
    # Angel returns naive IST timestamps. On a market holiday or weekend the API
    # continues to serve the last available session's candles (from yesterday or
    # earlier). Comparing dates is the simplest, foolproof way to detect this:
    # if the newest candle is not from today the exchange is closed — do nothing.
    latest_candle_date = df.index[-1].date()   # naive IST datetime → IST date
    if latest_candle_date != now_ist.date():
        logger.info(
            "%s: latest candle is from %s but today is %s "
            "— market closed (holiday/weekend), skipping",
            name,
            df.index[-1].strftime("%Y-%m-%d"),
            now_ist.strftime("%Y-%m-%d"),
        )
        return

    # 2. Compute indicators; use iloc[-1] = last CLOSED candle
    # get_candles() already strips any forming candle, so iloc[-1] is always
    # the most recent fully-closed bar regardless of Angel API behaviour.
    df_sig = compute_signals(
        df,
        st1_period = config.ST1_PERIOD,
        st1_factor = config.ST1_FACTOR,
        st2_period = config.ST2_PERIOD,
        st2_factor = config.ST2_FACTOR,
        ma_length  = config.MA_LENGTH,
    )

    last        = df_sig.iloc[-1]
    signal      = last["signal"]
    close_price = last["close"]
    pos         = get_position(state, name)

    # candle_ts is the OPEN time of the bar (Angel convention).
    # Close time = candle_ts + interval (e.g. 17:00 candle closes at 17:15).
    candle_ts = df_sig.index[-1]

    logger.info(
        "%s | kite=%-20s | candle=%s | close=%.2f  st1=%.2f  st2=%.2f  ma=%.2f | signal=%-12s pos=%d",
        name, instrument["kite_tradingsymbol"],
        candle_ts.strftime("%Y-%m-%d %H:%M"),
        close_price, last["st1"], last["st2"], last["ma"], signal, pos,
    )

    # 3. Push latest snapshot to the web dashboard
    web_state.update_instrument(name, {
        "kite_tradingsymbol": instrument["kite_tradingsymbol"],
        "interval":           timeframe,
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
                    if ce_ltp == 0.0 or pe_ltp == 0.0:
                        logger.warning(
                            "%s: [PAPER] SYNTHETIC BUY skipped — could not fetch option LTPs "
                            "(CE=%.2f PE=%.2f). P&L would be unreliable.",
                            name, ce_ltp, pe_ltp,
                        )
                        raise RuntimeError(f"Option LTPs unavailable for {name} synthetic BUY")
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
            if long_only:
                logger.info("%s: [PAPER] SELL signal ignored (long-only strategy)", name)
            elif is_synthetic:
                # SHORT = SELL monthly CE with premium closest to target
                try:
                    target_premium = instrument.get("short_ce_target_premium", 300)
                    ce_info, ce_ltp = _find_ce_for_short(
                        name, exchange, expiry_date, price, strike_step, target_premium
                    )
                    if ce_ltp == 0.0:
                        raise RuntimeError(
                            f"CE LTP unavailable for {name} short CE — P&L would be unreliable"
                        )
                    paper_trading.open_position(
                        name, "SELL", ce_info["kite_tradingsymbol"], pnl_qty, price,
                        ce_symbol=ce_info["kite_tradingsymbol"],
                        entry_ce_price=ce_ltp,
                        ce_angel_token=ce_info["angel_token"],
                        ce_angel_symbol=ce_info["angel_symbol"],
                        is_short_ce=True,
                    )
                    trade_log.log_trade(name, "SELL_CE", ce_info["kite_tradingsymbol"], order_qty, dry_run=True)
                    notifier.notify_paper_open(name, "SELL", ce_info["kite_tradingsymbol"], order_qty, price)
                    logger.info(
                        "%s: [PAPER] SHORT CE | strike=%d | CE=%s (%.2f) | target=%.0f",
                        name, ce_info["strike"],
                        ce_info["kite_tradingsymbol"], ce_ltp, target_premium,
                    )
                except Exception as exc:
                    logger.error("%s: Failed to open short CE paper SELL: %s", name, exc)
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
                if paper_pos and paper_pos.get("is_short_ce"):
                    # Close short CE: fetch CE LTP for accurate P&L
                    ce_opt = {"kite_tradingsymbol": paper_pos["ce_symbol"], "angel_token": paper_pos["ce_angel_token"], "angel_symbol": paper_pos["ce_angel_symbol"], "angel_exchange": "NFO"}
                    ce_ltp = _fetch_ce_ltp_safe(ce_opt)
                    result = paper_trading.close_position(
                        name, price,
                        exit_ce_price=ce_ltp if ce_ltp else None,
                    )
                elif paper_pos and paper_pos.get("is_synthetic"):
                    # Close old 2-leg synthetic short (backward compat)
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
                if is_synthetic and paper_pos.get("is_short_ce"):
                    ce_opt = {"kite_tradingsymbol": paper_pos["ce_symbol"], "angel_token": paper_pos["ce_angel_token"], "angel_symbol": paper_pos["ce_angel_symbol"], "angel_exchange": "NFO"}
                    ce_ltp = _fetch_ce_ltp_safe(ce_opt)
                    upnl = paper_trading.get_unrealized_pnl(name, price, ce_ltp=ce_ltp or None)
                elif is_synthetic and paper_pos.get("is_synthetic"):
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
            entry_sym, entry_exchange = sym, exchange
            try:
                place_buy(kite, instrument)
            except InputException as exc:
                if not _is_tender_period_error(exc):
                    raise
                next_inst = _next_month_instrument(instrument)
                place_buy(kite, next_inst)
                entry_sym      = next_inst["kite_tradingsymbol"]
                entry_exchange = next_inst["exchange"]
            trade_log.log_trade(name, "BUY", entry_sym, order_qty)
            notifier.notify_trade(name, "BUY", entry_sym, order_qty, price)
            set_position(state, name, instrument["qty"], close_price, entry_sym, entry_exchange)

    elif signal == "SELL" and pos == 0:
        if long_only:
            logger.info("%s: SELL signal ignored (long-only strategy)", name)
        elif is_synthetic:
            # SHORT = SELL monthly CE with premium closest to target
            try:
                target_premium = instrument.get("short_ce_target_premium", 300)
                ce_info, ce_ltp = _find_ce_for_short(
                    name, exchange, expiry_date, price, strike_step, target_premium
                )
                ce_oid, entry_ce_ltp = place_short_ce(kite, instrument, ce_info)
                trade_log.log_trade(name, "SELL_CE", ce_info["kite_tradingsymbol"], order_qty)
                notifier.notify_trade(name, "SELL", ce_info["kite_tradingsymbol"], order_qty, price)
                set_position(
                    state, name, -instrument["qty"], close_price,
                    ce_info["kite_tradingsymbol"], exchange,
                    is_short_ce=True,
                    ce_tradingsymbol=ce_info["kite_tradingsymbol"],
                    entry_ce_price=entry_ce_ltp or ce_ltp,
                )
            except Exception as exc:
                logger.error("%s: Short CE SELL failed: %s", name, exc, exc_info=True)
                notifier.notify_synthetic_partial_fill(name, "", "", str(exc))
        else:
            entry_sym, entry_exchange = sym, exchange
            try:
                place_sell(kite, instrument)
            except InputException as exc:
                if not _is_tender_period_error(exc):
                    raise
                next_inst = _next_month_instrument(instrument)
                place_sell(kite, next_inst)
                entry_sym      = next_inst["kite_tradingsymbol"]
                entry_exchange = next_inst["exchange"]
            trade_log.log_trade(name, "SELL", entry_sym, order_qty)
            notifier.notify_trade(name, "SELL", entry_sym, order_qty, price)
            set_position(state, name, -instrument["qty"], close_price, entry_sym, entry_exchange)

    # SELL while long: exit the long, then also enter short (gap-down / signal flip).
    # EXIT_LONG: exit only.
    elif signal in ("EXIT_LONG", "SELL") and pos > 0:
        if signal == "SELL":
            logger.warning("%s: SELL fired while long — exiting long then entering short", name)
        _exit_ok = False
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
                _exit_ok = True
            except RuntimeError as exc:
                logger.error("%s: Synthetic EXIT LONG failed: %s", name, exc, exc_info=True)
                notifier.notify_synthetic_partial_fill(name, ce_sym, pe_sym, str(exc))
            if signal == "SELL" and _exit_ok and not long_only:
                try:
                    target_premium = instrument.get("short_ce_target_premium", 300)
                    ce_info, ce_ltp = _find_ce_for_short(
                        name, exchange, expiry_date, price, strike_step, target_premium
                    )
                    ce_oid, entry_ce_ltp = place_short_ce(kite, instrument, ce_info)
                    trade_log.log_trade(name, "SELL_CE", ce_info["kite_tradingsymbol"], order_qty)
                    notifier.notify_trade(name, "SELL", ce_info["kite_tradingsymbol"], order_qty, price)
                    set_position(
                        state, name, -instrument["qty"], close_price,
                        ce_info["kite_tradingsymbol"], exchange,
                        is_short_ce=True,
                        ce_tradingsymbol=ce_info["kite_tradingsymbol"],
                        entry_ce_price=entry_ce_ltp or ce_ltp,
                    )
                except Exception as exc:
                    logger.error("%s: Short CE SELL after exit-long failed: %s", name, exc, exc_info=True)
                    notifier.notify_synthetic_partial_fill(name, "", "", str(exc))
        else:
            # Use symbol stored in state at entry time — may differ from resolved
            # instrument if a tender-period next-month switch occurred at entry
            exit_sym = state.get(name, {}).get("kite_tradingsymbol") or sym
            if exit_sym != sym:
                logger.info("%s: EXIT LONG using state symbol %s (resolved=%s)", name, exit_sym, sym)
            close_long(kite, {**instrument, "kite_tradingsymbol": exit_sym})
            trade_log.log_trade(name, "EXIT_LONG", exit_sym, order_qty)
            notifier.notify_trade(name, "EXIT_LONG", exit_sym, order_qty, price)
            set_position(state, name, 0)
            if signal == "SELL" and not long_only:
                entry_sym, entry_exchange = sym, exchange
                try:
                    place_sell(kite, instrument)
                except InputException as exc:
                    if not _is_tender_period_error(exc):
                        raise
                    next_inst = _next_month_instrument(instrument)
                    place_sell(kite, next_inst)
                    entry_sym      = next_inst["kite_tradingsymbol"]
                    entry_exchange = next_inst["exchange"]
                trade_log.log_trade(name, "SELL", entry_sym, order_qty)
                notifier.notify_trade(name, "SELL", entry_sym, order_qty, price)
                set_position(state, name, -instrument["qty"], close_price, entry_sym, entry_exchange)

    # BUY while short: exit the short, then also enter long (gap-up / signal flip).
    # EXIT_SHORT: exit only.
    elif signal in ("EXIT_SHORT", "BUY") and pos < 0:
        if signal == "BUY":
            logger.warning("%s: BUY fired while short — exiting short then entering long", name)
        _exit_ok = False
        if is_synthetic:
            saved = state.get(name, {})
            if saved.get("is_short_ce"):
                # Close short CE: buy back the sold call
                ce_sym = saved.get("ce_tradingsymbol", "")
                try:
                    ce_oid, exit_ce_ltp = close_short_ce(kite, instrument, ce_sym)
                    trade_log.log_trade(name, "EXIT_SHORT_CE", ce_sym, order_qty)
                    notifier.notify_trade(name, "EXIT_SHORT", ce_sym, order_qty, price)
                    set_position(state, name, 0)
                    _exit_ok = True
                except RuntimeError as exc:
                    logger.error("%s: Short CE BUY-back failed: %s", name, exc, exc_info=True)
                    notifier.notify_synthetic_partial_fill(name, ce_sym, "", str(exc))
            else:
                # Backward compat: close old 2-leg synthetic short
                ce_sym = saved.get("ce_tradingsymbol", "")
                pe_sym = saved.get("pe_tradingsymbol", "")
                try:
                    ce_oid, pe_oid, exit_ce_ltp, exit_pe_ltp = close_synthetic_short(
                        kite, instrument, ce_sym, pe_sym
                    )
                    trade_log.log_trade(name, "EXIT_SHORT", f"{ce_sym}+{pe_sym}", order_qty)
                    notifier.notify_trade(name, "EXIT_SHORT", pe_sym, order_qty, price)
                    set_position(state, name, 0)
                    _exit_ok = True
                except RuntimeError as exc:
                    logger.error("%s: Synthetic EXIT SHORT failed: %s", name, exc, exc_info=True)
                    notifier.notify_synthetic_partial_fill(name, pe_sym, ce_sym, str(exc))
            if signal == "BUY" and _exit_ok:
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
                    logger.error("%s: Synthetic BUY after exit-short failed: %s", name, exc, exc_info=True)
                    notifier.notify_synthetic_partial_fill(
                        name, str(exc).split("CE placed")[-1][:60], "", str(exc)
                    )
        else:
            # Use symbol stored in state at entry time — may differ from resolved
            # instrument if a tender-period next-month switch occurred at entry
            exit_sym = state.get(name, {}).get("kite_tradingsymbol") or sym
            if exit_sym != sym:
                logger.info("%s: EXIT SHORT using state symbol %s (resolved=%s)", name, exit_sym, sym)
            close_short(kite, {**instrument, "kite_tradingsymbol": exit_sym})
            trade_log.log_trade(name, "EXIT_SHORT", exit_sym, order_qty)
            notifier.notify_trade(name, "EXIT_SHORT", exit_sym, order_qty, price)
            set_position(state, name, 0)
            if signal == "BUY":
                entry_sym, entry_exchange = sym, exchange
                try:
                    place_buy(kite, instrument)
                except InputException as exc:
                    if not _is_tender_period_error(exc):
                        raise
                    next_inst = _next_month_instrument(instrument)
                    place_buy(kite, next_inst)
                    entry_sym      = next_inst["kite_tradingsymbol"]
                    entry_exchange = next_inst["exchange"]
                trade_log.log_trade(name, "BUY", entry_sym, order_qty)
                notifier.notify_trade(name, "BUY", entry_sym, order_qty, price)
                set_position(state, name, instrument["qty"], close_price, entry_sym, entry_exchange)

    else:
        logger.info("%s: no action (signal=%s, pos=%d)", name, signal, pos)


def run_hourly_strategy():
    """Called at every hourly candle close (HH:00:05 IST). Processes 1H instruments."""
    web_state.record_run()

    now_ist = datetime.now(IST)
    logger.info("=" * 64)
    logger.info("Hourly strategy run at %s IST", now_ist.strftime("%Y-%m-%d %H:%M:%S"))

    kite = None
    try:
        kite = get_kite_session()
    except RuntimeError as exc:
        logger.warning("Kite session unavailable (%s) — signal calc continues, orders disabled", exc)

    state = load_state()

    for instrument in RESOLVED_HOURLY_INSTRUMENTS:
        try:
            _process_instrument(kite, state, instrument, now_ist)
        except Exception as exc:
            logger.error("Error processing %s: %s", instrument["name"], exc, exc_info=True)


# ── Scheduler ─────────────────────────────────────────────────────────────────

# Minutes per bar for each Angel interval string (mirrors angel_data._interval_minutes)
_INTERVAL_MINUTES: dict[str, int] = {
    "ONE_MINUTE":     1,
    "THREE_MINUTE":   3,
    "FIVE_MINUTE":    5,
    "TEN_MINUTE":     10,
    "FIFTEEN_MINUTE": 15,
    "THIRTY_MINUTE":  30,
    "ONE_HOUR":       60,
    "ONE_DAY":        1440,
}

# Short aliases (Kite-style) → normalised Angel string
_INTERVAL_ALIAS: dict[str, str] = {
    "1minute": "ONE_MINUTE", "3minute": "THREE_MINUTE", "5minute": "FIVE_MINUTE",
    "10minute": "TEN_MINUTE", "15minute": "FIFTEEN_MINUTE", "30minute": "THIRTY_MINUTE",
    "60minute": "ONE_HOUR", "day": "ONE_DAY",
}


def _candle_interval_minutes(interval: str) -> int:
    """Return the candle interval in whole minutes."""
    normalised = _INTERVAL_ALIAS.get(interval, interval)
    return _INTERVAL_MINUTES.get(normalised, 15)


def _candle_cron() -> dict:
    """APScheduler cron kwargs that fire 1s after each 15-minute candle close."""
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
    scheduler.add_job(run_hourly_strategy, CronTrigger(timezone=IST, minute=0, second=5))
    logger.info("Hourly scheduler added (fires at HH:00:05 IST)")

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
    logger.info("Scheduler started. Cron=%s", cron_kwargs)

    # Waitress (production WSGI server) runs in main thread — blocks until Ctrl+C.
    # Single process keeps the APScheduler thread and Flask in the same memory space
    # so web_state shared state works without IPC.
    from waitress import serve
    from webapp import app
    port = config.KITE_AUTH_PORT
    logger.info("Web server starting on port %d", port)
    try:
        serve(app, host="0.0.0.0", port=port, threads=4)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown()
        web_state.set_scheduler_running(False)
        logger.info("Shutdown complete.")
