# 籌碼與基本面官方端點最小驗證

本階段依 `3dafa_data_TEST_fetch.md`，先實作使用者指定的端點驗證。完整 MVP 的標準化、單位與報表口徑驗收尚未完成，不更動原規格的驗收勾選。

## 使用方式

在 `capital_api` 目錄執行，僅使用 Python 標準庫，不需安裝 requests／httpx。

```powershell
# 離線測試；不啟動 FastAPI、不讀 .env、不連網站或資料庫
.\.venv\Scripts\python.exe -X utf8 .\tests\test_market_data_smoke.py

# 一般 unittest discovery 也只執行離線測試
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -p test_market_data_smoke.py

# 明確啟動一次官方端點驗證；預設另建帶時間戳的輸出目錄
.\.venv\Scripts\python.exe -X utf8 .\tests\test_market_data_smoke.py --live

# 使用已保存的原始回應重新分析；完全不連線
.\.venv\Scripts\python.exe -X utf8 .\tests\test_market_data_smoke.py --replay .\tests\output\endpoint_validation\20261105_live_check
```

每次 live 驗證每個資料端點最多一個 GET，timeout 15 秒、回應上限 20 MB；不自動重試或跟隨重新導向。403／429 會停止該主機其餘端點，留下 skipped 紀錄。不得指定既存輸出目錄，以免覆蓋原始證據。所有端點均取得預期非空資料時 CLI 回傳 0，否則回傳 1；此結束碼只代表端點驗證。

HTTP 200、HTML 錯誤頁、空陣列及 JSON 錯誤訊息不能視為資料成功。程式另檢查每個來源的必要欄位，個股樣本使用回應實際存在的四位數代號，避免誤選清單前端 ETF；優先取上市 2330、上櫃 6488，不存在則選真實替代列。欄位和值原樣保存，尚未進行數值正規化或以日期欄位推定可得時間。

## 2026-11-05 實測

台北時間 13:20:54～13:20:59，12 個端點全部回傳 HTTP 200 與非空 JSON，必要欄位檢查亦通過。首次沙箱內嘗試遇到名稱解析失敗，沒有 HTTP 回應；保留該紀錄，之後經核准在沙箱外完成這一次實際驗證，並非程式自動重試。

| ID | 類別 | 上市列數／股票樣本 | 上櫃列數／股票樣本 | 回應期間 |
|---|---|---|---|---|
| M01 | 個股三大法人 | 7／1101 | 918／6488 | 2026-11-04 |
| M02 | 市場三大法人 | 6／不適用 | 8／不適用 | 2026-11-04 |
| M03 | 融資融券 | 1306／2330 | 919／6488 | 上市缺少日期；上櫃 2026-11-04 |
| M04 | 月營收 | 1088／2330 | 893／6488 | 資料年月 11509；出表日期 1151017 |
| M05 | 一般業損益表 | 108／1215 | 60／1584 | 115 年第 3 季 |
| M06 | 一般業資產負債表 | 108／1215 | 60／1584 | 115 年第 3 季 |

列數只表示本次指定 URL 的實際回傳筆數，並非全市場涵蓋率。T86 預設回應未含 2330，故取實際存在的 1101，沒有補造資料或增加查詢。財報亦從當次回應選樣本，不能據此認定所有公司都已公告同一季。

詳細證據見 [逐來源報告](../tests/output/endpoint_validation/20261105_live_check/report.md)、[結構化結果](../tests/output/endpoint_validation/20261105_live_check/results.json)；每個來源的 `raw/*.bin` 是原始 bytes，`raw/*.json` 保存網址、時間、HTTP、Content-Type、回應標頭與錯誤。輸出目錄已加入 `.gitignore`，實測檔案保留於本機。

## 尚未通過的完整 MVP 條件

2026-11-05 補充查證：已透過瀏覽器核對月營收、損益表、資產負債表共六個上市／上櫃樣本，44 個欄位比對相符；已確認樣本損益為 1～9 月累計，金額為新台幣仟元，EPS 為元／股，資產負債為期末時點且股數欄位另計。詳見 [財務資料查證報告](MARKET_DATA_FINANCIAL_VERIFICATION_20261105.md)。以下待辦保留為完整 MVP 範圍，不能因樣本已查證就認定全欄位與全公司完成。

- 上市 MI_MARGN 回應沒有資料日期，HTTP Date 只是伺服器回應時間。交易日保持未知；需另查官方文件與帶日期來源，不得拿 TPEx 的日期或抓取日期代填。本次沒有猜替代網址，也沒有再抓一次。
- `observed_period_fields` 只保存來源日期欄位；尚未完成日期有效性、交易日、新鮮度與公告可得時間驗證。
- 逐欄確認股／張／金額／比例，並建立正規化 mapping、數值驗證與樣本。
- 分清出表日期、營收所屬月份、財報年季、公告日期與修訂版本；來源未提供者保留未知。
- 損益表確認累計／單季口徑，資產負債表確認期末時點。不得僅憑「第 3 季」就當成單季數字。

因此全部結果的 `mvp_status` 仍為 `pending`，未把原 README 的 M01～M06 勾選為通過。

## 為什麼需要補充標準化格式

標準化是把不同來源欄位轉成一致名稱與型態，並保留能正確解讀數值的資訊。例如同一份損益表有「營業收入」與「基本每股盈餘（元）」，不能用單一總體 `unit` 描述所有欄位。融資券的「今日餘額」與「今日增減」也不同，即使單位相同也不能混用。

以下只示範欄位設計，不是真實數據，也尚未作為完整 MVP 的正式契約：

```json
{
  "values": {"revenue": null, "basic_eps": null},
  "units": {"revenue": null, "basic_eps": "TWD/share"},
  "period_basis": null,
  "source_report_date": null,
  "announced_at": null,
  "fetched_at": null
}
```

上面保留原先「查證前」的示意格式。此次已確認卜蜂、精剛損益樣本的 `revenue` 單位為新台幣仟元，`period_basis` 為年初至當季末累計；後續正式契約應填入已驗證資訊。其他未查證欄位仍不能自行假定。

## 為什麼現有下載函式不能直接套用

`app/services/stock_master_sources.py` 的 `read_official()` 是公司／ETF 主檔專用：

1. 它只接受公司及 ETF 四個既有網址；本次 12 個來源不在允許清單。
2. 它回傳解碼後的字串，呼叫端拿不到成功回應的 HTTP 狀態、Content-Type、回應標頭及原始 bytes。
3. 遇到 HTTP 錯誤時它轉成例外，沒有把錯誤頁原文保存成測試證據。

這些行為適合原本的主檔流程，但不足以完成此次診斷。新增測試使用自己的 `fetch_once()` 記錄上述資訊，不修改現有函式及其使用者。

官方索引：[TWSE OpenAPI](https://openapi.twse.com.tw/)／[TPEx OpenAPI](https://www.tpex.org.tw/openapi/)。本次結果以這台電腦保存的 HTTP 回應為準，瀏覽服務對 TPEx 文件索引曾回傳 403，沒有將其誤當本機端點失敗或繞過該限制。
