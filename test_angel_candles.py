"""
Diagnostic script: Angel login + getCandleData test for ETERNAL and POLYCAB.

Run this on the server to verify the expired-token issue and confirm the
correct May-contract tokens return data.

Usage:
    python test_angel_candles.py
"""
import json
import os
import time
from datetime import datetime, timedelta, date

import pyotp
from dotenv import load_dotenv
from SmartApi import SmartConnect

load_dotenv()

API_KEY     = os.getenv("ANGEL_API_KEY")
USERNAME    = os.getenv("ANGEL_USERNAME")
PIN         = os.getenv("ANGEL_PIN")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")


def login() -> SmartConnect:
    totp = pyotp.TOTP(TOTP_SECRET).now()
    print(f"[login] TOTP generated, logging in as {USERNAME} ...")
    smart = SmartConnect(API_KEY)
    data  = smart.generateSession(USERNAME, PIN, totp)
    if data["status"] is False:
        raise RuntimeError(f"Login failed: {data}")
    print(f"[login] OK — user={smart.userId}  token={smart.access_token[:20]}...")
    return smart


def candle_params(exchange: str, token: str, interval: str = "FIFTEEN_MINUTE") -> dict:
    to_dt   = datetime.now()
    from_dt = to_dt - timedelta(days=15)
    return {
        "exchange":    exchange,
        "symboltoken": token,
        "interval":    interval,
        "fromdate":    from_dt.strftime("%Y-%m-%d %H:%M"),
        "todate":      to_dt.strftime("%Y-%m-%d %H:%M"),
    }


def fetch(angel: SmartConnect, label: str, exchange: str, token: str, interval: str = "FIFTEEN_MINUTE"):
    p = candle_params(exchange, token, interval)
    print(f"\n[test] {label}")
    print(f"       exchange={exchange}  token={token}  interval={interval}")
    print(f"       fromdate={p['fromdate']}  todate={p['todate']}")
    time.sleep(0.4)   # respect 3-req/s rate limit
    try:
        resp = angel.getCandleData(p)
    except Exception as e:
        print(f"       ERROR: {e}")
        return

    status  = resp.get("status")
    message = resp.get("message")
    data    = resp.get("data") or []
    print(f"       status={status}  message={message}  rows={len(data)}")
    if data:
        first = data[0]
        last  = data[-1]
        print(f"       first candle: {first[0]}  close={first[4]}")
        print(f"       last  candle: {last[0]}   close={last[4]}")
        print(f"       RESULT: ✓ SUCCESS — data returned")
    else:
        print(f"       RESULT: ✗ EMPTY — no data (Angel returns this for expired/wrong tokens)")


def search_futures(angel: SmartConnect, symbol: str, exchange: str):
    """Search Angel for active futures matching symbol on exchange."""
    print(f"\n[search] Searching '{symbol}' on {exchange} ...")
    time.sleep(1.1)   # searchScrip: 1 req/s limit
    resp = angel.searchScrip(exchange, symbol)
    if resp.get("status") and resp.get("data"):
        futures = [r for r in resp["data"] if "FUT" in r.get("tradingsymbol", "")]
        if futures:
            print(f"  Found {len(futures)} FUT contracts:")
            for r in futures:
                print(f"    symbol={r['tradingsymbol']}  token={r['symboltoken']}")
        else:
            print(f"  No FUT contracts found")
    else:
        print(f"  Search failed: {resp.get('message')}")


def lookup_from_scrip_master(name: str, exchange: str):
    """Look up the nearest active contract from cached Angel scrip master."""
    try:
        raw  = json.loads(open("cache/angel_scrip_master.json", encoding="utf-8").read())
        today = date.today()
        from datetime import datetime as _dt
        candidates = []
        for r in raw:
            if r.get("exch_seg") != exchange:
                continue
            if r.get("instrumenttype") not in ("FUTSTK", "FUTIDX", "FUTCOM"):
                continue
            sym = r.get("symbol", "")
            if not sym.upper().startswith(name.upper()):
                continue
            try:
                expiry = _dt.strptime(r["expiry"], "%d%b%Y").date()
            except Exception:
                continue
            if expiry > today:
                candidates.append((expiry, r["symbol"], r["token"]))
        candidates.sort()
        print(f"\n[scrip_master] Active {name} futures on {exchange} (expiry > {today}):")
        for expiry, sym, tok in candidates:
            marker = " ← NEAREST (correct token to use)" if expiry == candidates[0][0] else ""
            print(f"    {sym}  token={tok}  expiry={expiry}{marker}")
        return candidates[0][2] if candidates else None
    except Exception as e:
        print(f"[scrip_master] Could not read cache: {e}")
        return None


def main():
    angel = login()

    print("\n" + "="*70)
    print("DIAGNOSIS: Why ETERNAL and POLYCAB return empty data")
    print("="*70)

    # ── ETERNAL ───────────────────────────────────────────────────────────────
    print("\n--- ETERNAL ---")

    # 1. Expired token (what the server is currently using)
    fetch(angel, "EXPIRED token 66824 = ETERNAL28APR26FUT (expired 2026-04-28)",
          exchange="NFO", token="66824")

    # 2. Correct nearest active token from cached scrip master
    correct_token_eternal = lookup_from_scrip_master("ETERNAL", "NFO")
    if correct_token_eternal and correct_token_eternal != "66824":
        fetch(angel, f"CORRECT token {correct_token_eternal} = nearest active NFO contract",
              exchange="NFO", token=correct_token_eternal)

    # 3. Search live API for current contracts
    search_futures(angel, "ETERNAL", "NFO")

    # ── POLYCAB ───────────────────────────────────────────────────────────────
    print("\n--- POLYCAB ---")

    # 1. Expired token
    fetch(angel, "EXPIRED token 66986 = POLYCAB28APR26FUT (expired 2026-04-28)",
          exchange="NFO", token="66986")

    # 2. Correct nearest active token
    correct_token_polycab = lookup_from_scrip_master("POLYCAB", "NFO")
    if correct_token_polycab and correct_token_polycab != "66986":
        fetch(angel, f"CORRECT token {correct_token_polycab} = nearest active NFO contract",
              exchange="NFO", token=correct_token_polycab)

    # 3. Search live API
    search_futures(angel, "POLYCAB", "NFO")

    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    print("""
Root cause: The server was started before April 28 (expiry day) and resolved
ETERNAL → token 66824 (ETERNAL28APR26FUT) and POLYCAB → token 66986
(POLYCAB28APR26FUT). Both contracts expired on 2026-04-28. Angel returns
empty data for expired tokens.

Fix: Restart the server so initialise() re-resolves all instruments using
fresh scrip masters. The nearest active contracts will then be:
  ETERNAL  → ETERNAL26MAY26FUT  (token 66158, expiry 2026-05-26)
  POLYCAB  → POLYCAB26MAY26FUT  (token 66336, expiry 2026-05-26)

Long-term fix: re_resolve_instrument() in main.py only searches
RESOLVED_INSTRUMENTS, not RESOLVED_STOCK_INSTRUMENTS, so the webapp
rollover-pin action fails for stock futures with the warning
"ETERNAL not found in RESOLVED_INSTRUMENTS".
""")


if __name__ == "__main__":
    main()
