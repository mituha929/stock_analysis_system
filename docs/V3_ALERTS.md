# V3.3 提醒引擎

本階段提供單股或少量股票持續監控、False→True 邊緣觸發、冷卻與重設，以及持久化觸發歷史。V3.4 已另加入 [通知中心及瀏覽器系統通知](V3_NOTIFICATIONS.md)。

## 快速開始

重啟 API 後，開啟 `/docs` 的「V3 提醒引擎」。MySQL 需已儲存對應股票 K 線。以下請求建立「2330 收盤價高於 MA20」提醒：

`POST /api/v1/alerts`

```json
{
  "name": "台積電站上 MA20",
  "symbol": "2330",
  "source": "mysql",
  "history_interval": "day",
  "candle_table": "candles",
  "enabled": true,
  "check_interval_seconds": 60,
  "cooldown_seconds": 300,
  "rule": {
    "field": "price",
    "operator": ">",
    "value": {"field": "ma20"}
  }
}
```

取得回傳的 `id`，使用 `POST /api/v1/alerts/{id}/check` 立即檢查。建立之後背景監控也會自動執行，手動檢查不繞過停用、冷卻或去重。

檢查回應包含 `alert`（完整狀態）、`event`（這次新觸發的紀錄，沒有則 null）與 `skipped`（若停用、尚未到期、並行修改或刪除則說明）。

**首次檢查即使符合也不觸發**：它只建立基準。例如先觀察到 2410 < MA20 2411，不符合；後續資料更新為收盤價高於新的 MA20，才會建立觸發紀錄。如果測試期間資料一直沒改變，沒有事件是正常行為。

可用 `GET /api/v1/alerts/{id}` 查看 `last_result`、`last_error`、`last_match`、`state`、`trigger_count`。用 `GET /api/v1/alerts/events?alert_id={id}` 查看歷史。`last_error` 不為 null 表示資料取得或檢查發生問題，不能僅依 HTTP 200 判斷資料成功。

## 端點

| 方法／路徑（共同前綴 `/api/v1/alerts`） | 用途 |
|---|---|
| POST 空路徑 | 建立提醒，201 |
| GET 空路徑 | 列出提醒及狀態 |
| GET `/{id}` | 查看詳細狀態 |
| PUT `/{id}` | 用完整設定更新提醒並重新建立基準 |
| PATCH `/{id}/enabled` | 傳 `{"enabled": false}` 停用，true 恢復 |
| POST `/{id}/reset` | 清除基準，保留冷卻期限及歷史 |
| POST `/{id}/check` | 立即檢查，保存狀態及可能的事件 |
| DELETE `/{id}` | 刪除提醒，204；保留歷史 |
| GET `/events` | 依新到舊查詢歷史；可指定 alert_id、limit（1–500） |

未知 id 回傳 404，無效設定回傳 422，提醒儲存無法使用回傳 503。最多保存 100 個提醒；停止監控可先停用提醒。

## 狀態與去重

- `WAITING`：等待首次有效基準或下一個上升邊緣。
- `TRIGGERED`：當次檢查建立事件；下次檢查進入 `COOLDOWN`。
- `COOLDOWN`：截至 `cooldown_until` 禁止新事件，但有效結果仍更新 `last_match`。
- `RESET`：冷卻結束，若仍符合則等待不符合；觀察到不符合後回到 `WAITING`。
- 冷卻內已觀察到 false，冷卻後第一次 true 可以觸發。冷卻內 false→true 被抑制且消耗，不會冷卻結束後補發。
- 缺資料（insufficient_data）、讀取失敗與過期報價不改變最後有效 true／false，避免製造假邊緣。
- 相同報價样本不再更新判斷；MySQL 相同時間的修訂若改變結果仍可判斷。時間倒退的資料忽略。
- 修改設定、停用後恢復、手動 reset 均重建基準，不追補停用期間變化；同值 enabled 操作不重設基準。冷卻期限與歷史不會因 reset 被清掉。
- 原子交易共同保存狀態與事件；版本比對防止並行手動／背景檢查重複觸發，以及刪除／停用後仍提交過期檢查結果。

## 資料來源

### MySQL

支援 V3.1 全部規則欄位、週期與資料表，沿用 V2 分析。預設每 60 秒檢查，只有單股／少量股票，不做全市場掃描。不呼叫 DLL 或自動回補。

最新一根是**已儲存 K 線**，可能尚未收盤或不是最新交易日。此模式不驗證新鮮度，沿用 V3.2 限制並在結果 warnings 顯示；建立提醒本身不會讓 MySQL 自動更新。首次基準及後續判斷均以實際儲存內容為準。

### Quote

```json
{
  "name": "台積電價格突破",
  "symbol": "2330",
  "source": "quote",
  "check_interval_seconds": 1,
  "cooldown_seconds": 300,
  "quote_max_age_seconds": 60,
  "rule": {"field": "price", "operator": ">", "value": 2500}
}
```

需先使用既有 API 登入群益並將股票加入自選股訂閱。引擎只讀取 COM callback 已更新的快取，不自行登入、訂閱或下單。允許 `price`、`volume`、`change_percent`，不允許混入 MA 或技術評分。`volume` 是快取中的當日累積成交量，`change_percent` 對照昨收。

採背景輪詢（最短一秒），**不是逐 callback 即時判斷**，可能漏掉兩次輪詢間短暫變化；不可當成逐筆保證。交叉運算使用本提醒前一次觀察的有效快照，不等同交易所逐筆交叉。沒有可用報價、連線未就緒、報價超過設定期限或時間在未來時，記錄 last_error 並保留基準。

## 保存與生命週期

採 Python 標準庫 SQLite，預設 `capital_api/data/alerts.sqlite3`，可用 `CAPITAL_ALERT_DB` 指定路徑。它獨立於行情 MySQL，無需改動既有商品／K 線資料表。保存設定、狀態、冷卻、上次樣本、觸發歷史；資料檔已加入 Git 忽略。

API lifespan 啟動監控執行緒，關閉時停止並等待正在執行的檢查結束。API 關閉時不監控，重啟恢復最後狀態，不回放停機期間資料。SQLite 狀態可避免多個本地程序重複提交事件，但建議沿用群益 API 單一 worker；多 worker 仍會重複做資料運算。

每個提醒序列檢查，外部查詢緩慢時其他提醒可能延後。輪詢間隔是最短排程間隔，不是即時執行保證。觸發事件不含已讀或推送狀態；這些狀態由 V3.4 通知紀錄另行保存。

## 驗證

執行 `python -m unittest discover -s tests`。新增測試包含 False→True、首次 true、持續 true、冷卻內變化、unknown、錯誤恢復、重啟、樣本去重、倒退時間、設定管理、並行檢查、輪詢關閉、quote 新鮮度及 HTTP API。

2026-09-14：91 項測試全部通過，其中本次新增 21 項。使用暫存 SQLite 實際寫入／重新開啟，MySQL 與報價使用模擬資料；尚未實測正式行情變化觸發或通知推送。
