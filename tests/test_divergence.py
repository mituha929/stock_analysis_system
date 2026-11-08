from datetime import datetime, timedelta, timezone
import unittest

from app.indicators.core import normalize_candles
from app.signals.divergence import detect_divergences


def candles_from_high_low(highs, lows):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return normalize_candles([
        {
            "timestamp": start + timedelta(days=index), "open": (high + low) / 2,
            "high": high, "low": low, "close": (high + low) / 2, "volume": 1000,
        }
        for index, (high, low) in enumerate(zip(highs, lows))
    ])


class DivergenceTests(unittest.TestCase):
    def test_detects_bearish_rsi_and_macd_divergence_at_swing_highs(self):
        candles = candles_from_high_low(
            [8, 9, 12, 9, 8, 9, 14, 10, 8],
            [5, 5, 6, 5, 4, 5, 7, 5, 4],
        )
        rsi = [None, None, 80, None, None, None, 70, None, None]
        macd = [None, None, 2.0, None, None, None, 1.0, None, None]
        result = detect_divergences(candles, rsi, macd)
        self.assertEqual(result["rsi"]["direction"], "bearish")
        self.assertEqual(result["macd"]["direction"], "bearish")
        self.assertEqual(result["rsi"]["previous_index"], 2)
        self.assertEqual(result["rsi"]["current_index"], 6)

    def test_detects_bullish_divergence_at_swing_lows(self):
        candles = candles_from_high_low(
            [12, 11, 10, 11, 12, 10, 9, 10, 11],
            [8, 7, 4, 7, 8, 6, 3, 6, 7],
        )
        rsi = [None, None, 20, None, None, None, 30, None, None]
        macd = [None, None, -2.0, None, None, None, -1.0, None, None]
        result = detect_divergences(candles, rsi, macd)
        self.assertEqual(result["rsi"]["direction"], "bullish")
        self.assertEqual(result["macd"]["direction"], "bullish")

    def test_requires_confirmed_pivots_and_aligned_series(self):
        candles = candles_from_high_low([8, 9, 12, 9, 8], [5, 5, 6, 5, 4])
        self.assertEqual(detect_divergences(candles, [50] * 5, [0] * 5), {})
        with self.assertRaises(ValueError):
            detect_divergences(candles, [50] * 4, [0] * 5)


if __name__ == "__main__":
    unittest.main()
