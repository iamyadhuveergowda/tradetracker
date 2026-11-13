#!/usr/bin/env python3
"""
build_dashboard.py — regenerates a richer index.html for the NIFTY Intraday Agent.

NIFTY mirror of the US dashboard generator (same structure, same rules):
reads ONLY files that already exist in this repo (plus the current index.html,
which it parses for the broker-side numbers it cannot recompute), and writes
index.html. This script makes no network requests and modifies nothing else.

MTM equity source (exact): the agent's mark_to_market_equity() is
starting_capital + realized_pnl + unrealized on open positions, with
starting capital Rs.1,00,000. With no open positions that equals
Rs.1,00,000 + ledger net, which is what this generator falls back to when
the old page has no MTM card. The live page's MTM card is parsed from the
old index.html first (same parse-then-fallback logic as the US generator),
so the number the agent last wrote is preserved, never recomputed.

Index cards (NIFTY 50, NIFTY BANK, INDIA VIX, NIFTY IT, NIFTY AUTO):
there is NO market_nifty.json in this repo — the US equivalent
(market_us.json) is written by a scheduled fetch script, and no such
fetch can run for NIFTY in GitHub Actions (the 5paisa feed needs the
server's broker session). So the strip reads market_nifty.json IF the
agent publishes one (see AGENT_CHANGE_NOTE.md — the follow-up agent
patch should write it from macro_context() on every cycle), and otherwise
carries the five cards forward from the old index.html. Carried values go
stale when the agent stops rendering the page, which is why publishing
market_nifty.json is part of the agent-side change, not an optional extra.
INDIA VIX is coloured inversely (a fall in fear is good news), like ^VIX
on the US page. Note the v1 page coloured a VIX fall red; v2 colours it
green — that is deliberate, not a bug.

Usage:
    python3 build_dashboard.py                 # read from repo root, write index.html
    python3 build_dashboard.py --out out.html  # write somewhere else
    python3 build_dashboard.py --repo /path    # different checkout
"""

from __future__ import annotations

import argparse
import ast
import html as _html
import json
import re
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")
_STAMP_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})(?::\d{2})?\s*(ET|UTC|IST)?", re.I
)

# Current live agent version, shown in the page header. Bump this when the
# agent is upgraded. The old page's version is never carried over (see
# render_html), so a stale header can never pin an old version again.
# (The v1 page still says v13.7 in its title while the live agent is v13.9.)
_AGENT_VERSION = "v13.11"

# Starting capital of the paper account (Rs.1,00,000). The MTM card falls
# back to START_CAPITAL + ledger net when the old page has no MTM card.
_START_CAPITAL = 100000.0

# The five index cards the v1 page renders, in display order. The agent's
# macro_context() returns many more symbols; only these five are shown.
_MACRO_ORDER = ["NIFTY 50", "NIFTY BANK", "INDIA VIX", "NIFTY IT", "NIFTY AUTO"]
# INDIA VIX is a fear gauge, so a fall is good news: colour it inversely.
_MACRO_INVERSE = {"INDIA VIX"}


def _to_ist_label(raw: str) -> str:
    """Normalize an 'Updated' stamp to IST for the page header.

    Old pages carry ET or UTC stamps. Parse the stamp in its own zone and
    render it in IST; anything unparseable passes through unchanged so a
    bad stamp can never break the page.
    """
    if not raw:
        return raw
    m = _STAMP_RE.search(raw)
    if not m:
        return raw
    date_part, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
    tz = (m.group(4) or "IST").upper()
    try:
        naive = datetime.strptime(f"{date_part} {hh:02d}:{mm:02d}", "%Y-%m-%d %H:%M")
    except ValueError:
        return raw
    if tz == "ET":
        aware = naive.replace(tzinfo=ZoneInfo("America/New_York"))
    elif tz == "UTC":
        aware = naive.replace(tzinfo=timezone.utc)
    else:  # already IST (or no zone given — treat as IST, the display zone)
        aware = naive.replace(tzinfo=_IST)
    return aware.astimezone(_IST).strftime("%Y-%m-%d %H:%M IST")


# ---------------------------------------------------------------------------
# load helpers
# ---------------------------------------------------------------------------


def _load(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # never let a half-written file kill the page
        print(f"  ! could not parse {path.name}: {exc}")
        return default


def _load_jsonl(path: Path):
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def esc(value) -> str:
    return _html.escape("" if value is None else str(value), quote=True)


def rupees(value, digits=2) -> str:
    if value is None:
        return "—"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}₹{abs(value):,.{digits}f}"


def signed_rupees(value, digits=2) -> str:
    if value is None:
        return "—"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "—"
    sign = "-" if value < 0 else "+"
    return f"{sign}₹{abs(value):,.{digits}f}"


def pct(value, digits=1) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}%"


# ---------------------------------------------------------------------------
# broker-side numbers come from the existing index.html
# ---------------------------------------------------------------------------

_KPI_RE = re.compile(
    r'<div class="lbl">(?P<label>[^<]+)</div>\s*<div class="big[^"]*">(?P<value>.*?)</div>',
    re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")
_NUM_RE = re.compile(r"-?₹?-?[\d,]+(?:\.\d+)?")
# v1 macro card:
# <div class="card"><div class="lbl">NIFTY 50</div><div class="big">25,144.30</div>
# <div class="neg small">BEARISH -0.61%</div></div>
_MACRO_RE = re.compile(
    r'<div class="lbl">(?P<name>NIFTY 50|NIFTY BANK|INDIA VIX|NIFTY IT|NIFTY AUTO)</div>'
    r'\s*<div class="big">(?P<price>[\d,\.]+)</div>'
    r'\s*<div class="[^"]* small">(?P<label>[A-Z]+)\s*(?P<pct>[+-]?[\d\.]+)%</div>',
    re.S,
)


def parse_existing_index(path: Path) -> dict:
    """Pull the equity/available/realized/today numbers and the five index
    cards out of the current page.

    These are the only values that live in the broker account / live feed
    rather than in a repo file, so we reuse whatever the agent last wrote
    instead of inventing them. Falls back to computing equity from the
    ledger when absent.
    """
    out = {
        "equity": None,
        "available": None,
        "realized": None,
        "today_pnl": None,
        "updated": "",
        "version": "",
        "mode": "",
        "macro": {},
    }
    if not path.exists():
        return out
    text = path.read_text(encoding="utf-8")

    m = re.search(r'<div class="updated">(.*?)</div>', text, re.S)
    if m:
        raw = _html.unescape(_TAG_RE.sub("", m.group(1))).strip()
        raw = raw.split("·")[0]
        raw = re.sub(r"^Updated\s+", "", raw, flags=re.I)
        out["updated"] = raw.strip(" ·|")

    m = re.search(r'<span class="ver">([^<]+)</span>', text)
    if m:
        out["version"] = m.group(1).strip()
    m = re.search(r'<span class="modebadge">([^<]+)</span>', text) or re.search(
        r'<span class="badge (?:paper|live)">([^<]+)</span>', text)
    if m:
        out["mode"] = m.group(1).strip()

    wanted = {
        "MTM Equity": "equity",
        "Avail Margin": "available",
        "Realized P&L": "realized",
        "Today P&L": "today_pnl",
    }
    for match in _KPI_RE.finditer(text):
        label = match.group("label").replace("&amp;", "&").strip()
        key = wanted.get(label)
        if not key:
            continue
        raw = _TAG_RE.sub("", match.group("value"))
        num = _NUM_RE.search(raw.replace("₹", "").replace(",", ""))
        if num:
            out[key] = float(num.group(0))

    for match in _MACRO_RE.finditer(text):
        try:
            out["macro"][match.group("name")] = {
                "price": float(match.group("price").replace(",", "")),
                "label": match.group("label"),
                "percent": float(match.group("pct")),
            }
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# analytics
# ---------------------------------------------------------------------------


def trade_stats(trades):
    closed = [t for t in trades if t.get("net_pnl") is not None]
    wins = [t for t in closed if t["net_pnl"] > 0]
    losses = [t for t in closed if t["net_pnl"] <= 0]
    gross_win = sum(t["net_pnl"] for t in wins)
    gross_loss = -sum(t["net_pnl"] for t in losses)
    net = sum(t["net_pnl"] for t in closed)

    # R multiple: realised P&L over the risk that was actually at stake.
    for t in closed:
        try:
            risk_per_share = abs(t["entry"] - t.get("initial_sl", t.get("sl")))
            qty = t.get("qty") or 0
            t["_r"] = (t["net_pnl"] / (risk_per_share * qty)) if risk_per_share and qty else None
        except Exception:
            t["_r"] = None

    rs = [t["_r"] for t in closed if t["_r"] is not None]
    holds = []
    for t in closed:
        try:
            fmt = "%Y-%m-%dT%H:%M:%S%z"
            entry_ts = datetime.strptime(t["date"] + "T" + t["time"], "%Y-%m-%dT%H:%M:%S")
            exit_ts = datetime.strptime(t["exit_time"], fmt).replace(tzinfo=None)
            holds.append((exit_ts - entry_ts).total_seconds() / 60.0)
        except Exception:
            continue

    by_date = OrderedDict()
    for t in sorted(closed, key=lambda x: (x["date"], x["time"])):
        by_date[t["date"]] = by_date.get(t["date"], 0.0) + (t["net_pnl"] or 0)

    # NIFTY trades never span days (15:10 square-off), so dating P&L on
    # the trade date is exact; no pnl_by_date split is needed here.
    equity, curve = _START_CAPITAL, [("start", _START_CAPITAL)]
    for date, pnl in by_date.items():
        equity += pnl
        curve.append((date, equity))

    by_ticker = {}
    for t in closed:
        row = by_ticker.setdefault(t["ticker"], {"n": 0, "wins": 0, "pnl": 0.0})
        row["n"] += 1
        row["wins"] += 1 if t["net_pnl"] > 0 else 0
        row["pnl"] += t["net_pnl"]

    return {
        "trades": closed,
        "n": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (100.0 * len(wins) / len(closed)) if closed else None,
        "net": net,
        "gross_win": gross_win,
        "gross_loss": gross_loss,
        "profit_factor": (gross_win / gross_loss) if gross_loss else None,
        "avg_win": (gross_win / len(wins)) if wins else None,
        "avg_loss": (-gross_loss / len(losses)) if losses else None,
        "expectancy": (net / len(closed)) if closed else None,
        "best": max(closed, key=lambda t: t["net_pnl"]) if closed else None,
        "worst": min(closed, key=lambda t: t["net_pnl"]) if closed else None,
        "avg_r": mean(rs) if rs else None,
        "max_drawdown": _max_drawdown([p for _, p in curve]),
        "curve": curve,
        "by_date": by_date,
        "by_ticker": by_ticker,
        "avg_hold_min": mean(holds) if holds else None,
        "charges": sum((t.get("charges") or {}).get("total", 0) for t in closed),
        "long": len([t for t in closed if t["action"] == "BUY"]),
        "short": len([t for t in closed if t["action"] == "SELL"]),
    }


def _max_drawdown(series):
    peak, worst = None, 0.0
    for value in series:
        peak = value if peak is None else max(peak, value)
        worst = min(worst, value - peak)
    return worst


def desk_stats(history):
    rows = [h for h in history if h.get("verdict")]
    verdicts = Counter(h["verdict"] for h in rows)
    scored = [h for h in rows if h.get("outcome")]
    outcomes = Counter(h["outcome"] for h in scored)
    go = [h for h in rows if h["verdict"] == "GO"]
    go_scored = [h for h in go if h.get("outcome_r") is not None]
    rs = [h["outcome_r"] for h in go_scored]

    # would-be edge: what the skipped signals did, as R multiples
    skip = [h for h in rows if h["verdict"] == "SKIP" and h.get("outcome_r") is not None]
    skip_rs = [h["outcome_r"] for h in skip]

    buckets = OrderedDict()
    for h in rows:
        conf = h.get("confidence")
        if conf is None:
            continue
        band = f"{int(conf // 10) * 10}-{int(conf // 10) * 10 + 9}"
        bucket = buckets.setdefault(band, {"n": 0, "go": 0, "target": 0, "stop": 0, "close": 0, "rs": []})
        bucket["n"] += 1
        if h["verdict"] == "GO":
            bucket["go"] += 1
        if h.get("outcome") == "TARGET":
            bucket["target"] += 1
        if h.get("outcome") == "STOP":
            bucket["stop"] += 1
        if h.get("outcome") == "CLOSE":
            bucket["close"] += 1
        if h.get("outcome_r") is not None:
            bucket["rs"].append(h["outcome_r"])

    by_day = OrderedDict()
    for h in sorted(rows, key=lambda x: (x.get("signal_time") or "")):
        day = (h.get("signal_time") or "")[:10]
        if not day:
            continue
        d = by_day.setdefault(day, {"n": 0, "go": 0, "target": 0, "stop": 0, "close": 0})
        d["n"] += 1
        if h["verdict"] == "GO":
            d["go"] += 1
        d[{"TARGET": "target", "STOP": "stop", "CLOSE": "close"}.get(h.get("outcome") or "", "n")] += 1

    latencies = []
    for h in rows:
        try:
            fmt = "%Y-%m-%dT%H:%M:%S%z"
            sig = datetime.strptime(h["signal_time"], fmt)
            jud = datetime.strptime(h["judged_at"], fmt)
            latencies.append((jud - sig).total_seconds())
        except Exception:
            continue

    return {
        "rows": rows,
        "n": len(rows),
        "verdicts": verdicts,
        "outcomes": outcomes,
        "go": go,
        "go_scored": go_scored,
        "go_n": len(go),
        "go_targets": len([h for h in go_scored if h["outcome"] == "TARGET"]),
        "go_stops": len([h for h in go_scored if h["outcome"] == "STOP"]),
        "go_closes": len([h for h in go_scored if h["outcome"] == "CLOSE"]),
        "go_sum_r": sum(rs) if rs else None,
        "go_avg_r": mean(rs) if rs else None,
        "skip_sum_r": sum(skip_rs) if skip_rs else None,
        "skip_avg_r": mean(skip_rs) if skip_rs else None,
        "skip_n": len(skip_rs),
        "buckets": buckets,
        "by_day": by_day,
        "avg_latency_s": mean(latencies) if latencies else None,
        "first": min((h["signal_time"] for h in rows if h.get("signal_time")), default=None),
        "last": max((h["signal_time"] for h in rows if h.get("signal_time")), default=None),
    }


def scout_stats(rows):
    """Four-scout shadow panel. NIFTY rows carry picks/errors for Qwen,
    DeepSeek Flash, DeepSeek Pro and Mistral. There is NO qwen_error field
    in the file — Qwen errors are uncheckable from here (the live scout's
    failures surface in the agent log, not this file)."""
    if not rows:
        return None
    scans = len(rows)
    overlap = sum(len(r.get("overlap") or []) for r in rows)
    qwen_only = sum(len(r.get("qwen_only") or []) for r in rows)
    deep_only = sum(len(r.get("deepseek_only") or []) for r in rows)
    qwen_picks = sum(len(r.get("qwen_picks") or []) for r in rows)
    deep_picks = sum(len(r.get("deepseek_picks") or []) for r in rows)
    pro_picks = sum(len(r.get("deepseek_pro_picks") or []) for r in rows)
    mistral_picks = sum(len(r.get("mistral_picks") or []) for r in rows)
    err_flash = sum(1 for r in rows if r.get("deepseek_error"))
    err_pro = sum(1 for r in rows if r.get("deepseek_pro_error"))
    err_mistral = sum(1 for r in rows if r.get("mistral_error"))
    lats = [r["latency_ms"] for r in rows if isinstance(r.get("latency_ms"), (int, float))]
    return {
        "scans": scans,
        "overlap": overlap,
        "qwen_only": qwen_only,
        "deep_only": deep_only,
        "qwen_picks": qwen_picks,
        "deep_picks": deep_picks,
        "pro_picks": pro_picks,
        "mistral_picks": mistral_picks,
        "err_flash": err_flash,
        "err_pro": err_pro,
        "err_mistral": err_mistral,
        "avg_latency_ms": mean(lats) if lats else None,
        "agreement": (100.0 * overlap / (overlap + qwen_only + deep_only))
        if (overlap + qwen_only + deep_only)
        else None,
    }


def parse_indicators(raw):
    """NIFTY latest.json stores indicators as a string repr of a dict
    ("{'rsi': 49.26, 'atr': 5.1715, ...}"), not a dict. Parse it; fall
    back to the raw string so a bad value never breaks the page."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = ast.literal_eval(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {"_raw": raw}
    return {}


# ---------------------------------------------------------------------------
# svg charts (hand-rolled so the page has zero external dependencies)
# ---------------------------------------------------------------------------


def svg_line(points, width=760, height=170, pad=26, color="#8b93ff", fill="rgba(139,147,255,0.14)", labels=None):
    """points: list of (label, value). Returns an <svg> string."""
    values = [p[1] for p in points]
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        hi += abs(hi) * 0.05 or 1.0
        lo -= abs(lo) * 0.05 or 1.0
    span = hi - lo
    lo -= span * 0.12
    hi += span * 0.12
    span = hi - lo

    def x(i):
        return pad + (width - 2 * pad) * (i / max(1, len(points) - 1))

    def y(v):
        return pad + (height - 2 * pad) * (1 - (v - lo) / span)

    coords = [(x(i), y(v)) for i, (_, v) in enumerate(points)]
    line = " ".join(f"{px:.1f},{py:.1f}" for px, py in coords)
    area = f"{pad},{height - pad} " + line + f" {width - pad},{height - pad}"

    ticks = []
    for frac in (0, 0.5, 1):
        val = lo + span * frac
        gy = y(val)
        ticks.append(
            f'<line x1="{pad}" y1="{gy:.1f}" x2="{width - pad}" y2="{gy:.1f}" stroke="#2a2a3d" stroke-width="1"/>'
            f'<text x="4" y="{gy + 4:.1f}" class="ax">{rupees(val, 0)}</text>'
        )
    # baseline at the starting value, if it is inside the range
    base = points[0][1]
    if lo <= base <= hi:
        gy = y(base)
        ticks.append(
            f'<line x1="{pad}" y1="{gy:.1f}" x2="{width - pad}" y2="{gy:.1f}" '
            f'stroke="#5a5f7a" stroke-width="1" stroke-dasharray="4 4"/>'
        )

    dots = []
    for (px, py), (lbl, val) in zip(coords, points):
        cls = "pos" if val >= base else "neg"
        dots.append(
            f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3" class="{cls}">'
            f'<title>{esc(lbl)}: {rupees(val)}</title></circle>'
        )
    xlabels = []
    if labels:
        step = max(1, len(points) // 7)
        labels = labels[:len(points)] + [""] * max(0, len(points) - len(labels))
        for i in range(0, len(points), step):
            xlabels.append(f'<text x="{x(i):.1f}" y="{height - 6}" class="ax" text-anchor="middle">{esc(labels[i])}</text>')

    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class="chart">'
        f'<polygon points="{area}" fill="{fill}"/>'
        f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="2"/>'
        + "".join(ticks) + "".join(dots) + "".join(xlabels) + "</svg>"
    )


def svg_bars(items, width=760, height=170, pad=26):
    """items: list of (label, value). Positive bars green, negative red."""
    if not items:
        return ""
    values = [v for _, v in items]
    lo, hi = min(values + [0]), max(values + [0])
    if hi == lo:
        hi += 1
    span = (hi - lo) * 1.15 or 1
    lo -= span * 0.07
    hi += span * 0.07
    span = hi - lo
    zero = pad + (height - 2 * pad) * (1 - (0 - lo) / span)
    bw = max(3.0, (width - 2 * pad) / len(items) - 4)

    rects = []
    for i, (lbl, val) in enumerate(items):
        cx = pad + (width - 2 * pad) * ((i + 0.5) / len(items))
        vy = pad + (height - 2 * pad) * (1 - (val - lo) / span)
        top, h = (vy, zero - vy) if val >= 0 else (zero, vy - zero)
        cls = "barpos" if val >= 0 else "barneg"
        rects.append(
            f'<rect x="{cx - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{max(1, h):.1f}" class="{cls}">'
            f'<title>{esc(lbl)}: {signed_rupees(val)}</title></rect>'
        )
    step = max(1, len(items) // 8)
    xlabels = [
        f'<text x="{pad + (width - 2 * pad) * ((i + 0.5) / len(items)):.1f}" y="{height - 6}" class="ax" text-anchor="middle">{esc(items[i][0])}</text>'
        for i in range(0, len(items), step)
    ]
    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class="chart">'
        f'<line x1="{pad}" y1="{zero:.1f}" x2="{width - pad}" y2="{zero:.1f}" stroke="#5a5f7a" stroke-dasharray="4 4"/>'
        + "".join(rects) + "".join(xlabels) + "</svg>"
    )


def svg_candles(candles, entry=None, stop=None, target=None, width=760, height=190, pad=28):
    """candles: list of [time, open, high, low, close, volume]."""
    if not candles:
        return ""
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    refs = [v for v in (entry, stop, target) if v is not None]
    lo, hi = min(lows + refs), max(highs + refs)
    if hi == lo:
        hi += 1
    span = (hi - lo) * 1.1 or 1
    lo -= span * 0.05
    hi += span * 0.05
    span = hi - lo
    plot_w = width - 2 * pad
    cw = max(2.0, plot_w / len(candles) * 0.62)

    def y(v):
        return pad + (height - 2 * pad) * (1 - (v - lo) / span)

    out = []
    for i, c in enumerate(candles):
        cx = pad + plot_w * ((i + 0.5) / len(candles))
        _, o, h, l, cl, _v = (list(c) + [0] * 6)[:6]
        up = cl >= o
        cls = "cup" if up else "cdn"
        out.append(f'<line x1="{cx:.1f}" y1="{y(h):.1f}" x2="{cx:.1f}" y2="{y(l):.1f}" class="{cls}" stroke-width="1"/>')
        top = y(max(o, cl))
        hgt = max(1.0, abs(y(o) - y(cl)))
        out.append(f'<rect x="{cx - cw / 2:.1f}" y="{top:.1f}" width="{cw:.1f}" height="{hgt:.1f}" class="{cls}"/>')

    for val, label, col in ((entry, "ENTRY", "#e8e8f0"), (stop, "SL", "#ff6b6b"), (target, "TGT", "#34d17b")):
        if val is None or not (lo <= val <= hi):
            continue
        gy = y(val)
        out.append(
            f'<line x1="{pad}" y1="{gy:.1f}" x2="{width - pad}" y2="{gy:.1f}" stroke="{col}" stroke-width="1" stroke-dasharray="5 4" opacity="0.85"/>'
            f'<text x="{width - pad - 2}" y="{gy - 4:.1f}" class="ax" text-anchor="end" fill="{col}">{label} {val}</text>'
        )
    step = max(1, len(candles) // 8)
    for i in range(0, len(candles), step):
        cx = pad + plot_w * ((i + 0.5) / len(candles))
        out.append(f'<text x="{cx:.1f}" y="{height - 6}" class="ax" text-anchor="middle">{esc(candles[i][0])}</text>')
    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class="chart">' + "".join(out) + "</svg>"
    )


def svg_columns(items, width=760, height=110, pad=8):
    """items: list of (label, value). Compact horizontal strip: one column
    per item side by side, value above the column, label below. Positive
    bars green, negative red."""
    if not items:
        return ""
    values = [v for _, v in items]
    lo, hi = min(values + [0]), max(values + [0])
    if hi == lo:
        hi += 1
    span = (hi - lo) * 1.15 or 1
    lo -= span * 0.07
    hi += span * 0.07
    span = hi - lo
    top_pad = 16   # room for value labels
    bot_pad = 16   # room for ticker labels
    plot_h = height - top_pad - bot_pad
    zero = top_pad + plot_h * (1 - (0 - lo) / span)
    bw = max(3.0, (width - 2 * pad) / len(items) - 4)
    out = []
    for i, (lbl, val) in enumerate(items):
        cx = pad + (width - 2 * pad) * ((i + 0.5) / len(items))
        vy = top_pad + plot_h * (1 - (val - lo) / span)
        top, h = (vy, zero - vy) if val >= 0 else (zero, vy - zero)
        cls = "barpos" if val >= 0 else "barneg"
        out.append(
            f'<rect x="{cx - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{max(1, h):.1f}" class="{cls}">'
            f'<title>{esc(lbl)}: {signed_rupees(val)}</title></rect>'
        )
        out.append(f'<text x="{cx:.1f}" y="{top - 4:.1f}" class="ax" text-anchor="middle">{signed_rupees(val, 0)}</text>')
        out.append(f'<text x="{cx:.1f}" y="{height - 4}" class="ax" text-anchor="middle">{esc(lbl)}</text>')
    out.append(f'<line x1="{pad}" y1="{zero:.1f}" x2="{width - pad}" y2="{zero:.1f}" stroke="#5a5f7a" stroke-dasharray="4 4"/>')
    return f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class="chart">' + "".join(out) + "</svg>"


# ---------------------------------------------------------------------------
# index strip
# ---------------------------------------------------------------------------


def macro_strip(repo: Path) -> str:
    """The five index cards. Preferred source is market_nifty.json, written
    by the agent from macro_context() (see AGENT_CHANGE_NOTE.md). Until the
    agent publishes it, the cards are carried forward from the old
    index.html — with the carry-forward labelled, so a stale strip can
    never be mistaken for a live one."""
    mk = _load(repo / "market_nifty.json", None)
    cards = []
    source = ""
    if isinstance(mk, dict) and mk.get("indices"):
        for item in mk["indices"]:
            name = item.get("name", "")
            if name not in _MACRO_ORDER:
                continue
            try:
                price = float(item["price"])
            except (TypeError, ValueError):
                continue
            pct_v = item.get("percent")
            label = item.get("label", "")
            cards.append({"name": name, "price": price, "label": label, "percent": pct_v})
        if cards:
            fetched = mk.get("fetched_at_ist", "")
            source = f"live feed · {esc(fetched)}" if fetched else "live feed"
    if not cards:
        parsed = parse_existing_index(repo / "index.html")
        for name in _MACRO_ORDER:
            m = parsed["macro"].get(name)
            if m:
                cards.append({"name": name, "price": m["price"], "label": m["label"], "percent": m["percent"]})
        if cards:
            source = "carried from last agent render — stale once the agent stops rendering"
    if not cards:
        return (
            '<div class="panel mkt-empty"><b>Index levels not available.</b> '
            "The agent does not publish <code>market_nifty.json</code> yet (see "
            "<code>AGENT_CHANGE_NOTE.md</code>), and the old page has no index cards to carry forward.</div>"
        )

    tiles = []
    for c in cards:
        pct_v = c.get("percent")
        label = c.get("label", "")
        if pct_v is None:
            chg_txt = '<span class="dim">—</span>'
            cls = "dim"
        else:
            up = pct_v >= 0
            good = (not up) if c["name"] in _MACRO_INVERSE else up
            cls = "pos" if good else "neg"
            arrow = "▲" if up else "▼"
            chg_txt = f'<span class="{cls}">{arrow} {pct_v:+.2f}%</span>'
        tiles.append(
            f'<div class="mkt-tile">'
            f'<div class="mkt-name">{esc(c["name"])}</div>'
            f'<div class="mkt-px">{c["price"]:,.2f}</div>'
            f'<div class="mkt-chg">{esc(label)} {chg_txt}</div>'
            "</div>"
        )
    return (
        '<div class="panel mkt-panel">'
        f'<div class="mkt-head"><b>NIFTY indices</b> '
        f'<span class="dim">{esc(source)}</span></div>'
        f'<div class="mkt-grid">{"".join(tiles)}</div>'
        "</div>"
    )


def kpi(label, value, sub="", cls=""):
    sub_html = f'<div class="sub">{sub}</div>' if sub else ""
    return f'<div class="card"><div class="lbl">{label}</div><div class="big {cls}">{value}</div>{sub_html}</div>'


# ---------------------------------------------------------------------------
# html
# ---------------------------------------------------------------------------

CSS = """
:root{--bg:#0e0e16;--panel:#171722;--panel2:#1d1d2b;--line:#26263a;--txt:#e8e8f0;--dim:#9aa0b4;--acc:#8b93ff;--pos:#34d17b;--neg:#ff6b6b;--warn:#ffd166}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--txt);font-family:'Segoe UI',system-ui,-apple-system,sans-serif;margin:0;padding:18px 20px 60px;font-size:14px;line-height:1.45}
a{color:var(--acc);text-decoration:none}
h1{font-size:21px;margin:0;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
h2{font-size:15px;margin:26px 0 10px;letter-spacing:.3px;color:#cfd2ff}
h3{font-size:13px;margin:0 0 6px;color:var(--dim);text-transform:uppercase;letter-spacing:.6px}
.ver{color:var(--acc);font-size:14px;font-weight:400}
.badge{border-radius:4px;font-size:11px;padding:2px 7px;border:1px solid;letter-spacing:.4px}
.badge.paper{color:var(--warn);border-color:var(--warn);background:#2c2c44}
.badge.live{color:var(--neg);border-color:var(--neg);background:#3a2230}
.updated{color:var(--dim);font-size:11.5px;margin:4px 0 16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:9px;margin-bottom:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:9px 11px}
.card .lbl{color:var(--dim);font-size:10.5px;text-transform:uppercase;letter-spacing:.55px}
.card .big{font-size:18px;font-weight:600;margin-top:3px;white-space:nowrap}
.card .sub{font-size:10.5px;color:var(--dim);margin-top:2px}
.pos{color:var(--pos)}.neg{color:var(--neg)}.dim{color:var(--dim)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:12px}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:12px}
table{width:100%;border-collapse:collapse;font-size:12px;background:var(--panel2);border-radius:8px;overflow:hidden}
th{background:#232338;color:var(--acc);text-align:left;padding:7px 8px;white-space:nowrap;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.4px}
td{padding:7px 8px;border-top:1px solid var(--line);vertical-align:top}
tr.detail td{background:#141420;color:#c9c9d6;font-size:11.5px}
tr.click{cursor:pointer}
tr.click:hover td{background:#20202f}
.buy{color:var(--pos);font-weight:600}.sell{color:var(--neg);font-weight:600}
.rat{max-width:340px;color:#c9c9d6;font-size:11px}
.pill{display:inline-block;border-radius:10px;padding:1px 7px;font-size:10.5px;border:1px solid var(--line)}
.pill.go{color:var(--pos);border-color:var(--pos)}
.pill.skip{color:var(--dim)}
.empty{text-align:center;color:var(--dim);padding:18px}
.chart{width:100%;height:auto;display:block}
.ax{font-size:9.5px;fill:#7f859b}
.cup{stroke:#34d17b;fill:#34d17b}.cdn{stroke:#ff6b6b;fill:#ff6b6b}
.barpos{fill:#34d17b}.barneg{fill:#ff6b6b}
.note{background:#141420;border-left:3px solid var(--acc);padding:8px 11px;border-radius:0 6px 6px 0;margin:8px 0;font-size:12px;color:#c9c9d6}
.note b{color:var(--txt)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:12px}
.kv div:nth-child(odd){color:var(--dim)}
.mkt-panel{padding:10px 12px 12px}
.mkt-head{font-size:12.5px;margin-bottom:8px;display:flex;flex-wrap:wrap;gap:6px 10px;align-items:baseline}
.mkt-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:9px}
.mkt-tile{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:8px 10px 6px}
.mkt-name{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px}
.mkt-px{font-size:20px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}
.mkt-chg{font-size:12.5px;font-variant-numeric:tabular-nums;margin-top:1px;min-height:17px}
.mkt-empty{font-size:13px}
.tag{font-size:10.5px;color:var(--dim);border:1px solid var(--line);border-radius:4px;padding:1px 5px;margin-left:4px}
footer{margin-top:28px;color:var(--dim);font-size:11px;border-top:1px solid var(--line);padding-top:12px}
.toggle{background:#20202f;border:1px solid var(--line);color:var(--txt);border-radius:6px;padding:5px 11px;font-size:12px;cursor:pointer}
.toggle:hover{border-color:var(--acc)}
.row-actions{display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
"""

JS = """
var TRADES = __TRADES_JSON__;
document.addEventListener('click',function(ev){
  var row=ev.target.closest('tr.click');
  if(!row)return;
  var d=document.getElementById(row.dataset.detail);
  if(d)d.classList.toggle('hidden');
});
function filterTable(inputId, tableId){
  var q=document.getElementById(inputId).value.toLowerCase().trim();
  document.querySelectorAll('#'+tableId+' tbody tr.click').forEach(function(tr){
    var hay=(tr.dataset.key||tr.textContent).toLowerCase();
    tr.style.display = !q || hay.indexOf(q)>-1 ? '' : 'none';
  });
}
function downloadCSV(){
  var cols=['date','time','ticker','action','qty','remaining_qty','entry','sl','tgt','last_price','exit_price','status','net_pnl','rationale'];
  var lines=[cols.join(',')];
  TRADES.forEach(function(t){
    lines.push(cols.map(function(c){var v=t[c]==null?'':String(t[c]);return '"'+v.replace(/"/g,'""')+'"';}).join(','));
  });
  var a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([lines.join('\\n')],{type:'text/csv'}));
  a.download='trade_history.csv';a.click();
}
var h=document.createElement('style');h.textContent='.hidden{display:none}';document.head.appendChild(h);
"""


def render_html(
    repo: Path | None = None,
    *,
    equity=None,
    available=None,
    realized=None,
    today_pnl=None,
    updated_label="",
    mode="",
    version="",
) -> str:
    """Build the full page from the repo's JSON files.

    Pass the broker-side numbers explicitly when calling from the agent's sync
    step; otherwise they are parsed out of the current index.html.
    """
    repo = Path(repo) if repo else Path(__file__).resolve().parent
    old_index = repo / "index.html"

    if equity is None and old_index.exists():
        parsed = parse_existing_index(old_index)
        equity = parsed["equity"]
        available = parsed["available"]
        realized = parsed["realized"]
        today_pnl = parsed["today_pnl"]
        # The rebuild time is the "Updated" stamp: never carry over the old
        # page's stamp, or the header freezes at whatever the page said
        # when it was first written. An explicit label passed by a caller
        # is still honoured.
        updated_label = (
            _to_ist_label(updated_label)
            if updated_label
            else datetime.now(_IST).strftime("%Y-%m-%d %H:%M IST")
        )
        mode = mode or parsed["mode"]
        # Never carry over the old page's version either (bug on the v1
        # page: title still says v13.7 after the agent moved to v13.9).
        # Explicit arg wins, otherwise the current agent version above.
        version = version or _AGENT_VERSION

    trades_raw = _load(repo / "trades" / "daily_trade_ledger.json", [])
    gemini = _load(repo / "desk_history.json", [])
    mistral = _load(repo / "desk_history_mistral.json", [])
    verdict = _load(repo / "desk_verdict.json", {})
    verdict_m = _load(repo / "desk_verdict_mistral.json", {})
    latest = _load(repo / "signals" / "latest.json", {})
    earnings = _load(repo / "earnings_calendar_nifty.json", [])
    scout = _load_jsonl(repo / "signals" / "shadow_scout_deepseek.jsonl")
    macro_html = macro_strip(repo)

    ts = trade_stats(trades_raw)
    gs = desk_stats(gemini)
    ms = desk_stats(mistral)
    sc = scout_stats(scout)

    # fall back to the ledger when the page has never been written
    if realized is None:
        realized = ts["net"]
    if equity is None:
        equity = _START_CAPITAL + ts["net"]
    if available is None:
        available = equity

    # NIFTY "today" is IST, not UTC.
    today = datetime.now(_IST).strftime("%Y-%m-%d")
    todays = [t for t in ts["trades"] if t["date"] == today]
    if today_pnl is None:
        today_pnl = ts["by_date"].get(today, 0.0)

    mode = mode or "PAPER"
    version = version or _AGENT_VERSION
    mode_cls = "paper" if mode.upper() == "PAPER" else "live"
    if not updated_label:
        # No old page and no explicit label: stamp the rebuild time. The
        # template adds "· auto-refresh 3min" itself, so it is not part
        # of the label (an earlier version duplicated it here).
        updated_label = datetime.now(_IST).strftime("%Y-%m-%d %H:%M IST")

    earn_dates = {e["date"]: [] for e in earnings if e.get("date")}
    for e in earnings:
        if e.get("date"):
            earn_dates[e["date"]].append(e["ticker"])

    # ------------------------------------------------------------------ cards
    cards = [
        kpi("MTM Equity", rupees(equity), "starting ₹1,00,000"),
        kpi("Avail Margin", rupees(available)),
        kpi("Realized", signed_rupees(realized), pct(100.0 * realized / _START_CAPITAL, 2) + " on ₹1L",
            "pos" if (realized or 0) >= 0 else "neg"),
        kpi("Today P&L", signed_rupees(today_pnl), f"{len(todays)} trade{'s' if len(todays) != 1 else ''} today",
            "pos" if (today_pnl or 0) > 0 else ("neg" if (today_pnl or 0) < 0 else "")),
        kpi("Win Rate", pct(ts["win_rate"]), f'<span class="pos">{ts["wins"]}</span> / <span class="neg">{ts["losses"]}</span> of {ts["n"]}'),
        kpi("Profit Factor", f'{ts["profit_factor"]:.2f}' if ts["profit_factor"] else "—",
            f'{rupees(ts["gross_win"], 0)} won / {rupees(ts["gross_loss"], 0)} lost',
            "pos" if (ts["profit_factor"] or 0) >= 1 else "neg"),
        kpi("Expectancy", signed_rupees(ts["expectancy"]), "per closed trade",
            "pos" if (ts["expectancy"] or 0) >= 0 else "neg"),
        kpi("Avg R", f'{ts["avg_r"]:.2f}R' if ts["avg_r"] is not None else "—", "P&L ÷ risk at entry",
            "pos" if (ts["avg_r"] or 0) >= 0 else "neg"),
        kpi("Max Drawdown", rupees(ts["max_drawdown"], 0), "from running peak", "neg" if ts["max_drawdown"] < 0 else ""),
        kpi("Best / Worst",
            f'{signed_rupees(ts["best"]["net_pnl"], 0)} / {signed_rupees(ts["worst"]["net_pnl"], 0)}'
            if ts["best"] and ts["worst"] else "—",
            (f'{esc(ts["best"]["ticker"])} / {esc(ts["worst"]["ticker"])}' if ts["best"] and ts["worst"] else "")),
        kpi("Avg Hold", f'{ts["avg_hold_min"]:.0f} min' if ts["avg_hold_min"] else "—",
            "entry → exit", ""),
        kpi("Desk Judged", f'{ms["n"]}',
            f'{ms["verdicts"].get("GO", 0)} GO · {ms["verdicts"].get("SKIP", 0)} SKIP', ""),
    ]
    cards_html = "".join(cards)

    # ------------------------------------------------------------- charts
    curve = ts["curve"]
    curve_labels = [("start" if i == 0 else lbl[5:]) for i, (lbl, _) in enumerate(curve)]
    chart_curve = svg_line(curve, labels=curve_labels) if len(curve) > 1 else ""
    daily = [(d[5:], v) for d, v in ts["by_date"].items()]
    chart_daily = svg_bars(daily)

    ticker_rows = sorted(ts["by_ticker"].items(), key=lambda kv: kv[1]["pnl"])
    chart_ticker = svg_columns([(t, v["pnl"]) for t, v in ticker_rows])

    # ------------------------------------------------------- diagnostics
    findings = []
    if ts["n"]:
        stop_rate = len([t for t in ts["trades"] if (t.get("status") or "").startswith("LOSS")]) / ts["n"]
        findings.append(
            f'<b>{pct(100 * stop_rate, 0)} of closed trades stopped out</b> — {ts["losses"]} of {ts["n"]} '
            f'hit the initial stop, while only {ts["wins"]} reached a profit. Average win '
            f'{signed_rupees(ts["avg_win"])} vs average loss {signed_rupees(ts["avg_loss"])}, so the edge has to '
            f'come from a higher hit rate or a wider target.'
        )
    if ms["go_scored"]:
        finding = (
            f'<b>{"Desk GO calls are not adding edge yet" if (ms["go_avg_r"] or 0) <= 0 else "Desk GO calls are working"}</b> '
            f'— {ms["go_n"]} GO verdicts scored, {ms["go_targets"]} target / {ms["go_stops"]} stop / '
            f'{ms["go_closes"]} close, sum {ms["go_sum_r"]:+.2f}R ({ms["go_avg_r"]:+.3f}R average).'
        )
        if ms["skip_n"]:
            finding += (
                f' The {ms["skip_n"]} SKIPs did {ms["skip_sum_r"]:+.1f}R ({ms["skip_avg_r"]:+.3f}R average) — '
            )
            finding += (
                "the vetoes are filtering out the weaker setups."
                if (ms["skip_avg_r"] or 0) < (ms["go_avg_r"] or 0)
                else "the skipped names did <i>better</i> than the GO names, so the veto logic is currently cutting the wrong side."
            )
        findings.append(finding)
    if ms["buckets"]:
        bands = sorted(
            [(band, b) for band, b in ms["buckets"].items() if b["rs"]],
            key=lambda kv: int(kv[0].split("-")[0]),
        )
        if bands:
            detail = ", ".join(
                f'{band} &rarr; {mean(b["rs"]):+.3f}R ({b["n"]} signals)' for band, b in bands
            )
            populated = [(band, b) for band, b in bands if b["n"] >= 10]
            inverted = (
                populated
                and mean(populated[0][1]["rs"]) > mean(populated[-1][1]["rs"])
            )
            findings.append(
                f'<b>Confidence ordering is {"inverted" if inverted else "worth reviewing"}</b> '
                f'— average outcome by band: {detail}. '
                f'{"Higher confidence is currently scoring worse, so it is acting as a veto threshold rather than a predictor." if inverted else "Check whether the bands separate outcomes at all."}'
            )
    if ts["charges"]:
        findings.append(
            f'<b>Charges total {rupees(ts["charges"])} </b> across {ts["n"]} closed trades — '
            f'brokerage is a flat ₹40 per trade, so small trades carry the same fixed cost as large ones.'
        )
    findings_html = "".join(f'<div class="note">{f}</div>' for f in findings)

    # ---------------------------------------------------------- desk table
    def desk_row(h, idx, desk):
        # NIFTY desk rows carry analyst_notes (a dict), not "notes".
        notes = h.get("analyst_notes") or {}
        if not isinstance(notes, dict):
            notes = {"note": notes}
        oc = h.get("outcome")
        oc_cls = {"TARGET": "pos", "STOP": "neg"}.get(oc or "", "dim")
        r = h.get("outcome_r")
        detail_id = f"d-{desk}-{idx}"
        note_html = "".join(
            f'<div style="margin-bottom:6px"><b style="color:#8b93ff">{esc(k.replace("_", " ").title())}</b><br>{esc(v)}</div>'
            for k, v in notes.items() if v
        )
        extra = ""
        for key in ("feasibility", "results_risk"):
            if h.get(key):
                extra += f'<div style="margin-bottom:6px"><b style="color:#8b93ff">{key.replace("_", " ").title()}</b><br>{esc(h.get(key))}</div>'
        trig = f'<div style="margin-bottom:6px"><b style="color:#8b93ff">Trigger</b><br>{esc(h.get("trigger"))}</div>' if h.get("trigger") else ""
        key = " ".join(str(x) for x in (
            (h.get("signal_time") or "")[:16], h.get("ticker"), h.get("action"),
            h.get("verdict"), h.get("confidence"), h.get("outcome") or "",
            desk,
        ))
        return (
            f'<tr class="click" data-detail="{detail_id}" data-key="{esc(key)}">'
            f'<td>{esc((h.get("signal_time") or "")[:16].replace("T", " "))}</td>'
            f'<td><b>{esc(h.get("ticker"))}</b></td>'
            f'<td class="{"buy" if h.get("action") == "BUY" else "sell"}">{esc(h.get("action"))}</td>'
            f'<td><span class="pill {"go" if h.get("verdict") == "GO" else "skip"}">{esc(h.get("verdict"))}</span></td>'
            f'<td>{esc(h.get("confidence"))}</td>'
            f'<td>₹{h.get("entry")}</td><td>₹{h.get("stop")}</td><td>₹{h.get("target")}</td>'
            f'<td>{esc(h.get("qty"))}</td>'
            f'<td class="{oc_cls}">{esc(oc or "—")}{f" ({r:+.1f}R)" if r is not None else ""}</td>'
            f'</tr>'
            f'<tr id="{detail_id}" class="hidden"><td colspan="10">{trig}{extra}{note_html}</td></tr>'
        )

    gemini_rows = "".join(desk_row(h, i, "g") for i, h in enumerate(sorted(
        gemini, key=lambda x: x.get("signal_time") or "", reverse=True)[:40]))
    mistral_rows = "".join(desk_row(h, i, "m") for i, h in enumerate(sorted(
        mistral, key=lambda x: x.get("signal_time") or "", reverse=True)[:60]))

    # ------------------------------------------------------ verdict cards
    def verdict_card(v, title):
        if not v or not v.get("verdicts"):
            return f'<div class="panel"><h3>{title}</h3><div class="empty">No verdict on file.</div></div>'
        row = v["verdicts"][0]
        notes = row.get("analyst_notes") or {}
        if not isinstance(notes, dict):
            notes = {"note": notes}
        notes_html = "".join(
            f'<div style="margin-bottom:6px"><b style="color:#8b93ff">{esc(k.replace("_", " ").title())}</b><br>{esc(val)}</div>'
            for k, val in notes.items() if val
        )
        extra = ""
        for key in ("feasibility", "results_risk"):
            if row.get(key):
                extra += f'<div style="margin-bottom:6px"><b style="color:#8b93ff">{key.replace("_", " ").title()}</b><br>{esc(row.get(key))}</div>'
        return f"""
<div class="panel">
  <h3>{title}</h3>
  <div class="kv">
    <div>Signal</div><div><b>{esc(row.get('ticker'))}</b> · {esc(row.get('action'))} {esc(row.get('qty'))}
      @ ₹{row.get('entry')} · SL ₹{row.get('stop')} · TGT ₹{row.get('target')}</div>
    <div>Verdict</div><div><span class="pill {"go" if row.get('verdict') == 'GO' else 'skip'}">{esc(row.get('verdict'))}</span>
      · confidence {esc(row.get('confidence'))} · judged in {esc(v.get('age_min'))} min</div>
    <div>Trigger</div><div>{esc(row.get('trigger') or '—')}</div>
  </div>
  <div style="margin-top:8px">{extra}{notes_html}</div>
</div>"""

    # --------------------------------------------------------- trade rows
    def trade_row(t, idx):
        detail_id = f"t-{idx}"
        pnl = t.get("net_pnl") or 0
        r = t.get("_r")
        events = "".join(
            f'<div>{esc((e.get("time") or "")[11:16])} — {esc(e.get("text"))}</div>'
            for e in (t.get("events") or [])
        )
        charges = (t.get("charges") or {}).get("total")
        key = " ".join(str(x) for x in (
            t["date"], t["time"], t["ticker"], t["action"], t["qty"],
            t.get("status") or "", f"{pnl:.2f}", "win" if pnl > 0 else "loss",
        ))
        return (
            f'<tr class="click" data-detail="{detail_id}" data-key="{esc(key)}">'
            f'<td>{esc(t["date"])}</td><td>{esc(t["time"][:8])}</td>'
            f'<td><b>{esc(t["ticker"])}</b></td>'
            f'<td class="{"buy" if t["action"] == "BUY" else "sell"}">{esc(t["action"])}</td>'
            f'<td>{esc(t["qty"])}</td>'
            f'<td>₹{t["entry"]:.2f}</td><td>₹{(t.get("initial_sl") or t.get("sl") or 0):.2f}</td>'
            f'<td>₹{(t.get("tgt") or 0):.2f}</td><td>₹{(t.get("exit_price") or t.get("last_price") or 0):.2f}</td>'
            f'<td>{esc(t.get("status"))}</td>'
            f'<td class="{"pos" if pnl >= 0 else "neg"}">{signed_rupees(pnl)}</td>'
            f'<td>{f"{r:+.2f}R" if r is not None else "—"}</td>'
            f'<td class="rat">{esc((t.get("rationale") or "")[:220])}…</td>'
            f'</tr>'
            f'<tr id="{detail_id}" class="hidden"><td colspan="12">'
            f'<div class="two"><div><b style="color:#8b93ff">Full rationale</b><br>{esc(t.get("rationale"))}</div>'
            f'<div><b style="color:#8b93ff">Event log</b>{events or "<br>—"}'
            f'<div style="margin-top:6px" class="dim">ATR {esc(t.get("atr"))} · charges {rupees(charges)} · '
            f'BE {esc(t.get("breakeven_price"))} ({"set" if t.get("breakeven_set") else "not set"})</div></div></div>'
            f'</td></tr>'
        )

    ordered = sorted(ts["trades"], key=lambda t: (t["date"], t["time"]), reverse=True)
    trade_rows = "".join(trade_row(t, i) for i, t in enumerate(ordered))
    todays_rows = "".join(
        trade_row(t, 1000 + i) for i, t in enumerate(
            [t for t in ordered if t["date"] == today])) or (
        '<tr><td colspan="12" class="empty">No trades closed today.</td></tr>')

    csv_data = [
        {k: t.get(k, "") for k in (
            "date", "time", "ticker", "action", "qty", "remaining_qty",
            "entry", "sl", "tgt", "last_price", "exit_price", "status", "net_pnl", "rationale"
        )}
        for t in ts["trades"]
    ]
    csv_data.sort(key=lambda r: (str(r["date"]), str(r["time"])), reverse=True)
    trades_json = json.dumps(csv_data, ensure_ascii=False).replace("</", "<\\/")

    # ------------------------------------------------------------- signal
    sig_html = ""
    if latest and latest.get("candidates"):
        for c in latest["candidates"]:
            candles = c.get("candles_5m") or []
            ind = parse_indicators(c.get("indicators"))
            ind_txt = (
                f'RSI {esc(ind.get("rsi"))} · ATR {esc(ind.get("atr"))} · ATR 5m {esc(ind.get("atr_5m"))} '
                f'· VWAP {esc(ind.get("vwap"))} · vol ratio {esc(ind.get("vol_ratio"))}'
                if ind and "_raw" not in ind else esc(ind.get("_raw", "—"))
            )
            outcome_txt = ""
            if c.get("outcome"):
                outcome_txt = (
                    f'<div>Outcome</div><div>{esc(c.get("outcome"))} · {esc(c.get("outcome_reason") or "")}</div>'
                )
            sig_html += f"""
<div class="panel">
  <h3>Latest candidate · {esc(c.get('ticker'))} {esc(c.get('signal'))}</h3>
  <div class="kv">
    <div>Signal</div><div><b>{esc(latest.get('signal_id'))}</b> · {esc((latest.get('generated_at') or '')[:19])}</div>
    <div>Levels</div><div>LTP ₹{esc(c.get('ltp'))} · SL ₹{esc(c.get('stop'))} · TGT ₹{esc(c.get('target'))} · qty {esc(c.get('qty'))}</div>
    <div>Indicators</div><div>{ind_txt}</div>
    <div>Scout reason</div><div>{esc(c.get('scout_reason'))}</div>
    <div>NIFTY 50 LTP</div><div>{esc(latest.get('nifty_ltp'))}</div>
    {outcome_txt}
  </div>
  {svg_candles(candles, c.get('ltp'), c.get('stop'), c.get('target'))}
</div>"""
    else:
        sig_html = '<div class="panel"><h3>Latest candidate</h3><div class="empty">signals/latest.json is empty.</div></div>'

    # ----------------------------------------------------------- earnings
    upcoming = sorted([d for d in earn_dates if d >= today])[:8]
    earn_html = "".join(
        f'<div class="card"><div class="lbl">{esc(d)}</div><div class="big" style="font-size:14px">'
        f'{esc(", ".join(earn_dates[d]))}</div></div>' for d in upcoming
    ) or '<div class="empty">No upcoming earnings on file.</div>'

    # -------------------------------------------------------------- scout
    scout_html = ""
    if sc:
        scout_html = f"""
<div class="panel">
  <h3>Shadow scouts · Qwen vs DeepSeek Flash vs DeepSeek Pro vs Mistral</h3>
  <div class="kv">
    <div>Scans logged</div><div>{sc['scans']}</div>
    <div>Qwen picks</div><div>{sc['qwen_picks']}</div>
    <div>DeepSeek Flash picks</div><div>{sc['deep_picks']}</div>
    <div>DeepSeek Pro picks</div><div>{sc['pro_picks']}</div>
    <div>Mistral picks</div><div>{sc['mistral_picks']}</div>
    <div>Agreement</div><div>{sc['overlap']} overlapping · {sc['qwen_only']} Qwen-only · {sc['deep_only']} DeepSeek-only
      → <b>{pct(sc['agreement'])}</b> agreement</div>
    <div>Errors</div><div>Flash {sc['err_flash']} · Pro {sc['err_pro']} · Mistral {sc['err_mistral']} (Qwen: no error field in this file)</div>
    <div>Avg latency</div><div>{sc['avg_latency_ms']:.0f} ms</div>
  </div>
</div>"""

    # ---------------------------------------------------------- desk stats
    def desk_kpis(s, name):
        if not s["n"]:
            return f'<div class="panel"><h3>{name}</h3><div class="empty">No judgements on file.</div></div>'
        go_pct = 100.0 * s["go_n"] / s["n"]
        bucket_rows = []
        for band, b in s["buckets"].items():
            avg_r = mean(b["rs"]) if b["rs"] else None
            cell = (
                f'<td class="{"pos" if avg_r >= 0 else "neg"}">{avg_r:+.2f}R</td>'
                if avg_r is not None
                else '<td class="dim">—</td>'
            )
            bucket_rows.append(
                f'<tr><td>{esc(band)}</td><td>{b["n"]}</td><td>{b["go"]}</td>'
                f'<td>{b["target"]}/{b["stop"]}/{b["close"]}</td>{cell}</tr>'
            )
        buckets = "".join(bucket_rows)
        return f"""
<div class="panel">
  <h3>{name}</h3>
  <div class="grid">
    {kpi("Judged", str(s["n"]), f'{esc(s["first"] or "")[:10]} → {esc(s["last"] or "")[:10]}')}
    {kpi("GO rate", pct(go_pct), f'{s["go_n"]} of {s["n"]}')}
    {kpi("GO outcomes", f'{s["go_targets"]}T / {s["go_stops"]}S / {s["go_closes"]}C', "target / stop / close")}
    {kpi("GO sum R", f'{s["go_sum_r"]:+.1f}R' if s["go_sum_r"] is not None else "—",
         f'{s["go_avg_r"]:+.2f}R avg' if s["go_avg_r"] is not None else "",
         "pos" if (s["go_sum_r"] or 0) >= 0 else "neg")}
    {kpi("SKIP sum R", f'{s["skip_sum_r"]:+.1f}R' if s["skip_sum_r"] is not None else "—",
         f'{s["skip_avg_r"]:+.2f}R avg over {s["skip_n"]}' if s["skip_avg_r"] is not None else "",
         "pos" if (s["skip_sum_r"] or 0) >= 0 else "neg")}
    {kpi("Judge latency", f'{s["avg_latency_s"]:.0f}s' if s["avg_latency_s"] else "—", "signal → verdict")}
  </div>
  <table><thead><tr><th>Confidence</th><th>Signals</th><th>GO</th><th>T/S/C</th><th>Avg R</th></tr></thead>
  <tbody>{buckets}</tbody></table>
</div>"""

    js = JS.replace("__TRADES_JSON__", trades_json)

    # -------------------------------------------------------------- page
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="180">
<title>NIFTY Intraday Agent {esc(version)}</title>
<style>{CSS}</style></head><body>
<h1>NIFTY Intraday Agent <span class="ver">{esc(version)}</span>
<span class="badge {mode_cls}">{esc(mode)}</span></h1>
<div class="updated">Updated {esc(updated_label)} &middot; auto-refresh 3min &middot;
{ts['n']} closed trades &middot; {ms['n']} desk judgements</div>

{macro_html}

<div class="grid">{cards_html}</div>

<div class="two">
  <div class="panel"><h3>Equity curve (closed trades, ₹1,00,000 start)</h3>{chart_curve}</div>
  <div class="panel"><h3>Daily P&amp;L</h3>{chart_daily}</div>
</div>
<div class="two">
  <div class="panel"><h3>P&amp;L by ticker</h3>{chart_ticker}</div>
  <div class="panel"><h3>What the numbers say</h3>{findings_html}</div>
</div>

<h2>Today</h2>
<table><thead><tr><th>Date</th><th>Time (IST)</th><th>Ticker</th><th>Action</th><th>Qty</th><th>Entry</th><th>SL</th>
<th>TGT</th><th>Exit</th><th>Status</th><th>Net P&amp;L</th><th>R</th><th>Rationale</th></tr></thead>
<tbody>{todays_rows}</tbody></table>

<h2>Trade History <span class="tag">click a row for the full rationale and event log</span></h2>
<div class="row-actions">
  <input id="q" class="toggle" placeholder="Filter ticker / status…" onkeyup="filterTable('q','hist')" style="min-width:220px">
  <button class="toggle" onclick="downloadCSV()">Export CSV</button>
</div>
<table id="hist"><thead><tr><th>Date</th><th>Time (IST)</th><th>Ticker</th><th>Action</th><th>Qty</th><th>Entry</th><th>SL</th>
<th>TGT</th><th>Exit</th><th>Status</th><th>Net P&amp;L</th><th>R</th><th>Rationale</th></tr></thead>
<tbody>{trade_rows}</tbody></table>

<h2>Assistant Desk <span class="tag">advisory only — these are vetoes on the agent's signals, not trades</span></h2>
<div class="two">{verdict_card(verdict, "Gemini desk · last verdict")}{verdict_card(verdict_m, "Mistral desk · last verdict")}</div>
<div class="two">{desk_kpis(ms, "Mistral desk scorecard")}{desk_kpis(gs, "Gemini desk scorecard")}</div>

<div class="two">
  <div class="panel"><h3>Mistral desk · last 60 judgements</h3>
  <table><thead><tr><th>Signal time</th><th>Ticker</th><th>Action</th><th>Verdict</th><th>Conf</th><th>Entry</th><th>SL</th><th>TGT</th><th>Qty</th><th>Outcome</th></tr></thead>
  <tbody>{mistral_rows or '<tr><td colspan="10" class="empty">No judgements.</td></tr>'}</tbody></table></div>
  <div class="panel"><h3>Gemini desk · last 40 judgements</h3>
  <table><thead><tr><th>Signal time</th><th>Ticker</th><th>Action</th><th>Verdict</th><th>Conf</th><th>Entry</th><th>SL</th><th>TGT</th><th>Qty</th><th>Outcome</th></tr></thead>
  <tbody>{gemini_rows or '<tr><td colspan="10" class="empty">No judgements.</td></tr>'}</tbody></table></div>
</div>

<h2>Live signal</h2>
{sig_html}

<h2>Scout &amp; calendar</h2>
<div class="two">
  <div class="panel"><h3>Upcoming earnings on watched names</h3>
  <div class="grid">{earn_html}</div></div>
  {scout_html}
</div>

<footer>
Generated by <code>build_dashboard.py</code> from this repo's own JSON — no external requests, no CDN.
Advisory / paper-trading output only, not investment advice.
</footer>
<script>{js}</script>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None, help="repo root (default: script's directory)")
    ap.add_argument("--out", default=None, help="output file (default: <repo>/index.html)")
    args = ap.parse_args()

    repo = Path(args.repo).resolve() if args.repo else Path(__file__).resolve().parent
    out = Path(args.out).resolve() if args.out else repo / "index.html"
    html_text = render_html(repo)
    out.write_text(html_text, encoding="utf-8")
    print(f"wrote {out} ({len(html_text):,} bytes)")


if __name__ == "__main__":
    main()
