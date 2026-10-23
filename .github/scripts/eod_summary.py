#!/usr/bin/env python3
import os
import json
import requests
from datetime import datetime
from pathlib import Path
from openai import OpenAI
import yfinance as yf

BASE_DIR = Path(__file__).resolve().parent.parent.parent
HISTORY_FILE = BASE_DIR / "desk_history.json"
LEDGER_FILE_ALT = BASE_DIR / "trades" / "daily_trade_ledger.json"
LEDGER_FILE = BASE_DIR / "daily_trade_ledger.json" if (BASE_DIR / "daily_trade_ledger.json").exists() else LEDGER_FILE_ALT

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

def evaluate_signal_outcome(signal):
    """Programmatically check if a signal hit target or stop loss using yfinance."""
    ticker = signal.get("ticker")
    action = str(signal.get("action", "BUY")).upper()
    entry = float(signal.get("entry", 0))
    tgt = float(signal.get("tgt", 0))
    sl = float(signal.get("sl", 0))
    
    if not ticker or entry <= 0:
        return "PENDING", 0.0

    try:
        # Fetch today's price action for the NSE stock
        data = yf.download(f"{ticker}.NS", period="1d", interval="5m", progress=False)
        if data.empty:
            data = yf.download(f"{ticker}.NS", period="5d", interval="1d", progress=False)
        
        if data.empty:
            return "NO_DATA", 0.0

        day_high = float(data["High"].max())
        day_low = float(data["Low"].min())

        hit_target = False
        hit_stop = False

        if action == "BUY":
            if day_high >= tgt:
                hit_target = True
            if day_low <= sl:
                hit_stop = True
        else: # SELL / SHORT
            if day_low <= tgt:
                hit_target = True
            if day_high >= sl:
                hit_stop = True

        if hit_target and not hit_stop:
            return "WIN", abs(tgt - entry)
        elif hit_stop and not hit_target:
            return "LOSS", -abs(entry - sl)
        elif hit_target and hit_stop:
            # If both touched, check which came first or mark conservative outcome
            return "MIXED/STOP", -abs(entry - sl)
        else:
            return "OPEN/EXPIRED", 0.0
    except Exception as e:
        print(f"Failed to fetch price for {ticker}: {e}")
        return "ERROR", 0.0

def main():
    print(f"Reading history from: {HISTORY_FILE}")
    try:
        history = json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else []
    except Exception as e:
        print(f"Error reading history file: {e}")
        history = []

    if not history:
        print("Notice: desk_history.json is empty or missing.")
        return

    date_str = datetime.now().strftime("%d %b %Y")
    total_signals = len(history)
    
    evaluated_results = []
    go_wins = 0
    go_losses = 0
    skip_correct = 0
    skip_total = 0

    for h in history:
        verdict = h.get("verdict", "SKIP")
        outcome, pnl_pts = evaluate_signal_outcome(h)
        h["evaluated_outcome"] = outcome
        h["pnl_pts"] = pnl_pts
        evaluated_results.append(h)

        if verdict == "GO":
            if outcome == "WIN":
                go_wins += 1
            elif outcome in ("LOSS", "MIXED/STOP"):
                go_losses += 1
        elif verdict == "SKIP":
            skip_total += 1
            # If a SKIP trade hit a loss or didn't reach target, the skip was a good decision
            if outcome in ("LOSS", "MIXED/STOP", "OPEN/EXPIRED"):
                skip_correct += 1

    go_total_settled = go_wins + go_losses
    go_win_rate = (go_wins / go_total_settled * 100) if go_total_settled > 0 else 0.0
    skip_accuracy = (skip_correct / skip_total * 100) if skip_total > 0 else 0.0

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
        print("Error: API key is not set.")
        return

    client = OpenAI(
        api_key=api_key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
    )

    prompt = f"""
    You are the Chief Investment Officer. Generate an End-of-Day Performance Ledger based on the programmatic price evaluation results. Do not use code blocks, just raw markdown.
    
    Today's Date: {date_str}
    Total Signals Evaluated: {total_signals}
    GO: {go_count}, SKIP: {skip_count}
    
    Programmatic Outcome Metrics:
    - GO Win Rate: {go_win_rate:.1f}% ({go_wins} wins, {go_losses} losses out of {go_total_settled} settled)
    - SKIP Veto Accuracy: {skip_accuracy:.1f}% ({skip_correct} correct skips out of {skip_total})
    
    Detailed Evaluation History:
    {json.dumps(evaluated_results, indent=2)}
    
    Local Agent Ledger Snippet: {local_ledger_data}
    
    OUTPUT FORMAT EXACTLY LIKE THIS:
    *EOD Performance Ledger — {date_str}*
    Signals evaluated: {total_signals} (tracked: {go_count} GO / {skip_count} SKIP)
    GO simulated: win rate {go_win_rate:.1f}% | settled: {go_wins}W {go_losses}L
    SKIP veto accuracy: {skip_accuracy:.1f}% ({skip_correct} of {skip_total} safe avoids)
    Shadow desk book (cumulative): {total_signals} trades evaluated via live market high/low
    Local agent laptop book (cumulative): summary from ledger snippet
    
    *Learnings & Market Behavior:*
    [Provide a concise 2-paragraph analysis detailing which specific tickers hit targets or stops, why the desk's GO/SKIP decisions succeeded or failed based on price action, and what rules we can embed into the main agent next.]
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
            
    try:
        HISTORY_FILE.write_text("[]", encoding="utf-8")
    except Exception as e:
        print(f"Failed to reset history file: {e}")

if __name__ == "__main__":
    main()
