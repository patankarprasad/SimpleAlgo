"""
Helper to find Angel SmartAPI instrument tokens for your futures contracts.
Run once to populate the 'angel_token' fields in config.py (optional –
the main code auto-searches if the token is missing, but hardcoding it is faster).

Usage:
    python utils/find_tokens.py
"""
import sys, os, logging

# Force UTF-8 output on Windows so special chars don't crash cp1252 console
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# SmartAPI library uses logzero internally – silence it before importing
# anything that triggers SmartApi to load, otherwise it floods stdout with
# every search result (1000+ lines per instrument).
import logzero
logzero.loglevel(logging.WARNING)

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from angel_data import search_token

queries = [
    ("GOLDM",      "MCX"),
    ("CRUDEOIL",   "MCX"),
    ("NIFTY",      "NFO"),
    ("BANKNIFTY",  "NFO"),
]

for symbol, exchange in queries:
    print(f"\n--- {symbol} ({exchange}) ---")   # plain ASCII, no box-drawing chars
    search_token(symbol, exchange)
