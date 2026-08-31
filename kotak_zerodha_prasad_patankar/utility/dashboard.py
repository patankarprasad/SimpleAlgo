import os
import csv
import secrets
import hmac
from collections import defaultdict
from flask import Flask, render_template_string, request, Response, redirect
import sys
from datetime import datetime
from functools import wraps
import logging
from utility.ledger import disable_strategy_in_ledger

USERNAME = None
PASSWORD = None
CLIENT_NAME = "Unknown"

app = Flask(__name__)

CSRF_TOKEN = secrets.token_urlsafe(32)

logging.getLogger('werkzeug').disabled = True

# ================= BASE PATH =================
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

LOG_DIR = os.path.join(BASE_DIR, "logs")
CONFIG_PATH = os.path.join(BASE_DIR, "user_selection.csv")

REFRESH_SECONDS = 60

# ================= EDIT BLOCK WINDOW =================
EDIT_BLOCK_START = (9, 9)
EDIT_BLOCK_END   = (15, 20)

def is_edit_block_time():
    now = datetime.now()
    start = now.replace(hour=EDIT_BLOCK_START[0], minute=EDIT_BLOCK_START[1], second=0, microsecond=0)
    end   = now.replace(hour=EDIT_BLOCK_END[0],   minute=EDIT_BLOCK_END[1],   second=0, microsecond=0)
    return start <= now <= end

# ================= AUTH =================
def check_auth(username, password):
    return username == USERNAME and password == PASSWORD

def authenticate():
    return Response("Authentication Required", 401, {"WWW-Authenticate": 'Basic realm="Login Required"'})

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

def validate_csrf():
    token = request.form.get("csrf_token", "")
    return hmac.compare_digest(token, CSRF_TOKEN)

# ================= LEDGER =================
def find_ledger_file():
    if not os.path.exists(LOG_DIR):
        return None
    for file in os.listdir(LOG_DIR):
        if file.startswith("A_Ledger_for") and file.endswith(".csv"):
            return os.path.join(LOG_DIR, file)
    return None

def read_ledger():
    ledger_path = find_ledger_file()
    if not ledger_path:
        return None
    try:
        with open(ledger_path, newline="") as csvfile:
            return list(csv.DictReader(csvfile))
    except Exception as e:
        return f"Error reading ledger: {e}"

# ================= CONFIG =================
def read_config():
    if not os.path.exists(CONFIG_PATH):
        return []
    try:
        with open(CONFIG_PATH, newline="") as f:
            rows = list(csv.DictReader(f))
            cleaned = []
            for r in rows:
                cleaned.append({k.strip(): v for k, v in r.items()})
            return cleaned
    except Exception as e:
        logging.error(f"Config read error: {e}")
        return []

def write_config(rows):
    """Atomic write — NO backup"""
    if not rows:
        return
    try:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["key", "value", "reference"], extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, CONFIG_PATH)
        logging.info("user_selection.csv updated from dashboard")
    except Exception as e:
        logging.error(f"Config write error: {e}")

# ================= LOG =================
def find_log_file():
    if not os.path.exists(LOG_DIR):
        return None
    log_files = [os.path.join(LOG_DIR, f) for f in os.listdir(LOG_DIR) if f.endswith(".log")]
    if not log_files:
        return None
    return max(log_files, key=os.path.getmtime)

def tail_log(file_path, lines=10):
    try:
        with open(file_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            buffer = bytearray()
            pointer = f.tell()
            while pointer >= 0 and lines > 0:
                f.seek(pointer)
                byte = f.read(1)
                if byte == b"\n":
                    lines -= 1
                    if lines == 0:
                        break
                buffer.extend(byte)
                pointer -= 1
        return buffer[::-1].decode(errors="ignore")
    except Exception as e:
        return f"Error reading log: {e}"

def read_full_log(file_path):
    try:
        with open(file_path, "r", errors="ignore") as f:
            return f.read()
    except Exception as e:
        return f"Error reading log: {e}"

# ================= LEDGER PROCESS =================
_EXIT_PREFIXES = {"IND", "SL", "PSL", "TGT"}

def _exit_strategy_tag(gui_id):
    """Return the strategy tag embedded in an exit-order GuiOrdId.
    Entry GuiOrdId:  CEB_101530      (no prefix)
    Exit GuiOrdId:   IND_CEB_101845  → 'CEB'
                     PSL_SPE_101845  → 'SPE'
    """
    parts = gui_id.split("_")
    if len(parts) >= 2 and parts[0].upper() in _EXIT_PREFIXES:
        tag = parts[1]
    else:
        tag = gui_id.rsplit("_", 1)[0] if "_" in gui_id else gui_id
    if tag.upper().startswith("EX"):
        return "EXD"
    if tag.upper().startswith("NHF"):
        return "NHF"
    return tag

def process_data(rows):
    # Ledger format: one row per trade. status=ENTRY means still open; status=EXIT means closed.
    position_avg = {}
    tag_pnl = defaultdict(float)
    tag_count = defaultdict(int)
    cumulative_total = 0.0

    for row in rows:
        status = row.get("status", "").strip().upper()
        symbol = row.get("trdSym", "")
        gui_id = row.get("GuiOrdId", "")
        pnl_str = row.get("pnl", "").strip() if row.get("pnl") else ""

        if status == "ENTRY":
            try:
                strike = symbol[-7:] if len(symbol) >= 7 else symbol
                entry = float(row.get("entry_price", 0))
                tag = gui_id.rsplit("_", 1)[0] if "_" in gui_id else gui_id
                position_avg[(tag, strike)] = round(entry, 2)
            except:
                pass

        elif status == "EXIT":
            if pnl_str:
                try:
                    pnl_val = float(pnl_str)
                    ptag = gui_id.split("_")[0] if "_" in gui_id else gui_id
                    if ptag.upper().startswith("EX"):
                        ptag = "EXD"
                    elif ptag.upper().startswith("NHF"):
                        ptag = "NHF"
                    tag_pnl[ptag] += pnl_val
                    tag_count[ptag] += 1
                    cumulative_total += pnl_val
                except:
                    pass

    return position_avg, tag_pnl, tag_count, round(cumulative_total, 2)

# ================= TOGGLE ENDPOINT =================
@app.route("/toggle", methods=["POST"])
@requires_auth
def toggle_strategy():
    if not validate_csrf():
        return Response("CSRF validation failed", 403)
    
    key = request.form.get("key", "")
    if not key.startswith("STRATEGY_"):
        return redirect("/")
    rows = read_config()
    disabled = False
    for row in rows:
        if row["key"] == key:
            old_value = row["value"].strip().upper()
            row["value"] = "FALSE" if old_value == "TRUE" else "TRUE"
            # TRUE -> FALSE means strategy is being disabled
            if old_value == "TRUE":
                disabled = True            
            break
    write_config(rows)
    # Mark ledger entry as EXIT when strategy is disabled
    if disabled:
        strategy = key.replace("STRATEGY_", "")
        disable_strategy_in_ledger(strategy)
    return redirect("/")

# ================= MAIN ROUTE =================
@app.route("/", methods=["GET", "POST"])
@requires_auth
def dashboard():

    rows = read_ledger()
    if rows is None:
        return "<h2>Ledger file not found.</h2>"
    if isinstance(rows, str):
        return f"<h2>{rows}</h2>"

    config_rows = read_config()

    if request.method == "POST" and not is_edit_block_time():
        if not validate_csrf():
            return Response("CSRF validation failed", 403)
        new_rows = []
        for row in config_rows:
            key = row["key"]
            val = request.form.get(f"value_{key}", row["value"])
            new_rows.append({"key": key, "value": val, "reference": row.get("reference", "")})
        write_config(new_rows)
        config_rows = new_rows

    position_avg, tag_pnl, tag_count, cumulative_total = process_data(rows)

    log_file = find_log_file()
    log_tail = tail_log(log_file, 10) if log_file else "No log file"
    log_full = read_full_log(log_file) if log_file else "No log file"

    # Build strategy tiles from STRATEGY_* config keys
    strategy_tiles = []
    for row in config_rows:
        key = row["key"]
        if key.startswith("STRATEGY_"):
            name = key.replace("STRATEGY_", "")
            enabled = row["value"].strip().upper() == "TRUE"
            pnl_val = tag_pnl.get(name)
            trades = tag_count.get(name, 0)
            strategy_tiles.append({
                "key": key,
                "name": name,
                "enabled": enabled,
                "pnl": pnl_val,
                "trades": trades,
            })

    html = """<!DOCTYPE html>
<html>
<head>
<title>Trading Dashboard — {{ CLIENT_NAME }}</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="{{ refresh }}">
<style>
* { box-sizing: border-box; }
body {
    font-family: 'Segoe UI', Arial, sans-serif;
    background: #0f172a;
    margin: 0;
    color: #e2e8f0;
    min-height: 100vh;
}
.header {
    background: #111827;
    padding: 16px 28px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid #1f2937;
    flex-wrap: wrap;
    gap: 10px;
}
.header-title { font-size: 16px; font-weight: 600; }
.pnl-display  { font-size: 15px; }
.green { color: #22c55e; }
.red   { color: #ef4444; }
.tab-nav {
    background: #111827;
    display: flex;
    gap: 2px;
    padding: 0 28px;
    border-bottom: 1px solid #1f2937;
}
.tab-btn {
    padding: 10px 22px;
    background: none;
    border: none;
    border-bottom: 3px solid transparent;
    color: #94a3b8;
    cursor: pointer;
    font-size: 14px;
    font-weight: 500;
    transition: color 0.15s, border-color 0.15s;
}
.tab-btn:hover  { color: #e2e8f0; }
.tab-btn.active { color: #38bdf8; border-bottom-color: #38bdf8; }
.tab-content        { display: none; padding: 24px 28px; }
.tab-content.active { display: block; }
.tiles-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(175px, 1fr));
    gap: 14px;
    margin-bottom: 32px;
}
.tile {
    background: #1e293b;
    border-radius: 12px;
    padding: 16px;
    border: 2px solid #334155;
    display: flex;
    flex-direction: column;
    gap: 6px;
}
.tile.enabled  { border-color: #1d4ed8; }
.tile.disabled { border-color: #334155; opacity: 0.6; }
.tile-name { font-size: 18px; font-weight: 700; letter-spacing: 0.4px; }
.tile-badge {
    display: inline-block;
    padding: 2px 9px;
    border-radius: 20px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    width: fit-content;
}
.badge-enabled  { background: #1d4ed8; color: #bfdbfe; }
.badge-disabled { background: #374151; color: #9ca3af; }
.tile-pnl    { font-size: 15px; font-weight: 600; }
.tile-trades { font-size: 12px; color: #64748b; }
.tile-btn {
    margin-top: 6px;
    padding: 5px 0;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.3px;
    width: 100%;
}
.btn-disable { background: #dc2626; color: #fff; }
.btn-enable  { background: #16a34a; color: #fff; }
.btn-disable:hover { background: #b91c1c; }
.btn-enable:hover  { background: #15803d; }
.section-title {
    font-size: 12px;
    font-weight: 600;
    color: #64748b;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    margin: 0 0 12px;
}
table {
    width: 100%;
    border-collapse: collapse;
    background: #1e293b;
    border-radius: 12px;
    overflow: hidden;
    margin-bottom: 28px;
    box-shadow: 0 4px 12px rgba(0,0,0,0.3);
}
th {
    background: #111827;
    padding: 11px 12px;
    font-size: 12px;
    color: #94a3b8;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    border-bottom: 1px solid #334155;
}
td {
    padding: 10px 12px;
    text-align: center;
    border-bottom: 1px solid #1f2937;
    font-size: 14px;
}
.config-table input {
    width: 90%;
    background: #020617;
    border: 1px solid #334155;
    color: #e2e8f0;
    text-align: center;
    border-radius: 4px;
    padding: 4px 6px;
    font-size: 13px;
}
.save-btn {
    margin-top: 14px;
    padding: 9px 24px;
    background: #22c55e;
    border: none;
    border-radius: 6px;
    color: #fff;
    cursor: pointer;
    font-size: 14px;
    font-weight: 600;
}
.save-btn:disabled { background: #374151; color: #6b7280; cursor: not-allowed; }
.edit-block-msg { color: #ef4444; margin-bottom: 12px; font-size: 13px; }
.log-box {
    background: #020617;
    border-radius: 12px;
    padding: 16px;
    max-height: 72vh;
    overflow-y: auto;
}
.log-box pre {
    margin: 0;
    font-family: Consolas, monospace;
    font-size: 12px;
    color: #38bdf8;
    white-space: pre-wrap;
}
</style>
</head>
<body>

<div class="header">
    <div class="header-title">\U0001f4ca Trading Dashboard — {{ CLIENT_NAME }}</div>
    <div class="pnl-display">
        Cumulative PnL:&nbsp;
        <strong class="{{ 'green' if cumulative_total >= 0 else 'red' }}">{{ cumulative_total }}</strong>
    </div>
    <div style="font-size:13px;color:#64748b;">{{ now }}</div>
</div>

<div class="tab-nav">
    <button class="tab-btn active" onclick="showTab('overview',this)">Overview</button>
    <button class="tab-btn" onclick="showTab('config',this)">Config</button>
    <button class="tab-btn" onclick="showTab('logs',this)">Logs</button>
</div>

<!-- ===== TAB: OVERVIEW ===== -->
<div id="tab-overview" class="tab-content active">

    <p class="section-title">Strategies</p>
    <div class="tiles-grid">
    {% for tile in strategy_tiles %}
    <div class="tile {{ 'enabled' if tile.enabled else 'disabled' }}">
        <div class="tile-name">{{ tile.name }}</div>
        <span class="tile-badge {{ 'badge-enabled' if tile.enabled else 'badge-disabled' }}">
            {{ 'Enabled' if tile.enabled else 'Disabled' }}
        </span>
        {% if tile.pnl is not none %}
        <div class="tile-pnl {{ 'green' if tile.pnl >= 0 else 'red' }}">
            {{ '{:+,.0f}'.format(tile.pnl) }}
        </div>
        {% else %}
        <div class="tile-pnl" style="color:#475569;">No trades</div>
        {% endif %}
        <div class="tile-trades">{{ tile.trades }} trade{{ 's' if tile.trades != 1 else '' }}</div>
        <form action="/toggle" method="POST" style="margin:0;">
            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
            <input type="hidden" name="key" value="{{ tile.key }}">
            <button type="submit" class="tile-btn {{ 'btn-disable' if tile.enabled else 'btn-enable' }}">
                {{ 'Disable' if tile.enabled else 'Enable' }}
            </button>
        </form>
    </div>
    {% endfor %}
    </div>

    <p class="section-title">Open Positions ({{ position_avg | length }})</p>
    <table>
        <tr><th>Tag</th><th>Strike</th><th>Avg Entry</th></tr>
        {% for (tag, strike), avg in position_avg.items() %}
        <tr>
            <td><strong>{{ tag }}</strong></td>
            <td>{{ strike }}</td>
            <td>{{ avg }}</td>
        </tr>
        {% endfor %}
        {% if not position_avg %}
        <tr><td colspan="3" style="color:#475569;padding:16px;">No open positions</td></tr>
        {% endif %}
    </table>

    <p class="section-title">Closed PnL by Strategy</p>
    <table>
        <tr><th>Strategy</th><th>Trades</th><th>PnL</th></tr>
        {% for tag, total in tag_pnl.items() %}
        <tr>
            <td><strong>{{ tag }}</strong></td>
            <td>{{ tag_count[tag] }}</td>
            <td class="{{ 'green' if total >= 0 else 'red' }}">{{ total }}</td>
        </tr>
        {% endfor %}
        {% if not tag_pnl %}
        <tr><td colspan="3" style="color:#475569;padding:16px;">No closed trades today</td></tr>
        {% endif %}
    </table>

</div>

<!-- ===== TAB: CONFIG ===== -->
<div id="tab-config" class="tab-content">
    <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    {% if edit_block %}
    <div class="edit-block-msg">⚠ Parameter editing disabled during market hours (09:09–15:20)</div>
    {% endif %}
    <table class="config-table">
        <tr><th style="text-align:left;">Key</th><th>Value</th><th style="text-align:left;">Reference</th></tr>
        {% for row in config_rows %}
        <tr>
            <td style="text-align:left;">{{ row.key }}</td>
            <td>
                <input type="text"
                    name="value_{{ row.key }}"
                    value="{{ row.value }}"
                    {% if edit_block %}disabled{% endif %}>
            </td>
            <td style="text-align:left;color:#64748b;font-size:12px;">{{ row.reference }}</td>
        </tr>
        {% endfor %}
    </table>
    <button type="submit" class="save-btn" {% if edit_block %}disabled{% endif %}>
        Save Configuration
    </button>
    </form>
</div>

<!-- ===== TAB: LOGS ===== -->
<div id="tab-logs" class="tab-content">
    <p class="section-title">Full Log</p>
    <div class="log-box" id="logBox">
        <pre id="logPre">{{ log_full }}</pre>
    </div>
</div>

<script>
function showTab(name, btn) {
    document.querySelectorAll('.tab-content').forEach(function(el) { el.classList.remove('active'); });
    document.querySelectorAll('.tab-btn').forEach(function(el) { el.classList.remove('active'); });
    document.getElementById('tab-' + name).classList.add('active');
    if (btn) btn.classList.add('active');
    if (name === 'logs') scrollLogToBottom();
}
function scrollLogToBottom() {
    var box = document.getElementById('logBox');
    if (box) box.scrollTop = box.scrollHeight;
}
</script>

</body>
</html>"""

    now = datetime.now().strftime("%H:%M:%S")

    return render_template_string(
        html,
        CLIENT_NAME=CLIENT_NAME,
        csrf_token=CSRF_TOKEN,
        position_avg=position_avg,
        tag_pnl=tag_pnl,
        tag_count=tag_count,
        cumulative_total=cumulative_total,
        now=now,
        refresh=REFRESH_SECONDS,
        log_tail=log_tail,
        log_full=log_full,
        config_rows=config_rows,
        edit_block=is_edit_block_time(),
        strategy_tiles=strategy_tiles,
    )
