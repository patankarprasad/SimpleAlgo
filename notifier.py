"""
Telegram notification module for SimpleAlgo.

All public functions are fire-and-forget: they log errors but never raise,
so a Telegram failure never disrupts the algo.

Configure in .env:
  TELEGRAM_BOT_TOKEN=<your bot token>
  TELEGRAM_CHAT_ID=<your chat id>
  SERVER_BASE_URL=http://<vps-ip>:8880   (used in login reminder link)
"""
import logging
from datetime import date, datetime
from pathlib import Path

import requests
import pytz

import config

logger = logging.getLogger(__name__)
IST    = pytz.timezone("Asia/Kolkata")


# ── Core send ──────────────────────────────────────────────────────────────────

def _send(text: str) -> bool:
    """Send an HTML-formatted message to the configured Telegram chat."""
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


# ── Scheduled notifications ────────────────────────────────────────────────────

def notify_login_reminder() -> None:
    """
    8:30 AM daily reminder to log in to Kite.
    Includes a direct link to the auth server login page.
    """
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
    """
    import strategy_config as stcfg

    # Check token validity by reading the cache file directly
    tok_valid = False
    try:
        cache = Path(config.KITE_TOKEN_FILE).read_text()
        import json
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
