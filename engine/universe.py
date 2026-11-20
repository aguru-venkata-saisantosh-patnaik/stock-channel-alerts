"""Watchlist: US primary listings with market cap >= min_market_cap (TradingView screener, free)."""
from __future__ import annotations

import json
import time
from pathlib import Path

from tradingview_screener import Query, col


def yahoo_symbol(tv_name: str) -> str:
    # TradingView uses BRK.A, Yahoo uses BRK-A
    return tv_name.replace(".", "-").replace("/", "-")


def fetch(min_market_cap: float, top_n: int) -> dict:
    _, df = (
        Query()
        .set_markets("america")
        .select("name", "description", "exchange", "market_cap_basic", "type")
        .where(
            col("market_cap_basic") >= float(min_market_cap),
            col("is_primary") == True,  # noqa: E712  (screener DSL)
            col("type").isin(["stock", "dr"]),
        )
        .order_by("market_cap_basic", ascending=False)
        .limit(2000)
        .get_scanner_data()
    )
    if df is None or len(df) < 20:
        raise RuntimeError(f"screener returned only {0 if df is None else len(df)} stocks")
    stocks = []
    for rank, row in enumerate(df.itertuples(index=False), start=1):
        stocks.append(
            {
                "ticker": yahoo_symbol(row.name),
                "tv_symbol": row.ticker,  # e.g. NASDAQ:MSFT
                "name": row.description,
                "exchange": row.exchange,
                "market_cap": float(row.market_cap_basic),
                "rank": rank,
                "tier": "top" if rank <= top_n else "other",
            }
        )
    return {"fetched_at": time.time(), "stocks": stocks}


def load_or_refresh(path: Path, min_market_cap: float, top_n: int, max_age_days: float) -> dict:
    if path.exists():
        cached = json.loads(path.read_text())
        if time.time() - cached.get("fetched_at", 0) < max_age_days * 86400:
            return cached
    try:
        fresh = fetch(min_market_cap, top_n)
    except Exception:
        if path.exists():  # screener down: keep the last good list
            return json.loads(path.read_text())
        raise
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fresh, indent=1))
    return fresh
