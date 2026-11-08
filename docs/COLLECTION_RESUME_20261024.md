# 2026/10/24 恢復原回補、完整性驗證延後

## 已執行

使用者明確同意安全重啟並登入續抓。原API PID17608已斷線、回補已手動暫停、沒有執行中工作；只停止這個已核對的API，保留原本 `0.0.0.0:8000` 監聽方式，未使用reload或開第二個API。

- 新API PID7300，COM背景程序PID18184。
- 使用 `.env` 登入，帳密未放入命令列、報告或日誌。
- 原35組市場／週期計畫只固定 `as_of_date=2026-10-22`；市場、資料表、原歷史範圍、回抓根數、順序及force=false保留。
- MySQL `candle_backfill_progress` 共154539筆，準備前後及重啟後、開始續抓前的全表進度指紋一致。操作本身不刪除行情、不重設游標；開工後由既有增量規劃器依實際資料處理新截止日，不執行force／全市場歷史重抓。
- `.env` 新增 `CAPITAL_DAY_INTEGRITY_ENABLED=false`、`CAPITAL_WORKER_QUOTE_BACKEND=com`。只停用重型逐日完整性驗收，保留成交量驗證、MySQL交易、原日K優先與記憶體保護／3072 MiB輪替。
- 本機控制另記 `day_integrity_deferred`，包含10/22截止日、使用者延後要求與原設定（未明確設定，原預設true）。現有完整性缺口清單與歷史稽核資料不刪除，也不假裝已通過。

台北時間01:45:23已觀察到 `state=running`、`worker_active=true`、`pause_requested=false`、`quote_ready=true`，正處理上市日K，`windows=1`且處理數增加；查詢結束日10/22。`day_integrity_active=false`、`day_integrity_scheduled=false`。這是恢復成功的即時觀察，不是整批已抓完的聲明。

操作報告（含原計畫、指紋及啟動後樣本）：[collection-resume-20261024-01.json](../output/diagnostics/collection-resume-20261024-01.json)。啟動輸出位於同目錄同名 `.stdout.log`／`.stderr.log`。

## 後續

完整性驗證目前仍暫停；等這批10/22工作完成後再恢復，不在抓取過程插入稽核。要恢復原設定，須在安全區間結束後移除 `.env` 的 `CAPITAL_DAY_INTEGRITY_ENABLED=false` 或改為true，並經核准重新載入API設定；勿只改檔案就宣稱現有程序已生效。這次沒有新增自動重啟或完成後自動驗收機制。

沒有抓取近十年臺北市停班停課資料，也沒有修改正式交易日曆。10/23臨時休市及後續完整性判斷仍按前一份官方核對紀錄另外處理。

輔助操作腳本為 `scripts/resume_collection_only.py`；prepare／activate各有明確授權開關與PID、暫停、計畫、MySQL進度指紋保護，不會自行終止程序。相關42項離線測試通過，測試不接觸真實SDK或正式MySQL。業務程式碼未因本次操作改動。
