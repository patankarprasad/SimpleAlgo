"""
Zerodha KiteConnect login.

Zerodha uses an OAuth2-style redirect flow:
  1. User opens the auth server URL on their phone.
  2. Enters the app PIN, is redirected to Kite to log in.
  3. Kite redirects back to /callback with ?request_token=xxx.
  4. The server exchanges request_token for access_token and saves it here.

The algo then calls get_kite_session() which reads the cached token.
If no valid token exists today it raises a clear error pointing to the
auth server rather than falling back to Selenium.
"""
import json
import logging
from datetime import date
from pathlib import Path

from kiteconnect import KiteConnect

import config

logger = logging.getLogger(__name__)


def get_kite_session() -> KiteConnect:
    """
    Return an authenticated KiteConnect object.

    Reads today's access_token from the JSON cache written by the /callback route.
    Raises RuntimeError with a helpful message if no valid token exists.
    """
    token_path = Path(config.KITE_TOKEN_FILE)

    if token_path.exists():
        with open(token_path) as f:
            cache = json.load(f)
        if cache.get("date") == str(date.today()):
            kite = KiteConnect(api_key=config.KITE_API_KEY)
            kite.set_access_token(cache["access_token"])
            logger.info("Kite: reused cached session (date=%s)", cache["date"])
            return kite
        else:
            logger.warning(
                "Kite token is stale (cached date=%s, today=%s).",
                cache.get("date"), date.today(),
            )
    else:
        logger.warning("Kite token file not found: %s", config.KITE_TOKEN_FILE)

    raise RuntimeError(
        f"No valid Kite token for today ({date.today()}). "
        f"Please log in via the auth server: "
        f"http://<VPS-IP>:{config.KITE_AUTH_PORT}/"
    )


# ── Manual override ────────────────────────────────────────────────────────────
# Use this if you have obtained an access_token by some other means and want to
# inject it directly (e.g. from a Python REPL or a one-off script).
def manual_set_token(access_token: str):
    """Manually cache a Kite access_token for today."""
    cache = {"date": str(date.today()), "access_token": access_token}
    with open(config.KITE_TOKEN_FILE, "w") as f:
        json.dump(cache, f)
    logger.info("Kite: manually saved access token for today")
