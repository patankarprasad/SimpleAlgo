# SimpleAlgo – Supertrend + MA Strategy

Runs a **Supertrend (10,2) + Supertrend (10,3) + SMA-50** strategy on multiple
futures instruments, fetching data from **Angel SmartAPI** (free) and executing
orders through **Zerodha KiteConnect**.

---

## Project structure

```
SimpleAlgo/
├── config.py           ← instrument defaults, strategy params, credential loading
├── instrument_config.py ← runtime lots + stock-futures list (edited from /settings)
├── angel_login.py      ← auto-login to Angel SmartAPI (TOTP)
├── kite_login.py       ← auto-login to Kite (Selenium headless)
├── angel_data.py       ← fetch OHLCV candles from Angel
├── indicators.py       ← Supertrend + SMA (matches Pine Script)
├── order_manager.py    ← place/close orders on Kite
├── state.py            ← persist open positions to JSON
├── main.py             ← scheduler + strategy loop
├── utils/
│   ├── token_helper.py ← manual Kite token helper (use when Selenium fails)
│   └── find_tokens.py  ← look up Angel instrument tokens
├── deploy/
│   ├── algo.service    ← systemd service file for VPS
│   └── vps_setup.sh    ← one-shot VPS provisioning script
├── .env.example        ← copy to .env and fill in your credentials
└── requirements.txt
```

---

## Quick start (local / VPS)

### 1. Install dependencies
```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure credentials
```bash
cp .env.example .env
nano .env                         # fill in all values
```

### 3. Find active futures contracts & update config.py
```bash
python utils/find_tokens.py
```
Update the `angel_symbol`, `kite_tradingsymbol` fields in `config.py` to the
current month's active contract (e.g. `GOLDM25MAYFUT`).

### 4. First Kite login
Kite requires a browser login.  Run `utils/token_helper.py` on any machine with
a browser, then paste the access token on your VPS:
```bash
python utils/token_helper.py
```
You must do this **once per trading day** (Kite tokens expire at midnight).
If your VPS has a GUI or you install Chromium, `kite_login.py` will do it automatically.

### 5. Run once to test
```bash
python main.py --run-once
```

### 6. Start the scheduler
```bash
python main.py          # runs forever, fires at each candle close
```

---

## VPS deployment (Ubuntu 22.04)

```bash
bash deploy/vps_setup.sh
sudo systemctl start algo
sudo journalctl -u algo -f    # live logs
```

---

## Key design decisions

| Question | Choice | Reason |
|---|---|---|
| Historical data | Angel SmartAPI | Free, reliable OHLCV history |
| Order execution | Zerodha KiteConnect | Robust API, low latency |
| Candle timing | `iloc[-2]` (last closed candle) | Avoids trading on in-progress candles |
| ATR smoothing | Wilder's RMA (alpha=1/period) | Matches Pine Script `ta.rma()` exactly |
| Position state | JSON file | Simple, survives restarts |
| Login | TOTP for Angel (auto), Selenium for Kite | Angel fully supports TOTP; Kite needs browser OAuth |

---

## Updating expiry contracts

On expiry day, update `config.py` → `INSTRUMENTS` list with the new contract symbol
for the next month (e.g. `GOLDM25MAYFUT` → `GOLDM25JUNFUT`).  Then restart the service.

---

## Changing instruments from the dashboard

The **Settings** tab (`/settings`) edits what would otherwise need a code or
`.env` change plus a restart:

- **Stock futures** – add or remove NSE stocks, set the default lots and product.
  `STOCK_FUTURES*` in `.env` now only seeds `instrument_config.json` on first
  start; after that the dashboard is the source of truth.
- **Lots** – per-instrument lot count for every 15-minute, hourly and stock
  instrument. Broker order quantity is lots × lot size.
- **Restart Service** – restarts the whole process (also on the dashboard status
  bar). Needs the sudoers rule in DEPLOYMENT.txt section 8a.

Saves are applied to the running scheduler immediately — no restart needed.

An instrument holding an open position is locked: exits are sized from the
configured lots, so changing them mid-trade would close the wrong quantity.
Square off first.

---

## Risk warnings

- This is educational code. Test thoroughly in **paper trading** before using real money.
- No stop-loss is coded beyond the strategy's own supertrend exit – add one if needed.
- MCX instruments trade in the evening session; adjust `TRADE_END_TIME` accordingly.
- Always monitor positions; automated systems can misfire on API errors.
