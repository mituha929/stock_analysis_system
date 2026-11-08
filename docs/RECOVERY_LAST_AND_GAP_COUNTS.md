# 待補齊最後，商品與缺少日期分開統計

## 流程

1. 所有選定市場的日 K 正常抓取（`latest`）。
2. 其他選定週期正常抓取（`latest`），保留原本相對順序。
3. 所有週期待補齊／較舊歷史（`catchup`、`history`），維持每工作及整輪預算。
4. 若設定與實際日期允許，最後執行日 K 完整性驗收及缺口補抓。

空回應、退避、歷史待補或未通過驗收不再擋住其他週期的正常輪；仍保留資料與斷點，不假裝補齊。手動暫停、連線／資料庫錯誤及記憶體保護仍按原安全邊界停止。未選 day 不新增 day；CLI 的 max-jobs 仍可限制本次商品／工作範圍，不代表全部週期已遍歷。

舊保存計畫續抓時直接使用新順序，不重設進度。每日排程遇到舊版卡在日 K、尚未遍歷其他週期的普通計畫，先接續原保存範圍，不重新建全市場計畫或改截止日。使用者準備中的完整重抓、固定範圍與手動暫停仍受既有保護。

`priority_phase`：`day` 日 K 正常輪、`other` 其他週期正常輪、`recovery` 待補／驗收、`finished` 本輪無可自動執行工作。`normal_round_complete=true` 代表正常階段已遍歷，不保證每個商品都有資料。`day_pending`／`deferred` 是待補工作數；`audit_pending_symbols`／`audit_pending_dates` 是驗收待補商品及日期，不把兩種單位相加。

## 商品與日期

例如一檔商品缺十個日 K 日期，顯示「待查 1 檔／10 個日期」，不顯示成十檔。SQL 去重識別採行情表、市場、商品代號；同商品多週期不重複算商品，同商品同日／分 K 日期不重複計日期。整張與零股／不同市場仍是不同商品。

週月缺口另以期間計數，不把一根週 K 隨意展成七個缺少交易日。已解決項目預設不納入待查；僅停止重試的 pending_review 並非已解決。

- `GET /api/v1/backfill/empty-reviews`：新增整個範圍的 `counts = {records,symbols,dates,periods}`，不受分頁大小影響。`include_resolved=true` 時統計包含所選已解決紀錄。
- `GET /api/v1/backfill/day-integrity`：新增整個範圍的 `counts = {symbols,dates}`。
- `/backfill/control`：`pending_review` 相容保留為紀錄數，新欄位 `pending_review_symbols`／`pending_review_dates`／`pending_review_periods` 才是對應單位。日 K 驗收的 `unresolved_symbols` 與 `unresolved_dates` 用來顯示待查，不用排程閘門的 blocked_symbols 代替待查檔數。

## 驗收與套用

修改及離線驗收不重啟 API、不登入群益、不讀寫正式 MySQL 或重排正式工作；不修改 `.env`。本次沒有新增資料庫 schema，只調整既有查詢、工作順序、統計及畫面。人工指定重試多缺口的另一個問題不在本次兩項修改範圍內。

```powershell
.\.venv\Scripts\python.exe -B -X utf8 scripts\run_worker_tests_offline.py
node --check app/static/data-status.js
node scripts/test_data_status_frontend.cjs
```

新增的統計測試以記憶體 SQL 驗證一檔十日期、跨週期／重複計畫去重、市場／分頁／已解決狀態篩選；順序測試涵蓋退避、舊斷點續抓、七週期、零 recovery 預算、假日驗收最後及原保護不變。兩支隔離 MySQL 腳本的預期順序亦已更新，但本次未執行實機 MySQL／群益驗收。正式套用需要使用者安全重啟載入新版。

本次新增 16 項測試，完整 **799 項離線測試通過**（17.910 秒）；前端語法與行為測試通過，原控制 5 秒、資料快照 15 秒輪詢不變。舊快照若只有排程 blocked 數而無去重待查商品數，明確顯示「待查商品數尚未統計」，不以日期／排程筆數猜測檔數。
