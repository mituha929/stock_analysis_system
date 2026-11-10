# 指定期間的官方歷史資料抓取

使用者於 2026-11-05 明確將本次測試範圍擴為：上市／上櫃三大法人與融資融券近兩個月、月營收近五年、一般業損益表與資產負債表最近 20 季。因此原 MVP「只抓一個日期／一期」限制由這次指示擴充；其餘不動正式行情、資料庫與排程的限制繼續保留。

## 本次固定範圍

- 日資料：2026-09-05～2026-11-04，採既有官方休市快照與週末排除後的 38 個候選交易日；第一個交易日為 2026-09-07。個股三大法人、市場三大法人、個股融資融券各有上市／上櫃兩份報表，另保留上櫃市場法人「股票」與「有價證券」兩種範圍，共 266 組。
- 月營收：2021-10～2026-09，共 60 個月。上市／上櫃各分國內（0）、國外（1）公司，共 240 份歷史檔。結束月份沿用已確認可取得的最新完整月份，不將尚未全數申報的 10 月當成完整月份。
- 財報：2021-Q4～2026-Q3，共 20 季。一般業損益表、資產負債表各有上市／上櫃兩市場，共 80 組。2026-Q3 尚在公告期間，當季公司覆蓋可能不足，不補造未公告資料。

總計 586 組日期／月份／季度報表。原先 548 組計畫在發現櫃買市場彙總的兩種範圍後，補入 38 組股票彙總。每份來源通常一次回傳全市場，於本機解析；不是對每檔股票逐筆請求。原始完整回應保留，財報正規化只選一般業表格，不把金融等其他產業表格混入。

原驗收批次先後使用 4、3、2 秒間隔。現行日常與歷史下載預設及最低間隔為 1 秒。既有成功快取可直接重播。

## 本次交付結果

已完成 586／586 組報表，356,638 筆正規化紀錄；586 個原始檔 SHA-256 核對成功，沒有缺檔、重複主鍵或跨期完全相同的正規化資料。先前獨立瀏覽器查證的 12 個數值參考全部相符，23 項離線測試通過。

下載期間的兩次無 HTTP 回應及一次 HTTP 502，均已明確補抓成功，舊失敗紀錄保留於 `attempt_history`；HTTP 502 原文另存 attempt 檔。

2026-11-10 按使用者核定的 ±10 元容差重跑驗收，`download_complete=true`、`complete=true`。2026-10-07 上櫃「有價證券」法人彙總的三列差額均為 +1 元，列在 `accepted_exceptions`，官方淨額照錄。2026-Q3 的保存快照只涵蓋抓取時已公告的公司。

詳見 [完整性與數值驗收](../tests/output/history_20261105/audit.md) 及 [結構化驗收結果](../tests/output/history_20261105/audit.json)。

## 官方來源的確認依據

以下官方頁面原文及引用的 JavaScript 已保存至 `tests/output/history_source_discovery/`：

| 資料 | 官方查詢頁 | 確認的報表路徑／參數 |
|---|---|---|
| 上市個股法人 | [T86](https://www.twse.com.tw/zh/trading/foreign/t86.html) | 頁面 `data-api=/fund/T86`；`date=YYYYMMDD`、`selectType=ALLBUT0999`、`response=json` |
| 上市市場法人 | [BFI82U](https://www.twse.com.tw/zh/trading/foreign/bfi82u.html) | `data-api=/fund/BFI82U`；`dayDate=YYYYMMDD`、`type=day` |
| 上市融資券 | [MI_MARGN](https://www.twse.com.tw/zh/trading/margin/mi-margn.html) | `data-api=/marginTrading/MI_MARGN`；`date=YYYYMMDD`、`selectType=ALL`；回應包含日期及個股表 |
| 上櫃個股法人 | [日明細](https://www.tpex.org.tw/zh-tw/mainboard/trading/major-institutional/detail/day.html) | 頁面 `tables.init` 的 `insti/dailyTrade`，`type=Daily`、`sect=EW` |
| 上櫃市場法人 | [日彙總](https://www.tpex.org.tw/zh-tw/mainboard/trading/major-institutional/summary/day.html) | `insti/summary`，`type=Daily`；`prod=0` 上櫃有價證券、`prod=1` 上櫃股票，分開保存 |
| 上櫃融資券 | [融資融券餘額](https://www.tpex.org.tw/zh-tw/mainboard/trading/margin-trading/transactions.html) | `margin/balance` |
| 月營收 | [官方歷史彙總表](https://mopsov.twse.com.tw/mops/web/t21sc04_ifrs) | POST 的 TYPEK 為 sii0／sii1／otc0／otc1，指定民國年、月份；回應明確產生 `/nas/t21/{sii或otc}/t21sc03_{民國年}_{月}_{0或1}.html` 下載連結 |
| 損益表 | [官方季彙總表](https://mopsov.twse.com.tw/mops/web/t163sb04) | 頁面 form action 為 `ajax_t163sb04`；TYPEK=sii／otc、民國 year、season=01～04 |
| 資產負債表 | [官方季彙總表](https://mopsov.twse.com.tw/mops/web/t163sb05) | 頁面 form action 為 `ajax_t163sb05`；同上 |

TWSE 的 `web-report.js` 確認報表使用 `/rwd/zh` 前綴。TPEx 的 `main.js` 確認 `/www/{LANG}/{ACTION}`，`tables.js` 確認正常 POST 及日期 `yyyy/mm/dd`；若送成 `YYYYMMDD`，來源可能回傳最新日期，因此嚴格核對 `response.date`，不把 HTTP 200 當成指定日期成功。

月營收下載路徑由官方表單回應取得，最早／最新月份及兩市場、國內／國外均先行實測；沒有將只供最新資料的 OpenAPI 當成歷史介面。

## 操作

在 `capital_api` 目錄：

```powershell
# 只建立有界計畫，不連線
.\.venv\Scripts\python.exe -X utf8 .\scripts\collect_market_history.py

# 測試最早／最新一期；已保存的相同 URL＋POST body 沿用快取
.\.venv\Scripts\python.exe -X utf8 .\scripts\collect_market_history.py --probe --live --interval 1

# 執行全部；中斷後重跑同一指令接續尚未取得的來源
.\.venv\Scripts\python.exe -X utf8 .\scripts\collect_market_history.py --live --interval 1

# 僅在需要時明確補抓先前沒有 HTTP 回應的連線錯誤，每份一次
.\.venv\Scripts\python.exe -X utf8 .\scripts\collect_market_history.py --live --interval 1 --retry-network-errors

# 或明確補抓無 HTTP 回應及 502／503／504 伺服器錯誤；不重試 403／429
.\.venv\Scripts\python.exe -X utf8 .\scripts\collect_market_history.py --live --interval 1 --retry-transient-errors

# 僅用已保存原文重新解析，完全不連線
.\.venv\Scripts\python.exe -X utf8 .\scripts\collect_market_history.py --replay

# 獨立完整性與數值驗收；未完成或有待查項目時回傳非零
.\.venv\Scripts\python.exe -X utf8 -m scripts.audit_market_history

# 離線測試
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -p test_market_history.py
```

可用 `--groups daily`、`--groups revenue` 或 `--groups financial` 進行分組執行；同一目錄的 `jobs.json`／進度報告反映最後一次執行所選範圍。完整驗收應以未加 `--groups` 的全範圍 replay 重建完整清單後執行。

預設輸出目錄為 `tests/output/history_20261105/`，已由 `.gitignore` 排除：

- `raw/`：原始 bytes 與請求中繼資料，含實際網址、POST 參數、HTTP、Content-Type、時間、SHA-256。早期格式探索的回應保留，不會混入正規化成功資料。
- `normalized/`：逐報表正規化 JSON，含期間、來源、欄位單位、資料列；值以精確十進位字串保存，空值為 null。
- `parsed/`：解析狀態與來源追蹤。
- `plan.json`／`jobs.json`：本次日期與查詢範圍。
- `results.json`／`report.md`：抓取進度；`audit.json`／`audit.md`：獨立驗收。

## 資料語意與執行邊界

1. 歷史下載依序執行，預設及最低請求起始間隔為 1 秒，單次 timeout 25 秒。來源回應較慢時，等該次完成才繼續。不自動重試或跟隨重新導向。403、429 或封鎖頁會停止該主機後續請求，並保留錯誤與跳過紀錄。明確加上 `--retry-network-errors` 才補抓沒有 HTTP 回應且沒有內容的連線錯誤。`--retry-transient-errors` 另包含 HTTP 502、503、504，每份在該輪只試一次。舊失敗記錄保留在 `attempt_history`，原回應另存，成功快取與 HTTP 封鎖不會重抓。
2. 網路請求只能由 `--live` 明確啟動；一般計畫與離線測試不啟動網站查詢、FastAPI、群益登入或正式資料庫操作。
3. 個股法人保存外資（不含外資自營商）、投信、自營商買賣超股數。市場法人保存買進、賣出、淨額，單位為元，分類與合計列分別保留，不能重複加總。上櫃彙總的 `segment=stocks` 是股票範圍，未加該 segment 的是有價證券範圍，並附 `market_scope`，不可跨範圍直接合併。
4. 融資券保存前日／今日餘額，單位為張；不將餘額當成增減，也不強制乘 1,000 換成股，避免非標準交易單位商品錯算。
5. 營收金額、財報金額採新台幣仟元，百分比、EPS（元／股）、股數（股）逐欄另標註。一般業財報來源欄位名稱原樣保留，不強行將不同年度改名的欄位視為同一口徑。
6. 損益表是當年截至該季累計，資產負債表是期末時點。官方季彙總 HTML 不回顯年度，年度來自已查證的官方 year 參數，季別以回應標題核對；每份資料的 `period_provenance` 明列此限制。另檢查不同期間是否意外收到完全相同回應。
7. 有些公司 IPO、轉板、下市或尚未公告，不能假定每個代號每期都存在。報告區分「20 季來源均取得」與「每家公司都有 20 筆」；不以補零補齊個股歷史。
8. 公告可得時間、修訂版本、逐公司合併／個別報表類型仍未完整核實；本次檔案不能直接當成已消除未來資訊洩漏的回測資料。

市場法人差額為「買進減賣出再減公告淨額」，以元計算。非零差額保留 `net_difference_flag` 和帶正負號的 `net_difference_twd`。±10 元（含端點）內通過，超過則該報表驗收不通過。資產負債表仍按原驗收政策允許 2 仟元的資產、負債與權益等式差額。所有檢查保留官方原值。

本次新增程式只用 Python 標準庫；既有行情、資料庫表結構與排程不變。

## 2026-11-10 第二階段前置複核

第二階段的資料庫依使用者決定，正式資料使用 MySQL，測試使用隔離的 SQLite。此次只驗證既有檔案與官方查詢結果。

### 上櫃市場法人差額

以 `type=Daily`、`prod=0`、`date=2026/10/07`、`response=json` 重查櫃買中心。HTTP 200，回應日期為 2026-10-07，原文與 2026-11-05 保存的檔案逐位元組相同（SHA-256 `aa8e0882a5898541518fbeda769aaaecc415f7407568058e01bbf01f518ff840`）。直接從原文以整數重算 38 日 × 2 範圍 × 8 列，共 608 列。股票範圍 304 列全部相符，有價證券範圍只有下列三列差 1 元。

| 分類 | 買進減賣出（元） | 官方淨額（元） |
|---|---:|---:|
| 自營商避險 | -1,079,030,781 | -1,079,030,782 |
| 自營商合計 | -598,410,754 | -598,410,755 |
| 三大法人合計 | 3,129,349,136 | 3,129,349,135 |

官方淨額的子項與合計彼此相符，差額可追溯至自營商避險列。報表註明外幣加掛 ETF 按當日下午 3:30 公告匯率換算後，分別計入買進、賣出及淨額。這提供可能成因，尚未查到櫃買中心就該日差額或取整順序的說明。三筆均已按使用者核定的容差通過，並以旗標寫入 P1 的 SQLite 暫存庫。見[櫃買中心日報](https://www.tpex.org.tw/zh-tw/mainboard/trading/major-institutional/summary/day.html)與[既有原文](../tests/output/history_20261105/raw/9f5109a169367edefafe2bac179135a8e3c398ec0f1140fc838c5df0dce9c9d5.bin)。

### 財報公告、版本與產業

- 以公司代號、民國 115 年 10 月查詢歷史重大訊息。1215 卜蜂的第三季合併財報董事會通過公告於 2026-10-30 15:47:04 發布，1584 精剛的第三季合併財報重大訊息於 2026-10-29 17:27:11 發布，均為台灣時間。這兩個時間對應重大訊息。用公司代號、115 年第 3 季另查財務報告公告，兩份均為合併財報，並顯示更補正為「無」。所查的財報公告內容未列該報表的首次發布時間，完整報表及各欄位的歷史可得時間仍待核實。見[歷史重大訊息](https://mops.twse.com.tw/mops/#/web/t05st01)與[財務報告公告](https://mops.twse.com.tw/mops/#/web/t163sb01)。
- 以興櫃、114 年第 2 季查詢更補正專頁，確認元進莊（7895）同一份合併財報有兩次更正。歷史重大訊息分別記錄 2026-08-13 19:27:03 和 2026-09-21 18:22:25。專頁可追蹤更正事件，但本次尚未取得各版完整數值，因此既有財報快照的歷史版本仍未驗收。見[財務報告更補正查詢](https://mops.twse.com.tw/mops/#/web/t56sb31_q1)。
- 對照已存原文與正規化結果，2026-Q3 上櫃損益與資產負債原文均有 6026 福邦證，兩份一般業輸出均未納入該列。證券業等其他產業的表格與欄位對應仍待分別驗證。

### 來源授權

政府資料開放平臺明列適用第 1 版條款的資料集包括[上櫃個股法人](https://data.gov.tw/dataset/11856)、[上櫃股票法人彙總](https://data.gov.tw/dataset/11743)、[上櫃融資融券餘額](https://data.gov.tw/dataset/11387)、[上市月營收](https://data.gov.tw/dataset/18420)、[上櫃月營收](https://data.gov.tw/dataset/56510)、[上市一般業損益](https://data.gov.tw/dataset/91998)與[上市一般業資產負債](https://data.gov.tw/dataset/94831)。條款允許散布等再利用並要求顯名。授權核對以各資料集標示的資源為準，尤其上櫃股票彙總和有價證券彙總的範圍不同。歷史網頁端點、MOPS 歷史報表及附件仍需逐來源確認使用與對外再散布範圍。見[政府資料開放授權條款](https://data.gov.tw/license)、[證交所使用條款](https://www.twse.com.tw/zh/terms/use.html)與[櫃買中心使用條款](https://www.tpex.org.tw/zh-tw/gtsm_disclaimer.html?l=zh-tw)。

第一階段樣本與既有歷史範圍驗收已完成。第二階段 P1 的操作、證據與正式入庫待辦見[實作計畫](implementation_plan.md)。
