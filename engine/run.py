"""One scan: watchlist -> prices -> channels -> signals -> alerts -> site files.

    python -m engine.run --out out            normal run (skips when the NYSE is closed)
    python -m engine.run --out out --force    run even when the market is closed
    python -m engine.run --out out --dry-run  print alerts instead of sending them

Holdings come from the HOLDINGS_JSON environment variable (a GitHub secret), e.g.
    [{"ticker": "MCD", "entry": 230.94, "date": "2026-10-07"}]
They are only used for private alerts and never written to the public site files.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import html
import json
import math
import os
import sys
import traceback
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from . import channel, notify, prices, prompt, rules, universe

ROOT = Path(__file__).resolve().parent.parent
STATUS_ORDER = {"BUY": 0, "NEAR": 1, "NEUTRAL": 2, "EXCLUDED": 3}


# ---------------------------------------------------------------- helpers
def market_window(cfg: dict, now: dt.datetime) -> tuple[bool, str]:
    """(scan?, human status). Scans while the NYSE is open and shortly after the close."""
    import pandas_market_calendars as mcal

    nyse = mcal.get_calendar("NYSE")
    sched = nyse.schedule(start_date=now.date() - dt.timedelta(days=1), end_date=now.date())
    for _, s in sched.iterrows():
        o, c = s["market_open"].to_pydatetime(), s["market_close"].to_pydatetime()
        if o <= now <= c:
            return True, "open"
        if c < now <= c + dt.timedelta(minutes=cfg["post_close_minutes"]):
            return True, "just closed"
    return False, "closed"


def hkey(key: str) -> str:
    """Keyed hash so the public state file does not reveal which tickers you hold."""
    secret = os.environ.get("HOLDINGS_JSON", "none").encode()
    return hmac.new(secret, key.encode(), hashlib.sha256).hexdigest()[:20]


def load_holdings() -> list[dict]:
    raw = os.environ.get("HOLDINGS_JSON", "").strip()
    if not raw:
        return []
    items = json.loads(raw)
    out = []
    for h in items:
        t = str(h["ticker"]).upper().replace(".", "-")
        out.append({"ticker": t, "entry": float(h.get("entry") or 0) or None, "date": h.get("date")})
    return out


def fmt_money(x: float | None) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"${x:,.2f}"


def tv_link(row: dict) -> str:
    return f"https://www.tradingview.com/chart/?symbol={row.get('tv_symbol') or row['ticker']}"


# ---------------------------------------------------------------- scan
def scan(cfg: dict, out: Path, cache: Path, holdings: list[dict]) -> tuple[list[dict], dict, dict]:
    uni = universe.load_or_refresh(
        out / "universe.json", cfg["min_market_cap"], cfg["top_n"], cfg["universe_max_age_days"]
    )
    stocks = list(uni["stocks"])
    known = {s["ticker"] for s in stocks}
    # holdings that dropped out of the watchlist still need take-profit checks
    extra = [h["ticker"] for h in holdings if h["ticker"] not in known]
    closes = prices.load(sorted(known | set(extra)), cache)

    charts = out / "charts"
    charts.mkdir(parents=True, exist_ok=True)
    for old in charts.glob("*.json"):
        old.unlink()

    rows, private_rows = [], {}
    for s in stocks + [
        {"ticker": t, "tv_symbol": t, "name": t, "exchange": "", "market_cap": None, "rank": None, "tier": "other"}
        for t in extra
    ]:
        series = closes[s["ticker"]].dropna() if s["ticker"] in closes else pd.Series(dtype=float)
        ch = channel.compute(series.values, cfg["length"])
        row = rules.classify(s, ch, cfg)
        if len(series):
            row["last_date"] = series.index[-1].date().isoformat()
            if len(series) >= 2:
                row["change_pct"] = round((series.iloc[-1] / series.iloc[-2] - 1) * 100, 2)
        if s["ticker"] in extra:
            private_rows[s["ticker"]] = row
            continue
        rows.append(row)
        if ch is not None:
            win = series.iloc[-cfg["length"]:]
            (charts / f"{s['ticker']}.json").write_text(
                json.dumps(
                    {
                        "ticker": s["ticker"],
                        "d": [int(ts.timestamp()) // 86400 for ts in win.index],  # days since epoch
                        "c": [round(float(v), 2) for v in win.values],
                        "reg0": round(ch.reg_start, 4),
                        "reg1": round(ch.reg, 4),
                        "sigma": round(ch.sigma, 4),
                    },
                    separators=(",", ":"),
                )
            )

    rows.sort(key=lambda r: (STATUS_ORDER[r["status"]], r.get("z", 99) - r["buy_sigma"] if "z" in r else 99))
    by_ticker = {r["ticker"]: r for r in rows} | private_rows
    return rows, by_ticker, uni


# ---------------------------------------------------------------- events
def find_events(cfg, rows, by_ticker, holdings, state, today: dt.date):
    buy_alerted = set(state.get("buy_alerted", []))
    private = dict(state.get("private", {}))
    events = []

    for r in rows:
        t = r["ticker"]
        if r["status"] == "BUY" and t not in buy_alerted:
            events.append({"kind": "BUY", "row": r})
            buy_alerted.add(t)
        elif t in buy_alerted and (
            r["status"] == "EXCLUDED" or r.get("z", -99) > r["buy_sigma"] + cfg["rearm_sigma"]
        ):
            buy_alerted.discard(t)  # re-arm: it has to come back down to alert again

    for h in holdings:
        r = by_ticker.get(h["ticker"])
        if not r or "z" not in r:
            continue
        k_tp = hkey("TP:" + h["ticker"])
        if r["price"] >= r["reg"] and k_tp not in private:
            events.append({"kind": "TAKE_PROFIT", "row": r, "holding": h})
            private[k_tp] = today.isoformat()
        elif k_tp in private and r["z"] < -cfg["tp_rearm_sigma"]:
            private.pop(k_tp)
        if h.get("date"):
            try:
                held_days = (today - dt.date.fromisoformat(h["date"])).days
            except ValueError:
                continue
            n = held_days // cfg["recheck_days"]
            k_rc = hkey(f"RC:{h['ticker']}:{n}")
            if n >= 1 and k_rc not in private:
                events.append({"kind": "RECHECK", "row": r, "holding": h, "days": held_days})
                private[k_rc] = today.isoformat()

    new_state = {**state, "buy_alerted": sorted(buy_alerted), "private": private}
    return events, new_state


def compose(events: list[dict], pages_url: str) -> dict:
    """Build every message format from the events."""
    buys = [e for e in events if e["kind"] == "BUY"]
    tps = [e for e in events if e["kind"] == "TAKE_PROFIT"]
    rcs = [e for e in events if e["kind"] == "RECHECK"]

    parts = []
    if buys:
        parts.append(f"{len(buys)} BUY")
    if tps:
        parts.append(f"{len(tps)} TAKE-PROFIT")
    if rcs:
        parts.append(f"{len(rcs)} re-check")
    subject = "Std-dev channel: " + ", ".join(parts)

    short, text, html_parts, tg = [], [], [], []
    for e in buys:
        r = e["row"]
        line = (
            f"🟢 BUY ZONE {r['ticker']} ({r['name']}) {fmt_money(r['price'])}: {r['z']:+.2f}σ "
            f"(buy line {r['buy_sigma']}σ = {fmt_money(r['buy_line'])}). "
            f"Target (regression line) {fmt_money(r['reg'])}, {r['upside_to_reg_pct']:+.1f}%."
        )
        short.append(line)
        text.append(
            line
            + "\nRun the fundamental check before buying. Only buy if it PASSES."
            + f"\nChart: {tv_link(r)}\n\n--- Fundamental-check prompt ---\n"
            + prompt.build(r["name"], r["ticker"])
        )
        html_parts.append(
            f"<h3>🟢 BUY zone: {html.escape(r['ticker'])} ({html.escape(r['name'])})</h3>"
            f"<p>Price <b>{fmt_money(r['price'])}</b> is at <b>{r['z']:+.2f}σ</b>. "
            f"Buy line {r['buy_sigma']}σ = {fmt_money(r['buy_line'])}. "
            f"Take-profit target (regression line) {fmt_money(r['reg'])}, {r['upside_to_reg_pct']:+.1f}%.</p>"
            f"<p><a href='{tv_link(r)}'>Open chart</a></p>"
            f"<p><b>Before buying</b>, paste this into an AI assistant with web search on. Only buy if it PASSES:</p>"
            f"<pre style='white-space:pre-wrap;background:#f4f4f5;padding:12px;border-radius:8px'>"
            f"{html.escape(prompt.build(r['name'], r['ticker']))}</pre>"
        )
        tg.append(
            f"🟢 <b>BUY ZONE {html.escape(r['ticker'])}</b> ({html.escape(r['name'])})\n"
            f"Price {fmt_money(r['price'])} at {r['z']:+.2f}σ (buy line {r['buy_sigma']}σ = {fmt_money(r['buy_line'])})\n"
            f"Target {fmt_money(r['reg'])} ({r['upside_to_reg_pct']:+.1f}%)\n"
            f"Run the fundamental check first. The prompt is in your email and on the dashboard.\n"
            f"<a href=\"{tv_link(r)}\">Chart</a>"
        )
    for e in tps:
        r, h = e["row"], e["holding"]
        gain = f", {(r['price'] / h['entry'] - 1) * 100:+.1f}% vs entry {fmt_money(h['entry'])}" if h.get("entry") else ""
        line = (
            f"🔵 TAKE PROFIT {r['ticker']} {fmt_money(r['price'])} has reached the regression line "
            f"{fmt_money(r['reg'])}{gain}. Sell all or part of the position."
        )
        short.append(line)
        text.append(line + f"\nChart: {tv_link(r)}")
        html_parts.append(f"<h3>🔵 Take profit: {html.escape(r['ticker'])}</h3><p>{html.escape(line)}</p>")
        tg.append(f"🔵 <b>TAKE PROFIT {html.escape(r['ticker'])}</b>\n{html.escape(line)}")
    for e in rcs:
        r, h = e["row"], e["holding"]
        line = (
            f"🟡 RE-CHECK {r['ticker']}: held {e['days']} days. Re-run the fundamental check. "
            f"If the damage now looks permanent or the bankruptcy risk has risen, that is the only reason to sell at a loss."
        )
        short.append(line)
        text.append(line + "\n\n" + prompt.build(r["name"], r["ticker"]))
        html_parts.append(
            f"<h3>🟡 Re-check: {html.escape(r['ticker'])}</h3><p>{html.escape(line)}</p>"
            f"<pre style='white-space:pre-wrap;background:#f4f4f5;padding:12px;border-radius:8px'>"
            f"{html.escape(prompt.build(r['name'], r['ticker']))}</pre>"
        )
        tg.append(f"🟡 <b>RE-CHECK {html.escape(r['ticker'])}</b>\n{html.escape(line)}")

    footer = f"Dashboard: {pages_url}" if pages_url else ""
    disclaimer = "Automated alert from your own strategy rules. Not financial advice."
    return {
        "subject": subject,
        "short": "\n".join(short + ([footer] if footer else [])),
        "text": "\n\n".join(text + [footer, disclaimer]),
        "html": "".join(html_parts)
        + (f"<p><a href='{pages_url}'>Open dashboard</a></p>" if pages_url else "")
        + f"<p style='color:#71717a;font-size:12px'>{disclaimer}</p>",
        "tg": "\n\n".join(tg + ([html.escape(footer)] if footer else [])),
    }


# ---------------------------------------------------------------- artifact feed
def artifact_feed(rows: list[dict], out: Path, meta: dict) -> dict:
    """Compact data for the Claude artifact (one db document, < 256 KiB): all rows, plus
    weekly series for the stocks closest to their buy line (BUY and NEAR first)."""
    keep = ("ticker", "name", "tier", "rank", "market_cap", "price", "change_pct", "z", "buy_sigma",
            "buy_line", "reg", "dist_to_buy_pct", "upside_to_reg_pct", "status", "reason", "tv_symbol",
            "slope_pct_per_year")
    feed_rows = [{k: r.get(k) for k in keep if k in r} for r in rows]
    # rows are already sorted BUY, NEAR, then by distance to the buy line
    chartable = [r for r in rows if r["status"] != "EXCLUDED"]
    picks = [r for r in chartable if r["status"] in ("BUY", "NEAR")]
    picks += [r for r in chartable if r not in picks][: max(0, 8 - len(picks))]
    series = {}
    for r in picks[:12]:
        p = out / "charts" / f"{r['ticker']}.json"
        if not p.exists():
            continue
        c = json.loads(p.read_text())
        n = len(c["d"])
        idx = list(range(0, n, 5)) + ([n - 1] if (n - 1) % 5 else [])  # ~weekly + the latest bar
        series[r["ticker"]] = {
            "d": [c["d"][i] for i in idx],
            "c": [c["c"][i] for i in idx],
            "i": idx,
            "n": n,
            "reg0": c["reg0"], "reg1": c["reg1"], "sigma": c["sigma"],
        }
    return {**meta, "rows": feed_rows, "series": series}


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out")
    ap.add_argument("--cache", default=".cache/prices.parquet")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yml").read_text())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    now = dt.datetime.now(dt.timezone.utc)
    pages_url = os.environ.get("PAGES_URL", "")
    gh_out = os.environ.get("GITHUB_OUTPUT")

    go, market = market_window(cfg, now)
    if not go and not args.force:
        print(f"NYSE is {market}; nothing to do (use --force to scan anyway).")
        if gh_out:
            Path(gh_out).open("a").write("scanned=false\n")
        return 0

    try:
        holdings = load_holdings()
        rows, by_ticker, uni = scan(cfg, out, Path(args.cache), holdings)
    except Exception as e:
        traceback.print_exc()
        today = now.date().isoformat()
        if state.get("last_error_day") != today and not args.dry_run:
            msg = f"⚠️ Std-dev channel scan failed: {type(e).__name__}: {e}"
            notify.send_all("Std-dev channel: scan failed", msg, msg, f"<p>{html.escape(msg)}</p>", html.escape(msg))
            state["last_error_day"] = today
            state_path.write_text(json.dumps(state, indent=1))
        return 1

    events, new_state = find_events(cfg, rows, by_ticker, holdings, state, now.date())
    if events:
        msgs = compose(events, pages_url)
        if args.dry_run:
            print("=" * 70, "\nDRY RUN, would send:\n", msgs["subject"], "\n", msgs["text"][:4000])
        else:
            chans = notify.configured()
            fails = notify.send_all(msgs["subject"], msgs["short"], msgs["text"], msgs["html"], msgs["tg"])
            for f in fails:
                print("ALERT FAILED:", f)
            if chans and len(fails) == len(chans):
                new_state = state  # nothing got through: retry these alerts next run
            print(f"sent {len(events)} event(s) via {chans or 'no configured channels'}")
    else:
        print("no new alerts")

    counts = {k: sum(1 for r in rows if r["status"] == k) for k in STATUS_ORDER}
    counts["TAKE_PROFIT_SENT"] = sum(1 for e in events if e["kind"] == "TAKE_PROFIT")
    last_bar = max((r.get("last_date", "") for r in rows), default="")
    meta = {
        "generated_at": now.isoformat(timespec="seconds"),
        "market": market,
        "last_bar_date": last_bar,
        "universe_fetched_at": dt.datetime.fromtimestamp(uni["fetched_at"], dt.timezone.utc).isoformat(timespec="seconds"),
        "config": {k: cfg[k] for k in ("length", "min_market_cap", "top_n", "buy_sigma_top", "buy_sigma_other", "near_sigma")},
        "counts": {k: counts[k] for k in STATUS_ORDER},
        "prompt_template": prompt.TEMPLATE,
    }
    (out / "signals.json").write_text(json.dumps({**meta, "stocks": rows}, separators=(",", ":"), default=float))
    (out / "artifact.json").write_text(json.dumps(artifact_feed(rows, out, meta), separators=(",", ":"), default=float))
    if not args.dry_run:
        state_path.write_text(json.dumps(new_state, indent=1))
    print(f"{len(rows)} stocks: {meta['counts']}  (last bar {last_bar}, market {market})")
    if gh_out:
        Path(gh_out).open("a").write("scanned=true\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
