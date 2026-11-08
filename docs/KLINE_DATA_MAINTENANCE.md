# K 線資料維護

## 功能

- `GET /api/v1/backfill/status`：立即回傳已有唯讀快照，過期時由背景單一讀取器更新；首次回應可暫無 items，請檢查 `database_refreshing`，不代表沒有商品。即時控制狀態與排程不使用資料庫快照快取，不呼叫群益 DLL。
- `POST /api/v1/backfill/refresh-stale`：按實際 K 線與完整同步進度建立每檔、每週期的初始化／增量工作。
- `POST /api/v1/backfill/retry-failed`：只重新排入每檔商品、每個週期最新且仍為 `failed` 的工作；舊的失敗歷史不重跑。
- `/data-status`：上述功能的操作頁，每 15 秒唯讀更新一次畫面；「每日自動更新」區塊顯示最近10次永久 Log。
- `POST /api/v1/backfill/pause`：目前區間成功寫入後安全暫停，不關閉 FastAPI。
- `POST /api/v1/backfill/resume`：接續已保存的工作；沒有本機計畫時，依 query 的 markets／intervals／target_bars 在背景重建增量／初始化範圍並回傳202。若群益未連線或今日休市，持久保留至符合條件後執行，不會因空控制檔而默默略過。
- `GET /api/v1/backfill/control`：讀取本機進度、暫停原因與記憶體資訊，不依賴 MySQL；資料庫故障時仍可查看。

狀態頁的「已追蹤工作／覆蓋率」是工作追蹤比例，不代表歷史 K 線已全部齊全。`data_through` 改為有資料商品實際最新 K 線日期的保守下限；週／月 K 的時間標籤可能位於該期間第一天。頁面另顯示目前商品、初始化／增量模式、查詢範圍及已提交游標。

表格最右側「平均抓取時間」以秒／檔顯示：同市場、同週期，各商品最新一次 `completed` 或 `insufficient_history` 工作的 `completed_at - started_at` 平均，包含該次 API 等待、節流及 MySQL 寫入，不包含開始前排隊。只納入 `attempt_count=1`、起訖時間完整且非負的工作；重試／續跑因既有紀錄只有最後一段起始時間而排除，避免低估。等待、執行中及失敗工作不納入；沒有樣本顯示「—」，滑鼠停留可看樣本數。初始化與增量工作會合併計算，故不能把此數值當作純 API 延遲。API欄位為 `average_fetch_seconds`、`average_fetch_sample_count`，沿用既有狀態查詢，不新增統計查詢或資料表。

## 初始化、增量與持久進度

回補起點、續抓與假日日 K 驗收已串接 `stock_master` 的可靠掛牌邊界；包含掛牌當日與所在週／月。轉上市歷史、既有掛牌前行情優先保留，掛牌歷史不足不冒充達標。詳見 [掛牌日期與回補邊界](LISTING_HISTORY_BOUNDARIES.md)。

日 K 另有最近1,200根逐日期完整性驗收，**交易日不執行，週末／已確認休市日才檢查與補缺口**；不以每日回抓10根或工作完成數當成歷史齊全。交易日不因舊驗收未通過阻擋其他週期，但原日 K 工作仍優先處理。休市日專用排程沿用原時間，不重排一般計畫、斷點或同步商品主檔；空資料逐步擴大、缺口持久化並針對日期寫入。清單與證據規則見 [日 K 完整性驗收](DAY_CANDLE_INTEGRITY.md)。明確自訂固定日期範圍的工作不擅自擴張。

選定範圍含 day 時，先跑各市場日 K 正常輪，再跑其他週期正常輪，最後處理所有週期的待補齊／較舊歷史及符合時段的日 K 驗收。日 K 未補完仍保留 waiting_backfill 與斷點，但不擋其他週期正常抓取；終止失敗／歷史不足規則及目標根數不變。商品數與缺少日期數分開統計，詳見 [現行流程](RECOVERY_LAST_AND_GAP_COUNTS.md)。

初始化／完整重抓目標為一般週期 1200 根、週線 560 根、月線 100 根。初始化從最近區間向前分段查詢，達標即停止；一般增量工作重查最後一個已儲存期間及缺少範圍。每日排程另使用下述日常回抓目標，不以資料庫已存根數抵算當次回抓。這些設定不會刪除或裁切資料庫既有歷史。

初次探查若到達設定的歷史範圍，或連續 `CAPITAL_BACKFILL_EMPTY_WINDOW_LIMIT` 個區間無資料，就標示 `insufficient_history` 並結束初始化；這是有界探查結果，不保證更早的歷史一定不存在。可用明確 `--start-date`／`--end-date` 再探查其他範圍。1200 根週線約需23年、月線約需100年，通常無法全部取得；不補造 K 線，也不每天重跑初始化。

新增 `candle_backfill_progress` 保存工作模式、下一區間、目標及擁有者；`candle_sync_state` 保存每檔、每週期已驗證完整同步的截止日。每段 K 線寫入與游標更新在同一 MySQL 交易提交：寫入失敗就一起回滾。舊的過長等待工作保留紀錄並標示 `superseded`，不再重新查詢數十年已存在的歷史。工作鎖避免兩個新版本 worker 同時領取或恢復彼此的工作；切換版本前先停止舊版回補。

本機 SQLite 控制檔只保存少量進度與設定，不保存帳密或 K 線。手動暫停會持久保存，只有使用者繼續才解除。非手動暫停的 API 工作在交易日每15秒檢查一次，待連線與資料庫恢復後續跑；程式意外關閉的工作也可在重開並登入後接續。個別商品錯誤保留為失敗，使用「重試失敗工作」處理。

群益歷史查詢維持串行與共用連線；HTTP 操作鎖不再涵蓋整批背景工作。預設間隔 0.8 秒是「上一次查詢完成至下一次查詢開始」的間隔，每次 API 查詢一次，不按回傳根數等待。API 與 CLI 共用設定；0.5 秒僅為可配置的候選值，尚未以真實群益長時間驗證。

週／月 K 的多段查詢按完整週／月邊界切段，避免同一根期間 K 線被拆成兩段後互相覆寫。最後一段截止於該期間最後交易日。狀態頁對 MySQL 查詢使用單一在途請求，避免每15秒累積尚未結束的查詢；本機進度查詢獨立進行，讀取資料庫期間仍可要求安全暫停。

狀態快照預設5秒內共用；過期時先顯示上次結果並在背景更新。每個 API 程序只用一個背景讀取執行緒，最多保留12組查詢快照／等待範圍，失敗15秒後才重試。回應的 `database_refreshing`、`database_error`、`database_query_seconds`、`database_cache_age_seconds` 可區分 HTTP 回應時間、背景 SQL 時間及資料新鮮度；資料庫故障不會把舊結果冒充最新。`refresh=true` 要求立即排入刷新，但不重複建立在途工作。

前端每5秒讀取本機控制、每15秒檢查狀態快照；背景更新期間亦沿用15秒間隔，不額外輪詢。背景快照完成後，於下一次定時更新顯示；仍可手動按重新整理立即查詢。資料庫讀取不會鎖住暫停／續跑按鈕，操作訊息也不會被自動刷新蓋掉。`control_version=2` 表示伺服器已載入這次修正；若版本較舊，頁面會提醒安排重啟。詳細量測見 [狀態頁效能與續跑修正](DATA_STATUS_PERFORMANCE.md)。

目前零股商品主檔及資料表仍保留，但官方歷史 K 線函式以整股成交計算。零股表名稱不代表已取得零股成交 K 線；真正零股來源尚待另行決定。

## 手動執行每日更新

API 重新啟動並登入群益後，在 PowerShell 執行：

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/backfill/daily/run"
```

回傳 202 表示已受理。系統先要求目前工作在區間邊界停止，成功提交已收到的資料後，才獨占工作鎖，將所有舊的 pending／running K 線工作存檔並標記 `superseded`，移出執行佇列。原工作與斷點快照保存在 MySQL `manual_daily_displaced_jobs`，原計畫保存在控制資料庫的 `manual_daily_displaced`；不刪除既有行情。失敗與待查證紀錄保留，舊工作不會自動重新加入佇列。

接著系統使用原每日設定同步商品及回抓各週期，同一天已完成的工作也會重新抓取，已存在的同時間 K 線依原有 upsert 規則覆寫。休市日與收盤前以最近已完成交易日為準。此操作不執行歷史日 K 完整性驗收，也不代表資料已完整。連線、記憶體或資料庫未就緒時會等待，符合條件後自動繼續。切換進行中或同一天的新工作仍正在執行時，重複送出會回傳同一識別碼；舊工作已暫停、等待補齊或跨日時，則建立新識別碼並安全取代舊佇列。回應的 `created` 表示是否新建，`replaces_request_id` 記錄被取代的請求。回應明確宣告 UTF-8，供 Windows PowerShell 正確顯示中文。

查詢狀態：

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/backfill/control" |
    Select-Object state, stage, worker_active, pause_reason, manual_daily_run
```

`manual_daily_run.state` 表示切換狀態，例如 `waiting_stop`、`parking`、`waiting_memory`、`waiting_connection`、`running`。切換途中若 API 重啟，會保留切換進度並在連線和記憶體就緒後繼續。資料狀態頁的每日更新 Log 與工作狀態仍保留原有紀錄。202 回應不代表抓取已完成。每日 18:30 自動排程設定不因手動操作改變。

## 每日自動更新設定

在 `.env` 加入：

```dotenv
CAPITAL_DAILY_BACKFILL_ENABLED=true
CAPITAL_DAILY_BACKFILL_TIME=18:30
CAPITAL_DAILY_BACKFILL_TIMEZONE=Asia/Taipei
CAPITAL_DAILY_BACKFILL_MARKETS=all
CAPITAL_DAILY_BACKFILL_INTERVALS=1m,5m,15m,60m,day
CAPITAL_WEEKLY_BACKFILL_INTERVALS=week
CAPITAL_MONTHLY_BACKFILL_INTERVALS=month
CAPITAL_DAILY_BACKFILL_TARGETS=1m:1200,5m:1200,15m:1200,60m:1200,day:1200,week:560,month:100
CAPITAL_DAILY_REFRESH_TARGETS=1m:1200,5m:240,15m:60,60m:20,day:10,week:5,month:2
CAPITAL_DAILY_AGGREGATE_FROM_DAY=true
CAPITAL_DAILY_BACKFILL_TARGET_BARS=1200
CAPITAL_DAILY_BACKFILL_RESUME=true
CAPITAL_KLINE_REQUEST_INTERVAL_SECONDS=0.8
CAPITAL_BACKFILL_EMPTY_WINDOW_LIMIT=3
CAPITAL_BACKFILL_CONTROL_DB=./data/backfill_control.sqlite3
CAPITAL_BACKFILL_SCHEDULE_STATE_FILE=./data/backfill_scheduler_state.json
TWSE_HOLIDAY_CACHE_FILE=./data/twse_holiday_cache.json
```

修改後必須重新啟動 API。排程屬於 API 行程內服務，因此 API 關閉時不會執行。它不會自行使用 `.env` 帳密登入群益；排程時間到達後若尚未連線，會顯示「等待群益連線」，並每 15 秒檢查一次，連線完成後執行當日更新。

`CAPITAL_DAILY_REFRESH_TARGETS` 是每檔、每市場、每週期的本輪最新回抓目標：1 分 K 1200、5 分 K 240、15 分 K 60、60 分 K 20、日 K 10、週 K 5、月 K 2 根。整日／完整週月查詢可能回傳超過目標，超出部分全部保留；不足時依既有斷點向前查詢，空資料或資源保護仍可延後續跑，不保證來源一定有足夠資料。既有時間點以 upsert 更新，不建立重複列。這不是儲存上限，也不改動初始化／完整重抓目標。新值用於重啟後新建立的排程工作；已保存的未完成工作保留原目標及斷點，不會為換設定而自動重排或重置。

每日排程取得執行權後，會先依序同步市場 0／1／4 到 `instruments`，再同步市場 5／6 到 `odd_lot_instruments`。五個市場及兩組 MySQL 寫入全部成功後，才會建立並執行 K 線更新。任一商品主檔查詢或寫入失敗時，當次排程標記為錯誤且不執行 K 線；先前已成功提交的商品主檔批次仍會保留，不會回滾或刪除。

每次實際開始的排程都會寫入 MySQL `daily_backfill_runs`，記錄階段、商品主檔數、工作數及錯誤；執行期間每個已提交區間更新進度。頁面每15秒讀取最近10筆永久 Log，另以本機控制檔顯示當前區間、暫停及記憶體狀態。尚未完成群益登入時不反覆建立 MySQL Log。

`CAPITAL_DAILY_BACKFILL_INTERVALS` 放每日分鐘線與日線。預設 `CAPITAL_DAILY_AGGREGATE_FROM_DAY=true` 時，另將 `CAPITAL_WEEKLY_BACKFILL_INTERVALS`／`CAPITAL_MONTHLY_BACKFILL_INTERVALS` 的週／月線在每個交易日一起執行，以新查詢的完整日 K 範圍重算，包含當期至最近已收盤日；同一期 OHLCV 覆寫，未結束者標為暫定，不納入既有已收盤分析。來源範圍不受日 K 自身只回抓 10 根限制。日期鍵統一週一／月初，不改成每天一筆的週／月線。[計算、覆寫與斷點細節](DAILY_WEEK_MONTH_FROM_DAY.md)。

設 `CAPITAL_DAILY_AGGREGATE_FROM_DAY=false` 才沿用原生週／月查詢：在該期間最後交易日18:30加入，截止於已完成週／月；錯過者按市場完成標記補跑。兩種模式都不在休市日啟動，且不更改初始化／完整重抓的歷史目標。已保存的舊工作不會自動轉換成日 K 彙整工作。

排程每天會檢查本機 `TWSE_HOLIDAY_CACHE_FILE`。若尚無當年度資料，會使用證交所官方 `holidaySchedule?date=YYYY0101&response=json` 端點下載一次並原子寫入快取；12 月也會嘗試預抓下一年度。下載失敗或證交所尚未公布時不會覆蓋舊快取，並繼續使用內建 2026 清單及 `TWSE_HOLIDAYS` 備援。官方表中的「開始交易日」「最後交易日」等資訊列不會被誤存成休市日。

首次沒有工作紀錄也能自動初始化。`resume=true` 保留已提交區間；`resume=0` 重新排入本次計畫，但不覆寫正在執行中的工作或刪除歷史。

## 手動測試步驟

### 連線異常的恢復與暫停

API 背景回補及 `backfill_market_candles.py` 共用相同的恢復機制：

- 收到 `1095 / SK_ERROR_QUOTE_CONNECT_FIRST`、斷線事件，或等待 K 線完成逾時時，先恢復報價主機連線，再重試**同一筆、同一日期範圍**。
- 最多恢復兩次，每次先等待兩秒，再等待商品就緒事件 `OnConnection(3003, 0)`；使用既有登入，不反覆呼叫 Login。`2003` 只在 Login 回傳時視為已登入警告，不會對其他 API 一律忽略。
- 兩次都無法恢復時立即停止本輪。正在處理的工作退回 `pending`，保留嘗試次數、錯誤原因及已提交的 K 線；後續工作不再領取，也不會被整批改成 `failed`。
- API 因連線或資料庫故障暫停後，在交易日定期檢查並從已提交區間自動恢復；使用者手動暫停則須按「繼續回補」。CLI 遇到連線／資料庫故障每15秒嘗試接續，手動暫停會結束本次腳本並釋放連線，可用 `--resume` 重開。
- 初始化不足標示 `insufficient_history`。增量查詢完整結束但零筆成交，可以完成該查詢範圍；不因此把其他商品列為失敗。個別商品或解析錯誤才列 `failed`，資料庫故障會暫停整輪。

來源成交量另有明確的五次隔離機制：每次寫入前驗證，無效區間整批不寫、斷點不前進。按同一工作／查詢區間的連續資料錯誤計次，第1～4次等待至少15秒重試，第5次將該工作標為 `failed` 並接續其他商品。成功提交與計次歸零在同一交易；回滾不會歸零。總 `attempt_count` 不作為此門檻，資料庫／連線失敗亦不列入。詳見 [資料錯誤五次隔離](CANDLE_DATA_FAILURE_POLICY.md)。

## PowerShell 與記憶體管理

```powershell
# 預設初始化1200根，已存在資料只作增量；每次最多處理10檔。
.\.venv\Scripts\python.exe .\scripts\backfill_market_candles.py --markets 0 --intervals day --max-jobs 10
# 另一個視窗請求安全暫停（API與CLI共用控制檔）。
.\.venv\Scripts\python.exe .\scripts\backfill_market_candles.py --pause
# 接續最後保存的工作範圍。
.\.venv\Scripts\python.exe .\scripts\backfill_market_candles.py --resume
```

API 與獨立 CLI 不能同時擁有原生群益連線；請選一種方式執行。關閉 MySQL 或強制終止程序不是正常暫停方式。

接收暫存在每次查詢結束時清空，成功寫入後清掉區間資料引用；批次不加入歷史快取。互動快取最多20組、每組1200根，維護迴圈主動清理到期項目。兩種 worker 都監控自身程序記憶體，預設2GB警示、4GB安全暫停，並在定期／高用量時回收 Python 物件。這不是作業系統硬上限，也不包含 MySQL／Ollama 的記憶體；Python 釋放後可能保留空間重用，不能只以工作管理員是否立即下降判斷洩漏。

不中斷正式回補的離線測試：

```powershell
.\.venv\Scripts\python.exe .\scripts\test_backfill_recovery_offline.py
```

這個專用測試入口只載入相關模擬測試，並阻擋真正的 MySQL 連線與群益 DLL 啟動。不要為了測試重啟正式伺服器或切斷正式網路；程式碼修改要等之後重啟才套用到未啟用 `--reload` 的 API。

以下步驟需在 `capital_api` 目錄執行。先啟動 API：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app
```

1. 開啟 `http://127.0.0.1:8000/data-status`。確認畫面能顯示五個市場的狀態，且重新整理不會要求登入群益。
2. 開啟 `/docs`，執行 `POST /api/v1/capital/connect` 完成登入；確認 `GET /api/v1/health` 的 `quote_ready` 為 `true`。
3. 在資料狀態頁先選單一市場及 `day`，按「更新過期資料」。回應為 `202` 時等待背景工作；每 15 秒觀察 `pending/running` 減少。若資料原本已是最新，回應為 `200` 且不呼叫 DLL。
4. 只有確實有 `failed` 工作時才按「重試失敗工作」。確認回應的 `queued_jobs` 等於狀態頁原先顯示的最新失敗數，完成後失敗數下降。較舊但已有新成功工作的失敗歷史不會被重排。
5. 測試每日排程時，把 `CAPITAL_DAILY_BACKFILL_ENABLED` 設為 `true`，並將 `CAPITAL_DAILY_BACKFILL_TIME` 設為目前時間後兩至三分鐘。重啟 API、完成群益連線，在 `/data-status` 確認狀態依序變成等待、執行中、完成。
6. 測試等待登入時，可先不要連線群益，等排程時間到達後確認顯示「等待群益連線」；再登入，確認排程開始執行。

## 自動測試指令

```powershell
.\.venv\Scripts\python.exe -X utf8 .\scripts\test_backfill_recovery_offline.py --all
.\.venv\Scripts\python.exe -X utf8 .\scripts\test_backfill_memory_offline.py
# 需要 CREATE DATABASE 權限；僅建立隨機命名的獨立資料庫，結束後清除。
.\.venv\Scripts\python.exe -X utf8 .\scripts\test_backfill_mysql_isolated.py
```

上述測試不登入群益、不修改正式 MySQL。記憶體測試使用模擬 callback，不能取代群益 DLL 的長時間實測。程式碼與新增進度表會在新版本啟動／建立回補計畫時生效；本次開發測試不會重啟現有伺服器。
