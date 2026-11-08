"""Adapt stored V2 candle series to current/previous rule snapshots."""
from app.rules.fields import FIELDS, SCORES, SERIES
from app.rules.schemas import number
from app.signals import analyze_technical


def build_context(analysis):
    candles = analysis.candles

    def snapshot(index):
        values = dict.fromkeys(FIELDS)
        for field, (group, key) in SERIES.items():
            series = analysis.indicators.get(group, {}).get(key, [])
            value = series[index] if 0 <= index < len(series) else None
            values[field] = value if number(value) else None
        candle = candles[index]
        values.update(price=candle.close, volume=candle.volume)
        if index > 0 and candles[index - 1].close:
            values["change_percent"] = (candle.close / candles[index - 1].close - 1) * 100
        if index >= 20:
            window = candles[index - 20:index]
            values["breakout_20d_high"] = candle.close > max(c.high for c in window)
            values["breakdown_20d_low"] = candle.close < min(c.low for c in window)
        for prefix, left, right in (("macd", "macd_dif", "macd_signal"), ("kd", "kd_k", "kd_d")):
            group, key = SERIES[left]
            right_key = SERIES[right][1]
            a = analysis.indicators.get(group, {}).get(key, [])
            b = analysis.indicators.get(group, {}).get(right_key, [])
            if index >= 1 and index < min(len(a), len(b)) and all(number(v) for v in (a[index - 1], b[index - 1], a[index], b[index])):
                values[f"{prefix}_golden_cross"] = a[index - 1] <= b[index - 1] and a[index] > b[index]
                values[f"{prefix}_death_cross"] = a[index - 1] >= b[index - 1] and a[index] < b[index]
        return values

    current, previous = snapshot(len(candles) - 1), snapshot(len(candles) - 2)

    def add_scores(values, scores, summary):
        for key in SCORES:
            # V2 uses short category names and explicit composite score names.
            values[key] = scores.get(key, scores.get(key.removesuffix("_score")))
        values["trend"] = summary.get("direction")

    if len(candles) >= 20:
        add_scores(current, analysis.scores, analysis.summary)
    if len(candles) >= 21:
        prior = analyze_technical(candles[:-1])
        add_scores(previous, prior["scores"], prior["summary"])
    return current, previous
