# 籌碼與基本面第二階段實作計畫

2026-11-10 依使用者指示進入第二階段。工作區的第一階段依據是 `3dafa_data_TEST_fetch.md`，第二階段規格另以 `STOCK_DATA_MVP_TEST.md` 稱呼啟動門檻文件。第一階段實測與驗收依現有文件和下列證據記錄。正式資料使用既有 MySQL，測試使用隔離 SQLite。

## 第一階段與 P1 驗收

| 項目 | 代表日期或期別 | 上市樣本 | 上櫃樣本 | 來源欄位與單位 |
| --- | --- | --- | --- | --- |
| M01 個股三大法人 | 2026-11-04 | 2330 | 6488 | 外資（不含外資自營商）、投信及自營商買賣超，股 |
| M02 市場三大法人 | 2026-11-04 | 投信 | 投信（股票範圍） | 買進、賣出、官方淨額，新台幣元 |
| M03 融資融券 | 2026-11-04 | 2330 | 6488 | 融資及融券前日、當日餘額，張。上市用有交易日的官方日報 |
| M04 月營收 | 2026-09 | 2330 | 6488 | 單月及累計營收，新台幣仟元。成長率以百分比表示 |
| M05 一般業損益 | 2026-Q3 | 1215 | 1584 | 年初至第三季末累計。金額為新台幣仟元，EPS 為元／股 |
| M06 一般業資產負債 | 2026-Q3 | 1215 | 1584 | 第三季末時點。金額為新台幣仟元，股數與每股淨值另標單位 |

[12 份 JSON 樣本](../tests/output/samples/)逐欄附單位、官方欄位、期別證據、原檔雜湊及請求資料。M01–M03 的六份樣本直接與原始交易日和 20 個欄位值比對一致。M04–M06 另有先前的[官方頁面對照](MARKET_DATA_FINANCIAL_VERIFICATION_20261105.md)。[歷史驗收](../tests/output/history_20261105/audit.md)核對 586 組報表及 356,638 筆，三筆容差內來源差異，零阻擋問題。以現行解析器重讀 586 份原文，資料列與既存正規化結果一致。

P1 以保存的官方回應重跑來源解析、期間與欄位驗證、資料品質檢查及 SQLite 暫存。範圍包含 2026-11-04 交易日、2026-09 月營收、2026-Q3 一般業財報及 2026-10-07 上櫃兩種法人彙總。17 組報表、6,924 筆資料入庫，三筆差額旗標。第二次執行新增 0 組，SQLite `integrity_check` 為 `ok`。測試涵蓋 ±10 元通過並標記、±11 元拒絕、拒絕批次保留原快照、不同市場範圍分開及重跑去重。

在 `capital_api` 執行下列命令。P1 讀取 `tests/output/history_20261105/raw/` 及其請求中繼資料，輸出 `tests/output/market_data_p1/staging.sqlite3`、`results.json` 和代表樣本。

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m scripts.stage_market_data
.\.venv\Scripts\python.exe -B -X utf8 -m scripts.audit_market_history
.\.venv\Scripts\python.exe -B -X utf8 -m unittest discover -s tests -p 'test_market*.py'
```

查詢 SQLite 暫存庫中的旗標與帶符號差額。

```sql
SELECT p.market, p.period, p.market_scope, r.record_key,
       r.net_difference_flag, r.net_difference_twd
FROM staging_reports AS p
JOIN staging_records AS r USING (report_key, raw_sha256)
WHERE r.net_difference_flag = 1;
```

## 來源能力矩陣

日期是已實測範圍，官方最早可查期間與正式擷取頻率另待逐來源確認。初次端點及期間抓取驗證日期為 2026-11-05。

| 資料源 | 已驗證期間查詢 | 已保存的實測範圍 | 授權證據及待查範圍 |
| --- | --- | --- | --- |
| TWSE T86、BFI82U、MI_MARGN 日報 | 日報分別用 `date`、`dayDate`、`date`。MI_MARGN OpenAPI 與日報是不同介面 | 2026-09-07～11-04，38 交易日 | 歷史日報的使用與轉供仍待確認 |
| TPEx 個股法人、市場法人、融資券 | 官網歷史表單用 `YYYY/MM/DD`。市場彙總用 `prod=0/1` 分開取有價證券／股票 | 同上 | 股票口徑的對應資料集有開放條款。有價證券及歷史表單的使用與轉供待確認 |
| MOPS 月營收彙總 | 官方歷史檔按民國年、月、上市／上櫃及國內／外公司分開 | 2021-10～2026-09，60 個月 | 上市／上櫃的開放資料資源有條款，歷史檔的適用範圍待確認 |
| MOPS 一般業財報彙總 | 官方損益、資產負債歷史表單按市場、民國年與季別。回應核對季別，年度來自請求 | 2021-Q4～2026-Q3，20 季 | 部分一般業資料集有開放條款，歷史網頁及附件待確認 |

[來源、授權與時間查證](MARKET_DATA_HISTORY.md)保留官方連結。P1 以既有快取進行離線驗證。正式啟用各來源前仍需確認對應欄位、允許的擷取方式與使用範圍。

## P2 MySQL 入庫與版本

| 資料表 | 鍵與責任 |
| --- | --- |
| `market_data_sources`、`market_data_ingestion_runs` | 記錄來源、授權狀態、匯入結果及錯誤。尚未逐來源核實者保留 `pending` |
| `market_data_series`、`market_data_snapshots` | 以資料集、市場、期間和範圍識別報表，再以原文 SHA-256 去重。原文路徑、實際請求和欄位單位保存在快照，系列記錄目前有效版本 |
| `market_data_institutional_stock_daily`、`market_data_margin_stock_daily` | 存已驗證的三類法人買賣超股數，以及融資、融券前後餘額。個股買賣明細和融資券流量待確認欄位後擴充 |
| `market_data_margin_market_daily` | 上市讀取同一份日報的市場信用交易統計。上櫃張數依有完整日期的個股日報加總，融資金額從櫃買中心看板取得，並以同日融資及融券張數交叉核對年份和日期。各欄保留來源單位 |
| `market_data_institutional_market_daily` | 上櫃股票和有價證券各自形成一個系列。依版本保存法人分類、買進、賣出、官方淨額、`net_difference_flag` 與帶符號的 `net_difference_twd` |
| `market_data_revenue_monthly` | 依版本存公司每月、累計和對比營收，保留原始成長率和備註。未查證的合併範圍為 `NULL` |
| `market_data_financial_statements`、`market_data_financial_facts` | 依版本存公司、年季、報表類型與一般業分類。明細保存原始科目與單位，未查證的標準科目代碼及合併範圍為 `NULL` |

匯入從官方原始快取重算 SHA-256、驗證市場與期間，再解析數值。每份報表以單一交易提交版本、資料列與有效指標。相同原文略過，晚取得的不同快照可成為有效版本，較早取得的快照只存歷史。候選有效版本若缺少原先已存在的資料列，整份拒絕並保留原版，正式更正若確實需要刪除資料列，先人工核實。資料庫錯誤會回滾該報表，停止該輪後續匯入。

`revision` 是資料庫的報表版本序號，`first_seen_at` 是快取記載的首次擷取時間，`imported_at` 是入庫時間，均用 UTC 保存。現有快照未核實的 `published_at`、`available_at` 保持 `NULL`。

MySQL 金額使用 `DECIMAL(38,12)`。SQLite 測試以文字保存同一精確數值，避免浮點轉換。市場法人差額為買進減賣出再減官方淨額，零差額旗標為 0，非零為 1。絕對值 ≤10 元可提交，超過則拒絕該份報表。

[MySQL migration](../sql/market_data_p2_mysql.sql) 只新增 `market_data_*` 表。先審核 DDL、目標庫、既有同名表、使用權限及准許入庫的來源清單。匯入程式本身要求目標庫已具備 InnoDB 資料表，且正式模式須明確選擇資料庫與報表。SQLite 測試命令如下。

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m scripts.import_market_data
.\.venv\Scripts\python.exe -B -X utf8 -m scripts.import_market_data --print-mysql-ddl
```

輸出位於 `tests/output/market_data_p2/`。17 組官方快取匯入 6,924 筆報表資料、10,143 筆財務科目，三筆法人差額有旗標。重跑時 17 組皆辨識為既有版本，SQLite 完整性與外鍵檢查通過。30 項離線測試通過，涵蓋修訂、較舊快取、容差邊界、缺列及寫入中途失敗。正式 MySQL migration 與實庫相容性仍待人工審核後驗證。

## P3 定期抓取計畫

時區統一使用 `Asia/Taipei`。下列時間是初始檢查窗口，實際公告時點以每個來源回應為準。先為各來源確認授權、節流與窗口，再啟用對應任務。

| 資料集 | 初始排程 | 日期驗證與回看 |
| --- | --- | --- |
| 個股／市場法人、融資融券 | 交易日 18:00、20:00、22:00 依來源檢查，成功後停止該來源當日後續檢查。仍待發布者由次一交易日上午 09:00 的補跑處理 | 以交易日曆選應抓日期，核對回應日期與統計範圍，初期回看最近 5 個交易日 |
| 月營收 | 每月 1～15 日 20:00 檢查前一月，每月最後一個日曆日 20:00 查近期更正 | 按市場及國內／外公司分批，核對月份。月底回看最近 2 個已結束月份 |
| 季度財報 | 季末隔天起每日 21:00 檢查，適用截止日的隔天再補查一次 | 市場批次依較晚的通常期限安排，第一／三季按季末後 60 日，第二季至 8 月底，年度至翌年 3 月底。預設遇週末順延，可在日曆設定 `filing_closed_dates` 或依公告指定 `financial_deadlines`。公告未齊的已驗證資料保留為部分覆蓋 |

- 排程入口預設停用，可由 Windows 工作排程器以獨立工作啟動，並保留手動指定資料集、市場、期間的補跑。以單例鎖避免排程與手動工作重疊。
- 成功進度依來源、資料集、市場與範圍分開記錄。空報表、日期錯誤、欄位變更或已收錄公司消失時保留目前有效版。財報通過來源與資料驗證後可先存已公告公司，缺少預期公司時仍標為部分覆蓋。
- 日常抓取與歷史下載的預設及最低請求起始間隔均為 1 秒。所有請求依序執行。HTTP 403、429、驗證碼或登入牆即停止該來源，記錄原因。逾時與 5xx 最多兩次帶抖動的退避重試，超過後留待下次窗口或人工補跑。
- 記錄每輪開始與結束、應抓／實抓期間、缺漏、異常旗標、修訂、重試與耗時。先以少量日期演練重啟續跑、停跑與舊版保護，再啟用自動任務。

### P3 排程與手動補跑

在 `capital_api` 執行。省略 `--run` 只列計畫。加上 `--run` 預設重播已保存的快取，寫入 `tests/output/market_data_p345/workflow.sqlite3`。每次執行最多 50 組報表，結果與待查原因保存在同目錄的 `sqlite_runs/` 或 `mysql_runs/`。

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m scripts.run_market_data scheduled --at "2026-10-07T18:00:00+08:00" --run
.\.venv\Scripts\python.exe -B -X utf8 -m scripts.run_market_data manual --group daily --period 2026-10-07 --market TPEx
```

目前專案交易日曆快照的覆蓋截止日是 2026-10-26。超出範圍的日資料排程會回報 `trading_calendar_coverage_required`。正式排程前，提供覆蓋目標日且已核實的日曆檔給 `--calendar`。晚間補正回看最近五個已驗證交易日，月營收月底回看最近兩個月，財報在每月一日回看前兩季。

連線抓取需明確加上 `--live`，並提供核准來源與預期覆蓋清單。來源清單為 JSON 字串陣列，ID 為 `資料集:市場:範圍`，如 `institutional_market:TPEx:securities`。大盤融資融券的來源 ID 是 `margin_market:TWSE:`、`margin_market:TPEx:`、`margin_market:TPEx:amount`。預期清單以 `report_key` 對應當期應有的公司代號或法人分類。上市市場統計與上櫃融資金額驗證固定項目，上櫃張數沿用同日個股融資融券的預期覆蓋清單。未完成覆蓋核對的資料在 API 標為 `is_complete=false`。

正式 MySQL 依序人工審核並套用 [P2 建表](../sql/market_data_p2_mysql.sql) 與 [P3 進度表](../sql/market_data_p3_mysql.sql)。[Windows 排程腳本](../scripts/Register-MarketDataTask.ps1) 預設列出動作與 15 個觸發時點。核准後用 `-Install` 註冊，要求主機時區為台北，遇到既有同名工作會停止。報表已完成或同一窗口已嘗試時略過，避免三個觸發點重複抓取。跨行程以 SQLite 鎖或 MySQL 具名鎖保護；若來源封鎖則持久記錄在 `blocked_hosts.json`，需查明原因再解除。

### P4 已驗證快取的分段回填

回填以原歷史工作清單及快取為界，進度按報表保存。重跑同一命令會從尚未處理的報表續跑，之後再處理失敗項目。正式寫入另需明確指定 `--apply-mysql --confirm-database`，並先核對該來源及期間的允許用途。

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m scripts.run_market_data backfill --group daily --start 2026-09-07 --end 2026-11-04 --limit 50 --run
.\.venv\Scripts\python.exe -B -X utf8 -m scripts.run_market_data backfill --group revenue --start 2021-10 --end 2026-09 --limit 50 --run
.\.venv\Scripts\python.exe -B -X utf8 -m scripts.run_market_data backfill --group financial --start 2021-Q4 --end 2026-Q3 --limit 50 --run
```

三類剩餘組數均為 0。實際離線庫有 586 個報表版本、356,638 筆報表資料、1,745,282 筆財務科目及 3 筆市場法人差額旗標。SQLite 完整性和外鍵檢查通過。

### P5 私有唯讀查詢

主程式預設停用籌碼與基本面路由。完成來源使用與資料庫審核後，可設定 `CAPITAL_MARKET_DATA_API_ENABLED=true` 及至少 24 字元的獨立隨機 `CAPITAL_MARKET_DATA_API_TOKEN`，並以 `Authorization: Bearer …` 呼叫。資料連線使用 MySQL 唯讀交易。每個 API 行程每分鐘最多 60 次查詢，資料查詢有最多 5 秒的行程內快取，進度查詢即時讀取。

| 路由 | 常用參數 |
| --- | --- |
| `GET /api/v1/stocks/{code}/institutional` | `start`、`end`、可選 `market` |
| `GET /api/v1/markets/{market}/institutional` | `start`、`end`，上櫃須指定 `scope=stocks` 或 `securities` |
| `GET /api/v1/stocks/{code}/margin` | `start`、`end`、可選 `market` |
| `GET /api/v1/markets/{market}/margin` | `start`、`end`，依項目回傳餘額、單位和來源。上櫃較早的期間可由有日期的個股快取回填張數，金額需有可交叉核對的看板紀錄 |
| `GET /api/v1/stocks/{code}/revenue` | `months=12`、可選 `through=YYYY-MM`；預設以已存最新月份為終點 |
| `GET /api/v1/stocks/{code}/financials` | `year`、`quarter`、`statement=income_statement` 或 `balance_sheet` |
| `GET /api/v1/data/status` | 依報表列出執行狀態、最後嘗試、最後成功及覆蓋狀態 |

日期查詢最多 366 天，每頁最多 100 筆。回應附官方來源、期別、逐欄單位、有效版本及精確數值字串。法人市場彙總包含差額和旗標。每列的 `is_complete` 表示該版本已通過指定覆蓋清單，頁面欄位彙整本頁結果。歷史公告時間未知時保留 `null`。

隔離 SQLite 驗證 37 項離線測試。用回填的 SQLite 核對 7 個實值和狀態案例，另連續執行 40 次本機 ASGI 查詢。詳見[驗證結果](../tests/output/market_data_p345/verification.json)。正式 MySQL、任務註冊、實際官網排程與對外 APP 授權仍須依規格審核。

## 後續交付

- P2 交付 MySQL migration、入庫程式及 SQLite 驗收。正式套用須依第二階段規格先做人工審核。
- P3、P4 交付定期／手動更新、分段回填與監控。正式啟用需要最新日曆、各來源核准清單及當期應有識別鍵。一般業之外的報表欄位、法人買賣明細及融資券流量需逐類確認。
- P5 交付私有唯讀查詢與權限。對外供應資料前逐來源核對再散布範圍。
- P6 選股、Ollama 和回測。重大訊息時間與財報完整欄位可得時間分開記錄。未知時間及未取得的歷史版本不進嚴格時點回測。

後續按第二階段規格的分段審查進行。
