"""Small, dependency-free statistics helpers for time-series telemetry."""

from __future__ import annotations

from datetime import datetime
from typing import Sequence


def days_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 86400.0


def linear_slope(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Ordinary least-squares slope of ys over xs (0.0 when undefined)."""
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def round1(v: float) -> float:
    return round(v + 0.0, 1)
