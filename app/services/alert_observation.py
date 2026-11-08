"""Data adapters for alerts; no DLL login or subscriptions are performed here."""
from datetime import timezone, timedelta

from app.api.routes import analysis
from app.rules.context import build_context
from app.rules.engine import evaluate
from app.rules.schemas import number
from app.schemas.alerts import AlertCreate


def candle_epoch(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone(timedelta(hours=8)))
    return value.timestamp()


def observe_alert(alert, capital, now):
    config = AlertCreate.model_validate(alert["config"])
    if config.source == "mysql":
        result = analysis(config.symbol, history_interval=config.history_interval, candle_table=config.candle_table)
        current, previous = build_context(result)
        evaluated = evaluate(config.rule, current, previous)
        evaluated["warnings"] = [*result.warnings, "使用已儲存 K 線，未驗證新鮮度或是否收盤。"]
        return {"data_at": candle_epoch(result.latest_candle_at), "result": evaluated}
    if not capital.quote_ready:
        raise ValueError("群益報價尚未就緒；請先登入並訂閱此自選股")
    quote = capital.cache.get_quote(config.symbol)
    if quote is None or not number(quote.price):
        raise ValueError("尚無可用報價；請確認已訂閱此自選股")
    stamp = quote.updated_at
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    data_at = stamp.timestamp()
    if not 0 <= now - data_at <= config.quote_max_age_seconds:
        raise ValueError("報價已過期或時間異常")
    values = {"price": quote.price, "volume": quote.volume if number(quote.volume) else None,
              "change_percent": (quote.price / quote.prev_close - 1) * 100 if number(quote.prev_close) and quote.prev_close else None}
    result = evaluate(config.rule, values, alert["previous_values"])
    return {"key": f"quote:{data_at}:{values}", "data_at": data_at, "values": values, "result": result}
