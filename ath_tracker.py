#!/usr/bin/env python3
"""
ATH drawdown tracker with Telegram step-alerts.

Logic:
  - Pull full price history per ticker -> true all-time high (ATH)
  - Pull 1y history -> 52-week high (display only, not an alert trigger)
  - drawdown_pct = (ATH - current_price) / ATH * 100
  - Alert fires once drawdown_pct crosses 5%, then again every additional
    full 1% step (6%, 7%, 8%...). State is persisted so each step only
    alerts once, and resets if price recovers back above the 5% line.

Run on a schedule (cron). See README block at bottom for setup.
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests
import yfinance as yf

# ---------- CONFIG ----------

TICKERS = {
    "VWRA": "VWRA.L",
    "SPY": "SPY",
    "QQQ": "QQQ",
    "TQQQ": "TQQQ",
    "2800": "2800.HK",
    "IAU": "IAU",
}

ALERT_START_PCT = 5.0   # drawdown level at which alerting begins
ALERT_STEP_PCT = 1.0    # re-alert every additional full % drop

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ath_state.json")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ---------- STATE ----------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ---------- TELEGRAM ----------

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram not configured, skipping send:", message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"[ERROR] Telegram send failed: {e}")

# ---------- CORE ----------

def fetch_ticker_data(yahoo_symbol):
    """Returns (current_price, true_ath, high_52w, as_of_timestamp) or None on failure."""
    try:
        t = yf.Ticker(yahoo_symbol)
        hist_all = t.history(period="max", auto_adjust=False)
        if hist_all.empty:
            return None
        true_ath = float(hist_all["High"].max())

        # Prefer a live intraday quote (and its timestamp) when the market's open.
        current_price = None
        as_of = None
        try:
            intraday = t.history(period="1d", interval="1m", auto_adjust=False)
            if not intraday.empty:
                current_price = float(intraday["Close"].iloc[-1])
                as_of = intraday.index[-1]
        except Exception:
            pass

        # Market closed / no intraday data -> fall back to last daily close.
        if current_price is None:
            current_price = float(hist_all["Close"].iloc[-1])
            as_of = hist_all.index[-1]

        hist_1y = hist_all.tail(252)  # ~1 trading year
        high_52w = float(hist_1y["High"].max())

        return current_price, true_ath, high_52w, as_of
    except Exception as e:
        print(f"[ERROR] Failed to fetch {yahoo_symbol}: {e}")
        return None


def format_as_of(ts):
    """Format a pandas Timestamp into Singapore local time, e.g. '14 August at 12:57:47 pm GMT+8'."""
    try:
        sgt = ts.tz_convert("Asia/Singapore")
    except Exception:
        sgt = ts  # already naive / unexpected tz, best effort
    return sgt.strftime("%-d %B at %-I:%M:%S %p GMT+8")

def run():
    state = load_state()

    for label, symbol in TICKERS.items():
        data = fetch_ticker_data(symbol)
        if data is None:
            continue
        current_price, true_ath, high_52w, as_of = data
        as_of_str = format_as_of(as_of)

        drawdown_pct = (true_ath - current_price) / true_ath * 100
        drawdown_abs = true_ath - current_price
        drawdown_52w_pct = (high_52w - current_price) / high_52w * 100

        prev = state.get(label, {"last_alert_step_ath": 0, "last_alert_step_52w": 0})
        last_step_ath = prev.get("last_alert_step_ath", 0)
        last_step_52w = prev.get("last_alert_step_52w", 0)

        def step_for(drawdown):
            if drawdown >= ALERT_START_PCT:
                return ALERT_START_PCT + (
                    (drawdown - ALERT_START_PCT) // ALERT_STEP_PCT
                ) * ALERT_STEP_PCT
            return 0

        current_step_ath = step_for(drawdown_pct)
        current_step_52w = step_for(drawdown_52w_pct)

        fired = []
        if current_step_ath > last_step_ath:
            fired.append(f"ATH: down {drawdown_pct:.1f}% (ATH {true_ath:,.2f}, off by {drawdown_abs:,.2f})")
        if current_step_52w > last_step_52w:
            fired.append(f"52w high: down {drawdown_52w_pct:.1f}% (52w high {high_52w:,.2f})")

        if fired:
            msg = f"⚠️ <b>{label} ({symbol}) — as of {as_of_str}</b>\n@ {current_price:,.2f}\n" + "\n".join(fired)
            send_telegram(msg)
            print(f"[ALERT] {label}: {'; '.join(fired)}")

        # Reset each metric independently once price recovers above its threshold
        if drawdown_pct < ALERT_START_PCT:
            current_step_ath = 0
        if drawdown_52w_pct < ALERT_START_PCT:
            current_step_52w = 0

        state[label] = {
            "symbol": symbol,
            "last_alert_step_ath": current_step_ath,
            "last_alert_step_52w": current_step_52w,
        }

        print(f"{label} (as of {as_of_str}): {current_price:.2f} | ATH {true_ath:.2f} | DD {-drawdown_pct:.2f}% | 52w DD {-drawdown_52w_pct:.2f}%")

    save_state(state)

if __name__ == "__main__":
    run()

# ---------------------------------------------------------------------------
# SETUP
# ---------------------------------------------------------------------------
# 1. Telegram bot:
#    - Message @BotFather on Telegram -> /newbot -> follow prompts -> copy the token
#    - Message your new bot anything once (so it can find your chat)
#    - Visit https://api.telegram.org/bot<TOKEN>/getUpdates in a browser
#      -> find "chat":{"id": ...} in the JSON -> that's your TELEGRAM_CHAT_ID
#
# 2. Set env vars (put in ~/.bashrc or a .env you source before running):
#    export TELEGRAM_BOT_TOKEN="123456:ABC-your-token"
#    export TELEGRAM_CHAT_ID="123456789"
#
# 3. Test manually:
#    python3 ath_tracker.py
#
# 4. Schedule via cron (every 15 min, adjust as needed):
#    crontab -e
#    */15 * * * * /usr/bin/env bash -c 'source ~/.bashrc && /usr/bin/python3 /path/to/ath_tracker.py >> /path/to/tracker.log 2>&1'
#
#    Note: cron runs with a minimal environment, so the `source ~/.bashrc`
#    (or explicit export in the crontab line) is what gets the Telegram
#    env vars into the job. If it's not alerting, check tracker.log first.
# ---------------------------------------------------------------------------
