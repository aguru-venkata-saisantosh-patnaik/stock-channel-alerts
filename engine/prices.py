"""Daily closes from Yahoo Finance (free, via yfinance), with a local cache.

Close prices are split-adjusted but not dividend-adjusted, which is what a TradingView
chart shows by default. The full history is downloaded once per day; every other run
only fetches the last few days (today's bar holds the live price during the session).
"""
from __future__ import annotations

import datetime as dt
import time
from pathlib import Path

import pandas as pd
import yfinance as yf


def _download(tickers: list[str], period: str) -> pd.DataFrame:
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            raw = yf.download(
                tickers,
                period=period,
                interval="1d",
                auto_adjust=False,
                progress=False,
                group_by="ticker",
                threads=True,
            )
            if raw is None or raw.empty:
                raise RuntimeError("Yahoo returned no data")
            if isinstance(raw.columns, pd.MultiIndex):
                closes = raw.xs("Close", axis=1, level=1)
            else:  # single ticker
                closes = raw[["Close"]].rename(columns={"Close": tickers[0]})
            closes.index = pd.to_datetime(closes.index).tz_localize(None).normalize()
            return closes.sort_index()
        except Exception as e:  # rate limit / network
            last_err = e
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"price download failed: {last_err}")


def load(tickers: list[str], cache_path: Path, full_period: str = "12y") -> pd.DataFrame:
    """Wide DataFrame: index = date, columns = tickers, values = close."""
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    stamp = cache_path.with_suffix(".day")
    cached = None
    if cache_path.exists() and stamp.exists() and stamp.read_text().strip() == today:
        cached = pd.read_parquet(cache_path)

    missing = [t for t in tickers if cached is None or t not in cached.columns]
    if cached is None:
        closes = _download(tickers, full_period)
    else:
        closes = cached
        if missing:
            closes = closes.combine_first(_download(missing, full_period))
        recent = _download(tickers, "5d")
        closes = recent.combine_first(closes)  # newest values win

    closes = closes.reindex(columns=sorted(set(closes.columns) | set(tickers)))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    closes.to_parquet(cache_path)
    if cached is None:
        stamp.write_text(today)
    return closes
