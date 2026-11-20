"""The video's rules, applied to every lookback window of one stock.

Each row carries `windows` = {"10": {...}, "7": {...}, ...}, one entry per window the
stock has enough history for, all with the same fields. The row's top-level fields
mirror its *main* window:
  - watchlist stocks: the 10-year window (no 10 years of data = excluded, as in the video)
  - your own tickers: the longest window they have data for
"""
from __future__ import annotations

import math

import numpy as np

from . import channel


def window_bars(years: int, cfg: dict) -> int:
    return cfg["length"] if years == 10 else years * cfg["bars_per_year"]


def buy_levels(stock: dict, cfg: dict) -> list[float]:
    """Buy levels, shallowest first (e.g. [-3, -4])."""
    if stock.get("mine"):
        lv = cfg["my_buy_levels"]
    elif stock["tier"] == "top":
        lv = [-cfg["buy_sigma_top"]]
    else:
        lv = [-cfg["buy_sigma_other"]]
    return sorted((float(x) for x in lv), reverse=True)


def _r(x: float, n: int = 4):
    return None if x is None or not math.isfinite(x) else round(float(x), n)


def evaluate(ch: channel.Channel, levels: list[float], cfg: dict) -> dict:
    """One window's numbers, status and reason."""
    valid = [lv for lv in levels if ch.line(lv) > 0]
    shallow = valid[0] if valid else levels[0]
    buy_line = ch.line(shallow)
    w = {
        "bars": ch.length,
        "price": _r(ch.price),
        "reg": _r(ch.reg),
        "sigma": _r(ch.sigma),
        "z": _r(ch.z, 3),
        "slope_pct_per_year": _r(ch.slope_pct_per_year, 2),
        "lines": {k: _r(v) for k, v in ch.lines().items()},
        "buy_sigma": shallow,
        "buy_line": _r(buy_line),
        "buy_lines": {f"{lv:g}": _r(ch.line(lv)) for lv in levels},
        "valid_levels": valid,
        "hit_levels": [lv for lv in valid if ch.z <= lv],
        "dist_to_buy_pct": _r((ch.price / buy_line - 1) * 100, 2) if buy_line > 0 else None,
        "upside_to_reg_pct": _r((ch.reg / ch.price - 1) * 100, 2) if ch.price > 0 else None,
        "status": "NEUTRAL",
        "reason": "",
    }
    if ch.sigma <= 0 or not math.isfinite(ch.z):
        w.update(status="EXCLUDED", reason="Price has not moved enough to build a channel")
    elif ch.slope <= 0:
        w.update(status="EXCLUDED", reason="Channel is flat or pointing down (no growth over this window)")
    elif not valid:
        w.update(status="EXCLUDED", reason=f"The {levels[0]:g}σ buy line is below zero")
    elif ch.z <= shallow:
        w["status"] = "BUY"
    elif ch.z <= shallow + cfg["near_sigma"]:
        w["status"] = "NEAR"
    return w


def classify(stock: dict, closes: np.ndarray, cfg: dict) -> dict:
    """Return the dashboard row for one stock (closes = its daily closes, oldest first)."""
    closes = np.asarray(closes, dtype=float)
    closes = closes[np.isfinite(closes)]
    levels = buy_levels(stock, cfg)
    row = {**stock, "buy_levels": levels, "bars": int(len(closes)), "windows": {}}

    for years in cfg["windows_years"]:
        ch = channel.compute(closes, window_bars(years, cfg))
        if ch is not None:
            row["windows"][str(years)] = evaluate(ch, levels, cfg)

    if stock.get("mine"):
        main = next((str(y) for y in cfg["windows_years"] if str(y) in row["windows"]), None)
    else:
        main = "10" if "10" in row["windows"] else None
    row["main_window"] = main
    row["short_channel"] = bool(main and main != "10")
    # alerts may fire on any window, but only for stocks that qualify for a main channel
    row["alerts_on"] = main is not None

    if main is None:
        need = "1 year" if stock.get("mine") else f"{cfg['length']:,} daily bars (~10 years)"
        row.update(
            status="EXCLUDED",
            reason=f"Less than {need} of history",
            buy_sigma=levels[0],
            price=_r(float(closes[-1])) if len(closes) else None,
        )
        return row

    row.update({k: v for k, v in row["windows"][main].items() if k != "bars"})
    return row
