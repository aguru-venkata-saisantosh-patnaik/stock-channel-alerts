"""The video's buy / exclude rules applied to one stock's channel."""
from __future__ import annotations

from .channel import Channel


def classify(stock: dict, ch: Channel | None, cfg: dict) -> dict:
    """Return the dashboard row for one stock."""
    buy_k = cfg["buy_sigma_top"] if stock["tier"] == "top" else cfg["buy_sigma_other"]
    row = {
        **stock,
        "buy_sigma": -buy_k,
        "status": "EXCLUDED",
        "reason": "",
    }
    if ch is None:
        row["reason"] = f"Less than {cfg['length']} daily bars (~10 years) of history"
        return row

    buy_line = ch.line(-buy_k)
    row.update(
        price=round(ch.price, 4),
        reg=round(ch.reg, 4),
        sigma=round(ch.sigma, 4),
        z=round(ch.z, 3),
        slope_pct_per_year=round(ch.slope_pct_per_year, 2),
        lines={k: round(v, 4) for k, v in ch.lines().items()},
        buy_line=round(buy_line, 4),
        dist_to_buy_pct=round((ch.price / buy_line - 1) * 100, 2) if buy_line > 0 else None,
        upside_to_reg_pct=round((ch.reg / ch.price - 1) * 100, 2) if ch.price > 0 else None,
    )

    if ch.slope <= 0:
        row["reason"] = "Channel is flat or pointing down (no growth over 10 years)"
    elif buy_line <= 0:
        row["reason"] = f"The {-buy_k}σ buy line is below zero"
    elif ch.z <= -buy_k:
        row["status"] = "BUY"
    elif ch.z <= -buy_k + cfg["near_sigma"]:
        row["status"] = "NEAR"
    else:
        row["status"] = "NEUTRAL"
    return row
