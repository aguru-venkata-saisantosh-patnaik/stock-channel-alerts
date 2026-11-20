"""One scan: watchlist -> prices -> channels (every window) -> signals -> alerts -> health -> site files.

    python -m engine.run --out out                    normal run (skips when the NYSE is closed)
    python -m engine.run --out out --force            run even when the market is closed
    python -m engine.run --out out --dry-run          print alerts instead of sending them
    python -m engine.run --out out --simulate stale   pretend the price feed is 3 days behind (tests health alerts)

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

import pandas as pd
import yaml

from . import health, notify, prices, prompt, rules, universe

ROOT = Path(__file__).resolve().parent.parent
STATUS_ORDER = {"BUY": 0, "NEAR": 1, "NEUTRAL": 2, "EXCLUDED": 3}


# ---------------------------------------------------------------- helpers
def market_info(cfg: dict, now: dt.datetime) -> tuple[bool, str, str]:
    """(scan?, market status, latest session date). Scans while the NYSE is open and shortly after."""
    import pandas_market_calendars as mcal

    sched = mcal.get_calendar("NYSE").schedule(start_date=now.date() - dt.timedelta(days=10), end_date=now.date())
    sched = sched[sched["market_open"] <= pd.Timestamp(now)]
    if sched.empty:
        return False, "closed", ""
    session = sched.index[-1].date().isoformat()
    s = sched.iloc[-1]
    o, c = s["market_open"].to_pydatetime(), s["market_close"].to_pydatetime()
    if o <= now <= c:
        return True, "open", session
    if c < now <= c + dt.timedelta(minutes=cfg["post_close_minutes"]):
        return True, "just closed", session
    return False, "closed", session


def hkey(key: str) -> str:
    """Keyed hash so the public state file does not reveal which tickers you hold."""
    secret = os.environ.get("HOLDINGS_JSON", "none").encode()
    return hmac.new(secret, key.encode(), hashlib.sha256).hexdigest()[:20]


def load_holdings() -> list[dict]:
    raw = os.environ.get("HOLDINGS_JSON", "").strip()
    if not raw:
        return []
    out = []
    for h in json.loads(raw):
        t = str(h["ticker"]).upper().replace(".", "-")
        out.append({"ticker": t, "entry": float(h.get("entry") or 0) or None, "date": h.get("date")})
    return out


def fmt_money(x) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"${x:,.2f}"


def tv_link(row: dict) -> str:
    return f"https://www.tradingview.com/chart/?symbol={row.get('tv_symbol') or row['ticker']}"


def sort_key(r: dict):
    gap = r["z"] - r["buy_sigma"] if r.get("z") is not None else 99
    return (STATUS_ORDER[r["status"]], gap)


# ---------------------------------------------------------------- scan
def scan(cfg, out: Path, cache: Path, holdings, simulate: str | None):
    uni = universe.load_or_refresh(
        out / "universe.json", cfg["min_market_cap"], cfg["top_n"], cfg["universe_max_age_days"]
    )
    stocks = universe.with_my_tickers(uni, cfg["my_tickers"], out / "my_tickers.json")
    known = {s["ticker"] for s in stocks}
    extra = [h["ticker"] for h in holdings if h["ticker"] not in known]  # held but no longer listed
    closes = prices.load(sorted(known | set(extra)), cache)
    if simulate == "stale":
        closes = closes.iloc[:-3]

    charts = out / "charts"
    charts.mkdir(parents=True, exist_ok=True)
    for old in charts.glob("*.json"):
        old.unlink()
    max_bars = max(rules.window_bars(y, cfg) for y in cfg["windows_years"])

    rows, private_rows, bundle = [], {}, {}
    extra_stocks = [{"ticker": t, "tv_symbol": t, "name": t, "exchange": "", "market_cap": None,
                     "rank": None, "tier": "other", "mine": False} for t in extra]
    for s in stocks + extra_stocks:
        series = closes[s["ticker"]].dropna() if s["ticker"] in closes else pd.Series(dtype=float)
        row = rules.classify(s, series.values, cfg)
        if len(series):
            row["last_date"] = series.index[-1].date().isoformat()
            if len(series) >= 2:
                row["change_pct"] = round((series.iloc[-1] / series.iloc[-2] - 1) * 100, 2)
        if s in extra_stocks:
            private_rows[s["ticker"]] = row
            continue
        rows.append(row)
        if len(series) >= 20:
            win = series.iloc[-max_bars:]
            days = [int(ts.timestamp()) // 86400 for ts in win.index]
            c = [round(float(v), 2) for v in win.values]
            (charts / f"{s['ticker']}.json").write_text(
                json.dumps({"ticker": s["ticker"], "d": days, "c": c}, separators=(",", ":"))
            )
            bundle[s["ticker"]] = {"d0": days[0], "dd": [b - a for a, b in zip(days, days[1:])], "c": c}

    (out / "charts.json").write_text(json.dumps(bundle, separators=(",", ":")))
    rows.sort(key=sort_key)
    by_ticker = {r["ticker"]: r for r in rows} | private_rows
    return rows, by_ticker, uni, closes


# ---------------------------------------------------------------- events
def find_events(cfg, rows, by_ticker, holdings, state, today: dt.date):
    """Buy hits on any window and level, take-profit and re-check for holdings."""
    hits_state = set(state.get("hits", []))
    private = dict(state.get("private", {}))
    events = []

    for r in rows:
        t = r["ticker"]
        new_hits = []
        for w, v in r.get("windows", {}).items():
            for lv in r["buy_levels"]:
                key = f"{t}|{w}|{lv:g}"
                hit = r["alerts_on"] and v["status"] != "EXCLUDED" and lv in v["valid_levels"] and v["z"] <= lv
                if hit and key not in hits_state:
                    new_hits.append({"window": w, "level": lv, "z": v["z"], "line": v["buy_lines"][f"{lv:g}"]})
                    hits_state.add(key)
                elif key in hits_state and not hit and (
                    v["status"] == "EXCLUDED" or v["z"] > lv + cfg["rearm_sigma"]
                ):
                    hits_state.discard(key)  # re-arm: it has to come back down to alert again
        if new_hits:
            new_hits.sort(key=lambda h: (-int(h["window"]), h["level"]))
            events.append({"kind": "BUY", "row": r, "hits": new_hits})
    # forget keys of tickers no longer scanned
    live = {r["ticker"] for r in rows}
    hits_state = {k for k in hits_state if k.split("|")[0] in live}

    for h in holdings:
        r = by_ticker.get(h["ticker"])
        if not r or r.get("z") is None:
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

    new_state = {**state, "hits": sorted(hits_state), "private": private}
    new_state.pop("buy_alerted", None)  # v1 key
    return events, new_state


def _hits_text(hits) -> str:
    return ", ".join(f"{h['level']:g}σ on {h['window']}y ({fmt_money(h['line'])})" for h in hits)


def compose(events: list[dict], pages_url: str) -> dict:
    """Build every message format from the events."""
    buys = [e for e in events if e["kind"] == "BUY"]
    tps = [e for e in events if e["kind"] == "TAKE_PROFIT"]
    rcs = [e for e in events if e["kind"] == "RECHECK"]
    parts = []
    if buys:
        parts.append(f"{len(buys)} BUY ({', '.join(e['row']['ticker'] for e in buys[:5])})")
    if tps:
        parts.append(f"{len(tps)} TAKE-PROFIT")
    if rcs:
        parts.append(f"{len(rcs)} re-check")
    subject = "Sigma Channel: " + ", ".join(parts)

    short, text, html_parts, tg = [], [], [], []
    pre = "white-space:pre-wrap;background:#f4f4f5;padding:12px;border-radius:8px"
    for e in buys:
        r, hits = e["row"], e["hits"]
        main = f"main {r['main_window']}y channel at {r['z']:+.2f}σ, target {fmt_money(r['reg'])} ({r['upside_to_reg_pct']:+.1f}%)"
        line = f"🟢 BUY {r['ticker']} ({r['name']}) {fmt_money(r['price'])}: {_hits_text(hits)} · {main}"
        short.append(line)
        text.append(
            line + "\nRun the fundamental check before buying. Only buy if it PASSES."
            + f"\nChart: {tv_link(r)}\n\n--- Fundamental-check prompt ---\n" + prompt.build(r["name"], r["ticker"])
        )
        html_parts.append(
            f"<h3>🟢 Buy zone: {html.escape(r['ticker'])} ({html.escape(r['name'])}) {fmt_money(r['price'])}</h3>"
            f"<p>Hit: <b>{html.escape(_hits_text(hits))}</b><br>{html.escape(main)}</p>"
            f"<p><a href='{tv_link(r)}'>Open chart</a></p>"
            f"<p><b>Before buying</b>, paste this into an AI assistant with web search on. Only buy if it PASSES:</p>"
            f"<pre style='{pre}'>{html.escape(prompt.build(r['name'], r['ticker']))}</pre>"
        )
        tg.append(
            f"🟢 <b>BUY {html.escape(r['ticker'])}</b> ({html.escape(r['name'])}) {fmt_money(r['price'])}\n"
            f"Hit: {html.escape(_hits_text(hits))}\n{html.escape(main)}\n"
            f"Run the fundamental check first. The prompt is in your email and on the dashboard.\n"
            f"<a href=\"{tv_link(r)}\">Chart</a>"
        )
    for e in tps:
        r, h = e["row"], e["holding"]
        gain = f", {(r['price'] / h['entry'] - 1) * 100:+.1f}% vs entry {fmt_money(h['entry'])}" if h.get("entry") else ""
        line = (f"🔵 TAKE PROFIT {r['ticker']} {fmt_money(r['price'])} has reached the {r['main_window']}y regression line "
                f"{fmt_money(r['reg'])}{gain}. Sell all or part of the position.")
        short.append(line)
        text.append(line + f"\nChart: {tv_link(r)}")
        html_parts.append(f"<h3>🔵 Take profit: {html.escape(r['ticker'])}</h3><p>{html.escape(line)}</p>")
        tg.append(f"🔵 <b>TAKE PROFIT {html.escape(r['ticker'])}</b>\n{html.escape(line)}")
    for e in rcs:
        r = e["row"]
        line = (f"🟡 RE-CHECK {r['ticker']}: held {e['days']} days. Re-run the fundamental check. If the damage now "
                f"looks permanent or the bankruptcy risk has risen, that is the only reason to sell at a loss.")
        short.append(line)
        text.append(line + "\n\n" + prompt.build(r["name"], r["ticker"]))
        html_parts.append(f"<h3>🟡 Re-check: {html.escape(r['ticker'])}</h3><p>{html.escape(line)}</p>"
                          f"<pre style='{pre}'>{html.escape(prompt.build(r['name'], r['ticker']))}</pre>")
        tg.append(f"🟡 <b>RE-CHECK {html.escape(r['ticker'])}</b>\n{html.escape(line)}")

    footer = f"Dashboard: {pages_url}" if pages_url else ""
    disclaimer = "Automated alert from your own strategy rules. Not financial advice."
    return {
        "subject": subject,
        "short": "\n".join(short + ([footer] if footer else [])),
        "text": "\n\n".join(text + [footer, disclaimer]),
        "html": "".join(html_parts) + (f"<p><a href='{pages_url}'>Open dashboard</a></p>" if pages_url else "")
        + f"<p style='color:#71717a;font-size:12px'>{disclaimer}</p>",
        "tg": "\n\n".join(tg + ([html.escape(footer)] if footer else [])),
    }


def send_simple(subject: str, text: str, dry: bool) -> list[str]:
    if dry:
        print(f"DRY RUN, would send: {subject}\n{text}")
        return []
    return notify.send_all(subject, text, text, f"<pre>{html.escape(text)}</pre>", html.escape(text))


def health_alerts(hl: dict, state: dict, session: str, pages_url: str, dry: bool) -> dict:
    """One failure message per failing check per day; an all-clear when everything recovers."""
    alerted = dict(state.get("health_alerted", {}))
    failing = [c for c in hl["checks"] if c["status"] == "fail"]
    new = [c for c in failing if alerted.get(c["id"]) != session]
    if new:
        text = "\n".join(f"❌ {c['label']}: {c['detail']}" for c in new)
        send_simple("Sigma Channel: health check failed", f"⚠️ Health check failed\n{text}\n{pages_url}".strip(), dry)
        for c in new:
            alerted[c["id"]] = session
    if not failing and state.get("health_bad"):
        send_simple("Sigma Channel: all clear", "✅ All health checks pass again.", dry)
        alerted = {}
    return {**state, "health_alerted": alerted, "health_bad": bool(failing)}


def daily_summary(rows, hl, state, session, market, cfg, dry) -> dict:
    if not cfg.get("daily_summary") or market != "just closed" or state.get("summary_day") == session:
        return state
    scans = state.get("scans", {}).get(session, 0)
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in ("BUY", "NEAR")}
    closest = []
    for r in rows:
        if not r["alerts_on"]:
            continue
        for w, v in r["windows"].items():
            if v["status"] != "EXCLUDED":
                closest.append((v["z"] - v["buy_sigma"], r["ticker"], w, v["z"]))
    closest.sort()
    best = ", ".join(f"{t} {z:+.1f}σ ({w}y)" for _, t, w, z in closest[:5])
    icon = {"ok": "✅", "warn": "🟡", "fail": "❌"}[hl["status"]]
    text = (f"{icon} {session} · health {hl['status'].upper()} · {scans} scans · {len(rows)} tickers · "
            f"{counts['BUY']} BUY · {counts['NEAR']} NEAR\nClosest to a buy line: {best}")
    send_simple(f"Sigma Channel daily summary {session}", text, dry)
    return {**state, "summary_day": session}


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out")
    ap.add_argument("--cache", default=".cache/prices.parquet")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--simulate", choices=["stale"], default=None)
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yml").read_text())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    now = dt.datetime.now(dt.timezone.utc)
    pages_url = os.environ.get("PAGES_URL", "")
    gh_out = os.environ.get("GITHUB_OUTPUT")

    go, market, session = market_info(cfg, now)
    if not go and not args.force:
        print(f"NYSE is {market}; nothing to do (use --force to scan anyway).")
        if gh_out:
            Path(gh_out).open("a").write("scanned=false\n")
        return 0

    try:
        holdings = load_holdings()
        rows, by_ticker, uni, closes = scan(cfg, out, Path(args.cache), holdings, args.simulate)
    except Exception as e:
        traceback.print_exc()
        today = now.date().isoformat()
        if state.get("last_error_day") != today and not args.dry_run:
            send_simple("Sigma Channel: scan failed", f"⚠️ Scan failed: {type(e).__name__}: {e}", False)
            state["last_error_day"] = today
            state_path.write_text(json.dumps(state, indent=1))
        return 1

    new_state = dict(state)
    scans = {k: v for k, v in state.get("scans", {}).items() if k >= (now.date() - dt.timedelta(days=7)).isoformat()}
    scans[session] = scans.get(session, 0) + 1
    new_state["scans"] = scans

    send_failures: list[str] = []
    events = []
    if args.simulate:
        print(f"SIMULATION ({args.simulate}): trading alerts are skipped; only health checks run.")
    else:
        events, new_state = find_events(cfg, rows, by_ticker, holdings, new_state, now.date())
    if events:
        msgs = compose(events, pages_url)
        if args.dry_run:
            print("=" * 70, "\nDRY RUN, would send:\n", msgs["subject"], "\n", msgs["short"])
        else:
            chans = notify.configured()
            send_failures = notify.send_all(msgs["subject"], msgs["short"], msgs["text"], msgs["html"], msgs["tg"])
            for f in send_failures:
                print("ALERT FAILED:", f)
            if chans and len(send_failures) == len(chans):
                new_state = {**state, "scans": scans}  # nothing got through: retry these alerts next run
            print(f"sent {len(events)} event(s) via {chans or 'no configured channels'}")
    else:
        print("no new alerts")

    hl = health.run_checks(cfg, rows, closes, uni, [universe.yahoo_symbol(t) for t in cfg["my_tickers"]],
                           out, now, session, send_failures, notify.configured())
    for c in hl["checks"]:
        print(f"  health {c['status']:4} {c['label']}: {c['detail']}")
    new_state = health_alerts(hl, new_state, session, pages_url, args.dry_run)
    new_state = daily_summary(rows, hl, new_state, session, market, cfg, args.dry_run)

    counts = {k: sum(1 for r in rows if r["status"] == k) for k in STATUS_ORDER}
    meta = {
        "generated_at": now.isoformat(timespec="seconds"),
        "market": market,
        "session": session,
        "last_bar_date": max((r.get("last_date", "") for r in rows), default=""),
        "universe_fetched_at": dt.datetime.fromtimestamp(uni["fetched_at"], dt.timezone.utc).isoformat(timespec="seconds"),
        "config": {k: cfg[k] for k in ("length", "bars_per_year", "windows_years", "min_market_cap", "top_n",
                                       "buy_sigma_top", "buy_sigma_other", "near_sigma", "my_buy_levels")},
        "counts": counts,
        "health": hl,
        "prompt_template": prompt.TEMPLATE,
    }
    (out / "signals.json").write_text(json.dumps({**meta, "stocks": rows}, separators=(",", ":"), default=float))
    (out / "health.json").write_text(json.dumps(hl, indent=1))
    if not args.dry_run:
        state_path.write_text(json.dumps(new_state, indent=1))
    print(f"{len(rows)} tickers: {counts}  (session {session}, market {market}, health {hl['status']})")
    if gh_out:
        Path(gh_out).open("a").write("scanned=true\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
