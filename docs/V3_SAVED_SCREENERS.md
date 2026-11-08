# Saved Screener：儲存與重複執行策略

本功能對應規劃文件主題章節 V3.6 Saved Screener、末尾開發順序 V3.7。

## 建立策略

重啟 API，開啟 `/docs` 的「V3 儲存選股策略」，使用 `POST /api/v1/screeners`：

```json
{
  "name": "台積電與聯發科站上 MA20",
  "description": "從指定股票找出最新已儲存日K收盤價高於20日均線的商品",
  "configuration": {
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
}
```

`configuration` 就是先前 `POST /api/v1/screener/run` 的完整請求。省略的設定會以當前預設值展開並保存，包括預篩、排序、時間預算；以後重用時不會因單純省略欄位而改用新的預設。

建立只保存策略，不讀取 MySQL、不執行選股、不建立提醒或通知。不需要先登入群益。回傳 `id`、`name`、`description`、`configuration`、`revision=1`、UTC 建立／修改時間。名稱可以重複，策略用 id 區分。

## 查看與再次執行

- `GET /api/v1/screeners`：列出策略，支援 limit（1–100，預設50）、offset（預設0）。回傳 items 與 total。
- `GET /api/v1/screeners/{id}`：取得單一策略完整設定及 revision。
- `POST /api/v1/screeners/{id}/run`：不需 request body，直接使用已儲存策略執行。

執行回應保留既有選股結果格式，另加入 `saved_screener`，包含本次使用的策略名稱、版本與完整設定。資料不足、部分掃描、排序與 limit 等規則完全沿用 [V3 選股 API](V3_SCREENER.md)。直接執行與儲存策略執行共用同一個掃描鎖，忙碌時回傳409。

保存的是「條件」，不是股票結果或行情快照。每次執行會重新讀取當前 MySQL 資料；自選股策略使用當前自選股，市場／分類策略使用當前商品主檔。執行不修改策略版本。

執行開始時讀取一份策略設定快照；執行期間若策略被修改或刪除，本次仍使用原快照完成，回應中的 saved_screener 可用來辨識實際執行版本。它不會重新查詢已被改動的設定。

## 修改與刪除

`PUT /api/v1/screeners/{id}` 傳入完整的 name、description、configuration，另外帶上讀取到的 revision：

```json
{
  "name": "低風險偏多",
  "description": "上市櫃範圍",
  "expected_revision": 1,
  "configuration": {
    "universe": {"kind": "markets", "markets": [0, 1]},
    "rule": {
      "logic": "AND",
      "rules": [
        {"field": "direction_score", "operator": ">=", "value": 65},
        {"field": "risk_score", "operator": "<=", "value": 60}
      ]
    }
  }
}
```

成功後 revision 加1，created_at 保留，updated_at 更新。版本不符回傳409，避免兩個分頁覆蓋彼此修改；先 GET 最新設定後再編輯。PUT 是整份替換，configuration 省略的欄位重新採預設值，因此只想修改少數欄位時應保留 GET 得到的其他設定。

`DELETE /api/v1/screeners/{id}` 刪除策略，成功回傳204。只刪除该策略，不刪除行情、提醒、通知或其他策略；刪除後無內建復原。

## 保存、驗證及範圍

使用現有 SQLite 路徑 `CAPITAL_ALERT_DB`（預設 `data/alerts.sqlite3`），新增獨立 `saved_screeners` 表，API 啟動時自動建立。只要保存該資料檔，重啟後策略仍在。不修改行情 MySQL schema，不需額外套件。

策略名稱去除前後空白後不可為空，最多100字；說明最多2000字。巢狀規則、欄位白名單、資料表／市場、預篩與排序驗證重用 ScreenerRequest。格式錯誤回傳422，找不到策略回傳404，SQLite 或已存策略格式異常回傳503；後者不會靜默改用空規則執行。

本次完成 API 保存與重複執行，使用 `/docs` 操作；未加入策略 GUI、AI 建立／修改、自動排程、策略版本歷史或歷次選股結果保存。

## 測試

2026-09-15：128項自動測試通過（新增10項）。測試使用真實暫存 SQLite，驗證保存／重開資料庫、預設值保存、更新版本衝突、刪除隔離、分頁、已存資料損壞處理、HTTP API、按 ID 重用選股流程，以及執行時讀取當前自選股。選股資料來源以 mock 替代。

手動驗收：建立上方兩股策略 → 記下 id → GET 查看 → POST id/run → 重啟 API → GET 相同 id → 再執行。結果可能因行情更新而改變，策略設定應保持一致。

另已使用暫存策略資料庫與真實 MySQL 驗證：保存2330／2454的price > ma20策略、重新開啟策略資料庫、按ID執行，2檔處理完成、1檔符合，約0.032秒；重新載入的設定及revision與保存時一致。未向正式策略資料庫加入測試策略，也未修改行情資料。
