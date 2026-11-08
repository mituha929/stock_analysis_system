# V3.5 選股引擎與 V3.6 排序

## 使用方式

重啟 API，開啟 `/docs` →「V3 選股與排序」→ `POST /api/v1/screener/run`。不需要登入群益，但 MySQL 需有商品主檔與對應 K 線。

先用指定股票測試：

```json
{
  "universe": {
    "kind": "symbols",
    "markets": [0, 1],
    "symbols": ["2330", "2454"]
  },
  "history_interval": "day",
  "candle_table": "candles",
  "history_bars": 360,
  "rule": {
    "field": "price",
    "operator": ">",
    "value": {"field": "ma20"}
  },
  "limit": 20
}
```

上市櫃預篩、技術条件及排序範例：

```json
{
  "universe": {"kind": "markets", "markets": [0, 1]},
  "prefilter": {"min_price": 20, "min_volume": 1000},
  "history_interval": "day",
  "history_bars": 360,
  "rule": {
    "logic": "AND",
    "rules": [
      {"field": "direction_score", "operator": ">=", "value": 65},
      {"field": "risk_score", "operator": "<=", "value": 60}
    ]
  },
  "sort": [
    {"field": "matched_count", "direction": "desc"},
    {"field": "direction_score", "direction": "desc"},
    {"field": "risk_score", "direction": "asc"}
  ],
  "limit": 100,
  "timeout_seconds": 120
}
```

上述數字為操作範例，並非系統認定適用於所有策略的條件。

## 股票範圍與預篩

- `kind=markets`：預設上市櫃 `[0,1]`，可加入興櫃 `4`。
- `kind=symbols`：指定代號，去空白、大寫、去重；仍與市場及分類篩選取交集。
- `kind=watchlist`：取得現有服務的自選股，不必登入。空自選股回傳空結果，不擴大成全市場。
- `category_nos`：群益分類代碼篩選。`GET /api/v1/screener/categories` 列出整張商品主檔的市場、分類名稱及商品數。這不是另一份產業資料庫，不保證每個分類都是產業。
- 僅選取有效商品，排除既有系統排除的分類 94／98。主檔中其他商品類型仍可能出現，若只要特定類型應加入分類篩選。
- `candles` 對應 `instruments`，只接受市場 0／1／4；`odd_lot_candle` 對應 `odd_lot_instruments`，只接受 5／6。不可在同一次掃描混合整張與零股。
- `prefilter` 支援 `min_price/max_price/min_volume/max_volume`，邊界包含。使用最新已儲存 K 線的收盤價及成交量；成交量單位沿用儲存資料，程式不另行換算股／張。
- 先讀最新一根做預篩，通過後才讀取完整分析區間與執行 V2。沒有 K 線的商品列為 missing_data。

若同一 symbol 存在多個市場，因 K 線以 symbol／interval 儲存，只分析一次，保留 `markets` 清單並以主檔排序中的第一個市場作為 `market_no`。指定但不在範圍內的代號回報 outside_universe，不會悄悄忽略。

## 排序契約

只對根規則 `matched=true` 的股票排序。巢狀 AND／OR、交叉、缺值、符合原因沿用 V3.1 共用引擎。

預設依符合葉條件數降冪、方向分數降冪、風險分數升冪，屬於依序比較，不是把三個數字加成一個分數。純 AND 全符合時符合數可能相同；OR 可有不同符合數。

自訂 sort 欄位：`matched_count/direction_score/risk_score/price/volume/symbol`，direction 為 asc／desc，不可重複欄位。缺少分數始終排在有數值之後，無論升冪或降冪。最後同分依股票代號、市場別固定順序。

`limit`（1–1000，預設100）在整個已處理範圍排序後套用，不在找到前幾檔時停止。回傳 `rank`，並以 `matched_count/returned_count/truncated` 說明是否截取結果。

## 回應與限制

- `items`：符合股票、排名、主檔資訊、價格／成交量、方向／風險分數、條件數、逐條 details、reasons、K 線數及最新資料時間。
- `universe_count/processed_count/analyzed_count`：範圍數、已處理數、實際執行技術分析數。
- `excluded`／`excluded_counts`：預篩排除、無資料、資料不足、規則不符、個別分析錯誤；outside_universe 另計。
- `complete/ranking_complete`：是否掃完整個股票範圍。`status=partial` 時排序僅代表已處理部分，`remaining_symbols` 列未處理股票。
- `timeout_seconds`（1–300，預設120）：在股票之間檢查時間預算；正在執行的 SQL／分析不會強行中斷，因此不是 HTTP 硬性超時。
- 同一 API 行程一次只執行一個掃描，其他掃描回傳409。資料庫連線或查詢失敗回傳503，不當成「沒有符合股票」。
- 每次掃描共用單一 MySQL REPEATABLE READ 唯讀快照，結束回滾並關閉；不修改主檔／K 線，不登入或呼叫群益 DLL。
- `history_bars` 2–2000，預設360；未滿20根不提供評分。資料不足的指標仍為缺值，規則判斷沿用三值邏輯。
- V2 單股分析端點可能讀取更多 K 線；EMA、OBV、評分等受歷史起點影響，只有同一資料範圍下才適合精確比對。
- 只使用已儲存資料，不保證最新或已收盤，各股最後資料日期可能不同；請看每筆 latest_candle_at。需要新資料時先使用既有回補流程。
- 股票範圍超過10000檔會拒絕，請縮小市場／分類。同步 API 暫不提供工作佇列、進度查詢、取消或快照續跑。

此選股端點不建立提醒、不產生通知、不儲存策略、不呼叫 AI。Saved Screener（開發順序V3.7）已另行完成，見 [儲存選股策略](V3_SAVED_SCREENERS.md)；目前使用 `/docs` 操作，完整選股 GUI 屬後續整合。

## 驗證

2026-09-15：118 項自動測試通過（新增16項），涵蓋範圍、空自選股、預篩跳過分析、缺資料、排序方向／同分／null、先排序後limit、部分掃描、API 錯誤及唯讀 SQL 參數化／資源釋放。

已用真實 MySQL 的2330、2454完成讀取最新360根、規則判斷及排序測試，2檔全部處理完成。本次未寫入行情資料。

另以主檔市場0／1的全部2540檔商品進行真實唯讀掃描，使用 Direction Score >=65 AND Risk Score <=60，32.688秒完成，449檔符合，排序後回傳前5筆；2090檔不符、1檔資料不足、0檔分析錯誤。數量及耗時僅代表本次資料庫快照，不是後續執行保證。
