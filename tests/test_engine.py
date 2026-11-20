"""Offline checks of the channel math, rules and alert dedupe.  Run:  python -m tests.test_engine"""
import datetime as dt

import numpy as np
import yaml

from engine import channel, rules, run
from engine.run import ROOT

CFG = yaml.safe_load((ROOT / "config.yml").read_text())
L = CFG["length"]


def series(last_z: float, slope=0.05, noise=5.0, seed=1):
    rng = np.random.default_rng(seed)
    x = np.arange(L)
    y = 100 + slope * x + rng.normal(0, noise, L)
    ch = channel.compute(y, L)
    y[-1] = ch.reg + last_z * ch.sigma  # place the last close at the requested sigma
    return y


def stock(t="TEST", tier="other"):
    return {"ticker": t, "tv_symbol": f"NASDAQ:{t}", "name": t, "exchange": "NASDAQ",
            "market_cap": 1e11, "rank": 1 if tier == "top" else 50, "tier": tier}


def test_rules():
    assert rules.classify(stock(), channel.compute(np.ones(100), L), CFG)["status"] == "EXCLUDED"
    assert rules.classify(stock(), channel.compute(series(-4.5), L), CFG)["status"] == "BUY"
    assert rules.classify(stock(), channel.compute(series(-3.7), L), CFG)["status"] == "NEAR"
    assert rules.classify(stock(), channel.compute(series(-3.2), L), CFG)["status"] == "NEUTRAL"
    assert rules.classify(stock(tier="top"), channel.compute(series(-3.2), L), CFG)["status"] == "BUY"
    down = rules.classify(stock(), channel.compute(series(-4.5, slope=-0.01), L), CFG)
    assert down["status"] == "EXCLUDED" and "flat" in down["reason"]
    neg = rules.classify(stock(), channel.compute(series(-1, slope=0.002, noise=40), L), CFG)
    assert neg["status"] == "EXCLUDED" and "below zero" in neg["reason"], neg


def test_events_dedupe_and_rearm():
    today = dt.date(2026, 11, 20)
    buy = rules.classify(stock("AAA"), channel.compute(series(-4.5), L), CFG)
    ev, st = run.find_events(CFG, [buy], {"AAA": buy}, [], {}, today)
    assert [e["kind"] for e in ev] == ["BUY"]
    ev, st = run.find_events(CFG, [buy], {"AAA": buy}, [], st, today)
    assert ev == []  # no repeat while still in the zone
    near = rules.classify(stock("AAA"), channel.compute(series(-3.8), L), CFG)
    ev, st = run.find_events(CFG, [near], {"AAA": near}, [], st, today)
    assert "AAA" in st["buy_alerted"]  # small bounce does not re-arm
    up = rules.classify(stock("AAA"), channel.compute(series(-2.0), L), CFG)
    ev, st = run.find_events(CFG, [up], {"AAA": up}, [], st, today)
    assert "AAA" not in st["buy_alerted"]  # re-armed
    ev, st = run.find_events(CFG, [buy], {"AAA": buy}, [], st, today)
    assert [e["kind"] for e in ev] == ["BUY"]


def test_take_profit_and_recheck():
    today = dt.date(2026, 11, 20)
    hold = [{"ticker": "AAA", "entry": 80.0, "date": "2026-07-01"}]
    at_reg = rules.classify(stock("AAA"), channel.compute(series(0.1), L), CFG)
    ev, st = run.find_events(CFG, [at_reg], {"AAA": at_reg}, hold, {}, today)
    assert sorted(e["kind"] for e in ev) == ["RECHECK", "TAKE_PROFIT"]
    ev, st = run.find_events(CFG, [at_reg], {"AAA": at_reg}, hold, st, today)
    assert ev == []
    assert "AAA" not in str(st["private"])  # holdings are not readable in the public state
    msgs = run.compose([{"kind": "TAKE_PROFIT", "row": at_reg, "holding": hold[0]}], "https://x")
    assert "TAKE PROFIT AAA" in msgs["short"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
