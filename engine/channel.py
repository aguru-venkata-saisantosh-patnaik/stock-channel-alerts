"""Standard-deviation channel, matching TradingView's "Standard Deviation Channel".

A least-squares line is fitted to the last `length` daily closes; sigma is the
population standard deviation of the closes around that line. Validated against the
video's trades: MSFT crossed -3 sigma on 2026-03-27 and touched the regression line
on 2026-08-03; META crossed -4 sigma in mid-June 2022.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

LEVELS = {"p2": 2, "p1": 1, "reg": 0, "m3": -3, "m4": -4}


@dataclass
class Channel:
    reg: float          # regression value at the latest bar
    reg_start: float    # regression value at the first bar of the window
    sigma: float
    slope: float        # price units per bar
    price: float
    length: int

    @property
    def z(self) -> float:
        return (self.price - self.reg) / self.sigma if self.sigma > 0 else 0.0

    def line(self, k: float) -> float:
        return self.reg + k * self.sigma

    def lines(self) -> dict[str, float]:
        return {name: self.line(k) for name, k in LEVELS.items()}

    @property
    def slope_pct_per_year(self) -> float:
        # growth of the regression line over ~252 trading days, relative to its current level
        return 252 * self.slope / self.reg * 100 if self.reg else 0.0


def compute(closes: np.ndarray, length: int) -> Channel | None:
    """Channel over the last `length` closes; None when there is not enough history."""
    closes = np.asarray(closes, dtype=float)
    closes = closes[~np.isnan(closes)]
    if len(closes) < length:
        return None
    y = closes[-length:]
    x = np.arange(length, dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    fit = intercept + slope * x
    sigma = float(np.std(y - fit))
    return Channel(
        reg=float(fit[-1]),
        reg_start=float(fit[0]),
        sigma=sigma,
        slope=float(slope),
        price=float(y[-1]),
        length=length,
    )
