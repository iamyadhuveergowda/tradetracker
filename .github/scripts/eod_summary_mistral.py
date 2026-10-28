#!/usr/bin/env python3
"""
Mistral EOD scoring (NIFTY) — SCORING ONLY, no Telegram, no LLM.

Scores every unscored verdict in desk_history_mistral.json against what
price did AFTER the signal time, using the EXACT same deterministic
first-hit logic as the Gemini desk's eod_summary.py (yfinance 5-minute
bars sliced from the signal timestamp to the 15:30 close; a bar touching
both target and stop is AMBIGUOUS, never an automatic loss; records older
than 10 days without bars close as NO_DATA). The scoring functions below
are copied verbatim from eod_summary.py so both desks are graded by
identical rules — only the input file and the delivery differ.

Deliberately NOT included: the agent book (the Gemini EOD already
reports it), LLM narration, and any Telegram message. This job's only
output is the scored history file, which the daily desk comparison reads.
A second EOD Telegram message would be pure spam.

Usage: python3 eod_summary_mistral.py [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
SCORE_WINDOW_DAYS = 10  # older unscored records are closed as NO_DATA


def repo_root() -> Path:
    override = os.getenv("DESK_REPO_ROOT")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2]


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def fetch_day_rows(ticker: str, day) -> list[dict]:
    """5-minute bars for one IST date via yfinance, as plain rows
    [{'t': aware datetime IST, 'h','l','c'}]. [] on any failure."""
    try:
        import yfinance as yf
    except Exception as exc:
        print(f"yfinance unavailable: {exc}", file=sys.stderr)
        return []
    try:
        data = yf.download(f"{ticker}.NS", period="5d", interval="5m",
                           progress=False, auto_adjust=False)
        if data is None or len(data) == 0:
            return []
        if hasattr(data.columns, "nlevels") and data.columns.nlevels > 1:
            data.columns = data.columns.get_level_values(0)
        rows = []
        for ts, row in data.iterrows():
            t = ts.to_pydatetime()
            if t.tzinfo is None:
                t = t.replace(tzinfo=IST)
            t = t.astimezone(IST)
            if t.date() != day:
                continue
            rows.append({"t": t, "h": float(row["High"]),
                         "l": float(row["Low"]), "c": float(row["Close"])})
        rows.sort(key=lambda r: r["t"])
        return rows
    except Exception as exc:
        print(f"Bars fetch failed for {ticker}: {exc}", file=sys.stderr)
        return []


def slice_from_signal(rows: list[dict], signal_dt: datetime) -> list[dict]:
    """Only bars that START at or after the signal time — price action
    from before the signal existed must not grade the verdict."""
    return [r for r in rows if r["t"] >= signal_dt]


def score_record(rec: dict, rows: list[dict]) -> tuple[str, float | None]:
    """First-hit scoring, signal time -> 15:30 close. Theoretical R."""
    entry, stop, tgt = float(rec["entry"]), float(rec["stop"]), float(rec["target"])
    risk = abs(entry - stop)
    if risk <= 0 or not rows:
        return "PENDING", None
    buy = rec["action"] == "BUY"
    reward_r = abs(tgt - entry) / risk
    for b in rows:
        hit_t = (b["h"] >= tgt) if buy else (b["l"] <= tgt)
        hit_s = (b["l"] <= stop) if buy else (b["h"] >= stop)
        if hit_t and hit_s:
            return "AMBIGUOUS", None
        if hit_t:
            return "TARGET", round(reward_r, 2)
        if hit_s:
            return "STOP", -1.0
    last_close = float(rows[-1]["c"])
    r = (last_close - entry) / risk if buy else (entry - last_close) / risk
    return "CLOSE", round(r, 2)


def desk_stats(records: list[dict]) -> dict:
    scored = [r for r in records if r.get("outcome") in ("TARGET", "STOP", "CLOSE")]
    amb = sum(1 for r in records if r.get("outcome") == "AMBIGUOUS")
    out = {"judged": len(records), "scored": len(scored), "ambiguous": amb}
    for verdict in ("GO", "SKIP"):
        s = [r for r in scored if r.get("verdict") == verdict]
        if not s:
            out[verdict] = None
            continue
        rs = [float(r["outcome_r"]) for r in s if r.get("outcome_r") is not None]
        if verdict == "GO":
            good = sum(1 for r in rs if r > 0)
            out[verdict] = {"n": len(s), "positive_r_pct": round(100.0 * good / len(s), 1),
                            "mean_r_if_taken": round(statistics.mean(rs), 2) if rs else None}
        else:
            avoided = sum(1 for r in s if r.get("outcome") == "STOP"
                          or (r.get("outcome_r") is not None and float(r["outcome_r"]) < 0))
            out[verdict] = {"n": len(s), "would_have_lost_pct": round(100.0 * avoided / len(s), 1),
                            "mean_r_if_taken": round(statistics.mean(rs), 2) if rs else None}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="IST date to score (default: today IST)")
    args = ap.parse_args()

    root = repo_root()
    history_path = root / "desk_history_mistral.json"
    history = load_json(history_path, [])
    if not isinstance(history, list):
        history = []

    now_ist = datetime.now(IST)
    report_date = args.date or now_ist.date().isoformat()

    changed = False
    bars_cache: dict[tuple[str, object], list[dict]] = {}
    for rec in history:
        if rec.get("outcome") is not None:
            continue
        try:
            sig = datetime.fromisoformat(str(rec["signal_time"]))
        except Exception:
            continue
        sig_ist = sig.astimezone(IST)
        sig_date = sig_ist.date()
        if sig_date.isoformat() > report_date:
            continue
        if sig_date.isoformat() == report_date and now_ist.time() < dtime(15, 35):
            continue  # today's session may still be running
        age_days = (now_ist.date() - sig_date).days
        key = (rec["ticker"], sig_date)
        if key not in bars_cache:
            bars_cache[key] = fetch_day_rows(rec["ticker"], sig_date)
        rows = slice_from_signal(bars_cache[key], sig_ist)
        outcome, outcome_r = score_record(rec, rows)
        if outcome == "PENDING" and age_days > SCORE_WINDOW_DAYS:
            outcome, outcome_r = "NO_DATA", None
        if outcome != "PENDING":
            rec["outcome"], rec["outcome_r"] = outcome, outcome_r
            rec["outcome_at"] = now_ist.isoformat(timespec="seconds")
            changed = True
    if changed:
        save_json(history_path, history)

    today = [r for r in history if str(r.get("signal_time", ""))[:10] == report_date]
    for label, recs in (("Mistral desk today", today), ("Mistral desk cumulative", history)):
        d = desk_stats(recs)
        print(f"{label}: judged {d['judged']}, scored {d['scored']}, ambiguous {d['ambiguous']}")
        if d.get("GO"):
            print(f"  GO: {d['GO']['n']} verdicts, {d['GO']['positive_r_pct']}% ended positive-R, "
                  f"avg {d['GO']['mean_r_if_taken']}R if taken")
        if d.get("SKIP"):
            print(f"  SKIP: {d['SKIP']['n']} verdicts, {d['SKIP']['would_have_lost_pct']}% would have lost, "
                  f"avg {d['SKIP']['mean_r_if_taken']}R if taken")
    print(f"Scored history written: {changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
