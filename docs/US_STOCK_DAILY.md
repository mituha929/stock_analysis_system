# 15 檔美股／ADR：收盤後完整日 K 自動更新

新增獨立背景排程，沿用 `.env` 的 MySQL，寫入 `capital_stock_analysis.external_us_stock_daily`。不需群益登入，也不需開啟網頁。API 必須持續執行；本機 `.env` 已啟用，使用者重新啟動 API 後載入。

## 時間與範圍

- 每個已核對的美股交易日，在正常收盤後 20 分鐘開始，背景約每分鐘檢查到期工作。
- 美國夏令期間通常為台灣時間次日 **04:20**；冬令通常 **05:20**；提前收盤日依實際收盤時間加 20 分鐘，不寫死每日時刻。
- 共 15 檔：TSM、NVDA、AMD、AVGO、MU、AAPL、QCOM、MSFT、AMZN、GOOGL、ASML、AMAT、UMC、ASX、CHT。
- 每檔讀取最近 251 個交易日，前一根用於計算最早一根的漲跌，驗證成功後 upsert 最近 **250 根完整日 K**。既有更早資料保留，250 不是資料庫儲存上限。
- 整個 250 根範圍同批重讀，讓來源修正及調整後收盤價保持同批口徑；調整資料仍是來源目前版本，不是歷史各時點的不可變版本。
- 只存 `session='regular'`；排除盤前、盤後及尚未收盤的新交易日。
- 每個商品各自成功一次。休市、週末不建立不存在的日 K；失敗商品退避重試，已成功的商品不再下載。
- 重啟只補最近到期工作，最近 250 根範圍涵蓋的停機缺日可一起補回；超過此範圍的長期停機不宣稱已完整補回。

此功能不會替 `external_us_index_daily` 或 `external_futures_night_daily` 建立更新排程。既有四指數／TXF1 半小時快照排程繼續使用自己的資料表與市場時段。

## 資料驗證與寫入

來源為 Yahoo Finance 公開 chart 歷史行情。逐檔核對代號、交易所、USD 幣別、商品類型、美東時區、來源正常行情時間，以及 251 個完整連續交易日。

價格須為正數且符合開高低收關係，成交量須為可儲存的非負整數，調整收盤價不可缺失；日期重複、缺少任何必要交易日、格式變動或來源尚未到收盤，都拒絕該檔本次更新。不得補 0、取絕對值或自行修正價格。

沿用既有價格小數 8 位、UTC 時間與 ADR 欄位；原始精度、公司行動及計算方法存於 `source_record_json`。ADR 比例沿用 2026-11-02 已核對的當前參考，並非每天重新核對，也不是歷史比例版本。未新增匯率資料或 ADR 折溢價計算。

新增小型完成紀錄表 `external_us_stock_daily_updates`：

| 欄位 | 意義 |
|---|---|
| `source`、`symbol`、`market_date` | 複合主鍵，識別每日每商品的完成工作 |
| `batch_id` | 對應本次 250 根的匯入批次 |
| `bar_count` | 本次提交根數，固定 250 |
| `first_market_date`、`last_market_date` | 此批次行情日期範圍 |
| `fetched_at_utc` | 來源取得完成時間 |
| `completed_at_utc` | 提交前填入的完成記錄時間，不宣稱是精確提交時間 |

250 根行情與完成標記在同一 InnoDB 交易提交，提交前逐欄讀回比對。任一失敗回滾，不留下「已完成但行情沒寫入」的標記。

每日表沿用 `(source,symbol,market_date,session)` 唯一鍵；MySQL 商品鎖防止不同程序同商品並行提交。即使本機紀錄遺失，仍依資料庫完成標記避免重寫。若提交回覆中斷，下輪先查完成標記，保存時也會在商品鎖內重查，避免不明提交後重複寫入。

首次運行需對這兩張表有 CREATE TABLE、SELECT、INSERT、UPDATE 及 GET_LOCK 權限；不建立正式資料庫、不刪既有行情、不修改群益回補表。既有欄位或唯一鍵不相容時停止該次寫入，不自動 ALTER 修補。

## 設定

```dotenv
CAPITAL_US_STOCK_DAILY_ENABLED=true
CAPITAL_US_STOCK_DAILY_CLOSE_DELAY_MINUTES=20
CAPITAL_US_STOCK_DAILY_RETRY_SECONDS=600
CAPITAL_US_STOCK_DAILY_STATE_FILE=./data/us_stock_daily_schedule.json
```

預設程式值為關閉；本機 `.env` 已設 true，範本 `.env.example` 仍預設 false。環境變數優先於 `.env`，修改設定後需重新啟動 API。

失敗從 10 分鐘開始退避至 20、40 分鐘，之後最多每小時重試；成功商品不陪同重抓。資料庫暫時不可用時，先等待設定的重試時間，不先下載所有商品後再失敗。

本機狀態檔只保存逐檔進度與最近 100 筆結果；使用原子替換、獨立檔案鎖，不與群益 DLL 連線鎖共用。紀錄毀損或日曆年度尚未核對時保留行情，狀態端點顯示錯誤；不默默清空紀錄。應先檢查並保留原紀錄，再做人工修復。

日曆沿用 `config/external_market_sessions.json`，NYSE 目前核對 2025～2028。2025 為回查 250 根所需，包含 1 月 9 日 Carter 追悼休市及 7 月 3 日、11 月 28 日、12 月 24 日提前收盤。臨時停市或未核對年度須依官方更新日曆。

日曆來源：[NYSE 時段與日曆](https://www.nyse.com/trade/hours-calendars)、[NYSE 2025～2027 官方休市及提前收盤表](https://ir.theice.com/press/news-details/2024/NYSE-Group-Announces-2025-2026-and-2027-Holiday-and-Early-Closings-Calendar/default.aspx)、[2025-01-09 官方特別休市公告](https://ir.theice.com/press/news-details/2024/The-New-York-Stock-Exchange-Will-Close-Markets-on-January-9-to-Honor-the-Passing-of-Former-President-Jimmy-Carter-on-National-Day-of-Mourning/default.aspx)。

## 唯讀狀態查詢

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/external-market-quotes/us-stocks/schedule" |
    ConvertTo-Json -Depth 12
```

端點只讀小型本機紀錄與市場日曆，不連 MySQL、不啟動下載。提供設定、應更新的美東交易日、收盤與排程時間、下次排程時間，以及逐檔完成／失敗、重試時間和最近紀錄。

- `completed`：該商品本次 250 根與完成標記成功提交。
- `already_completed`：本機或 MySQL 已確認當日成功，不重抓。
- `failed`、`retry_wait`：來源／寫入失敗及等待，不代表行情為 0。
- `commit_uncertain`：提交回覆不明，下輪先核對資料庫完成標記。
- `database_unavailable`、`database_retry_wait`：資料庫暫時不可用。
- `state_or_calendar_error`：狀態檔或日曆不能使用，沒有繼續抓取。

日 K 是來源提供的正常交易時段日行情，不是交易所結算資料或永不修訂的官方認證收盤。選股端仍應確認資料日期是否為預期最近時段，不能只看到排程啟用就宣稱最新日 K 已存在。

## 驗證方式

```powershell
# 離線：真實 MySQL、COM、DLL 被封鎖；只用合成來源與暫存目錄
.\.venv\Scripts\python.exe -B -X utf8 scripts\run_worker_tests_offline.py test_us_stock_daily test_external_market_quote_scheduler test_external_market_quotes

# 真實 MySQL：只使用本次 us_daily_test_<UUID> 測試庫，最後刪除
.\.venv\Scripts\python.exe -B -X utf8 scripts\test_us_stock_daily_mysql.py
```

2026-11-03 驗證：完整離線回歸 981 項、隔離 MySQL 驗收 12 項通過，測試庫已刪除；15 檔各實抓 250 根並驗證成功，實抓未寫正式資料。尚未自行重啟 API，新的背景排程由使用者重啟後載入。

## 給選股端的時間規則

使用 `market_date`、`session_close_utc`、`fetched_at_utc` 及來源版本共同判斷。新排程不會把取得時間回填成更早的日期，也不替既有資料增加歷史版本還原能力。當次來源格式、事件清單與調整序列仍可能修訂，嚴格歷史重播須另保存決策當時的輸入版本。

關聯文件：[既有外部報價快照與品質](EXTERNAL_MARKET_QUOTES.md)、[官方台股基本資料](STOCK_MASTER.md)。
