"""
SimpleAlgo integrated web interface.

Routes
------
GET  /                  Dashboard — live instrument cards + status
GET  /positions         Current open positions with unrealized P&L
GET  /trades            In-memory trade log for this session
GET  /log               Last 200 lines of today's log; ?f=algo.log.DATE for archives
GET  /kite/login        PIN form → redirects to Zerodha OAuth
POST /kite/login        PIN verification
GET  /callback          Zerodha OAuth callback (saves access token)
GET  /settings          Settings — instrument lots, stock futures list, service control
POST /settings/…        Save lots / add-remove stock futures / stock defaults
POST /service/restart   Restart the whole service (systemd on the VPS)
GET  /api/status        JSON status endpoint
GET  /auth/login        Web login form (username + password from .env)
POST /auth/login        Credential check → sets session
GET  /auth/logout       Clears session

Started from main.py via app.run() in the main thread.
"""
import hmac
import html as _html
import json
import logging
import shlex
import subprocess
import sys
import threading
import time
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

import pytz
from flask import (
    Flask, flash, get_flashed_messages, jsonify, redirect, request, session, url_for,
)
from kiteconnect import KiteConnect

import config
import contract_pin
import instrument_config as icfg
import notifier
import strategy_config as stcfg
import trade_log as tlog
import web_state
import paper_trading
from angel_data import get_option_ltps
from kite_login import get_kite_session
from order_manager import square_off_all
from state import (
    load_state, set_position, get_position,
    refresh_position, clear_position, instrument_lock,
)

IST    = pytz.timezone("Asia/Kolkata")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = config.KITE_AUTH_SECRET_KEY


# ── CSS ────────────────────────────────────────────────────────────────────────

_CSS = """
:root {
  --bg:#0f1117; --card:#1a1d27; --border:#2e3245;
  --text:#e8eaf0; --muted:#8892a4; --gray:#4b5568;
  --accent:#3b82f6;
  --green:#16a34a; --green-l:#3dd68c; --green-bg:#0d3b26; --green-b:#1e6b45;
  --red:#dc2626; --red-l:#f87171; --red-bg:#3b0d0d; --red-b:#6b2020;
  --orange:#ea580c; --orange-l:#fb923c; --orange-bg:#3b2205; --orange-b:#7c3010;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:var(--bg);color:var(--text);min-height:100vh}

/* ── Nav ── */
nav{background:var(--card);border-bottom:1px solid var(--border);
    padding:0 16px;display:flex;align-items:center;gap:4px;height:50px;
    position:sticky;top:0;z-index:100;overflow-x:auto}
.nav-brand{font-weight:700;font-size:1rem;white-space:nowrap;
           margin-right:8px;flex-shrink:0}
nav a{color:var(--muted);text-decoration:none;font-size:0.82rem;
      padding:5px 11px;border-radius:8px;white-space:nowrap;transition:all .15s}
nav a:hover,nav a.active{background:rgba(59,130,246,.15);color:var(--accent)}
.nav-kite{margin-left:auto !important;flex-shrink:0;
          background:rgba(22,163,74,.12) !important;color:var(--green-l) !important}
.nav-kite:hover{background:rgba(22,163,74,.25) !important}

/* ── Re-login button ── */
.relogin-btn{background:rgba(59,130,246,.15);color:var(--accent);border:1px solid rgba(59,130,246,.3);
             border-radius:20px;font-size:0.75rem;font-weight:600;padding:4px 11px;
             cursor:pointer;white-space:nowrap;transition:all .15s}
.relogin-btn:hover{background:rgba(59,130,246,.3)}
.relogin-btn:disabled{opacity:.5;cursor:wait}

/* ── Layout ── */
.content{padding:20px 16px;max-width:1200px;margin:0 auto}
h2{font-size:1.05rem;margin-bottom:16px}

/* ── Status bar ── */
.status-bar{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px;
            align-items:center}
.badge{display:inline-flex;align-items:center;gap:5px;padding:4px 11px;
       border-radius:20px;font-size:0.75rem;font-weight:600;white-space:nowrap}
.b-ok   {background:var(--green-bg);color:var(--green-l);border:1px solid var(--green-b)}
.b-warn {background:var(--orange-bg);color:var(--orange-l);border:1px solid var(--orange-b)}
.b-err  {background:var(--red-bg);color:var(--red-l);border:1px solid var(--red-b)}
.b-neutral{background:var(--card);color:var(--muted);border:1px solid var(--border)}

/* ── Instrument cards ── */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--border);
      border-radius:12px;padding:16px}
.card-header{display:flex;justify-content:space-between;
             align-items:flex-start;margin-bottom:10px}
.card-name{font-size:1.05rem;font-weight:700}
.card-sym{font-size:0.72rem;color:var(--muted);margin-top:2px}
.card-pos{font-size:0.72rem;font-weight:600;padding:2px 9px;
          border-radius:10px;flex-shrink:0}
.p-long {background:var(--green-bg);color:var(--green-l)}
.p-short{background:var(--red-bg);color:var(--red-l)}
.p-flat {background:#1e2030;color:var(--muted)}
.card-signal{font-size:1.3rem;font-weight:700;margin:8px 0 10px}
.s-buy {color:var(--green-l)}.s-sell{color:var(--red-l)}
.s-exit{color:var(--orange-l)}.s-none{color:var(--muted)}
.metrics{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:0.78rem}
.ml{color:var(--muted)}.mv{text-align:right;font-variant-numeric:tabular-nums}
.card-foot{margin-top:10px;font-size:0.7rem;color:var(--gray)}

/* ── Table ── */
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:0.82rem}
th{text-align:left;padding:9px 11px;color:var(--muted);font-weight:600;
   font-size:0.75rem;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:9px 11px;border-bottom:1px solid rgba(46,50,69,.5);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.02)}

/* ── Pills ── */
.pill{display:inline-block;padding:2px 9px;border-radius:10px;
      font-size:0.72rem;font-weight:600}
.pl-buy {background:var(--green-bg);color:var(--green-l)}
.pl-sell{background:var(--red-bg);color:var(--red-l)}
.pl-exit{background:var(--orange-bg);color:var(--orange-l)}
.pl-none{background:#1e2030;color:var(--muted)}
.pl-dry {background:#1a1d40;color:#818cf8;border:1px solid #3730a3}

/* ── Log ── */
pre.log{background:#0a0c12;border:1px solid var(--border);border-radius:8px;
        padding:14px;font-size:0.7rem;line-height:1.6;overflow-x:auto;
        white-space:pre-wrap;word-break:break-all;max-height:70vh;overflow-y:auto}

/* ── Strategy toggle ── */
.toggle-wrap{display:flex;align-items:center;gap:6px;margin-top:10px;
             padding-top:10px;border-top:1px solid var(--border)}
.toggle-label{font-size:.72rem;color:var(--muted);flex:1}
.tog-btn{padding:3px 10px;border:none;border-radius:8px;font-size:.72rem;
         font-weight:600;cursor:pointer;transition:opacity .15s}
.tog-btn:hover{opacity:.82}
.tog-on {background:rgba(22,163,74,.2);color:var(--green-l);border:1px solid var(--green-b)}
.tog-off{background:rgba(75,85,104,.3);color:var(--muted);border:1px solid var(--border)}

/* ── Rollover / contract pin ── */
.rollover-wrap{display:flex;align-items:center;gap:8px;padding:6px 0 2px;
               border-top:1px solid var(--border);flex-wrap:wrap}
.rollover-btn{padding:3px 10px;border:none;border-radius:8px;font-size:.72rem;
              font-weight:600;cursor:pointer;transition:opacity .15s;
              background:rgba(234,179,8,.15);color:#fde047;
              border:1px solid rgba(234,179,8,.35)}
.rollover-btn:hover{opacity:.82}
.pin-badge{font-size:.68rem;color:#fde047;flex:1}
.pin-clear-btn{padding:2px 8px;border:none;border-radius:6px;font-size:.68rem;
               cursor:pointer;background:rgba(239,68,68,.15);color:#fca5a5;
               border:1px solid rgba(239,68,68,.3)}
.pin-clear-btn:hover{opacity:.82}

/* ── Card P&L ── */
.card-pnl{font-size:1rem;font-weight:700;margin:6px 0 10px;
          display:flex;align-items:baseline;gap:6px}
.pnl-est{font-size:.68rem;font-weight:400;color:var(--muted)}

/* ── Booked-manually badge / button ── */
.bm-wrap{display:flex;align-items:center;gap:8px;padding:6px 0 2px;
         border-top:1px solid var(--border);flex-wrap:wrap}
.bm-badge{font-size:.68rem;color:#fb923c;flex:1}
.bm-phase{font-size:.62rem;color:var(--muted);display:block;margin-top:1px}
.bm-btn{padding:3px 10px;border:none;border-radius:8px;font-size:.72rem;
        font-weight:600;cursor:pointer;transition:opacity .15s;
        background:rgba(234,88,12,.2);color:#fb923c;
        border:1px solid rgba(234,88,12,.4)}
.bm-btn:hover{opacity:.82}
.bm-clear-btn{padding:2px 8px;border:none;border-radius:6px;font-size:.68rem;
              cursor:pointer;background:rgba(75,85,104,.3);color:var(--muted);
              border:1px solid var(--border)}
.bm-clear-btn:hover{opacity:.82}

/* ── Halted banner / resume button ── */
.halt-wrap{display:flex;align-items:center;gap:8px;padding:6px 0 2px;
           border-top:1px solid rgba(239,68,68,.45);flex-wrap:wrap}
.halt-badge{font-size:.68rem;color:#fca5a5;font-weight:700;flex:1}
.halt-reason{font-size:.62rem;color:var(--muted);font-weight:400;
             display:block;margin-top:1px;word-break:break-word}
.halt-resume-btn{padding:3px 10px;border:none;border-radius:8px;font-size:.72rem;
                 font-weight:600;cursor:pointer;transition:opacity .15s;
                 background:rgba(239,68,68,.18);color:#fca5a5;
                 border:1px solid rgba(239,68,68,.4)}
.halt-resume-btn:hover{opacity:.82}

/* ── Stats bar ── */
.stats-bar{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));
           gap:10px;margin-bottom:20px}
.stat{background:var(--card);border:1px solid var(--border);border-radius:10px;
      padding:12px 14px}
.stat-label{font-size:.72rem;color:var(--muted);margin-bottom:4px}
.stat-value{font-size:1.1rem;font-weight:700}

/* ── Action buttons ── */
.btn-bar{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}
.btn-sm{display:inline-block;padding:8px 16px;border:none;border-radius:8px;
        font-size:.82rem;font-weight:600;cursor:pointer;text-decoration:none;
        transition:opacity .15s}
.btn-sm:hover{opacity:.85}
.btn-danger-sm{background:var(--red);color:#fff}
.btn-warn-sm{background:var(--orange);color:#fff}

/* ── Auth pages ── */
.auth-wrap{display:flex;align-items:center;justify-content:center;
           min-height:calc(100vh - 50px);padding:20px}
.auth-card{background:var(--card);border:1px solid var(--border);
           border-radius:16px;padding:32px 24px;width:100%;max-width:380px}
.auth-card h1{font-size:1.25rem;margin-bottom:6px}
.subtitle{font-size:0.83rem;color:var(--muted);margin-bottom:20px}
label{display:block;font-size:0.8rem;color:var(--muted);margin-bottom:5px}
input[type=text],input[type=password]{width:100%;padding:11px 13px;background:var(--bg);
  border:1px solid var(--border);border-radius:9px;color:var(--text);
  font-size:1rem;margin-bottom:14px;outline:none}
input[type=text]:focus,input[type=password]:focus{border-color:var(--accent)}
.btn{display:block;width:100%;padding:12px;border:none;border-radius:9px;
     font-size:0.92rem;font-weight:600;cursor:pointer;text-align:center;
     text-decoration:none;transition:opacity .15s}
.btn:hover{opacity:.88}
.btn-primary{background:var(--accent);color:#fff}
.btn-success{background:var(--green);color:#fff}
.btn-danger {background:var(--red);color:#fff;margin-top:8px}
.btn-secondary{background:var(--border);color:var(--text);margin-top:8px}
.err-msg{font-size:0.82rem;color:var(--red-l);margin-bottom:12px}
hr{border:none;border-top:1px solid var(--border);margin:18px 0}

/* ── Flash messages ── */
.flash-wrap{max-width:1200px;margin:14px auto 0;padding:0 16px;
            display:flex;flex-direction:column;gap:8px}
.flash{padding:10px 14px;border-radius:9px;font-size:.82rem;line-height:1.5;
       border:1px solid var(--border);background:var(--card);color:var(--text)}
.flash-ok  {background:var(--green-bg);color:var(--green-l);border-color:var(--green-b)}
.flash-warn{background:var(--orange-bg);color:var(--orange-l);border-color:var(--orange-b)}
.flash-err {background:var(--red-bg);color:var(--red-l);border-color:var(--red-b)}

/* ── Settings page ── */
.panel{background:var(--card);border:1px solid var(--border);border-radius:12px;
       padding:18px;margin-bottom:18px}
.panel-head{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:4px}
.panel-head h3{font-size:.95rem;font-weight:700}
.panel-desc{font-size:.75rem;color:var(--muted);line-height:1.55;margin-bottom:14px}
.panel-desc code{background:#0a0c12;border:1px solid var(--border);border-radius:4px;
                 padding:1px 5px;font-size:.72rem}
.set-tbl td,.set-tbl th{padding:7px 10px}
.set-tbl td.num{font-variant-numeric:tabular-nums;color:var(--muted)}
.inline-form{display:flex;align-items:center;gap:6px;margin:0}
.qty-input{width:74px;padding:5px 8px;background:var(--bg);border:1px solid var(--border);
           border-radius:7px;color:var(--text);font-size:.8rem;
           font-variant-numeric:tabular-nums;outline:none}
.qty-input:focus{border-color:var(--accent)}
.qty-input:disabled{opacity:.45;cursor:not-allowed}
.txt-input{padding:7px 10px;background:var(--bg);border:1px solid var(--border);
           border-radius:7px;color:var(--text);font-size:.85rem;outline:none;min-width:170px}
.txt-input:focus{border-color:var(--accent)}
select.txt-input{min-width:110px}
.btn-xs{padding:5px 12px;border:none;border-radius:7px;font-size:.75rem;font-weight:600;
        cursor:pointer;transition:opacity .15s;white-space:nowrap}
.btn-xs:hover{opacity:.85}
.btn-xs:disabled{opacity:.4;cursor:not-allowed}
.btn-save   {background:rgba(59,130,246,.18);color:var(--accent);
             border:1px solid rgba(59,130,246,.4)}
.btn-add    {background:rgba(22,163,74,.2);color:var(--green-l);
             border:1px solid var(--green-b)}
.btn-remove {background:rgba(239,68,68,.15);color:#fca5a5;
             border:1px solid rgba(239,68,68,.3)}
.btn-restart{background:rgba(239,68,68,.18);color:#fca5a5;
             border:1px solid rgba(239,68,68,.45);padding:9px 18px;font-size:.85rem}
.lock-note{font-size:.7rem;color:var(--orange-l);white-space:nowrap}
.add-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:4px}
.add-row .txt-input{flex:0 1 260px}
.add-row label{margin:0;white-space:nowrap}
.src-tag{font-size:.62rem;color:var(--muted);background:#1e2030;
         padding:1px 7px;border-radius:5px;white-space:nowrap}
.empty{text-align:center;padding:48px;color:var(--muted);font-size:0.88rem}
.note{font-size:0.75rem;color:var(--gray);margin-bottom:12px}
.pnl-pos{color:var(--green-l)}.pnl-neg{color:var(--red-l)}
"""


# ── Layout wrapper ─────────────────────────────────────────────────────────────

_NAV = [
    ("Dashboard", "/",          "dashboard"),
    ("Positions", "/positions", "positions"),
    ("Trades",    "/trades",    "trades"),
    ("Log",       "/log",       "log"),
    ("Settings",  "/settings",  "settings"),
]

_FLASH_CLASS = {"ok": "flash-ok", "warn": "flash-warn", "err": "flash-err"}


def _flash_html() -> str:
    """Render and consume any queued flash messages."""
    msgs = get_flashed_messages(with_categories=True)
    if not msgs:
        return ""
    items = "".join(
        f'<div class="flash {_FLASH_CLASS.get(cat, "")}">{msg}</div>'
        for cat, msg in msgs
    )
    return f'<div class="flash-wrap">{items}</div>'


# Shared by the dashboard status bar and the Settings page. Plain string (not an
# f-string) so the JS braces need no doubling; embed it as {_RESTART_JS}.
_RESTART_JS = """
function doRestart(btn){
  if(!confirm('Restart the SimpleAlgo service?\\n\\nThe scheduler stops for a few '
      + 'seconds. Open positions stay open at the broker and are reloaded from '
      + 'positions_state.json on startup.')) return;
  // Kill the page's meta-refresh so it cannot reload mid-restart.
  var mr=document.querySelector('meta[http-equiv="refresh"]');
  if(mr) mr.parentNode.removeChild(mr);
  btn.disabled=true;
  var orig=btn.innerHTML;
  btn.textContent='Restarting…';
  fetch('/service/restart',{method:'POST',headers:{'X-Requested-With':'XMLHttpRequest'}})
    .then(function(r){return r.json();})
    .then(function(d){
      if(!d.success){
        btn.textContent='\\u2717 '+(d.error||'Restart failed');
        btn.disabled=false;
        setTimeout(function(){btn.innerHTML=orig;},8000);
        return;
      }
      btn.textContent='Restarting — waiting for the server…';
      var tries=0;
      var poll=setInterval(function(){
        tries++;
        fetch('/api/status',{cache:'no-store'})
          .then(function(r){
            // Ignore the first couple of probes: the old process may still be
            // answering when they fire.
            if(r.ok&&tries>2){clearInterval(poll);location.reload();}
          })
          .catch(function(){});
        if(tries>60){
          clearInterval(poll);
          btn.textContent='Server did not come back — check the host';
        }
      },2000);
    })
    .catch(function(){
      btn.textContent='\\u2717 Request failed';
      btn.disabled=false;
      setTimeout(function(){btn.innerHTML=orig;},5000);
    });
}
"""


def _layout(title: str, body: str, active: str = "", refresh: int = 0) -> str:
    links = "".join(
        f'<a href="{url}" class="{"active" if active == key else ""}">{label}</a>'
        for label, url, key in _NAV
    )
    # A meta-refresh would wipe flash messages before they can be read, so any
    # page carrying one renders without auto-refresh this once.
    flashes = _flash_html()
    if flashes:
        refresh = 0
    refresh_tag = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{refresh_tag}
<title>{title} – SimpleAlgo</title>
<style>{_CSS}</style>
</head>
<body>
<nav>
  <span class="nav-brand">SimpleAlgo</span>
  {links}
  <a href="/kite/login" class="nav-kite">&#128274; Kite Login</a>
  <a href="/auth/logout" style="margin-left:4px;color:var(--muted) !important">Logout</a>
</nav>
{flashes}
{body}
</body>
</html>"""


# ── Formatting helpers ─────────────────────────────────────────────────────────

def _fmt(val, dec: int = 2) -> str:
    try:
        f = float(val)
        return "-" if f != f else f"{f:,.{dec}f}"   # NaN check: NaN != NaN
    except (TypeError, ValueError):
        return "-"


_SIG_CARD  = {"BUY": "s-buy", "SELL": "s-sell",
               "EXIT_LONG": "s-exit", "EXIT_SHORT": "s-exit"}
_SIG_PILL  = {"BUY": "pl-buy", "SELL": "pl-sell",
               "EXIT_LONG": "pl-exit", "EXIT_SHORT": "pl-exit"}
_SIG_LABEL = {"EXIT_LONG": "EXIT LONG", "EXIT_SHORT": "EXIT SHORT"}


def _signal_card(sig: str) -> str:
    label = _SIG_LABEL.get(sig, sig) if sig and sig != "None" else "-"
    cls   = _SIG_CARD.get(sig, "s-none")
    return f'<div class="card-signal {cls}">{label}</div>'


def _pill(sig: str) -> str:
    label = _SIG_LABEL.get(sig, sig) if sig and sig != "None" else "-"
    cls   = _SIG_PILL.get(sig, "pl-none")
    return f'<span class="pill {cls}">{label}</span>'


def _pos_badge(pos: int) -> str:
    if pos > 0: return '<span class="card-pos p-long">LONG</span>'
    if pos < 0: return '<span class="card-pos p-short">SHORT</span>'
    return '<span class="card-pos p-flat">FLAT</span>'


def _token_status() -> dict:
    path = Path(config.KITE_TOKEN_FILE)
    if not path.exists():
        return {"valid": False, "date": None}
    try:
        cache = json.loads(path.read_text())
        return {
            "valid": cache.get("date") == str(date.today()) and bool(cache.get("access_token")),
            "date":  cache.get("date"),
        }
    except Exception:
        return {"valid": False, "date": None}


def _save_token(access_token: str) -> None:
    Path(config.KITE_TOKEN_FILE).write_text(
        json.dumps({"date": str(date.today()), "access_token": access_token}, indent=2)
    )


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _check_credentials(username: str, password: str) -> bool:
    """Constant-time comparison to avoid timing attacks."""
    ok_user = hmac.compare_digest(username, config.WEB_USERNAME or "")
    ok_pass = hmac.compare_digest(password, config.WEB_PASSWORD or "")
    return ok_user and ok_pass


def login_required(f):
    """Decorator: redirect to /auth/login if the user is not in the session."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("auth_login", next=request.path))
        return f(*args, **kwargs)
    return decorated


@app.route("/auth/login", methods=["GET", "POST"])
def auth_login():
    error = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if _check_credentials(username, password):
            session["logged_in"] = True
            next_url = request.args.get("next") or "/"
            # Prevent open redirect: only allow relative paths with no netloc
            # (catches both http://evil.com and //evil.com variants)
            parsed = urlparse(next_url)
            if parsed.netloc or not next_url.startswith("/"):
                next_url = "/"
            return redirect(next_url)
        error = "Incorrect username or password."

    body = f"""
<div class="auth-wrap">
  <div class="auth-card">
    <h1>SimpleAlgo</h1>
    <p class="subtitle">Sign in to access the dashboard</p>
    {'<p class="err-msg">' + error + '</p>' if error else ''}
    <form method="post">
      <label for="username">Username</label>
      <input type="text" id="username" name="username"
             autocomplete="username" autofocus placeholder="username">
      <label for="password">Password</label>
      <input type="password" id="password" name="password"
             autocomplete="current-password" placeholder="••••••">
      <button type="submit" class="btn btn-primary">Sign in</button>
    </form>
  </div>
</div>"""
    # Login page has no nav (user is not authenticated yet)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Login – SimpleAlgo</title>
<style>{_CSS}</style>
</head>
<body>{body}</body>
</html>"""


@app.route("/auth/logout")
def auth_logout():
    session.clear()
    return redirect(url_for("auth_login"))


# ── Dashboard (/) ──────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def dashboard():
    snap  = web_state.snapshot()
    tok   = _token_status()
    sched = snap["scheduler"]
    state = load_state()
    inst_names = [i["name"] for i in config.INSTRUMENTS]
    enabled_map = stcfg.get_all(inst_names)
    all_inst_names = (
        inst_names
        + [i["name"] for i in config.HOURLY_INSTRUMENTS]
        + [i["name"] for i in config.STOCK_INSTRUMENTS]
    )
    bm_map   = stcfg.get_all_booked_manually(all_inst_names)
    halt_map = stcfg.get_all_halted(all_inst_names)

    # Status badges
    if config.DRY_RUN:
        tok_badge = '<span class="badge b-ok">&#10003; Kite token not required (DRY RUN)</span>'
    elif tok["valid"]:
        tok_badge = (
            '<span class="badge b-ok">&#10003; Kite token valid</span>'
            '<button class="relogin-btn" onclick="doRelogin(this)" title="Re-run auto-login">'
            '&#128274; Re-login</button>'
        )
    else:
        note = f"last: {tok['date']}" if tok["date"] else "never"
        tok_badge = (
            f'<span class="badge b-warn">&#9888; No Kite token ({note})</span>'
            f'<button class="relogin-btn" onclick="doRelogin(this)" title="Run auto-login now">'
            f'&#128274; Auto-login</button>'
            f'&nbsp;<a href="/kite/login" class="badge b-neutral" style="text-decoration:none">'
            f'&#128279; Manual login</a>'
        )

    if sched["running"]:
        last = sched["last_run"].strftime("%H:%M:%S") if sched["last_run"] else "not yet"
        sched_badge = (f'<span class="badge b-ok">&#9654; Scheduler running &bull; '
                       f'last {last} &bull; {sched["run_count"]} runs</span>')
    else:
        sched_badge = '<span class="badge b-neutral">&#9632; Scheduler stopped</span>'

    # Restart button — same endpoint as the Settings page. Hidden entirely when
    # no restart command is available, so it can never look like a dead control.
    restart_argv, restart_display = _restart_command()
    restart_badge = (
        f'<button class="btn-xs btn-restart" style="padding:4px 11px;font-size:.75rem"'
        f' onclick="doRestart(this)" title="{_html.escape(restart_display)}">'
        f'&#128260; Restart Service</button>'
    ) if restart_argv else ""

    hourly_names   = [i["name"] for i in config.HOURLY_INSTRUMENTS]
    hourly_enabled = stcfg.get_all(hourly_names)

    stock_names    = [i["name"] for i in config.STOCK_INSTRUMENTS]
    stock_enabled  = stcfg.get_all(stock_names)

    # Build name → exchange/mode lookup and load all active pins
    all_inst_defs  = config.INSTRUMENTS + config.HOURLY_INSTRUMENTS + config.STOCK_INSTRUMENTS
    inst_exchange  = {i["name"]: i["exchange"] for i in all_inst_defs}
    inst_synthetic = {i["name"] for i in all_inst_defs if i.get("mode") == "SYNTHETIC"}
    all_pins       = contract_pin.list_pins()
    # Hourly instruments pin under their underlying name (e.g. GOLDM_H → GOLDM).
    # Build a fallback map so the UI shows the pin badge on the hourly card too.
    hourly_underlying = {h["name"]: h.get("underlying", h["name"]) for h in config.HOURLY_INSTRUMENTS}

    # Instrument cards — show all configured instruments, even before first candle
    instruments = snap["instruments"]
    cards = "".join(
        _make_card(
            name, instruments.get(name, {}), state, enabled_map.get(name, True),
            exchange=inst_exchange.get(name, ""),
            pin=all_pins.get(name.upper()),
            show_rollover=(name not in inst_synthetic),
            booked_manually=bm_map.get(name),
            halted=halt_map.get(name),
        )
        for name in inst_names
    )
    hourly_cards = "".join(
        _make_card(
            name, instruments.get(name, {}), state,
            hourly_enabled.get(name, True), timeframe_label="1H",
            exchange=inst_exchange.get(name, ""),
            pin=(all_pins.get(name.upper())
                 or all_pins.get(hourly_underlying.get(name, name).upper())),
            show_rollover=(name not in inst_synthetic),
            booked_manually=bm_map.get(name),
            halted=halt_map.get(name),
        )
        for name in hourly_names
    )
    stock_cards = "".join(
        _make_card(
            name, instruments.get(name, {}), state,
            stock_enabled.get(name, True),
            exchange=inst_exchange.get(name, "NFO"),
            pin=all_pins.get(name.upper()),
            show_rollover=True,
            booked_manually=bm_map.get(name),
            halted=halt_map.get(name),
        )
        for name in stock_names
    )

    stock_section = ""
    if stock_names:
        stock_section = f"""
  <h2 style="margin-top:24px">Stock Futures (15M)</h2>
  <div class="grid">{stock_cards}</div>"""

    body = f"""
<div class="content">
  <div class="status-bar">
    {tok_badge}
    {sched_badge}
    <span class="badge b-neutral" id="clk"></span>
    {restart_badge}
  </div>
  <h2>15-Minute Strategies</h2>
  <div class="grid">{cards}</div>
  <h2 style="margin-top:24px">Hourly Strategies (1H)</h2>
  <div class="grid">{hourly_cards}</div>{stock_section}
</div>
<script>
(function(){{
  function tick(){{
    var d=new Date();
    var s=d.toLocaleTimeString('en-IN',{{timeZone:'Asia/Kolkata',
      hour:'2-digit',minute:'2-digit',second:'2-digit'}});
    document.getElementById('clk').textContent=s+' IST';
  }}
  tick(); setInterval(tick,1000);
}})();
function doRelogin(btn){{
  btn.disabled=true;
  var orig=btn.textContent;
  btn.textContent='Logging in…';
  fetch('/kite/relogin',{{method:'POST',headers:{{'X-Requested-With':'XMLHttpRequest'}}}})
    .then(function(r){{return r.json();}})
    .then(function(d){{
      if(d.success){{
        btn.textContent='✓ Done';
        btn.style.background='rgba(22,163,74,.25)';
        btn.style.color='var(--green-l)';
        setTimeout(function(){{location.reload();}},1200);
      }}else{{
        btn.textContent='✗ Failed — check logs';
        btn.style.background='rgba(220,38,38,.2)';
        btn.style.color='var(--red-l)';
        btn.disabled=false;
        setTimeout(function(){{btn.textContent=orig;btn.style.background='';btn.style.color='';}},4000);
      }}
    }})
    .catch(function(){{
      btn.textContent='✗ Error';
      btn.disabled=false;
      setTimeout(function(){{btn.textContent=orig;btn.style.background='';btn.style.color='';}},3000);
    }});
}}
{_RESTART_JS}
</script>"""
    return _layout("Dashboard", body, active="dashboard", refresh=30)


def _make_card(name: str, data: dict, state: dict, enabled: bool,
               timeframe_label: str = "", exchange: str = "",
               pin: dict | None = None, show_rollover: bool = True,
               booked_manually: dict | None = None,
               halted: dict | None = None) -> str:
    pos_size    = state.get(name, {}).get("position_size", 0)
    entry_price = state.get(name, {}).get("entry_price", 0.0)
    signal      = str(data.get("signal", ""))
    close       = data.get("close")
    updated     = data.get("updated_at")
    updated_str = updated.strftime("%H:%M:%S") if updated else "no data yet"

    # ── P&L section (only when in a position) ─────────────────────────────────
    pnl_html = ""
    if pos_size != 0 and entry_price and close:
        raw_pnl  = (float(close) - float(entry_price)) * pos_size
        pnl_cls  = "pnl-pos" if raw_pnl >= 0 else "pnl-neg"
        pnl_sign = "+" if raw_pnl >= 0 else ""
        pnl_html = (f'<div class="card-pnl">'
                    f'<span class="{pnl_cls}">{pnl_sign}{_fmt(raw_pnl)}</span>'
                    f'<span class="pnl-est">est. P&amp;L</span>'
                    f'<span style="font-size:.72rem;color:var(--muted)">'
                    f'@ {_fmt(entry_price)} entry</span>'
                    f'</div>')

    # ── Signal row — show exit-only context when in a position ────────────────
    sig_html = ""
    if data:  # have candle data
        if pos_size != 0:
            # In a position: only exit signals are relevant
            exit_sig = signal if signal in ("EXIT_LONG", "EXIT_SHORT") else None
            if exit_sig:
                sig_html = _signal_card(exit_sig)
            else:
                sig_html = '<div class="card-signal s-none">Holding</div>'
        else:
            sig_html = _signal_card(signal)

    # ── Metrics (hidden if no data) ────────────────────────────────────────────
    metrics_html = ""
    if data:
        metrics_html = f"""<div class="metrics">
    <span class="ml">Close</span>     <span class="mv">{_fmt(close)}</span>
    <span class="ml">ST1 (10,2)</span><span class="mv">{_fmt(data.get("st1"))}</span>
    <span class="ml">ST2 (10,3)</span><span class="mv">{_fmt(data.get("st2"))}</span>
    <span class="ml">MA50</span>       <span class="mv">{_fmt(data.get("ma"))}</span>
  </div>"""

    # ── Strategy toggle button ────────────────────────────────────────────────
    if enabled:
        tog_cls   = "tog-btn tog-on"
        tog_label = "&#10003; Enabled"
        tog_next  = "false"
    else:
        tog_cls   = "tog-btn tog-off"
        tog_label = "&#9632; Disabled"
        tog_next  = "true"

    toggle_html = f"""
  <div class="toggle-wrap">
    <span class="toggle-label">Strategy</span>
    <form method="post" action="/strategy/toggle" style="margin:0">
      <input type="hidden" name="name" value="{name}">
      <input type="hidden" name="enabled" value="{tog_next}">
      <button type="submit" class="{tog_cls}">{tog_label}</button>
    </form>
  </div>"""

    # Dim only the card body (not the toggle) so the button stays clickable
    body_style = ' style="opacity:.45;pointer-events:none"' if not enabled else ""

    tf_badge = (
        f'<span style="font-size:.62rem;color:var(--muted);background:#1e2030;'
        f'padding:1px 6px;border-radius:5px;margin-top:3px;display:inline-block">'
        f'{timeframe_label}</span>'
    ) if timeframe_label else ""

    # ── Rollover section ──────────────────────────────────────────────────────
    rollover_html = ""
    if show_rollover and exchange:
        if pin:
            pin_sym = pin.get("kite_tradingsymbol", "")
            pin_exp = pin.get("expiry", "")
            rollover_html = f"""
  <div class="rollover-wrap">
    <span class="pin-badge">&#128204; Pinned: {pin_sym} (exp {pin_exp})</span>
    <form method="post" action="/instrument/rollover/clear" style="margin:0">
      <input type="hidden" name="name"     value="{name}">
      <button type="submit" class="pin-clear-btn">&#10005; Clear Pin</button>
    </form>
  </div>"""
        else:
            rollover_html = f"""
  <div class="rollover-wrap">
    <form method="post" action="/instrument/rollover" style="margin:0">
      <input type="hidden" name="name"     value="{name}">
      <input type="hidden" name="exchange" value="{exchange}">
      <button type="submit" class="rollover-btn">&#8594; Roll to Next Month</button>
    </form>
  </div>"""

    # ── Halted section ────────────────────────────────────────────────────────
    # Set automatically when an order outcome could not be confirmed or a
    # synthetic order partially filled. Resume only after reconciling on Kite.
    halt_html = ""
    if halted:
        halt_reason = _html.escape(halted.get("reason", ""))
        halt_time   = _html.escape(halted.get("time", ""))
        halt_html = f"""
  <div class="halt-wrap">
    <span class="halt-badge">&#128721; HALTED since {halt_time}
      <span class="halt-reason">{halt_reason}</span>
      <span class="halt-reason">Verify the position on Kite before resuming.</span>
    </span>
    <form method="post" action="/strategy/resume_halt" style="margin:0"
          onsubmit="return confirm('Resume {name}? Only do this AFTER verifying on Kite that the actual position matches the dashboard.');">
      <input type="hidden" name="name" value="{name}">
      <button type="submit" class="halt-resume-btn">&#9654; Resume</button>
    </form>
  </div>"""

    # ── Booked-manually section ───────────────────────────────────────────────
    bm_html = ""
    if booked_manually:
        direction = booked_manually["direction"]
        sl_fired  = booked_manually["sl_fired"]
        if sl_fired:
            phase_text = "SL hit — waiting for next entry signal"
        else:
            phase_text = f"Waiting for {direction} SL signal to fire"
        bm_html = f"""
  <div class="bm-wrap">
    <span class="bm-badge">&#128203; Booked Manually
      <span class="bm-phase">{phase_text}</span>
    </span>
    <form method="post" action="/strategy/unbook_manually" style="margin:0">
      <input type="hidden" name="name" value="{name}">
      <button type="submit" class="bm-clear-btn">&#10005; Clear</button>
    </form>
  </div>"""
    elif pos_size != 0 and enabled:
        bm_html = f"""
  <div class="bm-wrap">
    <form method="post" action="/strategy/book_manually" style="margin:0">
      <input type="hidden" name="name" value="{name}">
      <button type="submit" class="bm-btn">&#128203; Book Manually</button>
    </form>
  </div>"""

    return f"""
<div class="card">
  <div{body_style}>
    <div class="card-header">
      <div>
        <div class="card-name">{name}</div>
        <div class="card-sym">{data.get("kite_tradingsymbol","")}&nbsp;{tf_badge}</div>
      </div>
      {_pos_badge(pos_size)}
    </div>
    {pnl_html}
    {sig_html}
    {metrics_html}
    <div class="card-foot">Updated {updated_str}</div>
  </div>
  {halt_html}
  {rollover_html}
  {bm_html}
  {toggle_html}
</div>"""


# ── Strategy toggle route ─────────────────────────────────────────────────────

@app.route("/strategy/toggle", methods=["POST"])
@login_required
def strategy_toggle():
    name    = request.form.get("name", "").strip()
    enabled = request.form.get("enabled", "true").lower() == "true"
    if name:
        stcfg.set_enabled(name, enabled)
        logger.info("Strategy %s set to enabled=%s", name, enabled)
    return redirect("/")


@app.route("/strategy/book_manually", methods=["POST"])
@login_required
def strategy_book_manually():
    """Mark a strategy as 'Booked Manually': clears the algo position state and
    blocks new entries until the natural SL fires and the next entry signal appears."""
    name = request.form.get("name", "").strip()
    if not name:
        return redirect("/")
    state = load_state()
    with instrument_lock(name):
        refresh_position(state, name)
        pos_size = get_position(state, name)
        if pos_size == 0:
            logger.warning("book_manually: %s has no open position in algo state — nothing to book", name)
            return redirect("/")
        direction = "LONG" if pos_size > 0 else "SHORT"
        # Clear algo position state — the broker position is already closed
        set_position(state, name, 0)
    # Also clear paper position if present (DRY_RUN case)
    if paper_trading.get_position_size(name) != 0:
        snap  = web_state.snapshot()
        close = snap["instruments"].get(name, {}).get("close", 0.0)
        paper_trading.close_position(name, float(close) if close else 0.0)
    stcfg.set_booked_manually(name, direction)
    logger.info("Strategy %s booked manually (was %s)", name, direction)
    return redirect("/")


@app.route("/strategy/unbook_manually", methods=["POST"])
@login_required
def strategy_unbook_manually():
    """Clear the 'Booked Manually' flag, resuming normal strategy behaviour."""
    name = request.form.get("name", "").strip()
    if name:
        stcfg.clear_booked_manually(name)
        logger.info("Strategy %s: booked-manually flag cleared", name)
    return redirect("/")


@app.route("/strategy/resume_halt", methods=["POST"])
@login_required
def strategy_resume_halt():
    """Clear a trading halt after the operator has reconciled the position on
    Kite. The halt was set because an order's outcome could not be confirmed
    (or a synthetic order partially filled), so the saved state was untrusted."""
    name = request.form.get("name", "").strip()
    if name:
        halted = stcfg.get_halted(name)
        stcfg.clear_halted(name)
        logger.warning(
            "Strategy %s: HALT cleared by operator (was: %s)",
            name, (halted or {}).get("reason", "?"),
        )
    return redirect("/")


# ── Contract rollover / pin routes ────────────────────────────────────────────

@app.route("/instrument/rollover", methods=["POST"])
@login_required
def instrument_rollover():
    """Pin the next-month futures contract for an instrument."""
    name     = request.form.get("name",     "").strip()
    exchange = request.form.get("exchange", "").strip()
    if not name or not exchange:
        logger.warning("instrument_rollover: missing name or exchange")
        return redirect("/")
    try:
        # Use the currently-resolved instrument's expiry as reference so we pin
        # the month AFTER whatever is currently being traded — not just contracts[1]
        # from the scrip master (which could skip a month if the near contract
        # has already expired from the master's active list).
        import main as _main
        all_instruments = (
            _main.RESOLVED_INSTRUMENTS
            + _main.RESOLVED_HOURLY_INSTRUMENTS
            + _main.RESOLVED_STOCK_INSTRUMENTS
        )
        current = next((i for i in all_instruments if i["name"] == name), None)
        after_expiry = current.get("expiry") if current else None
        # Hourly instruments (e.g. GOLDM_H) inherit their contract from the
        # underlying base (e.g. GOLDM).  The scrip master only knows the base
        # name, so pin and re-resolve under that name.
        # Look up underlying from config as well, in case the instrument failed
        # to resolve (e.g. current contract just expired) and isn't in any list.
        h_cfg = next(
            (h for h in config.HOURLY_INSTRUMENTS if h["name"] == name), None
        )
        underlying = (
            (current.get("underlying") if current else None)
            or (h_cfg.get("underlying") if h_cfg else None)
        )
        pin_name = underlying if underlying else name
        pinned = contract_pin.pin_next_month(pin_name, exchange, after_expiry=after_expiry)
        logger.info(
            "Webapp: rollover pin set for %s → %s (expires %s)",
            pin_name, pinned["kite_tradingsymbol"], pinned["expiry"],
        )
        # Immediately update the in-memory resolved instrument so the next
        # strategy tick picks up the new contract without needing an app restart.
        _main.re_resolve_instrument(pin_name)
    except Exception as exc:
        logger.error("instrument_rollover failed for %s: %s", name, exc)
    return redirect("/")


@app.route("/instrument/rollover/clear", methods=["POST"])
@login_required
def instrument_rollover_clear():
    """Clear the contract pin for an instrument, reverting to auto-select."""
    name = request.form.get("name", "").strip()
    if not name:
        return redirect("/")

    import main as _main
    all_instruments = (
        _main.RESOLVED_INSTRUMENTS
        + _main.RESOLVED_HOURLY_INSTRUMENTS
        + _main.RESOLVED_STOCK_INSTRUMENTS
    )
    current = next((i for i in all_instruments if i["name"] == name), None)
    h_cfg = next((h for h in config.HOURLY_INSTRUMENTS if h["name"] == name), None)
    underlying = (
        (current.get("underlying") if current else None)
        or (h_cfg.get("underlying") if h_cfg else None)
    )
    pin_name = underlying if underlying else name

    contract_pin.clear_pin(pin_name)
    logger.info("Webapp: rollover pin cleared for %s", pin_name)
    # Immediately update the in-memory resolved instrument so it reverts to
    # auto-selecting the nearest active contract without needing a restart.
    _main.re_resolve_instrument(pin_name)
    return redirect("/")


# ── Settings (/settings) ──────────────────────────────────────────────────────
#
# Instrument lots and the stock-futures list used to live in .env / config.py
# and needed a code edit plus a restart to change. They are now stored in
# instrument_config.json and edited here; every save is applied to the running
# scheduler immediately via main.reload_instruments().
#
# Guard rail: an instrument holding an open position is locked. Exits are sized
# from the CONFIGURED lots (order_manager._order_qty), not from the saved
# position size, so changing lots mid-trade would exit the wrong quantity and
# leave a stray position behind.

def _open_position_size(name: str, state: dict) -> int:
    """Live (or paper) position size for an instrument — 0 when flat."""
    size = int(state.get(name, {}).get("position_size", 0) or 0)
    if size:
        return size
    return int(paper_trading.get_position_size(name) or 0)


def _restart_command() -> tuple[list[str] | None, str]:
    """
    Return (argv, display) for the service restart, or (None, reason) when no
    restart command is available on this platform.

    --no-block makes systemctl queue the job and exit at once instead of
    waiting for a unit it is about to kill us with.
    """
    if config.RESTART_COMMAND:
        return shlex.split(config.RESTART_COMMAND), config.RESTART_COMMAND
    if sys.platform.startswith("win"):
        return None, ("No restart command configured. systemd is not available on "
                      "Windows — set RESTART_COMMAND in .env to restart this host.")
    argv = ["sudo", "-n", "systemctl", "restart", "--no-block", config.SERVICE_NAME]
    return argv, " ".join(argv)


def _apply_and_flash(what: str) -> None:
    """
    Push the saved instrument config into the running scheduler and report
    what changed. Any failure is surfaced — never silently swallowed — because
    the operator needs to know whether the change is actually live.
    """
    try:
        import main as _main
        report = _main.reload_instruments()
    except Exception as exc:
        logger.error("settings: reload_instruments failed after %s: %s", what, exc,
                     exc_info=True)
        flash(f"{what} saved, but applying it to the running algo failed: "
              f"{_html.escape(str(exc))}. Restart the service to pick it up.", "err")
        return

    parts = []
    for name, old, new in report["lots"]:
        parts.append(f"{name} lots {old} &rarr; {new}")
    for name in report["added"]:
        parts.append(f"{name} added")
    for name in report["removed"]:
        parts.append(f"{name} removed")

    if parts:
        flash(f"{what} applied to the running algo: " + ", ".join(parts) + ".", "ok")
        notifier.notify_instrument_config_changed(
            ", ".join(p.replace("&rarr;", "->") for p in parts)
        )
    else:
        flash(f"{what} saved.", "ok")

    for name, err in report["failed"]:
        flash(f"{name} could not be resolved against the scrip master "
              f"({_html.escape(err)}). Check the symbol — it will not be traded.",
              "err")


@app.route("/settings")
@login_required
def settings():
    state    = load_state()
    resolved = {i["name"]: i for i in web_state.get_resolved_instruments()}
    lots_cfg = icfg.get_all_lots()

    def rows(inst_defs: list[dict], removable: bool = False) -> str:
        out = []
        for inst in inst_defs:
            name     = inst["name"]
            live     = resolved.get(name, {})
            lot_size = live.get("lot_size")
            qty      = inst["qty"]
            pos      = _open_position_size(name, state)
            locked   = pos != 0
            override = name.upper() in lots_cfg

            order_qty = f"{qty * lot_size:,}" if lot_size else "&mdash;"
            sym       = _html.escape(str(live.get("kite_tradingsymbol") or ""))
            contract  = sym or '<span class="src-tag">not resolved</span>'

            if locked:
                side   = "LONG" if pos > 0 else "SHORT"
                status = f'<span class="lock-note">{side} {abs(pos):,} &mdash; locked</span>'
                action = (f'<input type="number" class="qty-input" value="{qty}" disabled '
                          f'title="Close the position before changing lots">')
                remove = ('<span class="lock-note" title="Close the position first">'
                          '&#128274;</span>') if removable else ""
            else:
                status = '<span class="src-tag">flat</span>'
                action = f"""<form method="post" action="/settings/lots" class="inline-form">
        <input type="hidden" name="name" value="{_html.escape(name)}">
        <input type="number" class="qty-input" name="lots" value="{qty}"
               min="1" max="1000" step="1" required>
        <button type="submit" class="btn-xs btn-save">Save</button>
      </form>"""
                if override and not removable:
                    action += f"""<form method="post" action="/settings/lots/reset" class="inline-form"
            style="margin-top:5px">
        <input type="hidden" name="name" value="{_html.escape(name)}">
        <button type="submit" class="btn-xs btn-remove">Reset to default</button>
      </form>"""
                remove = (f"""<form method="post" action="/settings/stocks/remove" class="inline-form"
            onsubmit="return confirm('Stop trading {_html.escape(name)}? '
                     + 'Its strategy settings are kept, but no new orders will be placed.');">
        <input type="hidden" name="name" value="{_html.escape(name)}">
        <button type="submit" class="btn-xs btn-remove">&#10005; Remove</button>
      </form>""") if removable else ""

            out.append(f"""<tr>
    <td><strong>{_html.escape(name)}</strong></td>
    <td style="font-size:.75rem;color:var(--muted)">{contract}</td>
    <td class="num">{lot_size if lot_size else "&mdash;"}</td>
    <td class="num">{order_qty}</td>
    <td>{status}</td>
    <td>{action}</td>
    {f"<td>{remove}</td>" if removable else ""}
  </tr>""")
        return "".join(out)

    def table(inst_defs: list[dict], removable: bool = False) -> str:
        if not inst_defs:
            return ('<p class="panel-desc" style="margin:0">'
                    'Nothing configured yet.</p>')
        extra = "<th></th>" if removable else ""
        return f"""<div class="tbl-wrap"><table class="set-tbl">
  <tr><th>Instrument</th><th>Contract</th><th>Lot size</th>
      <th>Order qty</th><th>Position</th><th>Lots</th>{extra}</tr>
  {rows(inst_defs, removable)}
</table></div>"""

    # ── Service panel ─────────────────────────────────────────────────────────
    argv, cmd_display = _restart_command()
    open_names = [
        i["name"]
        for i in config.INSTRUMENTS + config.HOURLY_INSTRUMENTS + config.STOCK_INSTRUMENTS
        if _open_position_size(i["name"], state) != 0
    ]
    if open_names:
        open_warn = (f'<div class="flash flash-warn" style="margin-bottom:12px">'
                     f'&#9888; {len(open_names)} open position(s): '
                     f'{_html.escape(", ".join(open_names))}. They are saved to '
                     f'positions_state.json and restored on startup, but the algo '
                     f'places no orders while it is down.</div>')
    else:
        open_warn = ""

    if argv is None:
        restart_btn = (f'<div class="flash flash-err">{_html.escape(cmd_display)}</div>')
    else:
        restart_btn = f"""<button class="btn-xs btn-restart" id="restart-btn"
        onclick="doRestart(this)">&#128260; Restart Service</button>
  <div class="panel-desc" style="margin:10px 0 0">
    Runs <code>{_html.escape(cmd_display)}</code>. Override with
    <code>RESTART_COMMAND</code> in .env.
  </div>"""

    # ── Stock futures panel ───────────────────────────────────────────────────
    stock_defaults = f"""<form method="post" action="/settings/stocks/defaults" class="add-row">
    <label style="margin:0">Default lots</label>
    <input type="number" class="qty-input" name="qty" value="{config.STOCK_FUTURES_QTY}"
           min="1" max="1000" step="1" required>
    <label style="margin:0">Product</label>
    <select class="txt-input" name="product">
      <option value="NRML" {"selected" if config.STOCK_FUTURES_PRODUCT == "NRML" else ""}>NRML (positional)</option>
      <option value="MIS"  {"selected" if config.STOCK_FUTURES_PRODUCT == "MIS"  else ""}>MIS (intraday)</option>
    </select>
    <button type="submit" class="btn-xs btn-save">Save defaults</button>
  </form>"""

    add_stock = f"""<form method="post" action="/settings/stocks/add" class="add-row">
    <input type="text" class="txt-input" name="name" placeholder="NSE symbol, e.g. RELIANCE"
           autocapitalize="characters" required>
    <input type="number" class="qty-input" name="lots" placeholder="lots"
           value="{config.STOCK_FUTURES_QTY}" min="1" max="1000" step="1">
    <button type="submit" class="btn-xs btn-add">&#43; Add stock</button>
  </form>"""

    body = f"""
<div class="content">
  <h2>Settings</h2>

  <div class="panel">
    <div class="panel-head"><h3>Service</h3></div>
    <p class="panel-desc">
      Restart the whole process — picks up .env changes, code updates and anything
      else that needs a cold start. Instrument changes made below do <em>not</em>
      need a restart; they are applied to the running scheduler as you save them.
    </p>
    {open_warn}
    {restart_btn}
  </div>

  <div class="panel">
    <div class="panel-head">
      <h3>Stock Futures</h3>
      <span class="src-tag">instrument_config.json</span>
    </div>
    <p class="panel-desc">
      NSE stocks traded on their nearest futures contract with the same
      supertrend + MA strategy. Edited here — <code>STOCK_FUTURES</code> in .env
      is only read once, to seed this list. A stock added here is enabled
      straight away and can take a trade at the next candle close; disable it on
      the Dashboard card first if you want it resolved but idle.
    </p>
    {table(config.STOCK_INSTRUMENTS, removable=True)}
    <hr>
    {add_stock}
    <p class="panel-desc" style="margin:14px 0 4px">
      Defaults applied to stock futures without their own lot override:
    </p>
    {stock_defaults}
  </div>

  <div class="panel">
    <div class="panel-head">
      <h3>15-Minute Instruments</h3>
      <span class="src-tag">lots editable</span>
    </div>
    <p class="panel-desc">
      Lots per order. The broker order quantity is lots &times; lot size, so
      changing this changes your exposure immediately on the next entry.
    </p>
    {table(config.INSTRUMENTS)}
  </div>

  <div class="panel">
    <div class="panel-head">
      <h3>Hourly Instruments</h3>
      <span class="src-tag">lots editable</span>
    </div>
    <p class="panel-desc">
      Hourly variants trade the same contract as their underlying but size
      independently.
    </p>
    {table(config.HOURLY_INSTRUMENTS)}
  </div>
</div>
<script>{_RESTART_JS}</script>"""
    return _layout("Settings", body, active="settings")


@app.route("/settings/lots", methods=["POST"])
@login_required
def settings_lots():
    """Set the lot count for one instrument (any list: 15M, hourly, or stock)."""
    name = request.form.get("name", "").strip()
    lots = request.form.get("lots", "").strip()
    try:
        clean_name = icfg.normalise_name(name)
        clean_lots = icfg.normalise_lots(lots)
    except ValueError as exc:
        flash(_html.escape(str(exc)), "err")
        return redirect("/settings")

    known = {
        i["name"]
        for i in config.INSTRUMENTS + config.HOURLY_INSTRUMENTS + config.STOCK_INSTRUMENTS
    }
    if clean_name not in known:
        flash(f"Unknown instrument {_html.escape(clean_name)}.", "err")
        return redirect("/settings")

    if _open_position_size(clean_name, load_state()) != 0:
        flash(f"{_html.escape(clean_name)} has an open position. Exits are sized from "
              f"the configured lots, so changing them now would close the wrong "
              f"quantity — square off first.", "err")
        return redirect("/settings")

    icfg.set_lots(clean_name, clean_lots)
    _apply_and_flash(f"{clean_name} lots")
    return redirect("/settings")


@app.route("/settings/lots/reset", methods=["POST"])
@login_required
def settings_lots_reset():
    """Drop an instrument's lot override, reverting to the config.py default."""
    name = request.form.get("name", "").strip()
    try:
        clean_name = icfg.normalise_name(name)
    except ValueError as exc:
        flash(_html.escape(str(exc)), "err")
        return redirect("/settings")

    if _open_position_size(clean_name, load_state()) != 0:
        flash(f"{_html.escape(clean_name)} has an open position — square off first.", "err")
        return redirect("/settings")

    icfg.clear_lots(clean_name)
    _apply_and_flash(f"{clean_name} lots reset to default")
    return redirect("/settings")


@app.route("/settings/stocks/add", methods=["POST"])
@login_required
def settings_stocks_add():
    """Add an NSE stock to the traded futures list."""
    name = request.form.get("name", "").strip()
    lots = request.form.get("lots", "").strip()
    try:
        clean_name = icfg.normalise_stock_name(name)
        clean_lots = icfg.normalise_lots(lots) if lots else None
    except ValueError as exc:
        flash(_html.escape(str(exc)), "err")
        return redirect("/settings")

    if clean_name in config.STOCK_FUTURES_NAMES:
        flash(f"{_html.escape(clean_name)} is already in the list.", "warn")
        return redirect("/settings")

    reserved = {i["name"] for i in config.INSTRUMENTS + config.HOURLY_INSTRUMENTS}
    if clean_name in reserved:
        flash(f"{_html.escape(clean_name)} is already configured as a "
              f"non-stock instrument.", "err")
        return redirect("/settings")

    icfg.add_stock(clean_name, clean_lots)
    _apply_and_flash(f"Stock future {clean_name}")
    return redirect("/settings")


@app.route("/settings/stocks/remove", methods=["POST"])
@login_required
def settings_stocks_remove():
    """Stop trading a stock future. Refused while a position is open."""
    name = request.form.get("name", "").strip()
    try:
        clean_name = icfg.normalise_name(name)
    except ValueError as exc:
        flash(_html.escape(str(exc)), "err")
        return redirect("/settings")

    if _open_position_size(clean_name, load_state()) != 0:
        flash(f"{_html.escape(clean_name)} has an open position. Removing it would "
              f"leave the position unmanaged — square off first.", "err")
        return redirect("/settings")

    icfg.remove_stock(clean_name)
    _apply_and_flash(f"Stock future {clean_name} removed")
    return redirect("/settings")


@app.route("/settings/stocks/defaults", methods=["POST"])
@login_required
def settings_stocks_defaults():
    """Set the default lots and Kite product used for stock futures."""
    try:
        qty     = icfg.normalise_lots(request.form.get("qty", "").strip())
        product = icfg.normalise_product(request.form.get("product", "").strip())
    except ValueError as exc:
        flash(_html.escape(str(exc)), "err")
        return redirect("/settings")

    # The product applies to every stock future, so a change would resize or
    # re-tag exits of anything currently open.
    state = load_state()
    open_stocks = [
        n for n in config.STOCK_FUTURES_NAMES if _open_position_size(n, state) != 0
    ]
    if open_stocks and (qty != config.STOCK_FUTURES_QTY
                        or product != config.STOCK_FUTURES_PRODUCT):
        flash(f"Open stock futures position(s): {_html.escape(', '.join(open_stocks))}. "
              f"Square off before changing the shared defaults.", "err")
        return redirect("/settings")

    icfg.set_stock_defaults(qty, product)
    _apply_and_flash("Stock futures defaults")
    return redirect("/settings")


@app.route("/service/restart", methods=["POST"])
@login_required
def service_restart():
    """
    Restart the whole service. The restart is fired from a background thread
    after a short delay so this response reaches the browser before systemd
    tears the process down.
    """
    argv, display = _restart_command()
    if argv is None:
        return jsonify({"success": False, "error": display}), 400

    # Pre-flight the sudo rule so a missing NOPASSWD entry is reported here
    # instead of failing silently in a thread that nobody is watching.
    if argv[0] == "sudo":
        try:
            probe = subprocess.run(["sudo", "-n", "true"],
                                   capture_output=True, timeout=10)
            if probe.returncode != 0:
                msg = ("Passwordless sudo is not configured for this user. Add the "
                       "sudoers rule from DEPLOYMENT.txt section 9a, or set "
                       "RESTART_COMMAND in .env.")
                logger.error("service_restart: %s (%s)",
                             msg, probe.stderr.decode(errors="replace").strip()[:200])
                return jsonify({"success": False, "error": msg}), 500
        except Exception as exc:
            logger.error("service_restart: sudo pre-flight failed: %s", exc)
            return jsonify({"success": False, "error": f"sudo check failed: {exc}"}), 500

    logger.warning("Service restart requested from the dashboard: %s", display)
    notifier.notify_service_restart(display)

    def _fire():
        time.sleep(1.5)
        try:
            # start_new_session detaches the child from our process group so a
            # SIGTERM aimed at us cannot kill the restart mid-flight.
            result = subprocess.run(argv, capture_output=True, timeout=60,
                                    start_new_session=True)
            if result.returncode != 0:
                logger.error("Service restart command failed (rc=%d): %s",
                             result.returncode,
                             result.stderr.decode(errors="replace").strip()[:300])
        except Exception as exc:
            logger.error("Service restart command raised: %s", exc)

    threading.Thread(target=_fire, daemon=True).start()
    return jsonify({"success": True, "command": display})


# ── Positions (/positions) ────────────────────────────────────────────────────

@app.route("/positions")
@login_required
def positions():
    state = load_state()
    snap  = web_state.snapshot()
    idata = snap["instruments"]
    msg   = request.args.get("msg", "")

    # ── Fetch live positions from Kite ─────────────────────────────────────────
    kite_net   = {}
    kite_error = None
    try:
        kite     = get_kite_session()
        kite_pos = kite.positions()
        for p in kite_pos.get("net", []):
            kite_net[p["tradingsymbol"]] = p
    except Exception as exc:
        kite_error = str(exc)

    # ── Only our algo's open positions ─────────────────────────────────────────
    algo_positions = {
        name: data for name, data in state.items()
        if data.get("position_size", 0) != 0
    }

    # ── Build rows + accumulate stats ─────────────────────────────────────────
    rows          = ""
    total_pnl     = 0.0
    total_pnl_known = True
    long_count    = 0
    short_count   = 0

    for name, pos_data in algo_positions.items():
        algo_pos     = pos_data.get("position_size", 0)
        sym          = pos_data.get("kite_tradingsymbol") or idata.get(name, {}).get("kite_tradingsymbol", "-")
        sig          = str(idata.get(name, {}).get("signal", ""))
        is_synthetic = pos_data.get("is_synthetic", False)
        is_short_ce  = pos_data.get("is_short_ce", False)
        ce_sym       = pos_data.get("ce_tradingsymbol", "")
        pe_sym       = pos_data.get("pe_tradingsymbol", "")

        if is_synthetic and ce_sym and pe_sym:
            # Two-leg synthetic future: look up CE and PE positions separately
            sym_display = f"{ce_sym} / {pe_sym}"
            ce_live     = kite_net.get(ce_sym, {})
            pe_live     = kite_net.get(pe_sym, {})
            live_qty    = ce_live.get("quantity")
            ce_pnl      = ce_live.get("pnl")
            pe_pnl      = pe_live.get("pnl")
            pnl_val     = (ce_pnl + pe_pnl) if (ce_pnl is not None and pe_pnl is not None) \
                          else (ce_pnl if ce_pnl is not None else pe_pnl)
            avg_price   = ce_live.get("average_price") or pos_data.get("entry_ce_price", 0)
            last_price  = ce_live.get("last_price") or idata.get(name, {}).get("close")
        elif is_short_ce and ce_sym:
            # Single-leg short CE
            sym_display = ce_sym
            kite_live   = kite_net.get(ce_sym, {})
            live_qty    = kite_live.get("quantity")
            pnl_val     = kite_live.get("pnl")
            avg_price   = pos_data.get("entry_ce_price", 0) or kite_live.get("average_price", 0)
            last_price  = kite_live.get("last_price") or idata.get(name, {}).get("close")
        else:
            sym_display = sym
            kite_live   = kite_net.get(sym, {})
            live_qty    = kite_live.get("quantity")
            pnl_val     = kite_live.get("pnl")
            avg_price   = kite_live.get("average_price") or pos_data.get("entry_price", 0)
            last_price  = kite_live.get("last_price")    or idata.get(name, {}).get("close")

        if algo_pos > 0:
            dir_html = '<span class="pill pl-buy">LONG</span>'
            long_count += 1
        else:
            dir_html = '<span class="pill pl-sell">SHORT</span>'
            short_count += 1

        display_qty = abs(live_qty) if live_qty is not None else abs(algo_pos)
        qty_note    = ""
        # For synthetic/short-CE, qty confirmation is handled by the status check;
        # suppress the inline warning to avoid noise from legacy state (lots vs contracts).
        if not is_synthetic and not is_short_ce:
            if live_qty is not None and live_qty != algo_pos:
                qty_note = ' <span title="Kite qty differs from algo state" style="color:var(--orange-l)">&#9888;</span>'

        if pnl_val is not None:
            total_pnl += pnl_val
            cls       = "pnl-pos" if pnl_val >= 0 else "pnl-neg"
            pnl_html  = f'<span class="{cls}">{_fmt(pnl_val)}</span>'
            src_note  = '<span style="font-size:.68rem;color:var(--muted)"> live</span>'
        elif last_price and avg_price:
            raw = (float(last_price) - float(avg_price)) * algo_pos
            total_pnl += raw
            cls       = "pnl-pos" if raw >= 0 else "pnl-neg"
            pnl_html  = f'<span class="{cls}">{_fmt(raw)}</span>'
            src_note  = '<span style="font-size:.68rem;color:var(--muted)"> est.</span>'
        else:
            total_pnl_known = False
            pnl_html  = "-"
            src_note  = ""

        if kite_error:
            status = '<span style="font-size:.72rem;color:var(--orange-l)">Kite unavailable</span>'
        elif is_synthetic and ce_sym and pe_sym:
            # Confirm both legs are present with equal sizes and correct directions.
            # Direction: LONG → CE qty > 0, PE qty < 0; SHORT → CE qty < 0, PE qty > 0.
            # We compare leg directions/sizes from Kite, NOT against stored position_size,
            # so this works correctly even for positions opened before the lots→contracts fix.
            ce_qty = kite_net.get(ce_sym, {}).get("quantity")
            pe_qty = kite_net.get(pe_sym, {}).get("quantity")
            if ce_qty is not None and pe_qty is not None and abs(ce_qty) == abs(pe_qty):
                direction_ok = (algo_pos > 0 and ce_qty > 0 and pe_qty < 0) or \
                               (algo_pos < 0 and ce_qty < 0 and pe_qty > 0)
                if direction_ok:
                    status = '<span style="font-size:.72rem;color:var(--green-l)">&#10003; Confirmed</span>'
                else:
                    status = '<span style="font-size:.72rem;color:var(--orange-l)">&#9888; Mismatch</span>'
            elif ce_qty is not None or pe_qty is not None:
                status = '<span style="font-size:.72rem;color:var(--orange-l)">&#9888; Mismatch</span>'
            else:
                status = "-"
        elif live_qty is not None and live_qty == algo_pos:
            status = '<span style="font-size:.72rem;color:var(--green-l)">&#10003; Confirmed</span>'
        elif live_qty is not None and live_qty != algo_pos:
            status = '<span style="font-size:.72rem;color:var(--orange-l)">&#9888; Mismatch</span>'
        else:
            status = "-"

        clear_btn = (
            f'<form method="post" action="/positions/clear/{name}" style="margin:0"'
            f' onsubmit="return confirm(\'Clear algo state for {name}? (No orders placed)\')">'
            f'<button type="submit" class="btn-sm btn-warn-sm" style="padding:2px 8px;font-size:.72rem">&#128465;</button>'
            f'</form>'
        )
        rows += f"""<tr>
  <td><strong>{name}</strong></td>
  <td style="color:var(--muted);font-size:.78rem">{sym_display}</td>
  <td>{dir_html}</td>
  <td style="text-align:right">{display_qty}{qty_note}</td>
  <td style="text-align:right">{_fmt(avg_price)}</td>
  <td style="text-align:right">{_fmt(last_price)}</td>
  <td style="text-align:right">{pnl_html}{src_note}</td>
  <td>{_pill(sig)}</td>
  <td>{status}</td>
  <td>{clear_btn}</td>
</tr>"""

    # ── Stats bar ──────────────────────────────────────────────────────────────
    total_count = long_count + short_count
    pnl_cls     = "pnl-pos" if total_pnl >= 0 else "pnl-neg"
    pnl_sign    = "+" if total_pnl >= 0 else ""
    pnl_display = (f'<span class="{pnl_cls}">{pnl_sign}{_fmt(total_pnl)}</span>'
                   + ('<span style="font-size:.7rem;color:var(--muted)"> est.</span>'
                      if not kite_error and not total_pnl_known else ""))

    stats_html = f"""
<div class="stats-bar">
  <div class="stat">
    <div class="stat-label">Open Positions</div>
    <div class="stat-value">{total_count}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Long / Short</div>
    <div class="stat-value">
      <span class="pnl-pos">{long_count}</span>
      &nbsp;/&nbsp;
      <span class="pnl-neg">{short_count}</span>
    </div>
  </div>
  <div class="stat">
    <div class="stat-label">Total P&amp;L</div>
    <div class="stat-value">{pnl_display if total_count else "—"}</div>
  </div>
</div>"""

    # ── Action buttons ─────────────────────────────────────────────────────────
    msg_banner = ""
    if msg == "sqoff_ok":
        msg_banner = '<p style="color:var(--green-l);margin-bottom:12px">&#10003; All positions squared off.</p>'
    elif msg == "sqoff_err":
        msg_banner = '<p style="color:var(--red-l);margin-bottom:12px">&#9888; Square-off failed — check log.</p>'
    elif msg.startswith("cleared_"):
        cleared_name = msg[len("cleared_"):]
        msg_banner = f'<p style="color:var(--green-l);margin-bottom:12px">&#10003; Cleared state for {_html.escape(cleared_name)}.</p>'
    elif msg == "paper_cleared":
        msg_banner = '<p style="color:var(--green-l);margin-bottom:12px">&#10003; Paper positions cleared.</p>'

    buttons_html = ""
    if algo_positions:
        buttons_html = f"""
<div class="btn-bar">
  <form method="post" action="/positions/sqoff" style="margin:0"
        onsubmit="return confirm('Place market orders to close ALL open positions on Kite?')">
    <button type="submit" class="btn-sm btn-danger-sm">&#9632; Square Off All</button>
  </form>
</div>"""

    kite_warn = (f'<p class="note" style="color:var(--orange-l);margin-bottom:12px">'
                 f'&#9888; Could not fetch live Kite positions: {_html.escape(kite_error)}</p>') if kite_error else ""

    if not algo_positions:
        content = f'{kite_warn}<div class="empty">No open algo positions.</div>'
    else:
        content = f"""{kite_warn}
{buttons_html}
<div class="tbl-wrap">
  <table>
    <thead><tr>
      <th>Instrument</th><th>Symbol</th><th>Direction</th>
      <th style="text-align:right">Qty</th>
      <th style="text-align:right">Avg Price</th>
      <th style="text-align:right">Last Price</th>
      <th style="text-align:right">P&amp;L</th>
      <th>Signal</th>
      <th>Status</th>
      <th></th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""

    # ── Paper positions section (dry-run mode) ─────────────────────────────────
    paper_positions = snap.get("paper_positions", {})
    paper_section   = ""
    if paper_positions:
        # Batch-fetch live option LTPs for synthetic positions via Angel getLtpData
        # (Kite's ltp() for options requires a higher API tier — Angel has no such restriction)
        live_option_ltps: dict[str, float] = {}
        angel_opt_list = []
        for pp in paper_positions.values():
            if pp.get("is_synthetic"):
                angel_opt_list.append({
                    "kite_tradingsymbol": pp["ce_symbol"],
                    "angel_token":        pp.get("ce_angel_token", ""),
                    "angel_symbol":       pp.get("ce_angel_symbol", ""),
                    "angel_exchange":     "NFO",
                })
                angel_opt_list.append({
                    "kite_tradingsymbol": pp["pe_symbol"],
                    "angel_token":        pp.get("pe_angel_token", ""),
                    "angel_symbol":       pp.get("pe_angel_symbol", ""),
                    "angel_exchange":     "NFO",
                })
        if angel_opt_list:
            try:
                live_option_ltps = get_option_ltps(angel_opt_list)
            except Exception as _ltp_exc:
                logger.warning("Could not fetch live option LTPs for paper positions: %s", _ltp_exc)

        paper_rows = ""
        paper_total_pnl = 0.0
        for pname, pp in paper_positions.items():
            cur_price = idata.get(pname, {}).get("close")
            psize     = pp["position_size"]
            dir_html  = '<span class="pill pl-buy">LONG</span>' if psize > 0 else '<span class="pill pl-sell">SHORT</span>'

            if pp.get("is_synthetic"):
                ce_ltp   = live_option_ltps.get(pp["ce_symbol"])
                pe_ltp   = live_option_ltps.get(pp["pe_symbol"])
                sym_disp = f'{pp["ce_symbol"]} / {pp["pe_symbol"]}'
                if ce_ltp is not None and pe_ltp is not None:
                    upnl    = psize * ((ce_ltp - pp["entry_ce_price"]) - (pe_ltp - pp["entry_pe_price"]))
                    paper_total_pnl += upnl
                    pnl_cls = "pnl-pos" if upnl >= 0 else "pnl-neg"
                    sign    = "+" if upnl >= 0 else ""
                    pnl_td  = (f'<span class="{pnl_cls}">{sign}{_fmt(upnl)}</span>'
                               f' <span style="font-size:.68rem;color:var(--muted)">live opts</span>')
                else:
                    pnl_td  = '<span style="color:var(--muted);font-size:.78rem">LTP unavail.</span>'
                last_td = f'{_fmt(ce_ltp)} / {_fmt(pe_ltp)}' if ce_ltp is not None else "-"
            else:
                sym_disp = pp["symbol"]
                if cur_price:
                    upnl    = (float(cur_price) - pp["entry_price"]) * psize
                    paper_total_pnl += upnl
                    pnl_cls = "pnl-pos" if upnl >= 0 else "pnl-neg"
                    sign    = "+" if upnl >= 0 else ""
                    pnl_td  = f'<span class="{pnl_cls}">{sign}{_fmt(upnl)}</span> <span style="font-size:.68rem;color:var(--muted)">unreal.</span>'
                else:
                    pnl_td  = "-"
                last_td = _fmt(cur_price)

            paper_rows += f"""<tr>
  <td><strong>{pname}</strong></td>
  <td style="color:var(--muted);font-size:.78rem">{sym_disp}</td>
  <td>{dir_html}</td>
  <td style="text-align:right">{pp["qty"]}</td>
  <td style="text-align:right">{_fmt(pp["entry_price"])}</td>
  <td style="text-align:right">{last_td}</td>
  <td style="text-align:right">{pnl_td}</td>
  <td style="color:var(--muted);font-size:.72rem">{pp["entry_time"]}</td>
</tr>"""
        ptotal_cls  = "pnl-pos" if paper_total_pnl >= 0 else "pnl-neg"
        ptotal_sign = "+" if paper_total_pnl >= 0 else ""
        paper_section = f"""
<h2 style="margin-top:28px">Paper Positions
  <span class="pill pl-dry" style="font-size:.72rem;vertical-align:middle">DRY RUN</span>
  <span style="font-size:.82rem;font-weight:400;color:var(--muted);margin-left:8px">
    Unrealised P&amp;L:
    <span class="{ptotal_cls}">{ptotal_sign}{_fmt(paper_total_pnl)}</span>
  </span>
</h2>
<div class="btn-bar" style="margin-bottom:10px">
  <form method="post" action="/positions/paper/clear" style="margin:0"
        onsubmit="return confirm('Clear all open paper positions? (No real orders placed)')">
    <button type="submit" class="btn-sm btn-warn-sm">&#128465; Clear Paper State</button>
  </form>
</div>
<div class="tbl-wrap">
  <table>
    <thead><tr>
      <th>Instrument</th><th>Symbol</th><th>Direction</th>
      <th style="text-align:right">Qty</th>
      <th style="text-align:right">Entry Price</th>
      <th style="text-align:right">Last Price</th>
      <th style="text-align:right">Unrealised P&amp;L</th>
      <th>Entry Time</th>
    </tr></thead>
    <tbody>{paper_rows}</tbody>
  </table>
</div>"""

    body = f"""
<div class="content">
  <h2>Positions</h2>
  {msg_banner}
  {stats_html}
  {content}
  {paper_section}
</div>"""
    return _layout("Positions", body, active="positions", refresh=30)


# ── Positions actions ─────────────────────────────────────────────────────────

@app.route("/positions/sqoff", methods=["POST"])
@login_required
def positions_sqoff():
    try:
        kite        = get_kite_session()
        instruments = web_state.get_resolved_instruments()
        state       = load_state()
        square_off_all(kite, instruments, state)
        logger.info("Web UI: square off all triggered")
        return redirect("/positions?msg=sqoff_ok")
    except Exception as exc:
        logger.error("Web UI square off failed: %s", exc)
        return redirect("/positions?msg=sqoff_err")


@app.route("/positions/clear/<name>", methods=["POST"])
@login_required
def positions_clear_one(name: str):
    """Clear algo state for a single position (no orders placed)."""
    state = load_state()
    with instrument_lock(name):
        if clear_position(state, name):
            logger.info("Web UI: cleared position state for %s", name)
    return redirect(f"/positions?msg=cleared_{name}")


@app.route("/positions/paper/clear", methods=["POST"])
@login_required
def positions_paper_clear():
    """Wipe all persisted paper positions (no real orders placed)."""
    paper_trading.clear_all()
    logger.info("Web UI: paper position state cleared")
    return redirect("/positions?msg=paper_cleared")


# ── Trades (/trades) ──────────────────────────────────────────────────────────

@app.route("/trades")
@login_required
def trades():
    available = tlog.list_dates()
    today     = str(date.today())

    # Selected date comes from ?date= query param, default to today
    selected = request.args.get("date", today)
    if selected not in available and today not in available:
        selected = available[0] if available else today

    entries = tlog.get_trades(selected)

    # ── Date selector ──────────────────────────────────────────────────────────
    options = "".join(
        f'<option value="{d}" {"selected" if d == selected else ""}>'
        f'{d}{" (today)" if d == today else ""}'
        f'</option>'
        for d in available
    ) if available else f'<option value="{today}" selected>{today} (today)</option>'

    date_selector = f"""
<form method="get" style="display:inline-flex;align-items:center;gap:8px;margin-bottom:16px">
  <label style="font-size:.82rem;color:var(--muted)">Date:</label>
  <select name="date" onchange="this.form.submit()"
    style="background:var(--card);border:1px solid var(--border);color:var(--text);
           padding:5px 10px;border-radius:8px;font-size:.82rem;cursor:pointer">
    {options}
  </select>
</form>"""

    # ── Trade rows ─────────────────────────────────────────────────────────────
    if not entries:
        content = '<div class="empty">No trades on this date.</div>'
    else:
        rows = ""
        for t in reversed(entries):
            action   = t["action"]
            pill_cls = _SIG_PILL.get(action, "pl-none")
            label    = _SIG_LABEL.get(action, action)
            dry      = ' <span class="pill pl-dry">DRY</span>' if t.get("dry_run") else ""
            rows += f"""<tr>
  <td style="color:var(--muted);font-size:.78rem">{t["time"]}</td>
  <td><strong>{t["name"]}</strong></td>
  <td><span class="pill {pill_cls}">{label}</span>{dry}</td>
  <td style="font-size:.78rem;color:var(--muted)">{t["symbol"]}</td>
  <td style="text-align:right">{t["qty"]}</td>
</tr>"""
        content = f"""<div class="tbl-wrap">
  <table>
    <thead><tr>
      <th>Time</th><th>Instrument</th><th>Action</th>
      <th>Symbol</th><th style="text-align:right">Qty</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""

    count_note = f"{len(entries)} trade{'s' if len(entries) != 1 else ''}" if entries else ""
    body = f"""
<div class="content">
  <h2>Trades
    <span style="font-size:.8rem;font-weight:400;color:var(--muted);margin-left:6px">{count_note}</span>
  </h2>
  {date_selector}
  {content}
</div>"""
    return _layout("Trades", body, active="trades")


# ── Log (/log) ────────────────────────────────────────────────────────────────

@app.route("/log")
@login_required
def log_view():
    import glob as _glob

    logs_dir = Path(config.LOGS_DIR)
    today_log = Path(config.LOG_FILE)

    # Collect available dated archive files (algo.log.YYYY-MM-DD)
    archived = sorted(
        [Path(p) for p in _glob.glob(str(logs_dir / "algo.log.*"))],
        reverse=True,
    )
    # Build list of (label, filename) for the selector — today first
    log_options = []
    if today_log.exists():
        log_options.append(("Today", today_log.name))
    for p in archived:
        suffix = p.suffix if p.suffix else p.name.split("algo.log")[-1]
        label  = suffix.lstrip(".")   # e.g. "2026-06-28"
        log_options.append((label, p.name))

    # Which file is requested?
    selected = request.args.get("f", today_log.name)
    # Sanitise: only allow names that match algo.log* inside logs_dir
    safe_names = {p.name for p in [today_log] + archived}
    if selected not in safe_names:
        selected = today_log.name

    chosen_path = logs_dir / selected
    if not chosen_path.exists():
        body = f'<div class="content"><h2>Log</h2><div class="empty">{_html.escape(selected)} not found.</div></div>'
        return _layout("Log", body, active="log")

    lines   = chosen_path.read_text(encoding="utf-8", errors="replace").splitlines()
    content = _html.escape("\n".join(reversed(lines[-200:])))

    # Build date selector options
    opts_html = "".join(
        f'<option value="{_html.escape(fname)}" {"selected" if fname == selected else ""}>'
        f'{_html.escape(label)}</option>'
        for label, fname in log_options
    )

    body = f"""
<div class="content">
  <h2 style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
    Log
    <select onchange="location.href='/log?f='+this.value"
            style="font-size:.8rem;background:var(--card);color:var(--text);
                   border:1px solid var(--border);border-radius:6px;padding:3px 8px">
      {opts_html}
    </select>
    <span style="font-size:.78rem;font-weight:400;color:var(--muted)">last 200 lines, newest first</span>
    <a href="/log?f={_html.escape(selected)}" style="font-size:.78rem;color:var(--accent)">&#8635; Refresh</a>
  </h2>
  <pre class="log">{content}</pre>
</div>"""
    return _layout("Log", body, active="log")


# ── Kite Login (/kite/login, /kite/relogin, /callback) ────────────────────────

@app.route("/kite/relogin", methods=["POST"])
@login_required
def kite_relogin():
    """Trigger auto-login from the dashboard button; returns JSON result."""
    import threading
    from kite_login import auto_login

    result = {"success": False}

    def _run():
        ok = auto_login()
        result["success"] = ok
        if ok:
            logger.info("Dashboard-triggered auto-login succeeded")
        else:
            logger.error("Dashboard-triggered auto-login failed")

    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout=30)   # wait up to 30 s for the login to complete

    return jsonify(result)


@app.route("/kite/login", methods=["GET"])
@login_required
def kite_login_page():
    kite = KiteConnect(api_key=config.KITE_API_KEY)
    logger.info("Kite login initiated from web UI")
    return redirect(kite.login_url())


@app.route("/callback")
def kite_callback():
    request_token = request.args.get("request_token")
    status_param  = request.args.get("status", "")

    if not request_token or status_param != "success":
        body = f"""
<div class="auth-wrap"><div class="auth-card">
  <h1>Login Failed</h1>
  <p class="subtitle">Kite returned status: <strong>{status_param or "unknown"}</strong></p>
  <a href="/kite/login" class="btn btn-danger">Try Again</a>
  <a href="/" class="btn btn-secondary">Dashboard</a>
</div></div>"""
        return _layout("Error", body), 400

    try:
        kite = KiteConnect(api_key=config.KITE_API_KEY)
        data = kite.generate_session(request_token, api_secret=config.KITE_API_SECRET)
        access_token = data["access_token"]
        _save_token(access_token)
        logger.info("Kite access token saved for %s", date.today())

        snippet = access_token[:6] + "…" + access_token[-4:]
        body = f"""
<div class="auth-wrap"><div class="auth-card">
  <h1>&#10003; Login Successful</h1>
  <p class="subtitle">Today's token has been saved.</p>
  <p style="color:var(--muted);font-size:.83rem;margin-bottom:24px;line-height:1.7">
    Date:&nbsp;<span style="color:var(--text)">{date.today()}</span><br>
    Token:&nbsp;<span style="color:var(--text)">{snippet}</span><br><br>
    The algo will use this token on the next candle close.
  </p>
  <a href="/" class="btn btn-success">Go to Dashboard</a>
</div></div>"""
        return _layout("Success", body)

    except Exception as exc:
        logger.error("generate_session failed: %s", exc)
        body = f"""
<div class="auth-wrap"><div class="auth-card">
  <h1>Session Error</h1>
  <p class="subtitle">Token exchange with Zerodha failed.</p>
  <p style="color:var(--red-l);font-size:.82rem;margin-bottom:20px">{exc}</p>
  <a href="/kite/login" class="btn btn-danger">Try Again</a>
</div></div>"""
        return _layout("Error", body), 500


# ── JSON status API ───────────────────────────────────────────────────────────

@app.route("/api/status")
@login_required
def api_status():
    snap = web_state.snapshot()
    tok  = _token_status()
    sched = snap["scheduler"]
    return jsonify({
        "kite_token_valid": True if config.DRY_RUN else tok["valid"],
        "scheduler_running": sched["running"],
        "last_run":  sched["last_run"].isoformat() if sched["last_run"] else None,
        "run_count": sched["run_count"],
        "instruments": {
            name: {
                "signal":     d.get("signal"),
                "close":      d.get("close"),
                "updated_at": d["updated_at"].isoformat() if d.get("updated_at") else None,
            }
            for name, d in snap["instruments"].items()
        },
    })
