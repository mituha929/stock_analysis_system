# 指定期間的官方歷史資料抓取

使用者於 2026-11-05 明確將本次測試範圍擴為：上市／上櫃三大法人與融資融券近兩個月、月營收近五年、一般業損益表與資產負債表最近 20 季。因此原 MVP「只抓一個日期／一期」限制由這次指示擴充；其餘不動正式行情、資料庫與排程的限制繼續保留。

## 本次固定範圍

- 日資料：2026-09-05～2026-11-04，採既有官方休市快照與週末排除後的 38 個候選交易日；第一個交易日為 2026-09-07。個股三大法人、市場三大法人、個股融資融券各有上市／上櫃兩份報表，另保留上櫃市場法人「股票」與「有價證券」兩種範圍，共 266 組。
- 月營收：2021-10～2026-09，共 60 個月。上市／上櫃各分國內（0）、國外（1）公司，共 240 份歷史檔。結束月份沿用已確認可取得的最新完整月份，不將尚未全數申報的 10 月當成完整月份。
- 財報：2021-Q4～2026-Q3，共 20 季。一般業損益表、資產負債表各有上市／上櫃兩市場，共 80 組。2026-Q3 尚在公告期間，當季公司覆蓋可能不足，不補造未公告資料。

總計 586 組日期／月份／季度報表。原先 548 組計畫在發現櫃買市場彙總的兩種範圍後，補入 38 組股票彙總。每份來源通常一次回傳全市場，於本機解析；不是對每檔股票逐筆請求。原始完整回應保留，財報正規化只選一般業表格，不把金融等其他產業表格混入。

本輪前段以 4 秒間隔下載，之後改成 3 秒；使用者再明確指定 2 秒，故目前預設及最低間隔改為 2 秒。既有成功快取不重抓，剩餘部分用 `--interval 2` 接續。

## 本次交付結果

已完成 586／586 組報表，356,638 筆正規化紀錄；586 個原始檔 SHA-256 核對成功，沒有缺檔、重複主鍵或跨期完全相同的正規化資料。先前獨立瀏覽器查證的 12 個數值參考全部相符，23 項離線測試通過。

下載期間的兩次無 HTTP 回應及一次 HTTP 502，均已明確補抓成功，舊失敗紀錄保留於 `attempt_history`；HTTP 502 原文另存 attempt 檔。

抓取完成與完整數值驗收分開：`download_complete=true`、`complete=false`。唯一的三個數值待查項目均來自 2026-10-07 上櫃「有價證券」法人彙總，自營商合計、自營商避險、三大法人合計各差 1 元；已核對原始回應，保留官方數字，不自行改寫公告淨額。2026-Q3 仍是公告中的不完整公司覆蓋，不視為所有公司均已公告。

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
.\.venv\Scripts\python.exe -X utf8 .\scripts\collect_market_history.py --probe --live --interval 2

# 執行全部；中斷後重跑同一指令接續尚未取得的來源
.\.venv\Scripts\python.exe -X utf8 .\scripts\collect_market_history.py --live --interval 2

# 僅在需要時明確補抓先前沒有 HTTP 回應的連線錯誤，每份一次
.\.venv\Scripts\python.exe -X utf8 .\scripts\collect_market_history.py --live --interval 2 --retry-network-errors

# 或明確補抓無 HTTP 回應及 502／503／504 伺服器錯誤；不重試 403／429
.\.venv\Scripts\python.exe -X utf8 .\scripts\collect_market_history.py --live --interval 2 --retry-transient-errors

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

1. 所有下載串行執行，依使用者指定將預設及最低請求起始間隔設為 2 秒、單次 timeout 25 秒；若來源回應較慢，等待該次完成才繼續，不會同時發出多筆。不自動重試、不跟隨重新導向。403、429 或封鎖頁會停止該主機後續請求，保留錯誤與跳過紀錄。明確加上 `--retry-network-errors` 才補抓「無 HTTP 回應且無內容」的連線錯誤；`--retry-transient-errors` 另包含 HTTP 502／503／504，每份在該輪只試一次。舊失敗紀錄放在 `attempt_history`、舊回應另存 attempt 檔，成功快取與 HTTP 封鎖不會重抓。
2. 網路請求只能由 `--live` 明確啟動；一般計畫與離線測試不啟動網站查詢、FastAPI、群益登入或正式資料庫操作。
3. 個股法人保存外資（不含外資自營商）、投信、自營商買賣超股數。市場法人保存買進、賣出、淨額，單位為元，分類與合計列分別保留，不能重複加總。上櫃彙總的 `segment=stocks` 是股票範圍，未加該 segment 的是有價證券範圍，並附 `market_scope`，不可跨範圍直接合併。
4. 融資券保存前日／今日餘額，單位為張；不將餘額當成增減，也不強制乘 1,000 換成股，避免非標準交易單位商品錯算。
5. 營收金額、財報金額採新台幣仟元，百分比、EPS（元／股）、股數（股）逐欄另標註。一般業財報來源欄位名稱原樣保留，不強行將不同年度改名的欄位視為同一口徑。
6. 損益表是當年截至該季累計，資產負債表是期末時點。官方季彙總 HTML 不回顯年度，年度來自已查證的官方 year 參數，季別以回應標題核對；每份資料的 `period_provenance` 明列此限制。另檢查不同期間是否意外收到完全相同回應。
7. 有些公司 IPO、轉板、下市或尚未公告，不能假定每個代號每期都存在。報告區分「20 季來源均取得」與「每家公司都有 20 筆」；不以補零補齊個股歷史。
8. 公告可得時間、修訂版本、逐公司合併／個別報表類型仍未完整核實；本次檔案不能直接當成已消除未來資訊洩漏的回測資料。

驗收數值政策：市場法人金額以元檢查，買進減賣出與公告淨額不相等即列待查；資產負債表以仟元呈現，資產減負債減權益容許 2 仟元的呈現／取整差額，超過即列待查。所有檢查都保留官方原值，不覆寫修正。

本次新增程式只用 Python 標準庫；既有行情、資料庫表結構與排程不變。
