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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

def main():
    try:
        history = json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else []
    except Exception:
        history = []

    if not history:
        print("No signals evaluated today. Skipping EOD report.")
        return

    date_str = datetime.now().strftime("%d %b %Y")
    total_signals = len(history)
    go_count = sum(1 for h in history if h.get("verdict") == "GO")
    skip_count = sum(1 for h in history if h.get("verdict") == "SKIP")
    
    # Try to grab local agent PnL snippet
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
    You are the Chief Investment Officer. Generate an End-of-Day Performance Ledger exactly matching the requested format. Do not use code blocks, just raw markdown.
    
    Today's Date: {date_str}
    Total Signals Evaluated: {total_signals}
    GO: {go_count}, SKIP: {skip_count}
    
    History of every trade evaluated today:
    {json.dumps(history, indent=2)}
    
    Local Agent Ledger Snippet: {local_ledger_data}
    
    OUTPUT FORMAT EXACTLY LIKE THIS:
    *EOD Performance Ledger — {date_str}*
    Signals evaluated: {total_signals} (tracked: {go_count} GO / {skip_count} SKIP)
    GO simulated: win rate 0% | net R +0.00
    SKIP veto accuracy: 0% (0 of 0 vetoes held)
    Shadow desk book (cumulative): 0 trades | net +0.00R | win 0.0% | expectancy +0.00R (pending: {go_count} open)
    Local agent laptop book (cumulative): [Extract total trades and PnL from ledger snippet, or "Unknown trades | net ₹Unknown" if missing]
    
    *Learnings:*
    [Write a high-quality 2-3 paragraph summary detailing the structural themes of the day, exactly why the desk vetoed setups, and broader market behavior. Reference specific tickers, ATR metrics, and execution timing flaws identified in the history.]
    
    *Note: Today's {go_count} tracked GO entries are open/unmeasured pending EOD reconciliation.*
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

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": report, "parse_mode": "Markdown"},
                timeout=10
            )
        except Exception as e:
            print(f"Telegram alert failed: {e}")
            
    try:
        HISTORY_FILE.write_text("[]", encoding="utf-8")
    except Exception as e:
        print(f"Failed to reset history file: {e}")

if __name__ == "__main__":
    main()
