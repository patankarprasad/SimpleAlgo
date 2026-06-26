"""
Zerodha KiteConnect login.

Two login paths:
  A) Automated (auto_login): headless POST to Kite's API with TOTP.
     Called by the scheduler at 8:00 AM daily.
  B) Manual (browser redirect): user visits /kite/login in the web UI,
     Zerodha OAuth callback saves the token via _save_token() in webapp.py.

get_kite_session() reads today's cached token regardless of which path was used.
"""
import json
import logging
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import pyotp
import requests as _requests
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


# ── Automated headless login ───────────────────────────────────────────────────

def auto_login(
    api_key:     str  = None,
    api_secret:  str  = None,
    user_id:     str  = None,
    password:    str  = None,
    totp_secret: str  = None,
    max_retries: int  = None,
    retry_delay: int  = None,
) -> bool:
    """
    Log in to Kite headlessly using credentials + TOTP (no browser required).

    Saves today's access_token to KITE_TOKEN_FILE on success, exactly like the
    manual browser flow — so get_kite_session() works transparently afterwards.

    Returns True on success, False if all attempts fail.
    Requires KITE_TOTP_SECRET (and KITE_USER_ID, KITE_PASSWORD) in .env.
    """
    api_key     = api_key     or config.KITE_API_KEY
    api_secret  = api_secret  or config.KITE_API_SECRET
    user_id     = user_id     or config.KITE_USER_ID
    password    = password    or config.KITE_PASSWORD
    totp_secret = totp_secret or config.KITE_TOTP_SECRET
    max_retries = max_retries if max_retries is not None else config.KITE_AUTO_LOGIN_RETRIES
    retry_delay = retry_delay if retry_delay is not None else config.KITE_AUTO_LOGIN_RETRY_DELAY

    if not all([api_key, api_secret, user_id, password, totp_secret]):
        logger.error(
            "auto_login: missing credentials — set KITE_API_KEY, KITE_API_SECRET, "
            "KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET in .env"
        )
        return False

    for attempt in range(1, max_retries + 1):
        try:
            session = _requests.Session()

            login_res = session.post(
                "https://kite.zerodha.com/api/login",
                data={"user_id": user_id, "password": password},
                timeout=15,
            ).json()
            request_id = login_res["data"]["request_id"]

            session.post(
                "https://kite.zerodha.com/api/twofa",
                data={
                    "user_id":     user_id,
                    "request_id":  request_id,
                    "twofa_value": pyotp.TOTP(totp_secret).now(),
                },
                timeout=15,
            )

            # Zerodha redirects to the app's redirect_uri with request_token.
            # requests raises on redirect; catch the final URL from the exception.
            try:
                resp = session.get(
                    f"https://kite.trade/connect/login?api_key={api_key}&v=3",
                    timeout=15,
                    allow_redirects=True,
                )
                parsed = urlparse(resp.url)
            except Exception as redir_exc:
                req_url = getattr(getattr(redir_exc, "request", None), "url", None)
                if not req_url:
                    raise
                parsed = urlparse(req_url)

            query_params  = parse_qs(parsed.query)
            request_token = query_params["request_token"][0]

            kite = KiteConnect(api_key=api_key)
            data = kite.generate_session(request_token, api_secret=api_secret)
            access_token = data["access_token"]

            token_path = Path(config.KITE_TOKEN_FILE)
            token_path.write_text(
                json.dumps({"date": str(date.today()), "access_token": access_token}, indent=2)
            )
            logger.info("auto_login: Kite access token saved for %s (attempt %d)", date.today(), attempt)
            return True

        except Exception as exc:
            logger.error("auto_login: attempt %d/%d failed: %s", attempt, max_retries, exc, exc_info=True)
            if attempt < max_retries:
                time.sleep(retry_delay)

    logger.error("auto_login: all %d attempts failed", max_retries)
    return False


# ── Manual override ────────────────────────────────────────────────────────────
# Use this if you have obtained an access_token by some other means and want to
# inject it directly (e.g. from a Python REPL or a one-off script).
def manual_set_token(access_token: str):
    """Manually cache a Kite access_token for today."""
    cache = {"date": str(date.today()), "access_token": access_token}
    with open(config.KITE_TOKEN_FILE, "w") as f:
        json.dump(cache, f)
    logger.info("Kite: manually saved access token for today")
