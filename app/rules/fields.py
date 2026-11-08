"""Single field registry for validation, discovery and V2 adaptation."""

SERIES = {
    **{f"ma{p}": ("ma", f"ma{p}") for p in (5, 10, 20, 60, 120, 240)},
    **{f"ema{p}": ("ema", f"ema{p}") for p in (12, 26)},
    **{f"rsi{p}": ("rsi", f"rsi{p}") for p in (6, 12, 14)},
    "macd_dif": ("macd", "dif"), "macd_signal": ("macd", "signal"),
    "macd_histogram": ("macd", "histogram"),
    "kd_k": ("kd", "k"), "kd_d": ("kd", "d"),
    "adx": ("dmi_adx", "adx"), "atr14": ("atr", "atr14"),
    "roc12": ("roc", "roc12"),
    "bollinger_upper": ("bollinger", "upper"),
    "bollinger_middle": ("bollinger", "middle"),
    "bollinger_lower": ("bollinger", "lower"),
    "volume_ratio_5d": ("volume", "ratio_5"),
    "volume_ratio_20d": ("volume", "ratio_20"),
    "volume_ma5": ("volume", "volume_ma5"),
    "volume_ma20": ("volume", "volume_ma20"), "obv": ("volume", "obv"),
}
SCORES = ("trend_score", "momentum_score", "volume_score", "structure_score", "direction_score", "risk_score")
BOOLEANS = ("macd_golden_cross", "macd_death_cross", "kd_golden_cross", "kd_death_cross", "breakout_20d_high", "breakdown_20d_low")
FIELDS = {name: "number" for name in (*SERIES, *SCORES, "price", "volume", "change_percent")}
FIELDS.update({name: "boolean" for name in BOOLEANS})
FIELDS["trend"] = "string"
TRENDS = ("strong_bullish", "bullish", "neutral", "bearish", "strong_bearish")
OPERATORS = (">", ">=", "<", "<=", "==", "!=", "between", "cross_above", "cross_below")

# Bounds apply to literal thresholds, not to stored market observations.
# MACD, ROC, change_percent, OBV and Bollinger bands can legitimately be negative.
BOUNDS = {name: (0, 100) for name in (*SCORES, "rsi6", "rsi12", "rsi14", "kd_k", "kd_d", "adx")}
BOUNDS.update({name: (0, None) for name in (
    "price", "volume", "atr14", "volume_ratio_5d", "volume_ratio_20d",
    "volume_ma5", "volume_ma20", *(name for name in SERIES if name.startswith(("ma", "ema")) and not name.startswith("macd")),
)})


def field_operators(name):
    return OPERATORS if FIELDS[name] == "number" else ("==", "!=")


def field_metadata():
    return [{"field": name, "type": kind, "operators": list(field_operators(name)),
             "minimum": BOUNDS.get(name, (None, None))[0],
             "maximum": BOUNDS.get(name, (None, None))[1]}
            for name, kind in FIELDS.items()]
