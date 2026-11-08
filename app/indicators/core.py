"""V2.1 OHLCV normalization and technical-indicator calculations.

Every result is aligned to the input candles.  ``None`` means that there are
not yet enough prior candles to calculate that value.  The implementation is
kept free of pandas/numpy so the FastAPI service has no extra runtime
dependency and the formulas can be unit-tested directly.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from math import isfinite, sqrt
from typing import Any


NumberSeries = list[float | None]


@dataclass(frozen=True, slots=True)
class NormalizedCandle:
    """The validated OHLCV representation used by every V2 indicator."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


def normalize_candles(candles: Iterable[Any]) -> list[NormalizedCandle]:
    """Validate and chronologically sort Pydantic candles or mapping objects.

    A candle must have finite prices, ``low <= open/close <= high``, and a
    non-negative integral volume.  Rejecting malformed market data prevents a
    misleading indicator value from silently reaching callers.
    """
    normalized: list[NormalizedCandle] = []
    for index, candle in enumerate(candles):
        if isinstance(candle, Mapping):
            source = candle
            get = source.__getitem__
        else:
            get = lambda field: getattr(candle, field)
        try:
            timestamp = get("timestamp")
            if not isinstance(timestamp, datetime):
                raise TypeError("timestamp must be a datetime")
            opening, high, low, close = (float(get(field)) for field in ("open", "high", "low", "close"))
            raw_volume = get("volume")
            volume = int(raw_volume)
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid candle at index {index}") from error
        if not all(isfinite(value) for value in (opening, high, low, close)):
            raise ValueError(f"candle {index} contains a non-finite price")
        if low > high or not low <= opening <= high or not low <= close <= high:
            raise ValueError(f"candle {index} has inconsistent OHLC prices")
        if volume < 0 or volume != raw_volume:
            raise ValueError(f"candle {index} has an invalid volume")
        normalized.append(NormalizedCandle(timestamp, opening, high, low, close, volume))
    return sorted(normalized, key=lambda candle: candle.timestamp)


def sma(values: list[float], period: int) -> NumberSeries:
    _positive_period(period)
    result: NumberSeries = [None] * len(values)
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= period:
            running -= values[index - period]
        if index >= period - 1:
            result[index] = running / period
    return result


def ema(values: list[float], period: int) -> NumberSeries:
    """EMA seeded with the period's SMA, the convention used for MACD here."""
    _positive_period(period)
    result: NumberSeries = [None] * len(values)
    if len(values) < period:
        return result
    previous = sum(values[:period]) / period
    result[period - 1] = previous
    multiplier = 2.0 / (period + 1)
    for index in range(period, len(values)):
        previous = (values[index] - previous) * multiplier + previous
        result[index] = previous
    return result


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal_period: int = 9) -> dict[str, NumberSeries]:
    if fast >= slow:
        raise ValueError("MACD fast period must be smaller than slow period")
    fast_ema, slow_ema = ema(closes, fast), ema(closes, slow)
    dif: NumberSeries = [None if left is None or right is None else left - right for left, right in zip(fast_ema, slow_ema)]
    compact_dif = [value for value in dif if value is not None]
    compact_signal = ema(compact_dif, signal_period)
    signal: NumberSeries = [None] * len(closes)
    start = next((index for index, value in enumerate(dif) if value is not None), len(closes))
    for offset, value in enumerate(compact_signal):
        signal[start + offset] = value
    histogram: NumberSeries = [None if value is None or signal_value is None else value - signal_value for value, signal_value in zip(dif, signal)]
    return {"dif": dif, "signal": signal, "histogram": histogram}


def stochastic_kd(candles: list[NormalizedCandle], period: int = 9) -> dict[str, NumberSeries]:
    _positive_period(period)
    k: NumberSeries = [None] * len(candles)
    d: NumberSeries = [None] * len(candles)
    previous_k = previous_d = 50.0
    for index in range(period - 1, len(candles)):
        window = candles[index - period + 1:index + 1]
        high, low = max(item.high for item in window), min(item.low for item in window)
        rsv = 50.0 if high == low else (candles[index].close - low) / (high - low) * 100
        previous_k = (2 * previous_k + rsv) / 3
        previous_d = (2 * previous_d + previous_k) / 3
        k[index], d[index] = previous_k, previous_d
    return {"k": k, "d": d}


def rsi(closes: list[float], period: int = 14) -> NumberSeries:
    _positive_period(period)
    result: NumberSeries = [None] * len(closes)
    if len(closes) <= period:
        return result
    changes = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
    avg_gain = sum(max(change, 0.0) for change in changes[:period]) / period
    avg_loss = sum(max(-change, 0.0) for change in changes[:period]) / period
    result[period] = _rsi_value(avg_gain, avg_loss)
    for index in range(period + 1, len(closes)):
        change = changes[index - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        result[index] = _rsi_value(avg_gain, avg_loss)
    return result


def roc(closes: list[float], period: int = 12) -> NumberSeries:
    _positive_period(period)
    result: NumberSeries = [None] * len(closes)
    for index in range(period, len(closes)):
        base = closes[index - period]
        result[index] = None if base == 0 else (closes[index] / base - 1) * 100
    return result


def bollinger(closes: list[float], period: int = 20, deviations: float = 2.0) -> dict[str, NumberSeries]:
    _positive_period(period)
    if deviations <= 0:
        raise ValueError("Bollinger deviations must be positive")
    middle = sma(closes, period)
    upper: NumberSeries = [None] * len(closes)
    lower: NumberSeries = [None] * len(closes)
    for index in range(period - 1, len(closes)):
        average = middle[index]
        assert average is not None
        window = closes[index - period + 1:index + 1]
        stddev = sqrt(sum((value - average) ** 2 for value in window) / period)
        upper[index], lower[index] = average + deviations * stddev, average - deviations * stddev
    return {"upper": upper, "middle": middle, "lower": lower}


def atr(candles: list[NormalizedCandle], period: int = 14) -> NumberSeries:
    _positive_period(period)
    result: NumberSeries = [None] * len(candles)
    if len(candles) <= period:
        return result
    ranges = _true_ranges(candles)
    previous = sum(ranges[1:period + 1]) / period
    result[period] = previous
    for index in range(period + 1, len(candles)):
        previous = (previous * (period - 1) + ranges[index]) / period
        result[index] = previous
    return result


def dmi_adx(candles: list[NormalizedCandle], period: int = 14) -> dict[str, NumberSeries]:
    _positive_period(period)
    empty = [None] * len(candles)
    plus_di, minus_di, adx = empty.copy(), empty.copy(), empty.copy()
    if len(candles) <= period:
        return {"plus_di": plus_di, "minus_di": minus_di, "adx": adx}
    tr = _true_ranges(candles)
    plus_dm, minus_dm = [0.0] * len(candles), [0.0] * len(candles)
    for index in range(1, len(candles)):
        up_move = candles[index].high - candles[index - 1].high
        down_move = candles[index - 1].low - candles[index].low
        plus_dm[index] = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm[index] = down_move if down_move > up_move and down_move > 0 else 0.0
    smooth_tr, smooth_plus, smooth_minus = sum(tr[1:period + 1]), sum(plus_dm[1:period + 1]), sum(minus_dm[1:period + 1])
    dx: NumberSeries = [None] * len(candles)
    for index in range(period, len(candles)):
        if index > period:
            smooth_tr = smooth_tr - smooth_tr / period + tr[index]
            smooth_plus = smooth_plus - smooth_plus / period + plus_dm[index]
            smooth_minus = smooth_minus - smooth_minus / period + minus_dm[index]
        if smooth_tr:
            plus_di[index], minus_di[index] = 100 * smooth_plus / smooth_tr, 100 * smooth_minus / smooth_tr
            total = plus_di[index] + minus_di[index]
            dx[index] = 0.0 if total == 0 else 100 * abs(plus_di[index] - minus_di[index]) / total
    dx_values = [value for value in dx[period:] if value is not None]
    if len(dx_values) >= period:
        previous_adx = sum(dx_values[:period]) / period
        first_adx = period * 2 - 1
        adx[first_adx] = previous_adx
        for index in range(first_adx + 1, len(candles)):
            value = dx[index]
            assert value is not None
            previous_adx = (previous_adx * (period - 1) + value) / period
            adx[index] = previous_adx
    return {"plus_di": plus_di, "minus_di": minus_di, "adx": adx}


def volume_indicators(candles: list[NormalizedCandle]) -> dict[str, NumberSeries]:
    volumes = [float(candle.volume) for candle in candles]
    volume_ma5, volume_ma20 = sma(volumes, 5), sma(volumes, 20)
    ratio_5 = [None if average in (None, 0) else volume / average for volume, average in zip(volumes, volume_ma5)]
    ratio_20 = [None if average in (None, 0) else volume / average for volume, average in zip(volumes, volume_ma20)]
    obv: NumberSeries = [None] * len(candles)
    if candles:
        running = 0.0
        obv[0] = running
        for index in range(1, len(candles)):
            if candles[index].close > candles[index - 1].close:
                running += candles[index].volume
            elif candles[index].close < candles[index - 1].close:
                running -= candles[index].volume
            obv[index] = running
    return {"volume_ma5": volume_ma5, "volume_ma20": volume_ma20, "ratio_5": ratio_5, "ratio_20": ratio_20, "obv": obv}


def calculate_indicators(candles: Iterable[Any]) -> dict[str, Any]:
    """Calculate the complete V2.1 indicator set from standard OHLCV candles."""
    data = normalize_candles(candles)
    closes = [candle.close for candle in data]
    ma = {f"ma{period}": sma(closes, period) for period in (5, 10, 20, 60, 120, 240)}
    exponential = {f"ema{period}": ema(closes, period) for period in (12, 26)}
    return {
        "candles": data,
        "ma": ma,
        "ema": exponential,
        "macd": macd(closes),
        "dmi_adx": dmi_adx(data),
        "kd": stochastic_kd(data),
        "rsi": {
            "rsi6": rsi(closes, 6),
            "rsi12": rsi(closes, 12),
            "rsi14": rsi(closes, 14),
        },
        "roc": {"roc12": roc(closes)},
        "bollinger": bollinger(closes),
        "atr": {"atr14": atr(data)},
        "volume": volume_indicators(data),
    }


def _true_ranges(candles: list[NormalizedCandle]) -> list[float]:
    values = [0.0] * len(candles)
    for index in range(1, len(candles)):
        current, previous = candles[index], candles[index - 1]
        values[index] = max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close))
    return values


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def _positive_period(period: int) -> None:
    if period <= 0:
        raise ValueError("period must be positive")
