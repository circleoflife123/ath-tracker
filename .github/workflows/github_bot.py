#!/usr/bin/env python3
"""
Runs once per GitHub Actions schedule tick. Each run:
  1. Checks Telegram for any /update command sent since the last run, replies if found
  2. Runs the ATH/52w drawdown check and fires step alerts

No persistent process needed — the GitHub Actions schedule itself is the "always on"
loop. /update replies land within one schedule interval (see workflow file), not
instantly. Telegram queues unread messages, so nothing is lost between runs.
"""

import os
import requests

from ath_tracker import TICKERS, fetch_ticker_data, format_as_of, load_state, save_state, run as run_alert_check

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def send_message(text):
    requests.post(
        f"{API_BASE}/sendMessage",
        data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=10,
    )


def build_update_message():
    lines = []
    for label, symbol in TICKERS.items():
        data = fetch_ticker_data(symbol)
        if data is None:
            lines.append(f"<b>{label}</b>: fetch failed")
            continue
        current_price, true_ath, high_52w, as_of = data
        as_of_str = format_as_of(as_of)
        dd_ath = (true_ath - current_price) / true_ath * 100
        dd_52w = (high_52w - current_price) / high_52w * 100
        lines.append(
            f"<b>{label} (as of {as_of_str})</b>\n"
            f"{current_price:,.2f} | ATH {true_ath:,.2f} (DD {-dd_ath:.1f}%) | "
            f"52w {high_52w:,.2f} (DD {-dd_52w:.1f}%)"
        )
    return "\n\n".join(lines)


def check_for_commands():
    """Poll Telegram once for any messages since the last run's offset."""
    state = load_state()
    offset = state.get("_telegram_offset")

    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset

    try:
        r = requests.get(f"{API_BASE}/getUpdates", params=params, timeout=15)
        data = r.json()
    except Exception as e:
        print(f"[ERROR] getUpdates failed: {e}")
        return

    for update in data.get("result", []):
        state["_telegram_offset"] = update["update_id"] + 1
        message = update.get("message", {})
        text = message.get("text", "")
        if text.startswith("/update"):
            print("[INFO] /update received, replying...")
            send_message(build_update_message())

    save_state(state)


if __name__ == "__main__":
    check_for_commands()
    run_alert_check()
