# V3.1 規則引擎與 V3.2 單股測試

本文件記錄已實作介面；階段編號依 V3 開發流程的「V3.20 最終開發順序」。

## 使用方式

依主 README 啟動 API，開啟 `/docs` 的「V3 規則測試」。資料需事先存入 MySQL；本 API 不呼叫群益 DLL、不回補、不寫入資料庫，也不建立提醒。

`GET /api/v1/rules/fields` 回傳完整欄位白名單、型態及每個欄位的可用運算子。`trend` 對應 V2 綜合方向分類，可能為 `strong_bullish`、`bullish`、`neutral`、`bearish`、`strong_bearish`；`bullish` 的相等條件不含 `strong_bullish`。

`POST /api/v1/rules/test/2330` 範例：

```json
{
  "history_interval": "day",
  "candle_table": "candles",
  "rule": {
    "logic": "AND",
    "rules": [
      {"field": "price", "operator": ">", "value": {"field": "ma20"}},
      {
        "logic": "OR",
        "rules": [
          {"field": "macd_golden_cross", "operator": "==", "value": true},
          {"field": "kd_golden_cross", "operator": "==", "value": true}
        ]
      },
      {"field": "volume_ratio_20d", "operator": ">=", "value": 1.5},
      {"field": "risk_score", "operator": "<=", "value": 60}
    ]
  }
}
```

`rule` 可直接是一條條件，也可以是 AND／OR 群組。預設日 K、`candles` 表；零股可指定 `odd_lot_candle`。週期支援 `1m/5m/15m/60m/day/week/month`，自訂資料表名稱沿用 V2 規則。

區間（含上下界）：

```json
{"rule": {"field": "rsi14", "operator": "between", "value": [30, 70]}}
```

欄位交叉：

```json
{"rule": {"field": "price", "operator": "cross_above", "value": {"field": "ma20"}}}
```

## 判斷契約

- 數值支援 `> >= < <= == != between cross_above cross_below`；布林及趨勢字串只支援 `== !=`。固定數字與數字字串不同，不接受 `"70"` 或布林代替數字。
- `value: {"field": "ma20"}` 表示欄位參照，雙方型態必須相同。`between` 只接受兩個有限數值，且下界不大於上界。
- 評分、RSI、KD、ADX 的固定門檻限制在 0–100；未知欄位、額外屬性、無效運算子、空群組、NaN／Infinity 都會拒絕。
- 最多 10 層、100 個節點（群組亦計入節點數）。輸入錯誤回傳 HTTP 422，且不讀資料庫。
- `cross_above`：前值 `<=` 前門檻，且現值 `>` 現門檻；`cross_below` 相反。固定值前後門檻相同，欄位參照則使用各期門檻。當期相等不算交叉。
- MACD／KD 交叉布林欄位由 V2 指標序列依上述契約判斷，不直接沿用 V2 文字交叉訊號。V2 既有 API 與評分公式保持原狀。
- 量比及突破欄位名称中的 `5d/20d` 為相容文件命名，實際使用所選週期的 5／20 根 K 線。突破比較最新收盤與前 20 根高／低點，不包含當根。
- 分數與 `trend` 至少需要 20 根 K 線；前一期評分需要至少 21 根，透過 V2 引擎對截止前一期資料計算。各指標依自己的暖機長度回傳缺值。
- 缺少必要數值時單條 `matched=null`、`status=insufficient_data`；缺值不會因 `!=` 或比較 `false` 而誤判成功。
- 群組採三值邏輯：AND 任一 false 即 false，否則有未知則未知；OR 任一 true 即 true，否則有未知則未知。根結果的 `matched` 僅在確定符合時為 true，其餘為 false，另以 `status` 區分不符與未知。
- 不短路略過條件：`total_rules` 計算全部葉節點；`matched_count` 計算符合的葉節點，OR 分支同樣計數；`unknown_count` 計算未知葉節點。

## 回應與資料時間

回傳 `symbol/name/source/interval/candle_table/candle_count/latest_candle_at/previous_candle_at/latest_written_at`，以及：

- `matched`、`status`、`matched_count`、`total_rules`、`unknown_count`。
- `reasons`：符合條件的原因。
- `details`：每個葉節點的路徑、欄位、運算子、設定值、實際值、比較目標、前期雙方數值、三值結果與原因。
- `warnings`：V2 警告及儲存資料限制。

使用最新兩根**已儲存** K 線，不自動判斷是否收盤，不承諾資料為最新；`latest_written_at` 是寫入時間，不是行情時間。這個階段沒有資料過期排除或盤中收盤確認機制。

沒有資料回傳 404，少於兩根 K 線回傳 422，MySQL 錯誤沿用 V2 的 503。

## 程式與驗證

- `app/rules/fields.py`：白名單及 V2 序列對照。
- `app/rules/schemas.py`：規則結構、型態與大小驗證。
- `app/rules/engine.py`：不依賴資料庫的共用判斷引擎。
- `app/rules/context.py`：V2 分析結果轉成最新／前一期欄位快照。
- `app/api/rules.py`：欄位查詢及單股測試 API，重用 V2 的資料读取與錯誤處理。
- `tests/test_rules.py`：比較邊界、交叉、巢狀計數、缺值、型態驗證、大小限制、V2 對照及實際 ASGI 請求／序列化測試。

2026-09-14：`python -m unittest discover -s tests` 共 70 項測試通過。資料庫以 mock 替代，未進行真實 MySQL 或群益登入驗證。未新增套件。

V3.3 Alert Engine 已另行實作，見 [提醒 API](V3_ALERTS.md)。規則測試端點仍保持唯讀，不會建立或更新提醒。使用者已回報 2330 真實 MySQL 單股 MA20 比較成功。
