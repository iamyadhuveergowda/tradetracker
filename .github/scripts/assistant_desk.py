#!/usr/bin/env python3
"""
Assistant desk for the NIFTY intraday agent — ADVISORY ONLY (fixed build).

Judges EVERY candidate in signals/latest.json with Gemini (4-analyst
framework) and:
  * writes desk_verdict.json      (latest judged batch)
  * appends to desk_history.json  (permanent record the EOD scorer grades)
  * sends one Telegram message per new signal batch

Fixes vs the previous build (reviewed 2026-10-23):
  * ALL candidates are judged, not only candidates[0].
  * Records carry entry/stop/target + signal timestamp, so the EOD can
    actually score them (previously the scorer read prices that were
    never stored and graded everything PENDING).
  * signal_id dedupe: re-pushes and manual re-runs never re-judge or
    re-alert the same batch, and never duplicate history records.
  * 2h ticker+direction dedupe (old Muse rule): a candidate whose ticker
    and action were already judged within the last 120 minutes is skipped
    for this batch, so re-published setups do not spam Telegram or inflate
    history. * Fixed template + trigger: every verdict carries a trigger
    condition (e.g. 'Valid only on a break below 1183.60'), and Telegram
    shows SL/Target + trigger + rationale + key risk per ticker.
  * age_min is the real judged delay, not a hardcoded 0.0.
  * A Gemini failure writes NOTHING and alerts NOTHING — no fabricated
    SKIP verdicts polluting the track record.
  * The sentiment analyst has no news feed and is instructed to judge
    from the tape and say so, instead of asserting headlines it cannot
    see. The model is one env setting (GEMINI_MODEL) shared with the
    EOD script; an unset/empty value falls back to the default.
  * FEASIBILITY (draft patch, Oct 2026, step 2 alone; 1 and 4 deferred):
    a deterministic flag, not a hard reject. If target_distance_atr
    is >=5.5 with <=60 min to close, or >=4.5 with <=30 min, the
    candidate is INFEASIBLE; a looser TIGHT tier covers >=5.5 with
    <=120 min or >=4.5 with <=60 min. Grounded: all 17 NIFTY ledger
    trades were <=3.89 ATR with >=136 min left (zero flags), while
    the Oct 23 batch was 5.96-7.65 ATR with 46 min left (all flagged).
    The flag is injected into the prompt, stored, and shown on
    Telegram. Gemini may still GO, but must justify it explicitly.
  * RESULTS RISK (draft patch, Oct 2026): earnings_calendar_nifty.json
    at the repo root lists {date, ticker} results dates. The desk
    injects a flag into the Gemini prompt ONLY for candidates whose
    ticker reports today, tomorrow, yesterday, or 2 days ago (weekend
    results). It never invents a results date, never blocks a trade
    (advisory only), and fails open if the file is missing/malformed.
    The flag is also stored in history/verdict and shown on Telegram.

The desk has no connection to the trading agent: nothing here can
place, block, or modify a trade.

Env (GitHub Actions secrets):
  GEMINI_API_KEY     required for verdicts
  GEMINI_MODEL       optional, default "gemini-3.8-flash"
  TELEGRAM_BOT_TOKEN optional (no Telegram if unset)
  TELEGRAM_CHAT_ID   optional

Stdlib only.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-3.8-flash"

def gemini_post_with_retry(req, timeout=90, attempts=3):
    """POST to Gemini with retries on timeouts / 5xx / 429 only.

    A 4xx other than 429 (e.g. 404 bad model) fails fast -- retrying
    cannot fix it. 429 is retryable: rate-limit windows are usually
    per-minute, so 429 uses longer backoff (30s then 60s) than
    timeouts/5xx (10s then 20s). A timeout, like the Oct 24 mock run,
    is retried: 90s per attempt. Raises the last error.
    """
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                last_exc = exc
            elif 400 <= exc.code < 500:
                raise
            else:
                last_exc = exc
        except Exception as exc:
            last_exc = exc
        if attempt < attempts:
            print(f"Gemini attempt {attempt} failed ({last_exc}); retrying...",
                  file=sys.stderr)
            if isinstance(last_exc, urllib.error.HTTPError) and last_exc.code == 429:
                time.sleep(30 * attempt)
            else:
                time.sleep(10 * attempt)
    raise last_exc



DEDUP_WINDOW_MINUTES = 120  # old Muse rule: same ticker+direction within 2h = same setup

def _parse_dt(s):
    try:
        dt = datetime.fromisoformat(str(s))
        return dt
    except Exception:
        return None

def filter_deduped(candidates, history, gen_dt):
    """Return (kept, skipped_tickers) applying the 2h ticker+direction rule.

    A candidate is skipped if history already holds the same ticker and
    action with a signal_time 0-120 min before this batch's signal_time.
    signal_id dedupe (in main) still runs first; this is in addition to it.
    """
    kept, skipped = [], []
    for c in candidates:
        tick = str(c.get("ticker", "")).upper()
        act = str(c.get("signal", ""))
        dup = False
        for r in history:
            if not isinstance(r, dict):
                continue
            if str(r.get("ticker", "")).upper() != tick or str(r.get("action", "")) != act:
                continue
            sig_dt = _parse_dt(r.get("signal_time") or r.get("judged_at"))
            if sig_dt is None:
                continue
            # normalise naive datetimes to the batch timezone
            if sig_dt.tzinfo is None and gen_dt.tzinfo is not None:
                sig_dt = sig_dt.replace(tzinfo=gen_dt.tzinfo)
            delta_min = (gen_dt - sig_dt).total_seconds() / 60.0
            if 0 <= delta_min <= DEDUP_WINDOW_MINUTES:
                dup = True
                break
        if dup:
            skipped.append(tick)
        else:
            kept.append(c)
    return kept, skipped


def _norm_ticker(tick) -> str:
    return str(tick or "").upper().replace("-", "").replace(" ", "")


def load_earnings_calendar(root: Path) -> dict:
    """{normalised ticker: {ISO dates}} from earnings_calendar_nifty.json.

    Fail-open, mirroring the US agent loader: a missing or malformed
    file disables the results-risk flag, with one log line, and the
    desk judges exactly as before. No date is ever invented here.
    """
    path = root / "earnings_calendar_nifty.json"
    out = {}
    if not path.exists():
        print("No earnings_calendar_nifty.json; results-risk flag inactive")
        return out
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        for row in rows:
            d = datetime.fromisoformat(str(row["date"])).date().isoformat()
            out.setdefault(_norm_ticker(row["ticker"]), set()).add(d)
    except Exception as exc:
        print(f"earnings_calendar_nifty.json unreadable ({exc}); results-risk flag OFF")
        return {}
    return out


def results_flag(calendar: dict, ticker, signal_date) -> str:
    """One-line flag, or '' if no results date is near the signal date.

    Window: reports today, tomorrow, yesterday, or 2 days ago (covers
    a Saturday result flagged on Monday). Anything else is not flagged,
    so a stale calendar cannot spam every verdict.
    """
    dates = calendar.get(_norm_ticker(ticker)) or set()
    if not dates or signal_date is None:
        return ""
    for d in sorted(dates):
        try:
            ed = datetime.fromisoformat(d).date()
        except Exception:
            continue
        delta = (ed - signal_date).days
        if delta == 0:
            return f"reports results TODAY ({d})"
        if delta == 1:
            return f"reports results TOMORROW ({d})"
        if delta == -1:
            return f"reported results YESTERDAY ({d})"
        if delta == -2:
            return f"reported results 2 DAYS AGO ({d})"
    return ""


def feasibility_flag(target_atr, minutes_left) -> str:
    """Deterministic time-to-target flag, or ''.

    Conservative by design: it flags only extremes, so the 17-trade
    NIFTY ledger (max 3.89 ATR, min 136 min left) gets zero flags.
    """
    if target_atr is None or minutes_left is None:
        return ""
    try:
        atr = float(target_atr); mins = int(minutes_left)
    except Exception:
        return ""
    if (mins <= 60 and atr >= 5.5) or (mins <= 30 and atr >= 4.5):
        return f"INFEASIBLE: target {atr:.2f} ATR with {mins} min to close"
    if (mins <= 120 and atr >= 5.5) or (mins <= 60 and atr >= 4.5):
        return f"TIGHT: target {atr:.2f} ATR with {mins} min to close"
    return ""


def minutes_to_close_nifty(gen_dt) -> int:
    return (15*60+30) - (gen_dt.hour*60 + gen_dt.minute)

def repo_root() -> Path:
    override = os.getenv("DESK_REPO_ROOT")
    if override:
        return Path(override)
    # .github/scripts/<this file> -> repo root is parents[2]
    return Path(__file__).resolve().parents[2]


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def gemini_judge(candidates: list[dict], nifty_ltp, signal_time_ist: str, calendar=None, signal_date=None, minutes_left=None) -> list[dict]:
    """One Gemini call judging all candidates through the 4-analyst
    framework. Returns one verdict dict per candidate (ticker-matched).
    Raises on transport/API failure — the caller writes nothing then."""
    api_key = os.environ["GEMINI_API_KEY"]
    features = []
    for c in candidates:
        entry, stop, tgt = float(c["ltp"]), float(c["stop"]), float(c["target"])
        atr = float(c.get("indicators", {}).get("atr") or 0) or 0.0
        risk = abs(entry - stop)
        features.append({
            "ticker": c["ticker"], "action": c["signal"],
            "entry": entry, "stop": stop, "target": tgt,
            "qty": c.get("qty", 0),
            "rsi": c.get("indicators", {}).get("rsi"),
            "atr": atr,
            "vol_ratio": c.get("indicators", {}).get("vol_ratio"),
            "stop_distance_atr": round(risk / atr, 2) if atr > 0 else None,
            "target_distance_atr": round(abs(tgt - entry) / atr, 2) if atr > 0 else None,
            "reward_to_risk": round(abs(tgt - entry) / risk, 2) if risk > 0 else None,
            "scout_reason": c.get("scout_reason", ""),
            "results_risk": results_flag(calendar or {}, c["ticker"], signal_date),
            "feasibility": feasibility_flag(round(abs(float(c["target"]) - float(c["ltp"])) / (float(c.get("indicators", {}).get("atr") or 0) or 0.0), 2) if float(c.get("indicators", {}).get("atr") or 0) else None, minutes_left),
            "candles_5m_last12": (c.get("candles_5m") or [])[-12:],
        })
    prompt = (
        "You are the Chief Investment Officer overseeing a 4-analyst desk for a NIFTY "
        "intraday paper-trading system. This is an ADVISORY review: verdicts are logged "
        "and graded at end of day; you do not place trades.\n\n"
        f"Session context: signal time {signal_time_ist} IST (session 09:15-15:30). "
        f"NIFTY spot: {nifty_ltp if nifty_ltp is not None else 'unknown'}. Candles are "
        "5-minute bars [time, open, high, low, close, volume].\n\n"
        "Evaluate EACH candidate through four lenses:\n"
        "1. TECHNICAL: momentum, RSI, ATR buffer, candle structure — clean setup or chasing?\n"
        "2. SENTIMENT: you have NO news feed. Judge sentiment ONLY from the tape "
        "(volume behaviour, follow-through, sector-wide prints visible in the data) and "
        "state plainly that no headline information is available. Never claim a stock "
        "has 'no negative news' — you cannot know that.\n"
        "3. MACRO: does the trade direction align with the index tape and the time of day? "
        "Late-session entries need exceptional structure.\n"
        "4. RISK: reward:risk, stop distance in ATR, whether the target is reachable in "
        "the time remaining.\n\n"
        "FEASIBILITY: each candidate carries a feasibility field computed from "
        "target_distance_atr and minutes left to the 15:30 close, NOT from Gemini. "
        "If it starts INFEASIBLE, you MUST mention it and default to SKIP unless "
        "the structure is exceptional and you say why; if TIGHT, weigh it in risk. "
        "If empty, do not invent a feasibility problem.\n\n"
        "RESULTS RISK: each candidate carries a results_risk field taken ONLY from "
        "earnings_calendar_nifty.json supplied with this prompt. If it is non-empty, "
        "you MUST mention it in risk/summary and weigh pre-/post-results volatility: "
        "a GO on a results-today/tomorrow name needs exceptional structure. If it is "
        "empty, do NOT invent a results date or claim a company is reporting.\n\n"
        "Default to skepticism: a SKIP costs nothing. Return ONLY this JSON: "
        "{\"verdicts\": [{\"ticker\": str, \"verdict\": \"GO\"|\"SKIP\", "
        "\"confidence\": int 0-100, \"trigger\": str, \"analyst_notes\": {\"technical\": str, "
        "\"sentiment\": str, \"macro\": str, \"risk\": str}, \"summary\": str}]} "
        "— one entry per candidate, citing concrete numbers from the payload. "
        "trigger is the price condition that makes the verdict actionable: for GO, "
        "the entry condition (e.g. 'Enter at market' or 'Enter only below 1183.60'); "
        "for SKIP, the condition that would make it valid (e.g. 'Valid only on a "
        "break below 1183.60'), or '' if no meaningful trigger exists in-session.\n\n"
        f"Candidates: {json.dumps(features)}"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    data = json.loads(gemini_post_with_retry(req))
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    parsed = json.loads(text)
    verdicts = parsed.get("verdicts", [])
    by_ticker = {str(v.get("ticker", "")).upper(): v for v in verdicts if isinstance(v, dict)}
    out = []
    for c in candidates:
        v = by_ticker.get(c["ticker"].upper())
        if v is None:
            raise RuntimeError(f"Gemini returned no verdict for {c['ticker']}")
        verdict = str(v.get("verdict", "SKIP")).upper()
        if verdict not in ("GO", "SKIP"):
            verdict = "SKIP"
        try:
            conf = max(0, min(100, int(v.get("confidence", 0))))
        except (TypeError, ValueError):
            conf = 0
        notes = v.get("analyst_notes") if isinstance(v.get("analyst_notes"), dict) else {}
        out.append({
            "ticker": c["ticker"], "verdict": verdict, "confidence": conf,
            "trigger": str(v.get("trigger", "")).strip(),
            "analyst_notes": {
                "technical": str(notes.get("technical", "")),
                "sentiment": str(notes.get("sentiment", "")),
                "macro": str(notes.get("macro", "")),
                "risk": str(notes.get("risk", "")),
            },
            "summary": str(v.get("summary", "")),
        })
    return out


def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("Telegram not configured; message was:\n" + text)
        return
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat, "text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status != 200:
                print(f"Telegram send failed: HTTP {resp.status}", file=sys.stderr)
    except Exception as exc:  # fail-soft: never fail the run over an alert
        print(f"Telegram send failed: {exc}", file=sys.stderr)


def main() -> int:
    root = repo_root()
    latest_path = root / "signals" / "latest.json"
    verdict_path = root / "desk_verdict.json"
    history_path = root / "desk_history.json"

    latest = load_json(latest_path, None)
    if not latest or not latest.get("candidates"):
        print("No candidates in signals/latest.json; nothing to judge.")
        return 0

    signal_id = str(latest.get("signal_id", ""))
    try:
        gen = datetime.fromisoformat(str(latest["generated_at"]))
    except Exception:
        print("latest.json has no parseable generated_at; skipping.")
        return 0
    gen_ist = gen.astimezone(IST)
    now_ist = datetime.now(IST)
    age_min = round((now_ist - gen_ist).total_seconds() / 60.0, 1)

    history = load_json(history_path, [])
    if not isinstance(history, list):
        history = []
    if any(isinstance(r, dict) and r.get("signal_id") == signal_id for r in history):
        print(f"Signal {signal_id} already judged; skipping.")
        return 0

    kept, skipped = filter_deduped(latest["candidates"], history, gen_ist)
    if skipped:
        print(f"Deduped (same ticker+direction within 2h, skipping): {', '.join(skipped)}")
    if not kept:
        print("All candidates deduped; skipping (no Gemini call, no commit).")
        return 0

    calendar = load_earnings_calendar(root)
    if not os.getenv("GEMINI_API_KEY"):
        print("GEMINI_API_KEY not set; cannot judge.", file=sys.stderr)
        return 0

    try:
        verdicts = gemini_judge(kept, latest.get("nifty_ltp"),
                                gen_ist.strftime("%H:%M"), calendar=calendar,
                                signal_date=gen_ist.date(), minutes_left=minutes_to_close_nifty(gen_ist))
    except Exception as exc:
        # Honest failure: no fabricated verdict, no history pollution.
        print(f"Gemini judging failed: {exc}", file=sys.stderr)
        return 0

    judged_at = now_ist.isoformat(timespec="seconds")
    cand_by_ticker = {c["ticker"]: c for c in latest["candidates"]}
    records = []
    for v in verdicts:
        c = cand_by_ticker[v["ticker"]]
        records.append({
            "signal_id": signal_id,
            "signal_time": gen_ist.isoformat(timespec="seconds"),
            "judged_at": judged_at,
            "age_min": age_min,
            "ticker": v["ticker"], "action": c["signal"],
            "entry": float(c["ltp"]), "stop": float(c["stop"]), "target": float(c["target"]),
            "qty": c.get("qty", 0),
            "verdict": v["verdict"], "confidence": v["confidence"],
            "trigger": v.get("trigger", ""),
            "results_risk": results_flag(calendar, v["ticker"], gen_ist.date()),
            "feasibility": feasibility_flag(round(abs(float(c["target"]) - float(c["ltp"])) / (float(c.get("indicators", {}).get("atr") or 0) or 0.0), 2) if float(c.get("indicators", {}).get("atr") or 0) else None, minutes_to_close_nifty(gen_ist)),
            "analyst_notes": v["analyst_notes"],
            "rationale": v["summary"],
            "outcome": None, "outcome_r": None, "outcome_at": None,
        })
    history.extend(records)
    save_json(history_path, history)
    save_json(verdict_path, {
        "signal_id": signal_id, "judged_at": judged_at,
        "age_min": age_min, "advisory_only": True, "verdicts": records,
    })

    lines = [f"ASSISTANT DESK (advisory) — {gen_ist:%H:%M} IST · NIFTY {latest.get('nifty_ltp', 'n/a')}"]
    for r in records:
        icon = "GO ✅" if r["verdict"] == "GO" else "SKIP ⛔"
        lines.append(f"{r['ticker']} {r['action']} @ {r['entry']:.2f} → {icon} ({r['confidence']})")
        lines.append(f"  SL {r['stop']:.2f} / Target {r['target']:.2f}")
        if r.get("trigger"):
            lines.append(f"  Trigger: {r['trigger'][:160]}")
        if r.get("results_risk"):
            lines.append(f"  Results risk: {r['results_risk']}")
        if r.get("feasibility"):
            lines.append(f"  Feasibility: {r['feasibility']}")
        if r["rationale"]:
            lines.append(f"  {r['rationale'][:160]}")
        risk = (r["analyst_notes"] or {}).get("risk", "")
        if risk:
            lines.append(f"  Key risk: {risk[:160]}")
    lines.append("Advisory only — the agent does not see this verdict.")
    send_telegram("\n".join(lines))
    print(f"Judged {len(records)} candidate(s) from {signal_id} (age {age_min} min).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
