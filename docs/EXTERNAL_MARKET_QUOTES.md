# 美股指數與台指期夜盤快照

自動排程每 30 分鐘分別檢查 DJI／INX／IXIC／SOX 及 CMoney TXF1 夜盤。每個商品獨立驗證、寫入及重試，不需群益登入。自行安全重啟 API 後載入新版本，API 須持續執行。

15 檔美股／ADR 已另加入[收盤後完整日 K 更新](US_STOCK_DAILY.md)，成功商品每日一次，寫入 `external_us_stock_daily`，不是本頁的半小時快照排程。指數／台指期夜盤歷史日表仍未加入更新。

## 交易時段

| 市場 | 正常時段 | 特殊情況 |
|---|---|---|
| 台指期夜盤 | 台灣時間開市日 15:00～次日 05:00 | 按夜盤開始日判斷休市；保留星期六凌晨及連假前最後夜盤 |
| 美股指數 | 美東 09:30～16:00 | `America/New_York` 自動換算夏令／冬令時間，處理美股休市與 13:00 提前收盤 |
| 收盤檢查 | 收盤後 20 分鐘 | 已知來源時間尚未到收盤時，每 5 分鐘重查，至收盤後 90 分鐘停止；時間未知不無限重試 |

交易中以開盤時間對齊每 30 分鐘。休市期間不反覆抓取；啟動後可檢查最近一個已結束時段的最新快照一次。失敗重試及收盤檢查互相獨立，不會使另一商品停止。定時檢查最多約 15 秒延遲；HTTP／資料庫耗時會另外影響開始時間。

官方核對清單：`config/external_market_sessions.json`。目前 TAIFEX 核對 2026，NYSE 核對 2025～2028；未核對年度顯示 `calendar_unverified`，停止該市場抓取並保留行情，不猜測休市日。此檔可自行新增經官方確認的日期／年度，修改後下一輪讀取；錯誤格式不會覆寫行情。

- `closed_dates`：依該市場當地日期填入 `YYYY-MM-DD`，TAIFEX 指夜盤開始日。
- `session_overrides`：例如 `"2026-11-27": {"close":"13:00"}`；可用 `closed:true` 取消當日開始的場次，或 `closed:false` 明確開市。
- `verified_years`：只有整年度官方行事曆核對完成才加入。`source_urls` 保留核對來源。
- 不把台北停班停課或台股日曆直接當作期貨／美股日曆。臨時公告、特別停市及盤中中止仍須依官方更新此檔。

來源：[期交所契約時段](https://www.taifex.com.tw/cht/2/tX)、[2026 期貨開休市表](https://www.taifex.com.tw/file/taifex/CHINESE/11/attach/台期交字第1140003036號函.pdf)、[期交所臨時公告](https://www.taifex.com.tw/cht/11/announcement)、[NYSE 日期及提前收盤](https://www.nyse.com/trade/hours-calendars)。2026-02-11 夜盤仍延續至 02-12 05:00；排程使用開始日，不宣稱開始日就是交易所歸屬交易日。

## 資料表與去重

- `capital_stock_analysis.external_market_quote_snapshots`：原有行情快照表，欄位不變，歷史保留。價格、漲跌、漲幅、開盤、高、低及夜盤金額／總量存於此表。來源相同時間與報價相同，只新增／更新檢查紀錄。數值相同但來源時間有推進，仍保存新報價；來源時間倒退標 `source_regressed`，保留較新的快照。
- `external_market_quote_checks`：新增的檢查紀錄，含商品、排程時段、檢查時間、嘗試次數、`updated`／`unchanged`／`source_regressed`／`failed`、對應快照、新鮮度、停機缺失範圍與錯誤類型。同一商品、排程時段唯一，重試更新次數，避免程序重啟重複寫入。
- TXF1 沒有可靠成交時間，僅在同一預期夜盤內按數值去重；不同夜盤保留，即使值相同。這不代表已證明當夜有新成交。
- 個別商品的快照與成功檢查在同一交易提交。專用檔案鎖及 MySQL 商品鎖防止並行寫入。提交結果不確定時保留 `needs_review`，不重複提交該商品、時段；其他商品繼續。
- 日期欄位在 MySQL 以 UTC 儲存，台灣時間加 8 小時。報價歷史不自動刪除；本機排程檔只保留最近 100 筆結果及五個商品狀態。舊 v1 排程紀錄自動保留並升級為 v2。

檢查狀態 `updated` 代表資料已變更；**不等同即時報價或當日已收盤**。

## 報價時間與品質

| `freshness` | 意義 |
|---|---|
| `current_session` | 報價屬目前時段且未超過允許延遲 |
| `latest_session` | 目前休市，來源時間屬最近時段，可能是數天前 |
| `delayed` | 交易中來源時間落後超過設定秒數，預設 45 分鐘 |
| `stale` | 來源時間早於預期時段 |
| `unknown_time` | 來源沒有可靠成交時間，TXF1 目前使用此狀態 |
| `future_time`／`outside_session_time` | 來源時間異常／超出預期時段容許範圍 |
| `calendar_unverified` | 無法確認市場日曆，不能判定新鮮度 |

`checked_at_utc`／`fetched_at_utc` 是檢查／取得時間，`quote_at_utc` 才是來源報價時間。TXF1 的 `quote_at_utc` 保留 NULL；頁面 `dateModified` 不當成成交時間。`quote_age_seconds` 提供實際年齡，`usable_for_expected_session=false` 表示不能當作該預期時段的有效新行情。

收盤檢查用 `close_status` 區分 `pending_source_update`、`source_time_verified`、`time_unknown`、`unverified`；`source_time_reached_close` 只表示來源時間到達收盤時間。網站快照不是交易所正式收盤／結算資料，`official_close_confirmed` 保持 false，不補造來源交易日。

來源：[鉅亨主要指數](https://invest.cnyes.com/indices/major) 的公開 quote API，以及 [CMoney TXF1 夜盤](https://www.cmoney.tw/forum/futures/TXF1?s=p) 的 JSON-LD 與 HTML 摘要面板逐欄核對。缺值、403 或格式變動拒絕該商品資料，不用 0／範例數字代替。`change_percent=1` 代表 1%；`amount_yi` 單位為頁面「億」；`total_volume` 不推算成張或口。

## 設定與查詢

專案 `.env` 已啟用 30 分鐘；其他新增設定未填則使用下列預設。程序環境變數優先於 `.env`，變更環境設定須自行重啟 API。

```dotenv
CAPITAL_EXTERNAL_QUOTES_ENABLED=true
CAPITAL_EXTERNAL_QUOTES_INTERVAL_MINUTES=30
CAPITAL_EXTERNAL_QUOTES_RETRY_SECONDS=300
CAPITAL_EXTERNAL_QUOTES_STATE_FILE=./data/external_market_quotes_schedule.json
CAPITAL_EXTERNAL_QUOTES_CALENDAR_FILE=./config/external_market_sessions.json
CAPITAL_EXTERNAL_QUOTES_CLOSE_DELAY_MINUTES=20
CAPITAL_EXTERNAL_QUOTES_CLOSE_RETRY_MINUTES=90
CAPITAL_EXTERNAL_QUOTES_MAX_AGE_SECONDS=2700
```

```powershell
# 設定、逐商品結果、新鮮度、最近紀錄；不連 MySQL／不觸發抓取
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/external-market-quotes/schedule" | ConvertTo-Json -Depth 12
# 唯讀查 MySQL 最新快照及最後檢查；重新計算目前新鮮度
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/external-market-quotes/latest" | ConvertTo-Json -Depth 12
```

API 關閉、電腦睡眠期間的快照無法從即時頁面還原。重新啟動只取得最新一筆，`missed_observations`／`missed_range` 記錄本時段跳過的排程點，`previous_check_at` 表示上次檢查；不宣稱跨場次／整段停機資料完整。這些資料不能直接當作完整 30 分鐘 OHLCV。

原一次性相容 CLI `scripts/fetch_external_market_quotes.py` 保留完整五筆驗證及 `--write` 全批寫入模式；不影響本排程的獨立時段成功紀錄與去重。新自動排程採逐商品寫入。沒有來源服務保證，網站變動須重新核對解析器。

## 驗證

```powershell
.\.venv\Scripts\python.exe -B -X utf8 scripts\run_worker_tests_offline.py test_external_market_quotes test_external_market_quote_scheduler
.\.venv\Scripts\python.exe -B -X utf8 scripts\test_external_market_quotes_mysql.py
```

後者只在本次隨機建立的 MySQL schema 寫入合成資料，結束後刪除本次測試庫，不改正式行情。涵蓋精確數值、交易回滾、逐商品檢查、去重、來源時間倒退及不同夜盤保留。

2026-11-02 改版驗收：完整離線回歸 961 項通過，真實 MySQL 隔離驗收 19 項通過，本次測試庫已刪除。台灣時間 01:14 五個公開來源逐一實抓成功；美股標為 `latest_session`（來源為 10/30 美東／10/31 台灣時間），TXF1 標為 `unknown_time`。報告 `output/diagnostics/external-quote-v2-live-20261102-011402.json`。本次實抓不寫正式資料、不更動正式排程紀錄、不重啟 API、不登入群益；新版由使用者自行重啟 API 後生效。
