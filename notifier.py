"""
Telegram notification module for SimpleAlgo.

All public functions are fire-and-forget: they log errors but never raise,
so a Telegram failure never disrupts the algo.

Configure in .env:
  TELEGRAM_BOT_TOKEN=<your bot token>
  TELEGRAM_CHAT_ID=<your chat id>
  SERVER_BASE_URL=http://<vps-ip>:8880   (used in login reminder link)
"""
import json
import logging
import threading
from datetime import date, datetime
from pathlib import Path

import requests
import pytz

import config

logger = logging.getLogger(__name__)
IST    = pytz.timezone("Asia/Kolkata")


# ── Core send ──────────────────────────────────────────────────────────────────

def _send_blocking(text: str) -> bool:
    """Send an HTML-formatted message to the configured Telegram chat (blocking)."""
    token   = (config.TELEGRAM_BOT_TOKEN or "").strip()
    chat_id = (config.TELEGRAM_CHAT_ID   or "").strip()

    if not token or not chat_id:
        logger.debug("Telegram not configured — skipping notification")
        return False

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={
                "chat_id":                  chat_id,
                "text":                     text,
                "parse_mode":               "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return True
        logger.warning("Telegram API %d: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)
        return False


def _send(text: str) -> None:
    """Fire-and-forget: submit the Telegram send to a background daemon thread."""
    t = threading.Thread(target=_send_blocking, args=(text,), daemon=True)
    t.start()


# ── Scheduled notifications ────────────────────────────────────────────────────

def _is_weekend() -> bool:
    """Return True if today is Saturday or Sunday."""
    return date.today().weekday() >= 5  # 5=Saturday, 6=Sunday


def _kite_logged_in() -> bool:
    """Return True if a valid Kite token for today is already cached."""
    try:
        data = json.loads(Path(config.KITE_TOKEN_FILE).read_text())
        return data.get("date") == str(date.today()) and bool(data.get("access_token"))
    except Exception:
        return False


def notify_kite_auto_login(success: bool, error: str = "") -> None:
    """
    Result of the 8:00 AM automated Kite login attempt.
    On failure includes the manual login link so the user can act immediately.
    """
    if success:
        _send(
            f"✅ <b>Kite Auto-Login Successful</b>\n\n"
            f"📅 {date.today().strftime('%d %b %Y')}\n"
            f"⏰ Logged in automatically at 08:00 IST\n"
            f"🔑 Token saved — algo is ready to trade."
        )
        logger.info("Telegram: Kite auto-login success notification sent")
    else:
        base      = (config.SERVER_BASE_URL or "").rstrip("/")
        login_url = f"{base}/kite/login" if base else f"http://your-vps-ip:{config.KITE_AUTH_PORT}/kite/login"
        err_line  = f"\n⚠️ Error: {error}" if error else ""
        _send(
            f"❌ <b>Kite Auto-Login Failed</b>\n\n"
            f"📅 {date.today().strftime('%d %b %Y')}{err_line}\n\n"
            f"Please log in manually before markets open:\n"
            f'🔗 <a href="{login_url}">Open Login Page</a>\n\n'
            f"⏰ Markets open in ~1 hour"
        )
        logger.warning("Telegram: Kite auto-login failure notification sent")


def notify_login_reminder() -> None:
    """
    8:30 AM daily reminder to log in to Kite.
    Skipped on weekends (when TRADING_DAYS_ONLY=true) or if already logged in.
    """
    if config.TRADING_DAYS_ONLY and _is_weekend():
        logger.info("Telegram: login reminder skipped (weekend)")
        return
    if _kite_logged_in():
        logger.info("Telegram: login reminder skipped (already logged in)")
        return

    base      = (config.SERVER_BASE_URL or "").rstrip("/")
    login_url = f"{base}/kite/login" if base else f"http://your-vps-ip:{config.KITE_AUTH_PORT}/kite/login"

    _send(
        f"🔔 <b>SimpleAlgo — Login Reminder</b>\n\n"
        f"Please log in to Zerodha Kite to enable trading today.\n\n"
        f'🔗 <a href="{login_url}">Open Login Page</a>\n\n'
        f"📅 {date.today().strftime('%d %b %Y')}\n"
        f"⏰ Markets open in ~45 minutes"
    )
    logger.info("Telegram: login reminder sent")


def notify_strategy_start(resolved_instruments: list, startup: bool = False,
                          dry_run: bool = False) -> None:
    """
    Status message — trading mode, Kite token validity + active instruments.
    Called at server startup (startup=True) and daily at 9:00 AM.
    The scheduled 9:00 AM call is skipped on weekends when TRADING_DAYS_ONLY=true.
    """
    if not startup and config.TRADING_DAYS_ONLY and _is_weekend():
        logger.info("Telegram: morning status skipped (weekend)")
        return

    import strategy_config as stcfg

    # Check token validity by reading the cache file directly
    tok_valid = False
    try:
        cache = Path(config.KITE_TOKEN_FILE).read_text()
        data = json.loads(cache)
        tok_valid = data.get("date") == str(date.today()) and bool(data.get("access_token"))
    except Exception:
        pass

    tok_line  = "✅ Kite token valid" if tok_valid else "❌ Kite token INVALID — orders disabled"
    mode_line = "🟡 <b>PAPER TRADING MODE</b> — no real orders will be placed" if dry_run \
                else "🟢 <b>LIVE TRADING MODE</b> — real orders active"

    names       = [i["name"] for i in resolved_instruments]
    enabled_map = stcfg.get_all(names)

    lines = []
    for inst in resolved_instruments:
        name    = inst["name"]
        sym     = inst.get("kite_tradingsymbol", "")
        hours   = f"{inst.get('trade_start','?')}–{inst.get('trade_end','?')}"
        enabled = enabled_map.get(name, True)
        status  = "✅" if enabled else "⛔ Disabled"
        lines.append(f"  {status} <b>{name}</b> <code>{sym}</code> {hours}")

    strategies = "\n".join(lines) if lines else "  (none resolved)"

    title = "🔄 <b>SimpleAlgo Restarted</b>" if startup else "🚀 <b>SimpleAlgo — Morning Status</b>"

    _send(
        f"{title}\n\n"
        f"📅 {date.today().strftime('%d %b %Y')}\n"
        f"{mode_line}\n"
        f"🔑 {tok_line}\n\n"
        f"<b>Active Strategies:</b>\n{strategies}"
    )
    logger.info("Telegram: strategy start notification sent")


# ── Rollover warning ──────────────────────────────────────────────────────────

def notify_rollover_warning(name: str, old_sym: str, new_sym: str, pos: int) -> None:
    """
    Alert when a contract has rolled over while a position is still open.
    The algo has halted trading this instrument until the state is corrected.
    """
    direction = "LONG" if pos > 0 else "SHORT"
    _send(
        f"🚨 <b>Contract Rollover — {name}</b>\n\n"
        f"Open {direction} position recorded in <code>{old_sym}</code>\n"
        f"but the live contract is now <code>{new_sym}</code>.\n\n"
        f"⛔ Trading paused for {name} until resolved.\n\n"
        f"<b>Action required:</b>\n"
        f"1. Close the old position in <code>{old_sym}</code> manually on Kite\n"
        f"2. Use the dashboard → Clear State (or update positions_state.json)\n"
        f"3. Restart the algo"
    )
    logger.critical("Telegram: rollover warning sent for %s", name)


# ── Tender-period auto-rollover ────────────────────────────────────────────────

def notify_tender_period_rollover(
    name: str, old_sym: str, new_sym: str, new_expiry
) -> None:
    """
    Alert sent when an order is automatically retried on the next month's
    contract because the current contract entered the MCX tender period.
    """
    _send(
        f"🔄 <b>Tender Period Auto-Rollover — {name}</b>\n\n"
        f"Blocked contract: <code>{old_sym}</code> (entering tender period)\n"
        f"New contract:     <code>{new_sym}</code> (expiry: {new_expiry})\n\n"
        f"Order automatically retried on the new contract.\n"
        f"All future signals for <b>{name}</b> will now use <code>{new_sym}</code>."
    )
    logger.warning("Telegram: tender period rollover notification sent for %s", name)


# ── Expiry-day alerts ──────────────────────────────────────────────────────────

def notify_expiry_warning(positions: list) -> None:
    """
    12:30 PM heads-up that some open futures positions expire today.
    `positions` is a list of dicts: {name, symbol, direction, qty}.
    A forced square-off follows automatically later in the afternoon.
    """
    lines = [
        f"  • <b>{p['name']}</b> <code>{p['symbol']}</code> — {p['direction']} qty {p['qty']}"
        for p in positions
    ]
    _send(
        f"⏰ <b>Expiry Day — Open Positions</b>\n\n"
        f"📅 {date.today().strftime('%d %b %Y')}\n\n"
        f"The following futures positions expire <b>today</b>:\n\n"
        + "\n".join(lines) +
        f"\n\n⚠️ These will be auto squared-off at 14:45 IST to avoid "
        f"physical delivery. Close manually before then if you want a "
        f"different exit price."
    )
    logger.warning("Telegram: expiry-day warning sent for %d position(s)", len(positions))


def notify_expiry_squareoff(name: str, symbol: str, direction: str, qty: int) -> None:
    """Confirms a position was force-closed by the expiry-day square-off job."""
    time_str = datetime.now(IST).strftime("%H:%M:%S IST")
    _send(
        f"✅ <b>Expiry Square-Off — {name}</b>\n\n"
        f"Symbol:    <code>{symbol}</code>\n"
        f"Direction: {direction}\n"
        f"Qty:       {qty}\n"
        f"Time:      {time_str}\n\n"
        f"Closed automatically ahead of expiry to avoid physical delivery."
    )
    logger.info("Telegram: expiry square-off confirmation sent for %s", name)


def notify_expiry_squareoff_failed(name: str, symbol: str, exc_msg: str) -> None:
    """Critical alert when the expiry-day forced square-off order itself fails."""
    time_str = datetime.now(IST).strftime("%H:%M:%S IST")
    _send(
        f"🚨 <b>EXPIRY SQUARE-OFF FAILED — {name}</b>\n\n"
        f"Symbol: <code>{symbol}</code>\n"
        f"Time:   {time_str}\n"
        f"Error:  {exc_msg}\n\n"
        f"⛔ Position is STILL OPEN and expires today — physical delivery risk.\n"
        f"<b>Close this manually on Kite immediately.</b>"
    )
    logger.critical("Telegram: expiry square-off FAILURE alert sent for %s", name)


# ── Trade notifications ────────────────────────────────────────────────────────

_ICONS = {
    "BUY":        "📈",
    "SELL":       "📉",
    "EXIT_LONG":  "🔚",
    "EXIT_SHORT": "🔚",
}
_LABELS = {
    "BUY":        "BUY  — Long Entry",
    "SELL":       "SELL — Short Entry",
    "EXIT_LONG":  "EXIT LONG",
    "EXIT_SHORT": "EXIT SHORT",
}


def notify_trade(name: str, action: str, symbol: str,
                 qty: int, price: float) -> None:
    """Real order placed on Kite — clearly labelled LIVE TRADE."""
    icon     = _ICONS.get(action, "🔔")
    label    = _LABELS.get(action, action)
    time_str = datetime.now(IST).strftime("%H:%M:%S IST")
    _send(
        f"🟢 <b>[LIVE TRADE] {label}</b>\n\n"
        f"Instrument: <b>{name}</b>\n"
        f"Symbol:     <code>{symbol}</code>\n"
        f"Price:      ₹{price:,.2f}\n"
        f"Qty:        {qty}\n"
        f"Time:       {time_str}\n\n"
        f"<b>Real order placed on Kite.</b>"
    )
    logger.info("Telegram: live trade notification sent (%s %s)", action, name)


# ── Paper trading notifications ────────────────────────────────────────────────

def notify_paper_open(name: str, action: str, symbol: str,
                      qty: int, price: float) -> None:
    """Paper trade entry notification."""
    icon  = _ICONS.get(action, "🔔")
    label = _LABELS.get(action, action)
    time_str = datetime.now(IST).strftime("%H:%M:%S IST")
    _send(
        f"🟡 <b>[PAPER TRADE] {label}</b>\n\n"
        f"Instrument: <b>{name}</b>\n"
        f"Symbol:     <code>{symbol}</code>\n"
        f"Price:      ₹{price:,.2f}\n"
        f"Qty:        {qty}\n"
        f"Time:       {time_str}\n\n"
        f"<i>Paper trade — no real order placed on Kite.</i>"
    )
    logger.info("Telegram: paper trade open (%s %s)", action, name)


def notify_paper_close(result: dict) -> None:
    """
    Paper trade exit notification with full P&L summary.
    `result` is the dict returned by paper_trading.close_position().
    """
    pnl  = result["pnl"]
    icon = "✅" if pnl >= 0 else "❌"
    sign = "+" if pnl >= 0 else ""
    time_str = datetime.now(IST).strftime("%H:%M:%S IST")
    _send(
        f"{icon} <b>[PAPER TRADE] EXIT {result['direction']}</b>\n\n"
        f"Instrument: <b>{result['name']}</b>\n"
        f"Symbol:     <code>{result['symbol']}</code>\n"
        f"Direction:  {result['direction']}\n"
        f"Qty:        {result['qty']}\n\n"
        f"Entry:      ₹{result['entry_price']:,.2f}  @ {result['entry_time']}\n"
        f"Exit:       ₹{result['exit_price']:,.2f}  @ {time_str}\n\n"
        f"<b>P&amp;L: {sign}₹{pnl:,.2f}</b>\n\n"
        f"<i>Paper trade — no real order placed on Kite.</i>"
    )
    logger.info("Telegram: paper trade close (%s P&L=%.2f)", result["name"], pnl)


# ── Order rejection alerts ────────────────────────────────────────────────────

def notify_order_rejected(
    name: str,
    action: str,
    symbol: str,
    exc_msg: str,
) -> None:
    """
    Alert when a single-leg order is rejected or times out on Kite.
    Position state is NOT updated — the algo will retry on the next signal.
    """
    time_str = datetime.now(IST).strftime("%H:%M:%S IST")
    _send(
        f"🚨 <b>ORDER REJECTED — {name}</b>\n\n"
        f"Action:  <b>{action}</b>\n"
        f"Symbol:  <code>{symbol}</code>\n"
        f"Time:    {time_str}\n"
        f"Reason:  {exc_msg}\n\n"
        f"⛔ Position state NOT updated — algo will retry on next signal."
    )
    logger.error("Telegram: order rejected alert sent for %s %s %s", action, name, symbol)


# ── Synthetic futures alerts ───────────────────────────────────────────────────

def notify_synthetic_partial_fill(
    name: str,
    completed_leg: str,
    failed_leg: str,
    exc_msg: str,
) -> None:
    """
    Critical alert when one leg of a synthetic order placement fails.
    The completed leg is left open on Kite — human intervention is required
    to close it manually before the algo resumes trading this instrument.
    """
    _send(
        f"🚨 <b>PARTIAL SYNTHETIC FILL — {name}</b>\n\n"
        f"Leg placed:  <code>{completed_leg}</code>\n"
        f"Leg FAILED:  <code>{failed_leg}</code>\n"
        f"Error: {exc_msg}\n\n"
        f"⛔ Position state NOT updated — algo will retry on next signal.\n\n"
        f"<b>ACTION REQUIRED:</b> Close the open leg <code>{completed_leg}</code> "
        f"manually on Kite before the next signal fires."
    )
    logger.critical("Telegram: synthetic partial fill alert sent for %s", name)


def notify_instrument_halted(name: str, reason: str) -> None:
    """
    Critical alert: automated trading for one instrument has been halted
    because the algo can no longer trust that its saved position matches the
    broker (unconfirmed order outcome, or a partial synthetic fill).
    The algo will NOT place any further orders for it until the operator
    verifies the position on Kite and presses Resume on the dashboard.
    """
    time_str = datetime.now(IST).strftime("%H:%M:%S IST")
    _send(
        f"🛑 <b>TRADING HALTED — {name}</b>\n\n"
        f"Time:   {time_str}\n"
        f"Reason: {reason}\n\n"
        f"⛔ The algo will place NO further orders for {name} "
        f"(strategy ticks, expiry square-off and Square Off All will all skip it).\n\n"
        f"<b>ACTION REQUIRED:</b>\n"
        f"1. Check the actual position for {name} on Kite.\n"
        f"2. Fix any mismatch manually (close/open legs as needed).\n"
        f"3. Press <b>Resume</b> on the {name} dashboard card."
    )
    logger.critical("Telegram: instrument halted alert sent for %s", name)


def notify_service_restart(command: str) -> None:
    """Operator pressed Restart Service on the dashboard."""
    time_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    _send(
        f"🔁 <b>SERVICE RESTART</b>\n\n"
        f"Time:    {time_str}\n"
        f"Command: <code>{command}</code>\n\n"
        f"Requested from the dashboard. Open positions are kept in "
        f"positions_state.json and reloaded on startup."
    )
    logger.warning("Telegram: service restart notification sent (%s)", command)


def notify_instrument_config_changed(summary: str) -> None:
    """Instrument lots / stock futures list edited from the dashboard."""
    time_str = datetime.now(IST).strftime("%H:%M:%S IST")
    _send(
        f"⚙️ <b>INSTRUMENT CONFIG CHANGED</b>\n\n"
        f"Time: {time_str}\n"
        f"{summary}"
    )
    logger.info("Telegram: instrument config change sent (%s)", summary)
