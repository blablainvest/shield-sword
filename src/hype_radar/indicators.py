from __future__ import annotations

import math
from statistics import mean, pstdev
from typing import Iterable, List, Sequence

from .models import Candle


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    if denominator == 0:
        return default
    return numerator / denominator


def pct_return(candles: Sequence[Candle], periods: int = 1) -> float:
    if len(candles) <= periods:
        return 0.0
    old = candles[-periods - 1].close
    new = candles[-1].close
    return safe_div(new - old, old)


def rolling_returns(candles: Sequence[Candle], periods: int = 1) -> List[float]:
    values: List[float] = []
    for index in range(periods, len(candles)):
        old = candles[index - periods].close
        new = candles[index].close
        values.append(safe_div(new - old, old))
    return values


def zscore(value: float, history: Sequence[float]) -> float:
    if len(history) < 8:
        return 0.0
    baseline = list(history[:-1]) if len(history) > 12 else list(history)
    sigma = pstdev(baseline)
    if sigma <= 1e-12:
        return 0.0
    return (value - mean(baseline)) / sigma


def relative_growth(current: float, history: Sequence[float]) -> float:
    clean = [item for item in history if item > 0]
    if not clean:
        return 1.0
    return safe_div(current, mean(clean), 1.0)


def rsi(candles: Sequence[Candle], period: int = 14) -> float:
    if len(candles) <= period:
        return 50.0
    gains: List[float] = []
    losses: List[float] = []
    closes = [candle.close for candle in candles[-period - 1 :]]
    for previous, current in zip(closes, closes[1:]):
        change = current - previous
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))
    avg_gain = mean(gains) if gains else 0.0
    avg_loss = mean(losses) if losses else 0.0
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles: Sequence[Candle], period: int = 14) -> float:
    if len(candles) <= period:
        return 0.0
    true_ranges: List[float] = []
    recent = candles[-period - 1 :]
    for previous, current in zip(recent, recent[1:]):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return mean(true_ranges) if true_ranges else 0.0


def vwap(candles: Sequence[Candle]) -> float:
    turnover = sum(candle.turnover for candle in candles)
    volume = sum(candle.volume for candle in candles)
    if turnover > 0 and volume > 0:
        return turnover / volume
    weighted = sum(candle.close * candle.volume for candle in candles)
    return safe_div(weighted, volume, candles[-1].close if candles else 0.0)


def volume_concentration(candles: Sequence[Candle], lookback: int = 4) -> float:
    recent = list(candles[-lookback:])
    total = sum(candle.volume for candle in recent)
    if total <= 0:
        return 0.0
    return max(candle.volume for candle in recent) / total


def volume_declining_on_highs(candles: Sequence[Candle], lookback: int = 6) -> bool:
    recent = list(candles[-lookback:])
    if len(recent) < lookback:
        return False
    made_high = recent[-1].high >= max(candle.high for candle in recent[:-1])
    volume_lower = recent[-1].volume < mean(candle.volume for candle in recent[:-1])
    return made_high and volume_lower


def failed_breakout(candles: Sequence[Candle], lookback: int = 8) -> bool:
    recent = list(candles[-lookback:])
    if len(recent) < lookback:
        return False
    prior_high = max(candle.high for candle in recent[:-2])
    broke_above = recent[-2].high > prior_high or recent[-1].high > prior_high
    closed_back_below = recent[-1].close < prior_high
    return broke_above and closed_back_below


def structure_breakdown(candles: Sequence[Candle], lookback: int = 8) -> bool:
    recent = list(candles[-lookback:])
    if len(recent) < lookback:
        return False
    prior_lows = [candle.low for candle in recent[:-1]]
    return recent[-1].close < min(prior_lows[-4:])


def rounded_price(value: float) -> float:
    if value <= 0:
        return value
    digits = 8 if value < 1 else 6 if value < 10 else 4 if value < 1000 else 2
    return round(value, digits)

