"""
Central configuration – instruments, lot sizes, and strategy params.
Credentials are loaded from .env (never hardcode them here).
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Angel SmartAPI Credentials ─────────────────────────────────────────────────
# Variable names match the existing Angel login class convention exactly.
ANGEL_API_KEY     = os.getenv("ANGEL_API_KEY")       # API key from Angel One developer portal
ANGEL_USERNAME    = os.getenv("ANGEL_USERNAME")       # Client code (e.g. A123456)
ANGEL_PIN         = os.getenv("ANGEL_PIN")            # 4-digit trading MPIN
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")   # Base-32 TOTP secret from Angel QR setup

KITE_API_KEY    = os.getenv("KITE_API_KEY")
KITE_API_SECRET = os.getenv("KITE_API_SECRET")
KITE_USER_ID    = os.getenv("KITE_USER_ID")
KITE_PASSWORD   = os.getenv("KITE_PASSWORD")
KITE_PIN        = os.getenv("KITE_PIN")

# ── Telegram notifications ─────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")
SERVER_BASE_URL    = os.getenv("SERVER_BASE_URL",    "")  # e.g. http://1.2.3.4:8880

# ── Web dashboard credentials ──────────────────────────────────────────────────
WEB_USERNAME = os.getenv("WEB_USERNAME", "admin")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "changeme")

# ── Kite Auth Web Server ───────────────────────────────────────────────────────
KITE_AUTH_PORT       = int(os.getenv("KITE_AUTH_PORT", "8080"))   # port to listen on
KITE_AUTH_PIN        = os.getenv("KITE_AUTH_PIN", "0000")         # PIN to protect /login
KITE_AUTH_SECRET_KEY = os.getenv("KITE_AUTH_SECRET_KEY", "change-me-in-dotenv")  # Flask session key

# ── Angel rate-limit delays  (source: smartapi.angelbroking.com/docs/RateLimit) ─
# getCandleData : 3 req/s  → min gap 0.334s → use 0.4s
# searchScrip   : 1 req/s  → min gap 1.0s   → use 1.1s
# getLtpData    : 10 req/s → min gap 0.1s   → use 0.15s
ANGEL_BASE_DELAY   = float(os.getenv("ANGEL_BASE_DELAY",   "0.4"))   # getCandleData (3/s limit)
ANGEL_SEARCH_DELAY = float(os.getenv("ANGEL_SEARCH_DELAY", "1.1"))   # searchScrip   (1/s limit)
ANGEL_LTP_DELAY    = float(os.getenv("ANGEL_LTP_DELAY",    "0.15"))  # getLtpData    (10/s limit)

# ── Strategy parameters ────────────────────────────────────────────────────────
ST1_PERIOD   = 10
ST1_FACTOR   = 2.0
ST2_PERIOD   = 10
ST2_FACTOR   = 3.0
MA_LENGTH    = 50

CANDLE_INTERVAL = os.getenv("CANDLE_INTERVAL", "FIFTEEN_MINUTE")
CANDLE_LOOKBACK = int(os.getenv("CANDLE_LOOKBACK", 200))
DRY_RUN         = os.getenv("DRY_RUN", "false").strip().lower() == "true"

# ── Instruments ────────────────────────────────────────────────────────────────
# Minimal static config – no hardcoded tokens or expiry-specific symbols.
# scrip_master.resolve_instrument() enriches each entry at startup with:
#   angel_token, angel_symbol, angel_exchange,
#   kite_tradingsymbol, kite_instrument_token,
#   expiry, lot_size, tick_size
#
# Fields:
#   name        – underlying name exactly as it appears in both broker masters
#   exchange    – MCX / NFO / NSE / BSE (shared by both brokers)
#   qty         – number of lots (1 lot = lot_size units, resolved from master)
#   product     – NRML for positional/overnight; MIS for intraday
#   trade_start – IST HH:MM  when the algo may start entering trades
#   trade_end   – IST HH:MM  after which open positions are squared off
#
# NFO (NIFTY / BANKNIFTY) : 09:15 – 15:30
# MCX (GOLDM / CRUDEOIL)  : 09:00 – 23:30  (MCX evening session ends at 23:30)

INSTRUMENTS = [
    {
        "name":        "GOLDM",
        "exchange":    "MCX",
        "qty":         1,
        "product":     "NRML",
        "trade_start": "09:00",
        "trade_end":   "23:30",
    },
    {
        "name":        "CRUDEOIL",
        "exchange":    "MCX",
        "qty":         1,
        "product":     "NRML",
        "trade_start": "09:00",
        "trade_end":   "23:30",
    },
    {
        "name":        "NIFTY",
        "exchange":    "NFO",
        "qty":         1,           # 1 lot = 25 units (resolved from master)
        "product":     "NRML",
        "trade_start": "09:15",
        "trade_end":   "15:30",
    },
    {
        "name":        "BANKNIFTY",
        "exchange":    "NFO",
        "qty":         1,           # 1 lot = 30 units (resolved from master)
        "product":     "NRML",
        "trade_start": "09:15",
        "trade_end":   "15:30",
    },
]

# ── Session token cache paths ──────────────────────────────────────────────────
KITE_TOKEN_FILE  = "kite_token.json"
ANGEL_TOKEN_FILE = "angel_token.json"
STATE_FILE       = "positions_state.json"
