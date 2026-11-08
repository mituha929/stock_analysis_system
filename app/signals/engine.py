"""V2.2 technical signals and category-based scoring.

This module intentionally consumes the V2.1 indicator output rather than
recalculating formulas.  Keeping indicators, signals, and scoring separate
makes each conclusion inspectable and avoids counting a single price move as
many independent signals.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.indicators.core import calculate_indicators
from app.signals.divergence import detect_divergences


def analyze_technical(candles: Iterable[Any]) -> dict[str, Any]:
    """Return V2.2 signals, explainable scores, and risk warnings for OHLCV."""
    indicators = calculate_indicators(candles)
    data = indicators["candles"]
    if len(data) < 2:
        raise ValueError("at least two candles are required for V2.2 analysis")
    latest, previous = len(data) - 1, len(data) - 2
    close, prior_close = data[latest].close, data[previous].close
    volume, prior_volume = data[latest].volume, data[previous].volume
    signals: dict[str, str] = {}
    reasons: list[str] = []
    warnings: list[str] = []

    ma = indicators["ma"]
    macd = indicators["macd"]
    kd = indicators["kd"]
    bollinger = indicators["bollinger"]
    volume_data = indicators["volume"]
    dmi = indicators["dmi_adx"]
    rsi_value = indicators["rsi"]["rsi14"][latest]
    atr_value = indicators["atr"]["atr14"][latest]

    _moving_average_signals(signals, reasons, close, prior_close, ma, latest, previous)
    _macd_signals(signals, reasons, macd, latest, previous)
    _kd_signals(signals, reasons, kd, latest, previous)
    _bollinger_signals(signals, reasons, bollinger, close, latest)
    _volume_signals(signals, reasons, volume_data, close, prior_close, volume, prior_volume, latest)
    _structure_signals(signals, reasons, data, close, volume_data, latest)
    divergences = detect_divergences(data, indicators["rsi"]["rsi14"], macd["dif"])
    _divergence_signals(signals, reasons, warnings, divergences)

    scores = _scores(
        close=close, ma=ma, macd=macd, kd=kd, dmi=dmi, rsi_value=rsi_value,
        roc_value=indicators["roc"]["roc12"][latest], volume_data=volume_data,
        data=data, signals=signals, latest=latest,
    )
    scores["risk_score"] = _risk_score(close, data, ma, indicators, rsi_value, atr_value, latest, warnings)
    summary = _summary(scores)
    _risk_warnings(rsi_value, atr_value, indicators, latest, warnings)
    warnings = _unique(warnings)
    action_evaluation = _action_evaluation(scores, summary, warnings)

    return {
        "indicators": indicators,
        "signals": signals,
        "divergences": divergences,
        "scores": scores,
        "summary": summary,
        "action_evaluation": action_evaluation,
        "reasons": _unique(reasons),
        "warnings": warnings,
    }


def _divergence_signals(
    signals: dict[str, str], reasons: list[str], warnings: list[str], divergences: dict[str, dict[str, int | str]],
) -> None:
    labels = {"rsi": "RSI", "macd": "MACD"}
    for indicator, divergence in divergences.items():
        direction = str(divergence["direction"])
        signals[f"{indicator}_divergence"] = direction
        label = labels[indicator]
        if direction == "bearish":
            warnings.append(f"{label} 頂背離：價格創較高高點，但 {label} 未同步創高")
        else:
            reasons.append(f"{label} 底背離：價格創較低低點，但 {label} 未同步創低")


def _moving_average_signals(signals: dict[str, str], reasons: list[str], close: float, prior_close: float, ma: dict[str, list[float | None]], latest: int, previous: int) -> None:
    ma5, ma10, ma20, ma60 = (ma[key][latest] for key in ("ma5", "ma10", "ma20", "ma60"))
    if _all_present(ma5, ma10, ma20, ma60):
        if ma5 > ma10 > ma20 > ma60:
            signals["ma_alignment"] = "bullish_alignment"
            reasons.append("均線呈多頭排列（MA5 > MA10 > MA20 > MA60）")
        elif ma5 < ma10 < ma20 < ma60:
            signals["ma_alignment"] = "bearish_alignment"
            reasons.append("均線呈空頭排列（MA5 < MA10 < MA20 < MA60）")
    current_ma20, prior_ma20 = ma["ma20"][latest], ma["ma20"][previous]
    if _all_present(current_ma20, prior_ma20):
        if prior_close <= prior_ma20 < close:
            signals["ma20_cross"] = "breakout"
            reasons.append("價格向上突破 MA20")
        elif prior_close >= prior_ma20 > close:
            signals["ma20_cross"] = "breakdown"
            reasons.append("價格向下跌破 MA20")


def _macd_signals(signals: dict[str, str], reasons: list[str], macd: dict[str, list[float | None]], latest: int, previous: int) -> None:
    dif, signal = macd["dif"], macd["signal"]
    if _all_present(dif[latest], signal[latest], dif[previous], signal[previous]):
        if dif[previous] <= signal[previous] < dif[latest]:
            signals["macd_cross"] = "golden_cross"
            reasons.append("MACD 出現黃金交叉")
        elif dif[previous] >= signal[previous] > dif[latest]:
            signals["macd_cross"] = "death_cross"
            reasons.append("MACD 出現死亡交叉")
        elif dif[latest] > signal[latest]:
            signals["macd_cross"] = "bullish"
        else:
            signals["macd_cross"] = "bearish"


def _kd_signals(signals: dict[str, str], reasons: list[str], kd: dict[str, list[float | None]], latest: int, previous: int) -> None:
    k, d = kd["k"], kd["d"]
    if _all_present(k[latest], d[latest], k[previous], d[previous]):
        if k[previous] <= d[previous] < k[latest]:
            signals["kd_cross"] = "golden_cross"
            reasons.append("KD 出現黃金交叉")
        elif k[previous] >= d[previous] > k[latest]:
            signals["kd_cross"] = "death_cross"
            reasons.append("KD 出現死亡交叉")
        else:
            signals["kd_cross"] = "bullish" if k[latest] > d[latest] else "bearish"


def _bollinger_signals(signals: dict[str, str], reasons: list[str], bollinger: dict[str, list[float | None]], close: float, latest: int) -> None:
    upper, lower = bollinger["upper"][latest], bollinger["lower"][latest]
    if upper is not None and close > upper:
        signals["bollinger"] = "upper_breakout"
        reasons.append("收盤價突破布林上軌")
    elif lower is not None and close < lower:
        signals["bollinger"] = "lower_breakdown"
        reasons.append("收盤價跌破布林下軌")


def _volume_signals(signals: dict[str, str], reasons: list[str], volume_data: dict[str, list[float | None]], close: float, prior_close: float, volume: int, prior_volume: int, latest: int) -> None:
    ratio_5, ratio_20 = volume_data["ratio_5"][latest], volume_data["ratio_20"][latest]
    if ratio_20 is not None and ratio_20 >= 1.5:
        signals["volume_spike"] = "above_20d_1_5x"
        reasons.append("成交量高於 20 日均量 1.5 倍")
    elif ratio_5 is not None and ratio_5 >= 1.2:
        signals["volume_spike"] = "above_5d_1_2x"
        reasons.append("成交量高於 5 日均量 1.2 倍")
    if close > prior_close and volume > prior_volume:
        signals["price_volume"] = "price_up_volume_up"
        reasons.append("價漲量增")
    elif close > prior_close:
        signals["price_volume"] = "price_up_volume_down"
    elif close < prior_close and volume > prior_volume:
        signals["price_volume"] = "price_down_volume_up"
    elif close < prior_close:
        signals["price_volume"] = "price_down_volume_down"


def _structure_signals(signals: dict[str, str], reasons: list[str], data: list[Any], close: float, volume_data: dict[str, list[float | None]], latest: int) -> None:
    if latest < 20:
        return
    prior_window = data[latest - 20:latest]
    resistance, support = max(item.high for item in prior_window), min(item.low for item in prior_window)
    ratio_20 = volume_data["ratio_20"][latest]
    if close > resistance:
        signals["structure"] = "breakout"
        reasons.append("收盤價突破近 20 根 K 線高點")
        if ratio_20 is not None and ratio_20 >= 1.5:
            signals["volume_breakout"] = "confirmed"
            reasons.append("突破獲得放量確認")
    elif close < support:
        signals["structure"] = "breakdown"
        reasons.append("收盤價跌破近 20 根 K 線低點")


def _scores(*, close: float, ma: dict[str, list[float | None]], macd: dict[str, list[float | None]], kd: dict[str, list[float | None]], dmi: dict[str, list[float | None]], rsi_value: float | None, roc_value: float | None, volume_data: dict[str, list[float | None]], data: list[Any], signals: dict[str, str], latest: int) -> dict[str, int]:
    trend = 50
    ma20, ma60, ma5 = ma["ma20"][latest], ma["ma60"][latest], ma["ma5"][latest]
    trend += _direction_points(close, ma20, 15) + _direction_points(ma20, ma60, 20) + _direction_points(ma5, ma20, 10)
    trend += {"bullish_alignment": 20, "bearish_alignment": -20}.get(signals.get("ma_alignment"), 0)
    trend += _direction_points(macd["dif"][latest], macd["signal"][latest], 15)
    trend += _sign_points(macd["histogram"][latest], 10)
    trend += _direction_points(dmi["plus_di"][latest], dmi["minus_di"][latest], 5)
    if (adx := dmi["adx"][latest]) is not None and adx >= 25:
        trend += 5

    momentum = 50
    momentum += {"golden_cross": 20, "death_cross": -20}.get(signals.get("kd_cross"), 0)
    momentum += _direction_points(kd["k"][latest], kd["d"][latest], 10)
    momentum += _sign_points(None if rsi_value is None else rsi_value - 50, 15)
    if rsi_value is not None and 50 <= rsi_value <= 70:
        momentum += 10
    momentum += _sign_points(roc_value, 15)
    histogram, prior_histogram = macd["histogram"][latest], macd["histogram"][latest - 1]
    if _all_present(histogram, prior_histogram) and abs(histogram) > abs(prior_histogram):
        momentum += 20 if histogram > 0 else -20

    volume_score = 50
    ratio_5, ratio_20 = volume_data["ratio_5"][latest], volume_data["ratio_20"][latest]
    if ratio_5 is not None:
        volume_score += 15 if ratio_5 >= 1.2 else -10
    if ratio_20 is not None:
        volume_score += 25 if ratio_20 >= 1.5 else -15
    volume_score += {"price_up_volume_up": 20, "price_up_volume_down": -15, "price_down_volume_up": -20, "price_down_volume_down": 5}.get(signals.get("price_volume"), 0)
    obv = volume_data["obv"]
    if latest and _all_present(obv[latest], obv[latest - 1]):
        volume_score += 15 if obv[latest] > obv[latest - 1] else -15 if obv[latest] < obv[latest - 1] else 0
    if signals.get("volume_breakout") == "confirmed":
        volume_score += 25

    structure = 50
    structure += {"breakout": 30, "breakdown": -30}.get(signals.get("structure"), 0)
    if signals.get("volume_breakout") == "confirmed":
        structure += 30
    structure += _direction_points(close, ma20, 15)
    if latest >= 20:
        support = min(item.low for item in data[latest - 20:latest])
        if support and close >= support and (close - support) / support <= 0.03:
            structure += 10

    trend, momentum, volume_score, structure = (_clamp(value) for value in (trend, momentum, volume_score, structure))
    return {
        "trend": trend,
        "momentum": momentum,
        "volume": volume_score,
        "structure": structure,
        "direction_score": round(trend * 0.40 + momentum * 0.30 + volume_score * 0.20 + structure * 0.10),
    }


def _risk_score(close: float, data: list[Any], ma: dict[str, list[float | None]], indicators: dict[str, Any], rsi_value: float | None, atr_value: float | None, latest: int, warnings: list[str]) -> int:
    score = 0
    if rsi_value is not None and rsi_value >= 70:
        score += 20
    elif rsi_value is not None and rsi_value <= 30:
        score += 15
    atr = indicators["atr"]["atr14"]
    if _all_present(atr_value, atr[latest - 1]) and atr_value > atr[latest - 1]:
        score += 20
    upper, lower, middle = (indicators["bollinger"][key][latest] for key in ("upper", "lower", "middle"))
    if latest >= 1 and _all_present(upper, lower, middle):
        prior_upper, prior_lower, prior_middle = (indicators["bollinger"][key][latest - 1] for key in ("upper", "lower", "middle"))
        if _all_present(prior_upper, prior_lower, prior_middle) and (upper - lower) / middle > (prior_upper - prior_lower) / prior_middle * 1.2:
            score += 20
    ma20 = ma["ma20"][latest]
    if ma20:
        distance = abs(close - ma20) / ma20
        score += 20 if distance > 0.10 else 10 if distance > 0.05 else 0
    if latest >= 5:
        change = abs(close / data[latest - 5].close - 1)
        score += 20 if change > 0.10 else 10 if change > 0.05 else 0
    return _clamp(score)


def _risk_warnings(rsi_value: float | None, atr_value: float | None, indicators: dict[str, Any], latest: int, warnings: list[str]) -> None:
    if rsi_value is not None and rsi_value >= 70:
        warnings.append("RSI 位於相對高檔，追價風險提高")
    elif rsi_value is not None and rsi_value <= 30:
        warnings.append("RSI 位於相對低檔，跌勢與反彈不確定性提高")
    prior_atr = indicators["atr"]["atr14"][latest - 1]
    if _all_present(atr_value, prior_atr) and atr_value > prior_atr:
        warnings.append("ATR 上升，近期波動風險提高")


def _summary(scores: dict[str, int]) -> dict[str, Any]:
    direction = _level(scores["direction_score"], ((80, "strong_bullish"), (65, "bullish"), (45, "neutral"), (30, "bearish")), "strong_bearish")
    risk = _level(scores["risk_score"], ((81, "high"), (61, "elevated"), (31, "normal")), "low")
    category_directions = [_bias(scores[key]) for key in ("trend", "momentum", "volume", "structure")]
    dominant = _bias(scores["direction_score"])
    matches = sum(value == dominant for value in category_directions) if dominant != "neutral" else max(category_directions.count("bullish"), category_directions.count("bearish"), category_directions.count("neutral"))
    agreement = "high" if matches == 4 else "medium" if matches >= 3 else "low"
    return {"direction": direction, "risk": risk, "agreement": agreement, "agreement_count": matches, "category_directions": category_directions}


def _action_evaluation(scores: dict[str, int], summary: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    """Build an auditable candidate score from deterministic technical results.

    This is deliberately separate from the language model.  Higher risk and
    explicit warnings reduce the direction score, while agreement only makes
    a small adjustment.  The thresholds are an initial rule set that must be
    backtested before any real-money use.
    """
    direction_score = scores["direction_score"]
    risk_score = scores["risk_score"]
    agreement_count = int(summary["agreement_count"])
    risk_penalty = round(risk_score * 0.25)
    warning_penalty = min(20, len(warnings) * 10)
    agreement_adjustment = {0: -10, 1: -8, 2: -4, 3: 0, 4: 5}.get(agreement_count, -10)
    action_score = _clamp(direction_score - risk_penalty - warning_penalty + agreement_adjustment)

    if agreement_count < 3:
        action_label = "訊號分歧，暫無明確訊號"
    elif risk_score >= 61:
        action_label = "風險偏高，暫無正向操作訊號"
    elif len(warnings) >= 2 and action_score >= 65:
        action_label = "訊號分歧，暫無明確訊號"
    elif action_score >= 80:
        action_label = "強勢偏多候選"
    elif action_score >= 65:
        action_label = "偏多候選"
    elif action_score >= 45:
        action_label = "中性，暫無明確訊號"
    elif action_score >= 30:
        action_label = "偏空候選"
    else:
        action_label = "強勢偏空候選"

    return {
        "available": True,
        "scoring_version": "v1",
        "action_score": action_score,
        "action_label": action_label,
        "components": {
            "trend": scores["trend"],
            "momentum": scores["momentum"],
            "volume": scores["volume"],
            "structure": scores["structure"],
            "direction_score": direction_score,
            "risk_score": risk_score,
        },
        "weights": {"trend": 0.40, "momentum": 0.30, "volume": 0.20, "structure": 0.10},
        "risk_penalty": risk_penalty,
        "warning_count": len(warnings),
        "warning_penalty": warning_penalty,
        "agreement_count": agreement_count,
        "agreement_adjustment": agreement_adjustment,
        "formula": "方向分數 - 風險分數×25% - 警告扣分 + 訊號一致性調整",
        "score_ranges": [
            {"minimum": 80, "maximum": 100, "label": "強勢偏多候選"},
            {"minimum": 65, "maximum": 79, "label": "偏多候選"},
            {"minimum": 45, "maximum": 64, "label": "中性，暫無明確訊號"},
            {"minimum": 30, "maximum": 44, "label": "偏空候選"},
            {"minimum": 0, "maximum": 29, "label": "強勢偏空候選"},
        ],
        "safety_rules": [
            "四類訊號少於三類同方向時，標示為訊號分歧。",
            "風險分數達 61 分以上時，不顯示正向操作訊號。",
        ],
        "disclaimer": "此評分僅依歷史技術指標計算，不代表未來績效，也不是買賣建議。",
    }


def _direction_points(left: float | None, right: float | None, points: int) -> int:
    if not _all_present(left, right):
        return 0
    return points if left > right else -points if left < right else 0


def _sign_points(value: float | None, points: int) -> int:
    if value is None:
        return 0
    return points if value > 0 else -points if value < 0 else 0


def _all_present(*values: float | None) -> bool:
    return all(value is not None for value in values)


def _clamp(value: int | float) -> int:
    return max(0, min(100, round(value)))


def _bias(score: int) -> str:
    return "bullish" if score >= 65 else "bearish" if score <= 44 else "neutral"


def _level(score: int, levels: tuple[tuple[int, str], ...], fallback: str) -> str:
    for threshold, label in levels:
        if score >= threshold:
            return label
    return fallback


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
