"""Offline checks of the channel math, rules, alert dedupe and health checks.  Run:  python -m tests.test_engine"""
import copy
import datetime as dt
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from engine import channel, health, rules, run
from engine.run import ROOT

CFG = yaml.safe_load((ROOT / "config.yml").read_text())
L = CFG["length"]
TODAY = dt.date(2026, 11, 20)


def series(last_z: float, n=L, slope=0.05, noise=5.0, seed=1, window=None):
    """n closes on a noisy trend, with the last close placed at `last_z` sigma of the `window` channel."""
    rng = np.random.default_rng(seed)
    x = np.arange(n)
    y = 100 + slope * x + rng.normal(0, noise, n)
    w = window or min(n, L)
    ch = channel.compute(y, w)
    y[-1] = ch.reg + last_z * ch.sigma
    return y


def stock(t="TEST", tier="other", mine=False):
    return {"ticker": t, "tv_symbol": f"NASDAQ:{t}", "name": t, "exchange": "NASDAQ",
            "market_cap": 1e11, "rank": 1 if tier == "top" else 50, "tier": tier, "mine": mine}


def cls(s, y):
    return rules.classify(s, y, CFG)


def test_channel_matches_numpy():
    y = series(-2.0)
    ch = channel.compute(y, L)
    b, a = np.polyfit(np.arange(L), y[-L:], 1)
    fit = a + b * np.arange(L)
    z = (y[-1] - fit[-1]) / np.std(y[-L:] - fit)
    assert abs(ch.reg - fit[-1]) < 1e-6 and abs(ch.z - z) < 1e-9


def test_main_window_rules():
    assert cls(stock(), np.ones(100))["status"] == "EXCLUDED"
    assert cls(stock(), series(-4.5))["status"] == "BUY"
    assert cls(stock(), series(-3.7))["status"] == "NEAR"
    assert cls(stock(), series(-3.2))["status"] == "NEUTRAL"
    assert cls(stock(tier="top"), series(-3.2))["status"] == "BUY"
    down = cls(stock(), series(-4.5, slope=-0.01))
    assert down["status"] == "EXCLUDED" and "flat" in down["reason"]
    neg = cls(stock(), series(-1, slope=0.002, noise=40))
    assert neg["status"] == "EXCLUDED" and "below zero" in neg["reason"], neg


def test_short_history_fallback():
    # 6 years of data: a watchlist stock is excluded, your ticker falls back to the 5y window
    y = series(-1.0, n=6 * 252, window=5 * 252)
    w = cls(stock(), y)
    assert w["status"] == "EXCLUDED" and w["main_window"] is None and not w["alerts_on"]
    assert set(w["windows"]) == {"5", "3", "1"}  # shorter windows still computed for viewing
    m = cls(stock(mine=True), y)
    assert m["main_window"] == "5" and m["short_channel"] and m["alerts_on"]
    assert m["buy_levels"] == [-3.0, -4.0]
    tiny = cls(stock(mine=True), series(0, n=100, window=100))
    assert tiny["main_window"] is None and "1 year" in tiny["reason"]


def test_any_window_two_levels_and_rearm():
    s = stock("MINE", mine=True)
    deep = cls(s, series(-4.5))  # -4.5σ on the 10y window
    ev, st = run.find_events(CFG, [deep], {"MINE": deep}, [], {}, TODAY)
    assert len(ev) == 1 and ev[0]["kind"] == "BUY"
    keys = {(h["window"], h["level"]) for h in ev[0]["hits"]}
    assert ("10", -3.0) in keys and ("10", -4.0) in keys  # both levels on the 10y window
    ev, st = run.find_events(CFG, [deep], {"MINE": deep}, [], st, TODAY)
    assert ev == []  # no repeat while still in the zone
    mid = copy.deepcopy(deep)
    mid["windows"]["10"]["z"] = -3.6  # above -4 but not by 0.5σ: -4 stays latched
    ev, st = run.find_events(CFG, [mid], {"MINE": mid}, [], st, TODAY)
    assert ev == [] and "MINE|10|-4" in st["hits"]
    up = copy.deepcopy(deep)
    up["windows"]["10"]["z"] = -3.4  # 0.6σ above -4: re-armed; still below -3: stays latched
    ev, st = run.find_events(CFG, [up], {"MINE": up}, [], st, TODAY)
    assert "MINE|10|-4" not in st["hits"] and "MINE|10|-3" in st["hits"]
    ev, st = run.find_events(CFG, [deep], {"MINE": deep}, [], st, TODAY)
    assert [(h["window"], h["level"]) for h in ev[0]["hits"]] == [("10", -4.0)]


def test_excluded_window_never_alerts():
    r = cls(stock(), series(-4.5, slope=-0.01))  # downward channel
    ev, _ = run.find_events(CFG, [r], {"TEST": r}, [], {}, TODAY)
    assert all(h["window"] != "10" for e in ev for h in e["hits"])


def test_take_profit_and_recheck():
    hold = [{"ticker": "AAA", "entry": 80.0, "date": "2026-07-01"}]
    at_reg = cls(stock("AAA"), series(0.1))
    ev, st = run.find_events(CFG, [at_reg], {"AAA": at_reg}, hold, {}, TODAY)
    assert sorted(e["kind"] for e in ev) == ["RECHECK", "TAKE_PROFIT"]
    ev, st = run.find_events(CFG, [at_reg], {"AAA": at_reg}, hold, st, TODAY)
    assert ev == []
    assert "AAA" not in str(st["private"])  # holdings are not readable in the public state
    msgs = run.compose([{"kind": "TAKE_PROFIT", "row": at_reg, "holding": hold[0]}], "https://x")
    assert "TAKE PROFIT AAA" in msgs["short"]


# ---------------------------------------------------------------- health
def _health_fixture(tmp: Path, *, stale=False, nan=False, few=False, lose_chart=False):
    days = pd.bdate_range(end="2026-11-20", periods=L + 10)
    msft = pd.Series(series(-1.0, n=L + 10), index=days)
    rows = []
    n = 150 if few else 220
    for i in range(n):
        y = series(-1.0, seed=i)
        r = cls(stock(f"T{i}"), y)
        r["last_date"] = "2026-11-17" if stale else "2026-11-20"
        rows.append(r)
    for t in CFG["my_tickers"]:
        r = cls(stock(t, mine=True), series(-1.0))
        r["last_date"] = "2026-11-17" if stale else "2026-11-20"
        r["rank"] = None
        rows.append(r)
    if nan:
        rows[0]["windows"]["10"]["z"] = float("nan")
    (tmp / "charts").mkdir(exist_ok=True)
    for r in rows[1:] if lose_chart else rows:
        (tmp / "charts" / f"{r['ticker']}.json").write_text("{}")
    return rows, pd.DataFrame({"MSFT": msft})


def _checks(tmp, **kw):
    rows, closes = _health_fixture(tmp, **kw)
    now = dt.datetime(2026, 11, 20, 20, tzinfo=dt.timezone.utc)
    hl = health.run_checks(CFG, rows, closes, {"fetched_at": now.timestamp()}, CFG["my_tickers"],
                           tmp, now, "2026-11-20", [], ["telegram"])
    return {c["id"]: c["status"] for c in hl["checks"]}


def test_health_checks():
    with tempfile.TemporaryDirectory() as d:
        clean = _checks(Path(d))
        # synthetic MSFT cannot reproduce the video's trade, so "known" fails on fake data by design
        assert all(v == "ok" for k, v in clean.items() if k != "known"), clean
    with tempfile.TemporaryDirectory() as d:
        st = _checks(Path(d), stale=True)
        assert st["fresh"] == "fail" and st["prices"] == "fail"
    with tempfile.TemporaryDirectory() as d:
        assert _checks(Path(d), nan=True)["math"] == "fail"
    with tempfile.TemporaryDirectory() as d:
        assert _checks(Path(d), few=True)["count"] == "fail"
    with tempfile.TemporaryDirectory() as d:
        assert _checks(Path(d), lose_chart=True)["charts"] == "fail"


def test_health_alert_dedupe_and_all_clear():
    bad = {"status": "fail", "checks": [{"id": "fresh", "label": "Price data is fresh", "status": "fail", "detail": "x"}]}
    good = {"status": "ok", "checks": [{"id": "fresh", "label": "Price data is fresh", "status": "ok", "detail": "y"}]}
    st = run.health_alerts(bad, {}, "2026-11-20", "", dry=True)
    assert st["health_alerted"] == {"fresh": "2026-11-20"} and st["health_bad"]
    st2 = run.health_alerts(bad, st, "2026-11-20", "", dry=True)
    assert st2["health_alerted"] == st["health_alerted"]  # not re-sent the same day
    st3 = run.health_alerts(good, st2, "2026-11-20", "", dry=True)
    assert st3["health_alerted"] == {} and not st3["health_bad"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
