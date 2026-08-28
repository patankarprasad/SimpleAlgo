"""
Central configuration – instruments, lot sizes, and strategy params.
Credentials are loaded from .env (never hardcode them here).
"""
import os
import sys
from dotenv import load_dotenv

import instrument_config as _icfg

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

load_dotenv(os.path.join(BASE_DIR, ".env"))

# ── Angel SmartAPI Credentials ─────────────────────────────────────────────────
# Variable names match the existing Angel login class convention exactly.
ANGEL_API_KEY     = os.getenv("ANGEL_API_KEY")       # API key from Angel One developer portal
ANGEL_USERNAME    = os.getenv("ANGEL_USERNAME")       # Client code (e.g. A123456)
ANGEL_PIN         = os.getenv("ANGEL_PIN")            # 4-digit trading MPIN
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")   # Base-32 TOTP secret from Angel QR setup

KITE_API_KEY      = os.getenv("KITE_API_KEY")
KITE_API_SECRET   = os.getenv("KITE_API_SECRET")
KITE_USER_ID      = os.getenv("KITE_USER_ID")
KITE_PASSWORD     = os.getenv("KITE_PASSWORD")
KITE_PIN          = os.getenv("KITE_PIN")
KITE_TOTP_SECRET  = os.getenv("KITE_TOTP_SECRET")   # Base-32 TOTP secret for automated login

# ── Kite auto-login settings ───────────────────────────────────────────────────
KITE_AUTO_LOGIN_RETRIES     = int(os.getenv("KITE_AUTO_LOGIN_RETRIES",     "3"))
KITE_AUTO_LOGIN_RETRY_DELAY = int(os.getenv("KITE_AUTO_LOGIN_RETRY_DELAY", "5"))  # seconds

# ── Data provider ──────────────────────────────────────────────────────────────
# "angel" uses Angel SmartAPI (free).  "kite" uses Kite historical data API
# (requires a paid Kite Connect Historical Data subscription).
DATA_PROVIDER = os.getenv("DATA_PROVIDER", "angel").lower()

# ── Telegram notifications ─────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")
SERVER_BASE_URL    = os.getenv("SERVER_BASE_URL",    "")  # e.g. http://1.2.3.4:8880

# ── Web dashboard credentials ──────────────────────────────────────────────────
WEB_USERNAME = os.getenv("WEB_USERNAME", "")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")
if not WEB_USERNAME or not WEB_PASSWORD:
    raise RuntimeError(
        "WEB_USERNAME and WEB_PASSWORD must be set in your .env file. "
        "The web dashboard will not start without them."
    )

# ── Kite Auth Web Server ───────────────────────────────────────────────────────
KITE_AUTH_PORT       = int(os.getenv("KITE_AUTH_PORT", "8080"))   # port to listen on
KITE_AUTH_PIN        = os.getenv("KITE_AUTH_PIN", "0000")         # PIN to protect /login
KITE_AUTH_SECRET_KEY = os.getenv("KITE_AUTH_SECRET_KEY", "change-me-in-dotenv")  # Flask session key

# ── Angel rate-limit delays  (source: smartapi.angelbroking.com/docs/RateLimit) ─
# getCandleData : 3 req/s  → min gap 0.334s → use 0.6s (extra headroom to avoid
#                 burst throttling when many instruments fetch simultaneously)
# searchScrip   : 1 req/s  → min gap 1.0s   → use 1.1s
# getLtpData    : 10 req/s → min gap 0.1s   → use 0.15s
ANGEL_BASE_DELAY   = float(os.getenv("ANGEL_BASE_DELAY",   "0.6"))   # getCandleData (3/s limit)
ANGEL_SEARCH_DELAY = float(os.getenv("ANGEL_SEARCH_DELAY", "1.1"))   # searchScrip   (1/s limit)
ANGEL_LTP_DELAY    = float(os.getenv("ANGEL_LTP_DELAY",    "0.15"))  # getLtpData    (10/s limit)

# ── Strategy parameters ────────────────────────────────────────────────────────
ST1_PERIOD   = 10
ST1_FACTOR   = 2.0
ST2_PERIOD   = 10
ST2_FACTOR   = 3.0
MA_LENGTH    = 50

CANDLE_LOOKBACK = 200
DRY_RUN             = os.getenv("DRY_RUN", "false").strip().lower() == "true"
TRADING_DAYS_ONLY   = os.getenv("TRADING_DAYS_ONLY", "true").strip().lower() == "true"

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
#   timeframe   – Angel interval string (e.g. FIFTEEN_MINUTE, ONE_HOUR)
#
# NFO (NIFTY / BANKNIFTY) : 09:15 – 15:30
# MCX (GOLDM / CRUDEOIL)  : 09:00 – 23:30  (MCX evening session ends at 23:30)

_DEFAULT_INSTRUMENTS = [
    {
        "name":          "GOLDM",
        "exchange":      "MCX",
        "qty":           2,
        "contract_size": 10,        # 10 grams/lot — PnL multiplier only (Kite order qty stays 1)
        "product":       "NRML",
        "trade_start":   "09:00",
        "trade_end":     "23:30",
        "timeframe":     "FIFTEEN_MINUTE",
    },
    {
        "name":          "CRUDEOIL",
        "exchange":      "MCX",
        "qty":           2,
        "contract_size": 100,       # 100 barrels/lot — PnL multiplier only (Kite order qty stays 1)
        "product":       "NRML",
        "trade_start":   "09:00",
        "trade_end":     "23:30",
        "timeframe":     "FIFTEEN_MINUTE",
    },
    {
        "name":          "SILVERM",
        "exchange":      "MCX",
        "qty":           2,
        "contract_size": 5,         # 5 kg/lot — PnL multiplier only (Kite order qty stays 1)
        "product":       "NRML",
        "trade_start":   "09:00",
        "trade_end":     "23:30",
        "timeframe":     "FIFTEEN_MINUTE",
    },
    {
        "name":          "NATURALGAS",
        "exchange":      "MCX",
        "qty":           2,
        "contract_size": 1250,      # 1250 MMBTU/lot — PnL multiplier only (Kite order qty stays 1)
        "product":       "NRML",
        "trade_start":   "09:00",
        "trade_end":     "23:30",
        "timeframe":     "FIFTEEN_MINUTE",
    },
    {
        "name":          "COPPER",
        "exchange":      "MCX",
        "qty":           1,
        "contract_size": 2500,      # 2500 kg/lot — PnL multiplier only (Kite order qty stays 1)
        "product":       "NRML",
        "trade_start":   "09:00",
        "trade_end":     "23:30",
        "timeframe":     "FIFTEEN_MINUTE",
    },
    {
        "name":          "ZINC",
        "exchange":      "MCX",
        "qty":           2,
        "contract_size": 5000,      # 5000 kg/lot — PnL multiplier only (Kite order qty stays 1)
        "product":       "NRML",
        "trade_start":   "09:00",
        "trade_end":     "23:30",
        "timeframe":     "FIFTEEN_MINUTE",
    },
    {
        "name":                  "NIFTY",
        "exchange":              "NFO",
        "qty":                   2,           # lot_size resolved from scrip master
        "product":               "NRML",
        "trade_start":           "09:15",
        "trade_end":             "15:30",
        "mode":                  "SYNTHETIC", # LONG: synthetic future (ATM CE + PE); SHORT: sell CE
        "strike_step":           50,          # NIFTY strikes are multiples of 50
        "timeframe":             "FIFTEEN_MINUTE",
        "spot_index_name":       "Nifty 50",  # Angel symbol for NSE spot index (used for indicators)
        "short_ce_target_premium": 300,       # SELL the monthly CE whose LTP is closest to this
    },
    {
        "name":                  "BANKNIFTY",
        "exchange":              "NFO",
        "qty":                   1,           # lot_size resolved from scrip master
        "product":               "NRML",
        "trade_start":           "09:15",
        "trade_end":             "15:30",
        "mode":                  "SYNTHETIC", # LONG: synthetic future (ATM CE + PE); SHORT: sell CE
        "strike_step":           100,         # BANKNIFTY strikes are multiples of 100
        "timeframe":             "FIFTEEN_MINUTE",
        "spot_index_name":       "Nifty Bank", # Angel symbol for NSE spot index (used for indicators)
        "short_ce_target_premium": 700,        # SELL the monthly CE whose LTP is closest to this
    },
]

_DEFAULT_HOURLY_INSTRUMENTS = [
    {
        "name":          "GOLDM_H",
        "underlying":    "GOLDM",       # inherits resolved tokens from base instrument
        "exchange":      "MCX",
        "qty":           1,
        "contract_size": 10,
        "product":       "NRML",
        "trade_start":   "09:00",
        "trade_end":     "23:30",
        "timeframe":     "ONE_HOUR",
        "long_only":     True,
    },
    {
        "name":          "CRUDEOIL_H",
        "underlying":    "CRUDEOIL",
        "exchange":      "MCX",
        "qty":           1,
        "contract_size": 100,
        "product":       "NRML",
        "trade_start":   "09:00",
        "trade_end":     "23:30",
        "timeframe":     "ONE_HOUR",
        "long_only":     True,
    },
    {
        "name":          "SILVERM_H",
        "underlying":    "SILVERM",
        "exchange":      "MCX",
        "qty":           1,
        "contract_size": 5,
        "product":       "NRML",
        "trade_start":   "09:00",
        "trade_end":     "23:30",
        "timeframe":     "ONE_HOUR",
        "long_only":     True,
    },
    {
        "name":                  "NIFTY_H",
        "underlying":            "NIFTY",
        "exchange":              "NFO",
        "qty":                   1,
        "product":               "NRML",
        "trade_start":           "09:15",
        "trade_end":             "15:30",
        "mode":                  "SYNTHETIC",
        "strike_step":           50,
        "timeframe":             "ONE_HOUR",
        "long_only":             True,
        "spot_index_name":       "Nifty 50",
        "short_ce_target_premium": 300,
    },
    {
        "name":                  "BANKNIFTY_H",
        "underlying":            "BANKNIFTY",
        "exchange":              "NFO",
        "qty":                   1,
        "product":               "NRML",
        "trade_start":           "09:15",
        "trade_end":             "15:30",
        "mode":                  "SYNTHETIC",
        "strike_step":           100,
        "timeframe":             "ONE_HOUR",
        "long_only":             True,
        "spot_index_name":       "Nifty Bank",
        "short_ce_target_premium": 700,
    },
]

# ── Stock Futures ──────────────────────────────────────────────────────────────
# NSE stock names whose nearest futures contract is traded with the same
# supertrend + MA strategy.  Each stock is a plain futures instrument (no
# SYNTHETIC mode).
#
# The list lives in instrument_config.json and is edited from the dashboard
# Settings page.  STOCK_FUTURES / STOCK_FUTURES_QTY / STOCK_FUTURES_PRODUCT in
# .env are only used to SEED that file the first time it is created, so an
# existing deployment keeps its stocks after the upgrade; after that the
# dashboard is the source of truth and the .env values are ignored.
def _env_seed(key: str, default: str) -> str:
    """
    Read a seed value, tolerating a trailing inline comment.

    systemd's EnvironmentFile= keeps "# ..." as part of the value (python-dotenv
    strips it), and load_dotenv does not override what systemd already set. That
    difference is harmless for a value re-read every start, but these three seed
    a file ONCE — a polluted value would be frozen in permanently.
    """
    return os.getenv(key, default).split("#", 1)[0].strip() or default


_raw_stocks           = _env_seed("STOCK_FUTURES", "")
_ENV_STOCK_NAMES      = [s.strip().upper() for s in _raw_stocks.split(",") if s.strip()]
_ENV_STOCK_QTY        = int(_env_seed("STOCK_FUTURES_QTY", "1"))
_ENV_STOCK_PRODUCT    = _env_seed("STOCK_FUTURES_PRODUCT", "NRML")

_icfg.seed_stock_futures(_ENV_STOCK_NAMES, _ENV_STOCK_QTY, _ENV_STOCK_PRODUCT)


# ── Dashboard-editable overlay ─────────────────────────────────────────────────
# INSTRUMENTS, HOURLY_INSTRUMENTS and STOCK_INSTRUMENTS are rebuilt from the
# _DEFAULT_* lists above plus the runtime overrides in instrument_config.json.
# Read them as config.INSTRUMENTS (never `from config import INSTRUMENTS`) so a
# reload is picked up without a restart.

INSTRUMENTS:           list[dict] = []
HOURLY_INSTRUMENTS:    list[dict] = []
STOCK_INSTRUMENTS:     list[dict] = []
STOCK_FUTURES_NAMES:   list[str]  = []
STOCK_FUTURES_QTY:     int        = _ENV_STOCK_QTY
STOCK_FUTURES_PRODUCT: str        = _ENV_STOCK_PRODUCT


def _with_lots(inst_def: dict, lots: dict[str, int]) -> dict:
    """Return a copy of inst_def with qty replaced by its dashboard override."""
    override = lots.get(inst_def["name"].upper())
    return inst_def if override is None else {**inst_def, "qty": override}


def reload_instrument_config() -> None:
    """
    Re-read instrument_config.json and rebuild the three instrument lists.

    Called once at import and again whenever the dashboard saves a change.
    This only rebuilds the *definitions* — main.reload_instruments() applies
    them to the live resolved instruments.
    """
    global INSTRUMENTS, HOURLY_INSTRUMENTS, STOCK_INSTRUMENTS
    global STOCK_FUTURES_NAMES, STOCK_FUTURES_QTY, STOCK_FUTURES_PRODUCT

    lots = _icfg.get_all_lots()
    STOCK_FUTURES_QTY, STOCK_FUTURES_PRODUCT = _icfg.get_stock_defaults(
        _ENV_STOCK_QTY, _ENV_STOCK_PRODUCT,
    )
    STOCK_FUTURES_NAMES = _icfg.get_stock_futures(_ENV_STOCK_NAMES)

    INSTRUMENTS        = [_with_lots(i, lots) for i in _DEFAULT_INSTRUMENTS]
    HOURLY_INSTRUMENTS = [_with_lots(i, lots) for i in _DEFAULT_HOURLY_INSTRUMENTS]
    STOCK_INSTRUMENTS  = [
        {
            "name":        name,
            "exchange":    "NFO",
            "qty":         lots.get(name, STOCK_FUTURES_QTY),
            "product":     STOCK_FUTURES_PRODUCT,
            "trade_start": "09:15",
            "trade_end":   "15:30",
            "timeframe":   "FIFTEEN_MINUTE",
        }
        for name in STOCK_FUTURES_NAMES
    ]


reload_instrument_config()

# ── Session token cache paths ──────────────────────────────────────────────────
LOGS_DIR           = os.path.join(BASE_DIR, "logs")
LOG_FILE           = os.path.join(LOGS_DIR, "algo.log")
KITE_TOKEN_FILE    = os.path.join(BASE_DIR, "kite_token.json")
ANGEL_TOKEN_FILE   = os.path.join(BASE_DIR, "angel_token.json")
STATE_FILE         = os.path.join(BASE_DIR, "positions_state.json")
PAPER_STATE_FILE   = os.path.join(BASE_DIR, "paper_positions_state.json")  # persisted paper trade positions
CONTRACT_PIN_FILE  = os.path.join(BASE_DIR, "contract_pin.json")            # manual rollover overrides
INSTRUMENT_CONFIG_FILE = str(_icfg.CONFIG_FILE)   # dashboard-editable lots + stock list

# ── Service restart (dashboard "Restart Service" button) ───────────────────────
# On the Linux VPS the dashboard restarts the app through systemd. This needs a
# passwordless sudo rule — see DEPLOYMENT.txt section 9a.
# Set RESTART_COMMAND to override entirely (e.g. on Windows, or for a
# non-systemd supervisor).
SERVICE_NAME    = os.getenv("SERVICE_NAME", "simplealgo")
RESTART_COMMAND = os.getenv("RESTART_COMMAND", "").strip()
