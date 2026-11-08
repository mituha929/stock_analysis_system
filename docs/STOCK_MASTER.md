# 官方股票／ETF 掛牌日期與基本資料（MySQL）

依使用者確認，正式資料全部沿用專案 `.env` 的 `MYSQL_*`，**不另建 SQLite 或 PostgreSQL**。使用四張基本資料表，ETF 與股票放在同一主檔；不修改 `instruments`、`candles`、零股或原有回補資料；不登入群益、不執行下單、不觸發 K 線重抓。

## 官方來源與適用範圍

| 市場 | 公開 JSON | 掛牌日期欄位 | 股票名稱 |
| --- | --- | --- | --- |
| TWSE | https://openapi.twse.com.tw/v1/opendata/t187ap03_L | 上市日期 | 公司簡稱 |
| TPEx | https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O | DateOfListing（在 TPEx 資料代表上櫃日） | CompanyAbbreviation |
| TWSE ETF | https://www.twse.com.tw/rwd/zh/ETF/list?response=json | 上市日期 | 證券簡稱 |
| TPEx ETF | https://info.tpex.org.tw/api/etfFilter （依官網使用空 body POST 取得全清單） | listingDate（上櫃日期） | stockName |

櫃買欄位規格確認來源：https://www.tpex.org.tw/openapi/swagger.json 。該規格 DateOfListing 的中文描述沿用「上市日期」，本程式以資料集市場區分其語意，絕不與 TWSE 上市日期合併。

本功能涵蓋官方**公司及 ETF 名單**，不是群益商品主檔全體商品。公司名單上市 1,097 筆（含 6 筆六位數 TDR）、上櫃 894 筆；ETF 額外接兩個官方來源，含一般、槓桿／反向、期貨、債券、主動式及外幣加掛。不含權證、ETN、指數、全部特別股等其他商品，也不憑群益商品數量推算漏抓。首次匯入無法回復已不在現行官方名單的所有歷史公司／ETF／轉板史；只保留實際取得的官方記錄與之後觀察到的變更，不虛構過往掛牌日。

日期接受西元 YYYYMMDD、YYYY-MM-DD、YYYY/MM/DD、YYYY.MM.DD，以及民國 YYYMMDD、YYY/MM/DD，嚴格檢查實際年月日。不存在的日期停止整批；缺少日期用 NULL，不取公司／基金成立日、興櫃日代替。代號維持字串，保留前導零、六位代號及五位數字加一個大寫字母（例如 00631L、00625K、00400A、00679B）。

ETF 來源官網：[TWSE ETF 上市清單](https://www.twse.com.tw/zh/products/securities/etf/products/list.html)、[TPEx ETF 投資篩選器](https://info.tpex.org.tw/ETF/zh/filter.html)。這兩個 JSON 不提供可確認的資料出表日，因此 `source_date` 記錄擷取當日，明確標 `source_date_kind=retrieval_date`，**不冒充官方出表日**；公司名單仍為 `official_report_date`。ETF 清單被上游快取的程度無法靠掛牌日判斷，不能把 retrieval_date 當作所有欄位已在當日更新的保證。

TWSE 一列可能有多個幣別／代號／掛牌日；逐一按幣別標籤對應後拆成獨立列，不能把新臺幣日期套給人民幣／美元加掛代號。例如 006205 為 2011-09-26，00625K 為 2016-08-08。未來預定掛牌 ETF 存入 `stock_listing_plans`，不進正式主檔。

產業保留官方代碼 `industry_code`，並依官方產業分類表轉為名稱；兩市場金融業等名稱分開。對照來源：[TWSE 資訊傳輸作業手冊](https://wwwc.twse.com.tw/staticFiles/product/broker/ff80808166388ea1016676ac941001e0.pdf)、[TPEx 收市後交易資訊格式說明](https://www.tpex.org.tw/storage/regular_system/%E6%96%B0%E7%89%88%E6%94%B6%E5%B8%82%E5%BE%8C%E4%BA%A4%E6%98%93%E8%B3%87%E8%A8%8A%E6%A0%BC%E5%BC%8F%E8%AA%AA%E6%98%8E%28V1.33%E7%89%88%29.pdf?t=20251127)。未知代碼的名稱保留 NULL 並記錄警告，不猜測；TWSE 91 為 TDR 類，非公司產業名稱，industry 留 NULL。

## 操作

在 `capital_api` 目錄執行：

```powershell
# 只抓取／驗證，不連 MySQL、不建立任何資料表
.\.venv\Scripts\python.exe -B -X utf8 scripts\sync_stock_master.py

# 抓取兩市場公司＋ETF 四個來源；全數通過後一起提交到既有 MySQL
.\.venv\Scripts\python.exe -B -X utf8 scripts\sync_stock_master.py --write

# 只查已儲存資料，不連官方網站，也不需要 API 運作／群益登入
.\.venv\Scripts\python.exe -B -X utf8 scripts\sync_stock_master.py --profile 2330
.\.venv\Scripts\python.exe -B -X utf8 scripts\sync_stock_master.py --profile 6488 --market TPEx
.\.venv\Scripts\python.exe -B -X utf8 scripts\sync_stock_master.py --profile 00631L
.\.venv\Scripts\python.exe -B -X utf8 scripts\sync_stock_master.py --runs
```

初次寫入需現有 schema 的 CREATE TABLE、SELECT、INSERT、UPDATE 及一般 `GET_LOCK` 權限。舊版升級另需 ALTER TABLE 權限，只在 `stock_master`／`stock_listing_plans` 增加 `security_type` 與 `source_date_kind` 兩個欄位；不刪欄位、不換主鍵、不刪原日期或歷史。中斷後可重跑，只補尚未存在的欄位。不需要 CREATE DATABASE；升級完畢後不再建表／ALTER。API 讀取可用僅 SELECT 權限的帳號。未增新套件，不印出帳密或原始公司聯絡人資訊。環境變數優先於 `.env`。

## 儲存模型

| 表格 | 用途 |
| --- | --- |
| stock_master | 六個要求欄位 code/name/market/listing_date/industry/updated_at，加 security_type=STOCK／DR／ETF 及來源／觀察狀態；主鍵 `(code, market)` |
| stock_master_history | 每次實際變動的前後內容、run_id、時間；包含市場日期及公司統編變更，不因名單移除而刪除 |
| stock_master_sync_runs | 每次更新狀態、來源出表日、指紋、成功／變更／未變／缺值／預定掛牌筆數、警告、失敗原因；排程去重 |
| stock_listing_plans | 掛牌日在擷取當日之後的預定資料，不混入 stock_master；日後官方名單確實提供且日期已到，才新增正式資料，舊預定列標 activated 保留 |

MySQL DATETIME 一律為 **UTC**。API／CLI JSON 轉為帶 `+08:00` 的台灣時間；DATE 使用 `YYYY-MM-DD`。`updated_at` 是內容／觀察狀態最後變更時間，未變更時不重寫；`last_seen_at` 才是最近一次成功取得該列的時間。

新列日期缺少時 listing_date=NULL。已有官方日期且統編相同的新列日期缺少，保留前次官方值，明確標 `date_status=retained_previous_official`、`reported_listing_date=NULL`；不把舊值冒充本次來源提供。統編改變時不可沿用舊公司日期。新未知產業代碼不沿用原產業名稱。

ETF 的 industry／industry_code／issuer_id 留 NULL；投信名稱／代碼不是基金的公司產業或公司統編，不混填。ETF 缺日期時，只有代號、市場、類型與名稱吻合才保留既有官方掛牌日；名稱也改變且日期缺失則整批停止。舊版歷史 JSON 原樣保留，不補造商品類型；本輪 TDR 分類更新為 DR 的變更另外記錄。

在整批成功後，原資料未見於現行名單只標 `presence_status=not_in_latest`；**這不是已確認下市、也不是下市日期**，其他欄位及歷史不刪除。市場轉換以各市場獨立列與變更歷史保留；不得將新上市日當成原上櫃日或人生第一個交易日。兩市場同時出現同一現行代號時整批暫停，避免猜測目前市場；未來預定轉板可與目前另一市場資料並存。

## 保護與一致性

- CLI／每週排程預設公司＋ETF 四個來源都取得並驗證，才變動基本資料。任何一份失敗，整批不更新；失敗紀錄仍保存。內部相容的公司兩市場更新只處理公司範圍，不會將 ETF 判為名單缺漏。
- 官方欄位集合與已確認的 schema 不同（含增加、移除、改名），停止整批；不偷偷切成其他來源。
- 公司每市場至少 500 筆，ETF 每市場至少 50 筆；整批皆無有效掛牌日、出表日不一致、未來出表日、出表日超過 14 日、來源日期倒退、相較該來源前次成功筆數縮減超過 20% 均停止。四個來源的基準分開保存，不將 ETF 誤當公司增減，也不因公司單獨更新而遺失 ETF 基準。需人工確認後修改規則，不靠空名單覆蓋。
- 使用此 schema 專用 MySQL advisory lock，防止 CLI、多 API worker、每週更新相互重疊；不占用群益 DLL 鎖或 K 線工作鎖。
- 來源抓取在資料交易外，HTTP timeout 每來源 25 秒。正式資料、歷史及 completed 紀錄在同一 InnoDB 交易；失敗全部回滾，再持久保存 failed 記錄。建表屬獨立 DDL，來源失敗時首次可能仍留下空表及失敗 Log，不會有半批基本資料。
- 程序異常退出，下次持有獨占鎖的更新會將遺留 running 標 interrupted。資料庫本身無法連線時無法保證新增 Log，CLI 仍回報失敗。若 `commit_uncertain=true`，先依 run_id 查紀錄，不要盲目重跑。
- 同名表結構、主鍵或 InnoDB 引擎必須符合；只接受本功能已知的舊版欄位集合進行上述加欄位升級，其他差異拒絕遷移。所有表先檢查才執行 DDL；DDL 非交易式，不能把已完成的加欄位聲稱為資料交易回滾。

## FastAPI 查詢

新路由要由使用者**自行重啟 API** 後才生效，本次不會替你重啟。讀取不呼叫官方網站／群益，不新增前端輪詢。

```text
GET /api/stocks/2330/profile
GET /api/stocks/6488/profile?market=TPEx
GET /api/stocks/0050/profile
GET /api/stocks/00631L/profile
GET /api/stocks/009831/listing-plans
GET /api/stocks/2330/history?limit=50&offset=0
GET /api/stocks/2330/listing-plans
GET /api/stocks/sync-runs?limit=10
```

亦提供 `/api/v1/stocks/...` 相容別名。profile 回傳六個基本欄位、`security_type`，加 `source` 來源／日期語意／缺值狀態及 `market_history`。目前市場唯一時優先回傳目前列；只有一個已不在名單的歷史列時可回傳，但明確帶 `not_in_latest`。多個歷史市場且無唯一目前列時回應 409，需指定 market；查無已掛牌資料為 404；未初始化／DB 失效為 503；不合法代號或市場為 422。只有預定掛牌不當作已掛牌回傳。

另一台電腦也可沿用既有 Tailscale/MySQL 帳號權限，直接唯讀查詢：

```sql
SELECT code, name, market, security_type, listing_date, industry, updated_at,
       source_date, source_date_kind, presence_status, date_status, last_seen_at
FROM stock_master WHERE code = '2330';
```

不要在另一台電腦建立新資料庫；使用既有 MySQL schema 及可讀取新表的帳號即可。不變更你原有防火牆或 Tailscale 設定。

## 每週更新

已加入獨立排程，但**預設關閉，不修改實際 `.env` 或立即啟動**。要啟用，將下列設定加入自己的 `.env`，再自行重啟 API：

```dotenv
CAPITAL_STOCK_MASTER_WEEKLY_ENABLED=true
CAPITAL_STOCK_MASTER_WEEKDAY=6
CAPITAL_STOCK_MASTER_TIME=20:00
CAPITAL_STOCK_MASTER_RETRY_SECONDS=3600
```

0=週一、6=週日；預設週日台灣時間 20:00。API 必須持續運作，但不需群益登入。錯過排程後只補最近一次，不重跑多年前排程；成功鍵永久存在 MySQL，重啟／多程序不重複。失敗預設一小時後重試，舊資料保留。非排程時間只在記憶體看時鐘，成功後不每分鐘查 MySQL。手動 CLI 更新仍可隨時執行。

## 驗收與界線

```powershell
# 封鎖正式 MySQL／DLL 的單元測試
.\.venv\Scripts\python.exe -B -X utf8 scripts\run_worker_tests_offline.py test_stock_master test_etf_master

# 真實 MySQL 整合：只建立 stock_master_test_<UUID>，用合成資料驗證後移除
# 需額外 CREATE/DROP DATABASE 權限；不寫正式表
.\.venv\Scripts\python.exe -B -X utf8 scripts\test_stock_master_mysql.py

# ETF 舊表升級與正式 MySQL 型態驗收（另建 etf_master_test_<UUID>，完成後清除）
.\.venv\Scripts\python.exe -B -X utf8 scripts\test_etf_master_mysql.py
```

掛牌日不能證明歷史 K 線齊全，也不能作為市場轉換前完全沒有交易的證據。後續已新增 [掛牌日期與回補邊界](LISTING_HISTORY_BOUNDARIES.md)：回補與日 K 缺口檢查使用已確認適用的掛牌下限，轉上市及既有較早行情保守保留，不刪除舊行情。MA60／MA120 判斷不變，仍以實際可用 K 線筆數與資料完整性計算。

## 實際驗收紀錄

- 新增 34 項離線測試通過；完整離線回歸 568 項通過。
- 真實 MySQL 隔離驗收 20 項通過，含實際寫入後故障回滾、更新紀錄、空值保護、市場轉換、跨程序鎖、重複更新及真實 SQL → ASGI API；自建測試 schema 已移除，合成資料未寫入正式表。
- 正式使用既有 `capital_stock_analysis` schema，首次 run_id：`acc4a0f4a119466799b8cdddaf60bfd1`，新增 1,991 筆。
- 再次重新抓取同步 run_id：`277066d38ff5421aab1c5640e2e28f84`，新增 0、變更 0、未變 1,991。主檔 1,991 列、歷史 1,991 列、預定掛牌 0 列；沒有重複新增歷史。
- 上市 1,097 筆，官方出表日 2026-10-21；上櫃 894 筆，官方出表日 2026-10-22。全部正式資料讀回後逐市場重新算指紋，與來源指紋一致；目前缺少掛牌日 0 筆。
- 正式資料的 FastAPI ASGI 路由驗證：2330 台積電＝TWSE／1994-09-05／半導體業；6488 環球晶＝TPEx／2015-09-25／半導體業。這是測試程序中的新路由驗證，**不是已重啟部署中的舊 API**。
- 本次未修改實際 `.env`、未啟用週排程、未重啟現有 API、未登入群益，也未更動 K 線或回補工作。

### ETF 擴充驗收（2026-10-23）

- 新增 17 項 ETF 單元測試，公司＋ETF 合計 51 項；完整離線回歸 585 項通過。原公司 MySQL 隔離驗收 20 項及 ETF 舊表升級驗收 18 項均通過，自建測試 schema 均已移除。
- 正式升級／匯入 run_id=`13767be679324cb7a493ecb191f9748e`。只新增兩個欄位到原基本資料／預定表，匯入已掛牌 ETF 360 筆（TWSE 242、TPEx 118），預定掛牌 2 筆分開保存。原公司清單 10 筆 TDR（其中 6 筆六位代號）僅將 security_type 由 STOCK 標為 DR，日期／名稱／產業未改動。
- 升級前後原 1,991 筆公司的代號、名稱、市場、掛牌日、產業、產業代碼及統編校驗值一致；原 1,991 筆歷史 JSON 校驗值亦一致，沒有改寫舊歷史。
- 再次同步 run_id=`15b8a5b1fad8493cb330994706dc6a90`：新增 0、變更 0、未變 2,351。正式主檔 2,351 列、歷史 2,363 列、預定表 2 列，重跑不增加重複主檔／歷史。
- 公司 TWSE 1,097、TPEx 894；ETF TWSE 清單原 239 列，按幣別拆成 244 代號，其中 242 已掛牌、2 預定；TPEx ETF 118。主檔＋預定資料逐來源重新計算全部欄位指紋，四個來源均一致，缺少掛牌日 0 筆。
- 新程式 ASGI → 正式 MySQL 驗證：0050＝2003-06-30、00631L＝2014-10-31、00625K＝2016-08-08、00679B＝2017-01-17、00411A＝2026-08-26，security_type 都為 ETF。
- 009831、009832 預定掛牌 2026-10-29。009831 一般 profile 為 404，listing-plans 可查，未冒充目前已掛牌。
- 仍未重啟部署中的 API；上述為測試程序讀正式 DB 的路由驗證。週更新設定維持 false，實際 `.env`／K 線／群益回補均未變動。
