"""V2.3 divergence detection based on confirmed price swing points.

A divergence compares two *confirmed* swing highs or lows.  It deliberately
does not compare adjacent candles: a pivot needs ``swing_window`` candles on
both sides, so the two newest candles are not eligible as pivots yet.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def detect_divergences(
    candles: Sequence[Any],
    rsi: Sequence[float | None],
    macd_dif: Sequence[float | None],
    *,
    swing_window: int = 2,
    lookback: int = 60,
) -> dict[str, dict[str, int | str]]:
    """Detect regular RSI/MACD top and bottom divergences.

    A regular bearish divergence is a higher price swing high with a lower
    oscillator high.  A regular bullish divergence is a lower price swing low
    with a higher oscillator low.  ``lookback`` bounds how far back a current
    signal may look, preventing stale pivots from being presented as current.
    """
    if swing_window < 1:
        raise ValueError("swing_window must be at least 1")
    if lookback < 1:
        raise ValueError("lookback must be at least 1")
    if len(candles) != len(rsi) or len(candles) != len(macd_dif):
        raise ValueError("candles and oscillator series must be aligned")

    start = max(swing_window, len(candles) - lookback)
    stop = len(candles) - swing_window
    highs = [index for index in range(start, stop) if _is_swing_high(candles, index, swing_window)]
    lows = [index for index in range(start, stop) if _is_swing_low(candles, index, swing_window)]
    result: dict[str, dict[str, int | str]] = {}
    for name, oscillator in (("rsi", rsi), ("macd", macd_dif)):
        top = _regular_divergence(candles, highs, oscillator, is_high=True)
        bottom = _regular_divergence(candles, lows, oscillator, is_high=False)
        # The newest eligible pair wins if unusual data happens to satisfy both.
        selected = max((item for item in (top, bottom) if item is not None), key=lambda item: int(item["current_index"]), default=None)
        if selected is not None:
            result[name] = selected
    return result


def _regular_divergence(
    candles: Sequence[Any], pivots: Sequence[int], oscillator: Sequence[float | None], *, is_high: bool) -> dict[str, int | str] | None:
    valid = [index for index in pivots if oscillator[index] is not None]
    if len(valid) < 2:
        return None
    previous, current = valid[-2:]
    prior_price = float(candles[previous].high if is_high else candles[previous].low)
    current_price = float(candles[current].high if is_high else candles[current].low)
    prior_oscillator, current_oscillator = float(oscillator[previous]), float(oscillator[current])
    if is_high and current_price > prior_price and current_oscillator < prior_oscillator:
        direction = "bearish"
    elif not is_high and current_price < prior_price and current_oscillator > prior_oscillator:
        direction = "bullish"
    else:
        return None
    return {
        "direction": direction,
        "pivot": "high" if is_high else "low",
        "previous_index": previous,
        "current_index": current,
    }


def _is_swing_high(candles: Sequence[Any], index: int, window: int) -> bool:
    value = float(candles[index].high)
    neighbours = [float(candles[item].high) for item in range(index - window, index + window + 1) if item != index]
    return all(value > item for item in neighbours)


def _is_swing_low(candles: Sequence[Any], index: int, window: int) -> bool:
    value = float(candles[index].low)
    neighbours = [float(candles[item].low) for item in range(index - window, index + window + 1) if item != index]
    return all(value < item for item in neighbours)
