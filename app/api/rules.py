from fastapi import APIRouter

from app.api.routes import analysis
from app.rules.context import build_context
from app.rules.engine import evaluate
from app.rules.fields import TRENDS, field_metadata
from app.rules.schemas import RuleTestRequest

router = APIRouter(prefix="/api/v1/rules", tags=["V3 規則測試"])


@router.get("/fields", summary="取得規則欄位與運算子白名單")
def rule_fields():
    return {"fields": field_metadata(), "trend_values": TRENDS, "max_depth": 10, "max_nodes": 100}


@router.post("/test/{symbol}", summary="以 MySQL K 線測試單股條件", description="唯讀：使用最新兩根已儲存 K 線，不連線群益、不回補、不建立提醒。資料可能包含未收盤 K 線；回傳時間供確認。")
def test_symbol_rules(symbol: str, payload: RuleTestRequest):
    result = analysis(symbol, history_interval=payload.history_interval, candle_table=payload.candle_table)
    current, previous = build_context(result)
    return {
        "symbol": result.symbol, "name": result.name, "source": result.source,
        "interval": result.interval, "candle_table": result.candle_table,
        "candle_count": result.candle_count, "latest_candle_at": result.latest_candle_at,
        "previous_candle_at": result.timestamps[-2], "latest_written_at": result.latest_written_at,
        "warnings": [*result.warnings, "使用已儲存 K 線，未驗證資料新鮮度或是否收盤。"],
        **evaluate(payload.rule, current, previous),
    }
