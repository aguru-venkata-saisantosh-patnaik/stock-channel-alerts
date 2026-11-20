"""Self-checks run after every scan. Results go to signals.json (health bar) and alerts."""
from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

import pandas as pd

from . import channel

RANK = {"ok": 0, "warn": 1, "fail": 2}


def _check(cid: str, label: str, status: str, detail: str) -> dict:
    return {"id": cid, "label": label, "status": status, "detail": detail}


def known_answer(closes: pd.DataFrame, length: int) -> tuple[str, str]:
    """MSFT must still show the video's trade: below -3σ on 2026-03-27, at/above the regression line on 2026-08-03."""
    if "MSFT" not in closes:
        return "fail", "MSFT prices are missing"
    s = closes["MSFT"].dropna()
    out = []
    for day, want in (("2026-03-27", "below"), ("2026-08-03", "above")):
        part = s[s.index <= pd.Timestamp(day)]
        ch = channel.compute(part.values, length)
        if ch is None:
            return "fail", f"not enough MSFT history before {day}"
        ok = ch.z <= -3 if want == "below" else ch.z >= 0
        out.append((ok, f"{day} {ch.z:+.2f}σ"))
    if all(ok for ok, _ in out):
        return "ok", "MSFT reproduces the video's trade (" + ", ".join(t for _, t in out) + ")"
    return "fail", "MSFT no longer matches the video's trade (" + ", ".join(t for _, t in out) + "). Data or math changed"


def run_checks(cfg, rows, closes, uni, my_tickers, out: Path, now: dt.datetime, session: str,
               send_failures: list[str], channels: list[str]) -> dict:
    h = cfg["health"]
    checks = []

    # 1. freshness: newest bar should be the current/last trading session
    newest = max((r.get("last_date", "") for r in rows), default="")
    checks.append(_check(
        "fresh", "Price data is fresh",
        "ok" if newest >= session else "fail",
        f"newest bar {newest or 'none'}, expected {session}",
    ))

    # 2. enough stocks + all of your tickers present
    n_watch = sum(1 for r in rows if r.get("rank"))
    have = {r["ticker"] for r in rows if r.get("bars", 0) > 0}
    missing_mine = [t for t in my_tickers if t not in have]
    bad = n_watch < h["min_stocks"] or missing_mine
    checks.append(_check(
        "count", "Enough stocks scanned", "fail" if bad else "ok",
        f"{n_watch} watchlist stocks (minimum {h['min_stocks']})"
        + (f"; missing your tickers: {', '.join(missing_mine)}" if missing_mine else f"; all {len(my_tickers)} of your tickers present"),
    ))

    # 3. per-ticker price coverage for the session
    stale = [r["ticker"] for r in rows if r.get("last_date", "") < session]
    pct = 100 * len(stale) / max(1, len(rows))
    checks.append(_check(
        "prices", "Prices downloaded",
        "ok" if pct == 0 else "warn" if pct <= h["max_missing_pct"] else "fail",
        f"{len(stale)} of {len(rows)} tickers without a {session} price"
        + (f" ({', '.join(stale[:8])}{'…' if len(stale) > 8 else ''})" if stale else ""),
    ))

    # 4. channel math sane
    broken = []
    for r in rows:
        for w, v in r.get("windows", {}).items():
            vals = (v.get("z"), v.get("reg"), v.get("sigma"))
            if any(x is None or not math.isfinite(x) for x in vals) or (v.get("sigma") or 0) <= 0:
                broken.append(f"{r['ticker']} {w}y")
    checks.append(_check(
        "math", "Channel math sane", "fail" if broken else "ok",
        f"{len(broken)} broken channel(s): {', '.join(broken[:6])}" if broken else "all channels finite with σ > 0",
    ))

    # 5. known answer from the video
    st, detail = known_answer(closes, cfg["length"])
    checks.append(_check("known", "Known answer still right", st, detail))

    # 6. chart files written
    expected = {r["ticker"] for r in rows if r.get("bars", 0) >= 20}
    files = {p.stem for p in (out / "charts").glob("*.json")}
    lost = sorted(expected - files)
    checks.append(_check(
        "charts", "Charts written", "fail" if lost else "ok",
        f"missing {len(lost)}: {', '.join(lost[:8])}" if lost else f"{len(files)} chart files",
    ))

    # 7. alert channels
    if send_failures:
        checks.append(_check("alerts", "Alert channels", "fail", "; ".join(send_failures)))
    elif not channels:
        checks.append(_check("alerts", "Alert channels", "warn", "no alert channel is configured"))
    else:
        checks.append(_check("alerts", "Alert channels", "ok", "configured: " + ", ".join(channels)))

    # 8. watchlist freshness
    age = max(0.0, (now.timestamp() - uni.get("fetched_at", 0)) / 86400)
    checks.append(_check(
        "universe", "Watchlist fresh",
        "ok" if age <= cfg["universe_max_age_days"] + 1 else "warn" if age <= h["universe_fail_days"] else "fail",
        f"screener list is {age:.1f} days old",
    ))

    worst = max(checks, key=lambda c: RANK[c["status"]])["status"]
    return {"status": worst, "checked_at": now.isoformat(timespec="seconds"), "checks": checks}
