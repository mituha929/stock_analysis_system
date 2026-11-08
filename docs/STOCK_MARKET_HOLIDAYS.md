# 台股休市日期：上市、上櫃、興櫃共用名單

本功能取得 **2016-10-24～2026-10-24** 的股票市場休市資料，以臺北當日往前十年、包含起迄日。雖然查詢 2016～2026 共十一個年度，最前與最後年度只取範圍內日期，不是十一個完整年度。

依使用者確認，名單共同適用上市 `TWSE`、上櫃 `TPEx`、興櫃 `ESB` 股票；不是期貨夜盤、國際債券或個別股票停牌日曆。掛牌前、個別停復牌或盤中暫停交易，仍須獨立處理。

原先獨立抓取的名單，現已由新版交易日曆讀取，套用一般回補、斷點續抓與日 K 缺口檢查；見 [空資料觀察與審核](EMPTY_HISTORY_REVIEW.md)。此次未重啟正式 API、未修改 `.env`、既有年度快取或正式 MySQL。歷史生成檔內 `runtime_calendar_applied=false` 只記錄當時抓取工具沒有部署行為，不是目前執行版本的狀態。

## 來源與取得方式

優先逐年下載使用者指定的證交所介面，格式是西元年加 `0101`：

```text
https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20250101&response=json
```

亦嘗試同一證交所介面的 `www.twse.com.tw` 主機。2026-10-24 實測，兩個主機的 2016～2020 年皆回傳 `stat=ok` 但 `data=[]`，只有 2021～2026 年有年表。因此不能把空回應視為該年沒有休市。

已從櫃買中心的官方英文歷史年表補足五個舊年度，完整原 HTML 與摘要值保存在官方快照內：

- [2016](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_105.htm)
- [2017](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_106.htm)
- [2018](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_107.htm)
- [2019](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_108.htm)
- [2020](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_109.htm)

舊版 HTML 有跨列儲存格、跨年度元旦及星期文字誤植，故不盲目以欄位位置猜日期。已檢閱全部表格、將日期整理成 `config/stock_market_holidays_supplements.json`，並設定已核對版本的 SHA-256（忽略空白的 UTF-8 內容）。重新下載時須與此版本相符，否則停止更新，等待重新檢閱。保留英文原說明與中文分類名稱，補行交易例外亦明確列出。

2018 年另以證交所官方 PDF 年表交叉核對，2019 年另以證交所官方資訊商店年表圖片交叉核對。參考檔案保存在 `output/diagnostics/stock-holidays-20261024-sources/`，不需任何 PDF 套件即可執行日常下載與合併工具。

## 哪些列會計入休市

- 國定假日、補假、調整放假。
- 「市場無交易，僅辦理結算交割」：沒有股票交易，納入。
- 「開始交易日」「最後交易日」：仍有交易，**不納入休市**。即使說明中提到次日休市，也不能誤把該列日期當休市。
- 週末另外列示，與公告假日同日只算一次。
- 週六補行交易優先於週末規則。官方[證交所歷史介紹](https://www.twse.com.tw/zh/about/company/history.html)列明，直到 **2019 年**才施行週末補行上班日不交易亦不交割；不能將今天的規則反推到全部歷史日期。

本次範圍內保留的五個週六交易日為 `2017-02-18`、`2017-06-03`、`2017-09-30`、`2018-03-31`、`2018-12-22`。2016 年的三個補行交易日也保留在舊年表，但落在本次起日之前，不納入結果。

若舊年度表缺漏，2019 年以前的未確認週六不會自行判為休市；未分類公告保留原文，不當作可安全排除的日期。

## 臨時休市不是年度表的完整內容

已另查證補登十筆：2019-08-09、2019-09-30、2023-08-03、2024-07-24、2024-07-25、2024-10-02、2024-10-03、2024-10-31、2026-07-10、2026-10-23。

其中 2019 年兩筆來源是中央社明確引述證交所與櫃買中心的全日休市公告，並說明包含興櫃股票；2026-07-10 是證券商公會承銷作業公告載明因休市順延。這三筆標為 `reported_exchange_notice`，**不冒稱取得交易所原始公告**。其餘七筆有交易所直接公告來源，標為 `reviewed_exchange_notice`。各筆日期、理由與連結保存在補充檔與合併結果的 `evidence`。

年度資料已補齊，不等於所有可能的歷史臨時公告已全部驗收。因此保留 `emergency_coverage_complete=false`，不將未列出的平日無條件認定為交易日。也不會把臺北市局部學校停課、下午或晚間停班直接視為股票全日休市。

## 檔案

- [可閱讀日期表](STOCK_MARKET_HOLIDAY_DATES.md)：年度筆數、公告日期、週末完整清單。
- `data/stock_market_holidays_official.json`：每年度完整 API 回應、官方 HTML、抓取時間、來源及摘要值。
- `config/stock_market_holidays_supplements.json`：已檢閱的官方歷史年表、週六交易例外與臨時休市證據。
- **`config/stock_market_holidays_manual.json`：你自行新增或更正的檔案**，重新下載不會覆蓋。
- `data/stock_market_holidays.json`：合併後的休市日期、三市場標記、逐日證據及覆蓋狀態。

## 指令

在 `capital_api` 目錄執行，不需啟動 API、不讀取帳密：

```powershell
# 重新下載、驗證官方來源並合併；手動檔不變
.\.venv\Scripts\python.exe -B -X utf8 scripts\manage_stock_market_holidays.py refresh

# 手動編輯後離線重建，不連網
.\.venv\Scripts\python.exe -B -X utf8 scripts\manage_stock_market_holidays.py build

# 只讀查看公告休市（加 --include-weekends 顯示所有週末）
.\.venv\Scripts\python.exe -B -X utf8 scripts\manage_stock_market_holidays.py list

# 查看單日狀態
.\.venv\Scripts\python.exe -B -X utf8 scripts\manage_stock_market_holidays.py check 2017-02-18
```

每個指令都可指定 `--as-of 2026-10-24 --years 10` 固定範圍。本次初始執行有使用這個日期。`check` 回傳 `closed`、`open`（有明確交易日／人工更正）、`unknown`（未覆蓋或待分類）、`not_listed`（年度表未列休市，但仍可能有未收錄的臨時休市）。不把 `not_listed` 視為已證實開市。

### 自行新增休市

以下用已查證的日期示範：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 scripts\manage_stock_market_holidays.py add 2026-10-23 --name "薔琵颱風休市" --reason "人工核對交易所公告" --source-url "https://eshop.twse.com.tw/zh/home/index"
```

相同人工日期只保留一筆，會更新原項目；省略 `--source-url` 時保留該人工項目原有的來源。若要人工更正為**開市**，加 `--open`，可以覆蓋官方假日或週末，因此請務必核對官方依據。

也可直接在人工檔 `records` 陣列增加：

```json
{
  "date": "2026-10-23",
  "name": "薔琵颱風休市",
  "is_closed": true,
  "reason": "人工核對交易所公告",
  "source_url": "https://eshop.twse.com.tw/zh/home/index",
  "enabled": true
}
```

人工紀錄優先於官方年表、補充公告及週末規則。`enabled=false` 只是停用該人工覆蓋、恢復官方／週末判斷，**不代表強制開市**；強制更正開市應使用 `is_closed=false`。手動編輯後執行 `build` 即更新合併檔；使用新版 runtime 的 worker 會重新載入修改後的檔案，不需為日曆改動再次重啟。尚在使用舊版的 API 仍須先自行安全重啟新版。

工具驗證日期格式、重複日期、布林值、非空原因與來源網址格式，但不會自動替你查證人工填寫的事實。未來日期不透過 `add` 寫成已發生歷史；官方全年年表的未來公告仍保留在快照，合併結果僅取截止日以內。

## 更新失敗的處理

下載／解析失敗、回傳年度錯誤、欄位變動、筆數倒退、已取得年度突然變空，或官方舊年表內容與核對版本不同時，停止該次更新，不以新回應取代舊檔。所有年度先下載與驗證，才寫出結果；人工檔在 `refresh` 過程始終不改動。未來若官方修改舊年表，須先人工檢閱並更新補充檔，不自動接受未知日期。

目前提供手動更新工具，不新增排程、不改動任何現有自動抓取排程。

## 本次驗收（2026-10-24）

已收錄 1,211 個休市日期：平日 173 日、週末 1,038 日。其中平日包括 163 日年度表休市與 10 日另行查證的臨時休市；同日去重，排除 5 個週六補行交易日。2016～2020 年由官方歷史年表補足，2021～2026 年取得官方 API，範圍內年度來源無缺漏、無未分類列；不宣稱歷史臨時事件完全無缺漏。

本次新增 30 項股票休市名單測試；與臺北市停班停課、既有交易日曆合計 59 項相關測試通過，整個專案 **743 項離線測試全部通過**。涵蓋年度格式與錯年檢查、空回應不誤判、舊年表摘要驗證、資料倒退與失敗保留舊檔、週六補行交易、人工覆蓋／停用／去重、來源層級、離線重建與只讀列示。

實機僅下載公開來源與產生獨立本地檔；未重新登入、未啟停 API、未更動 MySQL 或既有回補斷點。
