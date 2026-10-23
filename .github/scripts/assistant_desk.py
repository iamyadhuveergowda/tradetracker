#!/usr/bin/env python3
"""
Assistant Desk Multi-Analyst Engine:
Runs the 4-analyst institutional debate using Gemini, 
saves the verdict, and pushes an alert to Telegram.
"""

from __future__ import annotations
import os
import json
import re
import requests
from pathlib import Path
from openai import OpenAI

BASE_DIR = Path(__file__).resolve().parent.parent.parent
SIGNALS_FILE = BASE_DIR / "signals" / "latest.json"
VERDICT_FILE = BASE_DIR / "desk_verdict.json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

def send_telegram_alert(verdict_data: dict, ticker: str, action: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing. Skipping alert.")
        return

    verdict = verdict_data.get("verdict", "SKIP")
    confidence = verdict_data.get("confidence", 0)
    notes = verdict_data.get("analyst_notes", {})
    
    emoji = "🟢" if verdict == "GO" else "🔴"
    
    text = (
        f"{emoji} *ASSISTANT DESK VERDICT* {emoji}\n\n"
        f"*Ticker:* {ticker} ({action})\n"
        f"*Decision:* {verdict} (Confidence: {confidence}%)\n\n"
        f"*Chief's Summary:*\n{verdict_data.get('summary', 'No summary provided.')}\n\n"
        f"*Analyst Breakdown:*\n"
        f"📈 *Technical:* {notes.get('technical', 'N/A')}\n"
        f"📰 *Sentiment:* {notes.get('sentiment', 'N/A')}\n"
        f"🌍 *Macro:* {notes.get('macro', 'N/A')}\n"
        f"🛡️ *Risk:* {notes.get('risk', 'N/A')}"
    )

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        if not resp.ok:
            print(f"Telegram send failed: HTTP {resp.status_code} - {resp.text}")
        else:
            print("Telegram alert sent successfully.")
    except Exception as exc:
        print(f"Telegram exception: {exc}")

def run_multi_analyst_pipeline() -> None:
    if not SIGNALS_FILE.exists():
        print("No signals/latest.json found to analyze.")
        return

    try:
        data = json.loads(SIGNALS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Error reading signals file: {e}")
        return

    candidates = data.get("candidates", [])
    if not candidates:
        print("No candidates found in signals payload.")
        return

    target_candidate = candidates[0]
    ticker = target_candidate.get("ticker")
    action = target_candidate.get("signal")
    
    print(f"Starting Gemini 4-Analyst Deep Debate for {ticker} ({action})...")

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY secret is missing.")

    # Using Google's OpenAI-compatible endpoint
    client = OpenAI(
        api_key=api_key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
    )

    prompt = f"""
    You are the Chief Investment Officer overseeing a rigorous 4-analyst trading desk.
    Evaluate the following incoming intraday trade candidate through four distinct professional lenses:

    CANDIDATE SETUP:
    {json.dumps(target_candidate, indent=2)}

    THE 4-ANALYST DEBATE FRAMEWORK:
    1. TECHNICAL ANALYST: Evaluate momentum, RSI levels, ATR buffer, distance from VWAP, and candlestick conviction. Is price chasing or is there a clean structure?
    2. NEWS & SENTIMENT ANALYST: Screen for headline anomalies, unverified noise, or micro-catalysts that could invalidate the technical edge.
    3. SECTOR & MACRO ANALYST: Check if the sector trend aligns with index momentum (NIFTY/SPY regime rules). Are we fighting the broader tape?
    4. RISK MANAGER: Scrutinize the Risk-to-Reward ratio, stop-loss placement against noise/harvest zones, and capital exposure.

    CHIEF'S SYNTHESIS & FINAL VERDICT:
    Weigh all 4 perspectives critically. Default to skepticism. If any analyst flags high structural risk, reject it.
    
    Return your output strictly as a JSON object with this exact schema:
    {{
      "ticker": "{ticker}",
      "action": "{action}",
      "analyst_notes": {{
        "technical": "Summary of technical view",
        "sentiment": "Summary of news/sentiment view",
        "macro": "Summary of sector/macro view",
        "risk": "Summary of risk manager view"
      }},
      "verdict": "GO" or "SKIP",
      "confidence": <integer between 0 and 100>,
      "summary": "Concise 2-sentence rationale for the final verdict."
    }}
    """

    try:
        response = client.chat.completions.create(
            model="gemini-3.8-flash",
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a risk-first institutional investment committee."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=3000,
        )
        result_text = response.choices[0].message.content or "{}"
        
        # Clean up markdown code blocks if Gemini returns them
        result_text = re.sub(r"^```(?:json)?|```$", "", result_text.strip(), flags=re.I).strip()
        
        verdict_data = json.loads(result_text)
    except Exception as exc:
        print(f"Gemini Analyst chain execution failed: {exc}")
        verdict_data = {
            "ticker": ticker,
            "action": action,
            "verdict": "SKIP",
            "confidence": 0,
            "summary": f"Analyst chain encountered a parsing or connection error; defaulting to safe SKIP. Detail: {exc}",
            "analyst_notes": {}
        }

    # Fire Telegram Alert
    send_telegram_alert(verdict_data, ticker, action)

    # Format and save payload for the Oracle VM
    final_payload = {
        "signal_id": data.get("signal_id"),
        "ticker": ticker,
        "action": action,
        "verdict": verdict_data.get("verdict", "SKIP").upper(),
        "confidence": int(verdict_data.get("confidence", 50)),
        "age_min": 0.0,
        "analyst_notes": verdict_data.get("analyst_notes", {}),
        "rationale": verdict_data.get("summary", "")
    }

    VERDICT_FILE.write_text(json.dumps(final_payload, indent=2), encoding="utf-8")
    print(f"Successfully generated desk verdict for {ticker}: {final_payload['verdict']} (Confidence: {final_payload['confidence']}%)")

if __name__ == "__main__":
    run_multi_analyst_pipeline()
