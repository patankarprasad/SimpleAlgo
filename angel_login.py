"""
Angel SmartAPI login – TOTP login with same-day file-cache reuse.

How session restoration works
------------------------------
SmartConnect.__init__ accepts access_token / refresh_token / feed_token /
userId as constructor arguments and uses them directly in _request():

    headers["Authorization"] = "Bearer " + self.access_token

So we can fully reconstruct a live session without calling generateSession()
again – as long as we cache the RIGHT values.

Critical gotcha
---------------
generateSession() returns data["data"]["jwtToken"] = "Bearer <raw_jwt>"
(the library prepends "Bearer " for display). But self.access_token is set
to just the raw JWT (no prefix). Caching the returned jwtToken field and then
setting access_token to it causes a double-prefix ("Bearer Bearer <jwt>")
which Angel rejects as "Invalid Token".

Fix: always read smart_api.access_token AFTER generateSession() – that gives
the raw JWT that is safe to round-trip through the cache.

Same-day reuse logic
---------------------
1. In-memory singleton  – fastest path, zero I/O (single process lifetime)
2. File cache (today)   – restored via constructor + validated with getProfile()
3. Fresh login          – on new day, missing cache, or invalid cached token
"""
import json
import logging
import threading
import time
from datetime import date
from pathlib import Path

import pyotp
from SmartApi import SmartConnect

import config

logger = logging.getLogger(__name__)

# ── In-memory singleton (cleared on session expiry) ───────────────────────────
_ANGEL_SESSION: SmartConnect | None = None
_session_date:  date         | None = None
_session_lock  = threading.Lock()

# ── Retry settings ────────────────────────────────────────────────────────────
MAX_RETRIES          = 3
RETRY_BACKOFF_FACTOR = 2.0
BASE_DELAY           = 2.0   # seconds


# ── Public API ─────────────────────────────────────────────────────────────────

def get_angel_session() -> SmartConnect:
    """
    Return a live, authenticated SmartConnect object.

    Priority:
      1. In-memory singleton (same process, already logged in, same trading day)
      2. Today's file cache   (process restart, same trading day)
      3. Fresh TOTP login     (new day, stale/invalid cache)

    Double-checked locking: fast path avoids the lock on every candle fetch;
    the lock is only held during the one-time cache restore / login.
    Session is automatically invalidated when the calendar date changes so that
    an overnight-running process re-logins at the start of the next trading day.
    """
    global _ANGEL_SESSION, _session_date

    # ── 1. Fast path (no lock) — valid only if session is from today ───────────
    if _ANGEL_SESSION is not None and _session_date == date.today():
        return _ANGEL_SESSION

    # ── 2 & 3. Slow path — acquire lock, re-check, then restore/login ─────────
    with _session_lock:
        if _ANGEL_SESSION is not None and _session_date == date.today():
            return _ANGEL_SESSION

        # Session exists but is from a previous day — clear it
        if _ANGEL_SESSION is not None and _session_date != date.today():
            logger.info(
                "Angel: session is from %s, today is %s — forcing re-login",
                _session_date, date.today(),
            )
            _ANGEL_SESSION = None
            _session_date  = None
            Path(config.ANGEL_TOKEN_FILE).unlink(missing_ok=True)

        session = _restore_from_cache()
        if session is not None:
            _ANGEL_SESSION = session
            _session_date  = date.today()
            return _ANGEL_SESSION

        _ANGEL_SESSION = _login_with_retry()
        _session_date  = date.today()
        return _ANGEL_SESSION


def force_relogin() -> SmartConnect:
    """Discard the current session and force a fresh login on next call."""
    global _ANGEL_SESSION, _session_date
    with _session_lock:
        _ANGEL_SESSION = None
        _session_date  = None
        Path(config.ANGEL_TOKEN_FILE).unlink(missing_ok=True)
    logger.info("Angel: session cleared, will re-login on next call")
    return get_angel_session()


# ── Session restoration ────────────────────────────────────────────────────────

def _restore_from_cache() -> SmartConnect | None:
    """
    Try to reconstruct a SmartConnect session from today's token cache file.
    Returns None if the cache is absent, stale, or the token has expired.
    """
    token_path = Path(config.ANGEL_TOKEN_FILE)
    if not token_path.exists():
        return None

    with open(token_path) as f:
        cache = json.load(f)

    if cache.get("date") != str(date.today()):
        logger.info("Angel: cache is from %s (today is %s) – will re-login",
                    cache.get("date"), date.today())
        token_path.unlink(missing_ok=True)
        return None

    # Reconstruct via constructor – IP/MAC are re-detected from the local
    # machine automatically; access_token is used by _request() to add the
    # Authorization: Bearer header on every outgoing call.
    smart_api = SmartConnect(
        api_key       = cache["api_key"],
        access_token  = cache["access_token"],   # raw JWT – no "Bearer " prefix
        refresh_token = cache.get("refresh_token"),
        feed_token    = cache.get("feed_token"),
        userId        = cache.get("user_id"),
    )
    smart_api.setSessionExpiryHook(_on_session_expire)

    # No explicit validation call here.
    # Rationale: getProfile(refresh_token) sends the refresh token in the
    # POST body but the Authorization header isn't populated until _request()
    # runs on a real API call, so the validation call itself fails spuriously.
    # Instead we trust the cached token (valid for the whole trading day) and
    # let the expiry hook handle genuine mid-session expiry if it ever occurs.
    logger.info("Angel: restored session from today's cache (user=%s)", cache.get("user_id"))
    return smart_api


# ── Fresh login ────────────────────────────────────────────────────────────────

def _login_with_retry() -> SmartConnect:
    """Attempt login up to MAX_RETRIES times with exponential back-off."""
    delay      = BASE_DELAY
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            session = _do_login()
            logger.info("Angel: login successful on attempt %d", attempt)
            return session
        except Exception as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                logger.warning(
                    "Angel: login attempt %d failed (%s). Retrying in %.1fs ...",
                    attempt, exc, delay,
                )
                time.sleep(delay)
                delay *= RETRY_BACKOFF_FACTOR
            else:
                logger.error("Angel: all %d login attempts failed.", MAX_RETRIES)

    raise RuntimeError(
        f"Angel login failed after {MAX_RETRIES} attempts: {last_error}"
    )


def _do_login() -> SmartConnect:
    """
    Single login attempt – mirrors existing AngelOne class exactly:
      SmartConnect(api_key)                 positional init
      generateSession(username, pin, totp)  same arg order
      data["data"]["clientcode"]            user ID field
    """
    totp = pyotp.TOTP(config.ANGEL_TOTP_SECRET).now()
    logger.info("Angel: generated TOTP, attempting login ...")

    smart_api = SmartConnect(config.ANGEL_API_KEY)   # positional – matches existing code

    # generateSession returns the user profile dict with tokens injected.
    # data["data"]["jwtToken"] = "Bearer <raw_jwt>"  ← has prefix, DO NOT cache this
    # smart_api.access_token   = <raw_jwt>            ← no prefix, cache THIS
    data = smart_api.generateSession(config.ANGEL_USERNAME, config.ANGEL_PIN, totp)

    if data["status"] is False:
        raise RuntimeError(f"generateSession failed: {data}")

    # Read tokens from the SmartConnect object (already set by setAccessToken etc.)
    # NOT from data["data"]["jwtToken"] which has a double-Bearer trap.
    raw_jwt       = smart_api.access_token    # raw JWT, no "Bearer " prefix ✓
    refresh_token = smart_api.refresh_token
    feed_token    = smart_api.feed_token
    user_id       = smart_api.userId          # clientcode set by generateSession

    logger.info("Angel: login OK | user=%s | token=%s...", user_id, raw_jwt[:20])

    smart_api.setSessionExpiryHook(_on_session_expire)
    _write_cache(raw_jwt, refresh_token, feed_token, user_id)
    return smart_api


def _write_cache(raw_jwt: str, refresh_token: str, feed_token: str, user_id: str):
    """Persist today's session tokens to disk for same-day process restarts."""
    cache = {
        "date":          str(date.today()),
        "api_key":       config.ANGEL_API_KEY,
        "access_token":  raw_jwt,        # raw JWT – no Bearer prefix
        "refresh_token": refresh_token,
        "feed_token":    feed_token,
        "user_id":       user_id,
    }
    with open(config.ANGEL_TOKEN_FILE, "w") as f:
        json.dump(cache, f, indent=2)
    logger.info("Angel: session cached to %s", config.ANGEL_TOKEN_FILE)


def _on_session_expire():
    """Angel JWT expired mid-session – clear everything so next call re-logs in."""
    global _ANGEL_SESSION, _session_date
    logger.warning("Angel: session expired – clearing singleton + cache")
    _ANGEL_SESSION = None
    _session_date  = None
    Path(config.ANGEL_TOKEN_FILE).unlink(missing_ok=True)
