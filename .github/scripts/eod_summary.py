#!/usr/bin/env python3
import os
import json
import requests
from datetime import datetime
from pathlib import Path
from openai import OpenAI

BASE_DIR = Path(__file__).resolve().parent.parent.parent
HISTORY_FILE = BASE_DIR / "desk_history.json"
LEDGER_FILE_ALT = BASE_DIR / "trades" / "daily_trade_ledger.json"
LEDGER_FILE = BASE_DIR / "daily_trade_ledger.json" if (BASE_DIR / "daily_trade_ledger.json").exists() else LEDGER_FILE_ALT

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

def main():
    print(f"Reading history from: {HISTORY_FILE}")
    try:
        history = json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else []
    except Exception as e:
        print(f"Error reading history file: {e}")
        history = []

    if not history:
        print("Notice: desk_history.json is empty or missing. Creating a status update message for Telegram.")
        history = [{"ticker": "SYSTEM", "verdict": "INFO", "rationale": "No signals were logged in desk_history.json for this session."}]

    date_str = datetime.now().strftime("%d %b %Y")
    total_signals = len(history)
    go_count = sum(1 for h in history if h.get("verdict") == "GO")
    skip_count = sum(1 for h in history if h.get("verdict") == "SKIP")
    
    local_ledger_data = "No local ledger data found."
    try:
        target_ledger = LEDGER_FILE if LEDGER_FILE.exists() else LEDGER_FILE_ALT
        if target_ledger.exists():
            local_ledger_data = str(json.loads(target_ledger.read_text(encoding="utf-8")))[:1000]
    except Exception:
        pass

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY or OPENAI_API_KEY is not set.")
        return

    client = OpenAI(
        api_key=api_key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
    )

    prompt = f"""
    You are the Chief Investment Officer. Generate an End-of-Day Performance Ledger based on the provided history. Do not use code blocks, just raw markdown.
    
    Today's Date: {date_str}
    Total Signals Evaluated: {total_signals}
    GO: {go_count}, SKIP: {skip_count}
    
    History of trades evaluated:
    {json.dumps(history, indent=2)}
    
    Local Agent Ledger Snippet: {local_ledger_data}
    
    OUTPUT FORMAT EXACTLY LIKE THIS:
    *EOD Performance Ledger — {date_str}*
    Signals evaluated: {total_signals} (tracked: {go_count} GO / {skip_count} SKIP)
    GO simulated: win rate assessment based on logged targets/stops
    SKIP veto accuracy: evaluated from market moves
    Shadow desk book (cumulative): active tracking
    Local agent laptop book (cumulative): summary from ledger snippet
    
    *Learnings & Market Behavior:*
    [Provide a concise 2-paragraph analysis of the day's structural themes, ticker movements, and whether the GO/SKIP decisions successfully avoided drawdown or captured targets.]
    """

    try:
        response = client.chat.completions.create(
            model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            temperature=0.2,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
        )
        report = response.choices[0].message.content.strip()
    except Exception as e:
        print(f"LLM call failed: {e}")
        return

    print("Generated Report:\n", report)

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        print("Sending message to Telegram...")
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": report, "parse_mode": "Markdown"}
            res = requests.post(url, json=payload, timeout=15)
            print(f"Telegram response status: {res.status_code}, body: {res.text}")
        except Exception as e:
            print(f"Telegram alert failed: {e}")
    else:
        print("Warning: Telegram tokens are missing in environment variables. Message not sent.")
            
    try:
        HISTORY_FILE.write_text("[]", encoding="utf-8")
    except Exception as e:
        print(f"Failed to reset history file: {e}")

if __name__ == "__main__":
    main()
