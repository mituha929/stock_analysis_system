# 韭歐虧

以 Python 和 FastAPI 提供台股即時行情、歷史 K 線、技術分析、條件選股與提醒。群益行情由獨立背景程序接收，FastAPI 提供網頁和 API。歷史行情與商品基本資料存於 MySQL，提醒、已儲存策略和回補控制狀態使用本機 SQLite。

進度更新至 **2026-11-11**。

## 目前進度

| 模組 | 目前狀態 | 操作文件 |
| --- | --- | --- |
| 群益即時行情 | 已支援登入報價主機、訂閱自選股、成交價、五檔與逐筆成交。COM 背景程序支援連線恢復、訂閱還原與提交後輪替。 | [COM 報價後端](docs/CAPITAL_COM_BACKEND.md)、[背景程序與輪替](docs/CAPITAL_NATIVE_WORKER.md) |
| 台股歷史 K 線 | 已支援整張及零股歷史資料、初始化與增量回補、暫停續跑、每日更新與日 K 缺口追蹤。週／月 K 可由日 K 彙總。 | [K 線資料維護](docs/KLINE_DATA_MAINTENANCE.md)、[日 K 完整性](docs/DAY_CANDLE_INTEGRITY.md) |
| 台股商品與日曆 | 可同步上市／上櫃股票與 ETF 基本資料，追蹤掛牌日期及市場異動。回補流程使用休市、掛牌邊界和停復牌資料。 | [商品主檔](docs/STOCK_MASTER.md)、[掛牌邊界](docs/LISTING_HISTORY_BOUNDARIES.md)、[台股休市日](docs/STOCK_MARKET_HOLIDAYS.md) |
| V2 技術分析 | 從 MySQL 讀取 K 線，計算均線、MACD、RSI、布林通道等技術指標，提供單股分析與圖表。 | [分析 API](app/api/routes.py)、[技術指標實作](app/indicators/core.py) |
| V3 規則、選股與提醒 | 已實作規則測試、範圍預篩、選股排序、策略儲存與版本管理。提醒支援條件觸發、冷卻期與通知歷史。 | [規則](docs/V3_RULES.md)、[選股](docs/V3_SCREENER.md)、[策略](docs/V3_SAVED_SCREENERS.md)、[提醒](docs/V3_ALERTS.md)、[通知](docs/V3_NOTIFICATIONS.md) |
| V3 AI 選股與單股觀察 | 已實作本機 Ollama 規則解析、白話預覽、門檻調整與確認後的唯讀執行。支援日／週／月 K，也可預覽單股買進、賣出、加碼或減碼的觀察條件。正式股票資料與實際模型的端到端驗收待完成。 | [AI 選股](docs/V3_AI_PREVIEW.md)、[單股觀察](docs/SINGLE_STOCK_ENTRY.md) |
| 外部市場 | 已實作四個美股指數與台指期夜盤的半小時快照排程，以及 15 檔美股／ADR 的收盤後日 K 排程。每檔更新最近 250 根完整日 K。 | [指數與夜盤快照](docs/EXTERNAL_MARKET_QUOTES.md)、[美股日 K](docs/US_STOCK_DAILY.md) |
| 籌碼與基本面 | 支援個股與市場三大法人、個股與市場融資融券、月營收及季報。日常抓取與歷史下載預設間隔 1 秒，正式排程及 MySQL 入庫需先完成來源、覆蓋清單與日曆設定。 | [歷史驗收](docs/MARKET_DATA_HISTORY.md)、[第二階段實作與操作](docs/implementation_plan.md) |

## 本機安裝與啟動

環境以 Windows 和 64 位元 Python 3.12 為基準。即時行情及群益歷史 K 線需要安裝相同位元數的群益 SDK，並依群益步驟註冊 COM 元件。歷史資料、技術分析與外部市場排程需要 MySQL。啟用 AI 功能時，另需可用的本機 Ollama 服務與模型。 [群益元件安裝與排錯](docs/CAPITAL_DLL_REPAIR.md)、[Ollama 設定](docs/V3_AI_RULES.md)

在本 README 所在的目錄建立虛擬環境並安裝相依套件。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

建立本機 `.env`，依使用的功能填入設定。以下是設定格式，請換成自己的 DLL 路徑、MySQL 帳號與密碼。

```dotenv
CAPITAL_SKCOM_DLL=C:/path/to/CapitalAPI/x64/SKCOM.dll
CAPITAL_WORKER_QUOTE_BACKEND=com
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=your_mysql_user
MYSQL_PASSWORD=your_mysql_password
MYSQL_DATABASE=capital_stock_analysis
OLLAMA_ENABLED=false
```

確認資料庫與來源設定後，可在 `.env` 個別啟用 `CAPITAL_DAILY_BACKFILL_ENABLED`、`CAPITAL_STOCK_MASTER_WEEKLY_ENABLED`、`CAPITAL_EXTERNAL_QUOTES_ENABLED` 或 `CAPITAL_US_STOCK_DAILY_ENABLED`。Ollama 可用時，設定 `OLLAMA_ENABLED=true` 與 `OLLAMA_RULE_MODEL`。GitHub 上的安裝流程使用自行建立的 `.env`。群益 SDK、帳密及憑證保存在本機。 [.gitignore](.gitignore)

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

第一次使用 MySQL 功能時，在 Swagger UI 執行 `POST /api/v1/database/init` 建立或驗證專案資料庫與資料表。要驗證群益報價，可依序呼叫 `POST /api/v1/capital/connect` 登入、`POST /api/v1/watchlist` 新增自選商品，再用 `GET /api/v1/watchlist/quotes` 查看收到的行情。

## 籌碼與基本面更新

上市資料由臺灣證券交易所提供，上櫃資料由櫃買中心提供。籌碼資料包含個股與市場三大法人，以及個股與市場融資融券。日常抓取與歷史下載的預設及最低請求間隔均為 1 秒。

| 資料 | 檢查時間（台灣時間） |
| --- | --- |
| 四類籌碼日報 | 交易日 18:00 首查，20:00、22:00 補查。22:00 回看前 5 個交易日的更正，次一交易日 09:00 補查前一交易日未完成的報表。 |
| 月營收 | 每月 1～15 日 20:00 查上月。當月最後一個日曆日 20:00 回看最近兩個已結束月份。 |
| 損益表、資產負債表 | 季末隔天起每日 21:00 檢查，申報截止日隔天再補查。現行財報處理範圍為一般業，公告未齊的資料標為部分覆蓋。 |

正式抓取使用一個獨立的 Windows 排程工作，正式資料存入 MySQL。啟用前先套用 [資料表](sql/market_data_p2_mysql.sql) 和 [排程進度表](sql/market_data_p3_mysql.sql)，備妥涵蓋目標日期的交易日曆、來源核准清單及報表覆蓋清單，再依 [排程操作說明](docs/implementation_plan.md#p3-排程與手動補跑)預覽並註冊工作。

儀表板的籌碼與基本面預覽目前讀取本機回填的 SQLite，正式 MySQL 報表可由[私有查詢 API](docs/implementation_plan.md#p5-私有唯讀查詢)讀取。新下載的專案需要先準備本機回填資料，才能使用歷史報表預覽。

## 網頁與 API 入口

| 本機路徑 | 用途 |
| --- | --- |
| `/docs` | Swagger UI，可查看和測試 API |
| `/api/v1/health` | 檢查 API、群益連線與商品狀態 |
| `/dashboard` | 技術分析與圖表。本機可查詢已回填的籌碼與基本面歷史報表，以及從群益 API 取得的上市加權指數（TSEA）和櫃買指數（OTCA）盤後日 K。 |
| `/data-status` | K 線回補、排程和缺口狀態 |
| `/v3` | 規則測試、提醒、選股及策略管理 |
| `/v3#ai` | AI 條件解析、預覽、確認與執行 |
| `/notifications` | 通知歷史、未讀狀態與瀏覽器通知 |

## 目錄與驗證

| 路徑 | 內容 |
| --- | --- |
| `app/` | FastAPI、靜態網頁、行情服務、技術指標與選股邏輯 |
| `config/` | 交易時段、休市補充資料、回補排除規則和日 K 查核設定 |
| `data/` | 台股休市快照與本機執行資料。首次提交保留 `stock_market_holidays.json`。 |
| `scripts/` | 商品同步、歷史抓取、回補、稽核及驗收工具 |
| `sql/` | 籌碼與基本面資料表及排程進度表的 MySQL 建表腳本 |
| `tests/` | 自動測試。抓取證據與測試產物預設留在本機。 |
| `docs/` | 功能操作手冊及歷史驗收紀錄 |

在本機執行離線回歸測試時，可用專案的隔離入口。它使用暫存控制資料庫，並封鎖真實群益 COM／DLL 和 MySQL 連線。

```powershell
.\.venv\Scripts\python.exe -B -X utf8 .\scripts\run_worker_tests_offline.py
.\.venv\Scripts\python.exe -B -X utf8 -m unittest discover -s tests -p "test_market*.py"
```

## 修改日誌

| 日期 | 變更與驗證 |
| --- | --- |
| **2026-11-11** | 儀表板改由群益取得上市與上櫃指數日 K。新增市場融資融券後端、1 秒請求間隔及季報截止日補查。42 項市場資料測試通過，11/10 官方報表成功寫入隔離 SQLite，並從既有快取回放 76 組市場融資融券報表。 |
| **2026-11-10** | 完成籌碼與基本面第一階段驗收、P1 暫存與 P2 版本匯入，並離線驗證 P3 排程、P4 回填和 P5 私有查詢。 [第二階段實作計畫](docs/implementation_plan.md) |
| **2026-11-08** | 建立 GitHub 首頁 README，整理目前功能、安裝方式、驗證入口與修改日誌，並將專案命名為「韭歐虧」。 |
| **2026-11-05** | 完成 12 個籌碼／基本面端點實測，另比對六個財務樣本的 44 個欄位。歷史工具下載 586／586 組報表，產出 356,638 筆正規化紀錄。整體數值驗收仍有三個差異待查，2026 Q3 財報的公司覆蓋仍未完整。 [端點驗證](docs/MARKET_DATA_ENDPOINT_SMOKE.md)、[財務口徑查證](docs/MARKET_DATA_FINANCIAL_VERIFICATION_20261105.md)、[歷史抓取](docs/MARKET_DATA_HISTORY.md) |
| **2026-11-03** | 新增 15 檔美股／ADR 收盤後日 K 排程，逐檔驗證並在同一筆交易中寫入行情及完成紀錄。已有離線、隔離 MySQL 及來源抓取驗證紀錄。 [美股日 K](docs/US_STOCK_DAILY.md) |
| **2026-11-02** | 更新四個美股指數與台指期夜盤快照排程，加入交易時段、收盤後檢查、報價新鮮度和逐商品重試。 [外部市場快照](docs/EXTERNAL_MARKET_QUOTES.md) |
| **2026-10-30** | 新增 `/v3#ai` 入口，加入條件預覽、版本確認、唯讀選股與單股觀察流程。已完成離線、前端和隔離 MySQL 驗證，正式模型與股票資料的整體驗收待完成。 [AI 選股](docs/V3_AI_PREVIEW.md)、[單股觀察](docs/SINGLE_STOCK_ENTRY.md) |
| **2026-10-24 至 2026-10-26** | 以三檔商品、七種週期驗收 COM 抓取、隔離寫入及輪替後續抓。更新台股休市資料與日 K 缺口查核流程，並記錄舊查核狀態重建方式。 [COM 驗收](docs/COM_ACCEPTANCE_20261024.md)、[休市日](docs/STOCK_MARKET_HOLIDAYS.md)、[查核重建](docs/DAY_INTEGRITY_RESET.md) |
| **2026-10-18 至 2026-10-19** | 調整每日回抓目標及記憶體恢復條件。加入獨立群益背景程序與交易提交後的自動輪替。 [批次回補](README_BACKFILL_BATCH.md)、[背景程序](docs/CAPITAL_NATIVE_WORKER.md) |
| **2026-09-26** | 加入本機 Ollama 自然語言規則草稿、欄位白名單與結構化驗證。 [AI 解析](docs/V3_AI_RULES.md)、[規則驗證](docs/V3_AI_RULE_VALIDATION.md) |
| **2026-09-15** | 完成市場預篩、規則判斷、選股排序、策略儲存及 `/v3` 功能驗證介面，可測試規則、提醒、通知、選股與策略重複執行。以市場 0／1 共 2,540 檔商品進行真實 MySQL 掃描，約 32.688 秒完成，449 檔符合、1 檔資料不足、0 檔分析錯誤。[選股](docs/V3_SCREENER.md)、[策略](docs/V3_SAVED_SCREENERS.md) |
| **2026-09-14** | 完成 V3.1／V3.2 JSON 規則引擎與單股測試，支援欄位白名單、欄位比較、區間、交叉及巢狀 AND／OR；接續完成 V3.3 提醒引擎及 V3.4 通知中心，加入 False→True 觸發、冷卻、重設、SQLite 持久化、已讀管理與瀏覽器通知派送。[規則](docs/V3_RULES.md)、[提醒](docs/V3_ALERTS.md)、[通知](docs/V3_NOTIFICATIONS.md) |
| **2026-09-08 至 2026-09-09** | 強化 SKCOM DLL 環境、行情工作階段、重複連線防護與記憶體快取限制；完成 MySQL K 線技術分析 API、技術圖表輸出及分析路由測試，使歷史分析可與即時群益連線分離執行。 |
| **2026-08-30 至 2026-09-01** | 加入台股交易日曆、K 線儲存狀態檢查、過期資料判斷及背景回補流程。批次回補支援工作紀錄、中斷續傳、資料不足標記與同日重試控制。 |
| **2026-08-22 至 2026-08-25** | 完成 MySQL 連線、商品主檔同步及歷史 K 線寫入，使用股票代號、週期與時間去重並支援重複同步更新；加入批次寫入、同步續傳及跨行程 SKCOM 鎖定。 |
| **2026-08-15 至 2026-08-17** | 完成 V2 技術分析核心，包含 MA、EMA、MACD、KD、RSI、ROC、布林通道、ATR、DMI／ADX、量比與 OBV；加入趨勢、動能、成交量、市場結構、方向及風險評分，以及 RSI／MACD 背離判斷，並以 2330 多週期 K 線樣本進行驗證。 |
| **2026-08-08 至 2026-08-12** | 建立 FastAPI 與群益 SKCOM 的基礎整合，完成登入、報價主機連線、健康狀態、自選股管理、即時報價、五檔、Tick及記憶體快取架構，確立「群益 Callback → 快取 → HTTP API」的資料流程。 |
