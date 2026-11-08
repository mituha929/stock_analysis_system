# 長時間回補記憶體診斷

日期：2026-10-19，Asia/Taipei。

## 結論

目前可確認超標前的大幅增量發生於歷史查詢期間，並已離線重現歷史 COM session 的閒置計時問題。現有證據仍不足以把本次約 4 GiB 私有記憶體全部歸因於 DLL 洩漏或 session 重建；需要低用量起點的受控原生測試才能完成歸因。

本次只做唯讀診斷與離線模擬。新增此報告及一支離線重現腳本，未修改正式服務程式、設定、K 線、斷點或帳號，未重啟服務、重新登入群益或恢復正式回補。

## 正式程序與最後一次增量

- API PID：18148，啟動於 2026-10-18 01:51:48。
- 2026-10-19 14:07:56：私有記憶體約 4155.5 MiB，實體工作集約 3275.3 MiB。
- 約 14:16：私有記憶體約 4157.3 MiB，實體工作集約 3267.1 MiB。
- 該段約八分鐘的暫停觀察增加約 1.8 MiB，未出現另一個約 100 MiB 的跳升；記憶體也未回到恢復門檻。
- 正式回補仍為 `paused`，原因 `memory`；停止門檻 4096 MiB，恢復門檻 3072 MiB。

從既有 SQLite 控制檔以 `mode=ro` 讀到超標前最後 32 筆記憶體樣本，關鍵紀錄如下，時間已換算為台北時間：

| 時間 | 階段 | 私有記憶體 MiB | 與前筆差異 |
| --- | --- | ---: | ---: |
| 08:59:14.353510 | before_request | 3996.4 | 0.0 |
| 08:59:19.439605 | after_request_before_write | 4100.9 | +104.5 |
| 08:59:38.098915 | before_group_planning，執行過 GC | 4101.9 | +1.0 |

同一段前面的族群規劃與領取工作樣本大致維持 3996.4 MiB。這支持優先調查歷史查詢區段；樣本量測的是整個 API 程序，仍可能包含同時運行的背景活動，不能視為 DLL 內部配置的直接追蹤。

最近 32 筆紀錄只涵蓋超標前短時間，沒有保留從程序啟動到約 4 GiB 的完整增長曲線。`started_at`、`windows` 會在恢復輪次更新，不能把它們直接當作整個程序的累積統計。

## 已離線重現的 session 閒置計時問題

位置：`app/services/capital_history_service.py`，`_ReusableHistoryComSession._run()`。

目前程式在取得請求、尚未執行歷史查詢時，就設定 `idle_deadline = time.monotonic() + idle_timeout_seconds`；查詢完成後沒有重新計時。因此查詢與連線恢復耗時也被算進「閒置 30 秒」。

新增 `scripts/diagnose_history_idle_timeout_offline.py`，使用既有 session 主迴圈，替換 COM、時間、請求佇列與原生查詢，沒有載入真實 DLL、登入或連線資料庫。重現結果：

| 模擬查詢耗時 | 設定閒置期限 | 查詢完成後實際可閒置時間 |
| ---: | ---: | ---: |
| 0 秒 | 30 秒 | 30.00 秒 |
| 5 秒 | 30 秒 | 25.00 秒 |
| 31 秒，包含可能的連線恢復 | 30 秒 | 0.05 秒 |

這證明 session 可能過早結束，導致後續查詢重新建立 COM 物件、Login 與行情主機連線。即使修正計時，族群規劃、MySQL 寫入或兩輪工作相隔超過真正的 30 秒時，現有設計仍會重建 session。

本次沒有量到正式程序的 session ID、建立次數、每次建立／關閉的原生配置量。因此「session 重建使 DLL 資源持續殘留」目前是待驗證假設，不能稱為已證明的本次超標根因。

建議修正方向：閒置時間從請求完成後開始；另評估批次執行期間明確保留 session，互動查詢仍保留閒置釋放行為。

## 已執行的 Python 物件／快取壓力測試

執行現有 `scripts/test_backfill_memory_offline.py`：

- 240 次模擬查詢，每次 1200 根，合計處理 288000 根。
- 暖機後 Python 配置量：28.094 MiB。
- 最後 Python 配置量：28.284 MiB；原始值計算的增量約 0.189 MiB。
- 峰值：約 29.742 MiB。
- 歷史快取保持 20 組，接收暫存 0 筆，測試通過。

程式檢查亦確認批次呼叫使用 `cache_result=False`，接收暫存及寫入區段結果會清理。這些結果沒有重現持續增加的 Python K 線物件／互動快取，但模擬測試不包含原生 COM／DLL、真實 MySQL driver、完整族群規劃及所有正式背景工作。

先前真實查詢測試記錄在 `README_HISTORY_MEMORY_TEST_20261018.md`：首筆查詢增加約 122.262 MiB，後續 19 筆回應後合計增加約 0.883 MiB。這與本次歷史查詢區段約 104.5 MiB 的跳升值得對照，但不足以證明這兩次跳升來自同一個內部配置。

Python 配置量與程序私有記憶體是不同量測。原生程式透過 C allocator 配置的空間可能不在 Python 記憶體管理器的追蹤範圍，參考 [Python 3.12 記憶體管理文件](https://docs.python.org/3.12/c-api/memory.html) 與 [tracemalloc 文件](https://docs.python.org/3.12/library/tracemalloc.html)。

## 同時發現的資料寫入錯誤

在 MySQL 唯讀交易中核對最後一筆工作：

- 資料表：`candle_backfill_jobs`，工作 ID 569074。
- 商品：TSLQ，市場 0，週期 `month`。
- 狀態：`pending`，本次區間尚未成功提交。
- 控制檔記錄的查詢範圍：2018-02-01 至 2022-05-31。
- 錯誤：`K 線進度資料庫操作失敗：1264 (22003): Out of range value for column 'volume' at row 1`。
- MySQL 更新時間為 2026-10-19 00:59:19.526993 UTC，即台北 08:59:19.526993。
- 已透過 `information_schema.columns` 唯讀確認，正式 `candles` 與 `odd_lot_candle` 的 `volume` 均為 `BIGINT UNSIGNED NOT NULL`。

此錯誤發生在 104.5 MiB 跳升後的寫入階段。當時資料列的成交量原值及完整 callback 回傳內容沒有保存，本次不重新對高記憶體程序發送歷史查詢，故無法確認是負值、過大值或來源／解析異常。

資料庫現有 TSLQ 月線為 2 筆；這只是已儲存的內容，不能反推失敗查詢實際回傳的筆數。現行 `Candle.volume` 沒有限制到 MySQL `BIGINT UNSIGNED` 範圍，所有 MySQL 錯誤又會包裝成 `MySQLRepositoryError`，由 runner 以資料庫問題暫停。這類資料值錯誤可能在恢復後重試相同區間，應與暫時斷線區分；目前記憶體恢復門檻仍阻止正式回補繼續。

建議修正方向：記錄有問題的股票、時間與成交量原值，寫入前驗證可儲存範圍，將不可重試的資料錯誤隔離處理。不能把異常成交量任意歸零、取絕對值或改寫成其他數值。

## 完成歸因仍需要的驗證

同日後續唯讀查核已確認 TSLQ 該月線區間有 20／52 根回傳負成交量，足以解釋 1264；詳見 [成交量越界診斷](VOLUME_OUT_OF_RANGE_DIAGNOSIS_20261019.md)。這是後續補充證據，不代表先前約 4 GiB 記憶體累積的原因亦已確定。

1. 在釋放記憶體後，以正常重啟的新 API 程序建立低用量基準，保持正式回補暫停。
2. 對每次歷史 session 建立、查詢前後、關閉前後記錄 session ID、程序私有記憶體及 Python 配置量。
3. 比較連續共用 session 的短批次，與超過閒置期限後重建 session 的數輪唯讀查詢；設定低於正式 4 GiB 門檻的停止條件。
4. 另以離線／隔離資料驗證成交量錯誤分類與斷點行為。

這些原生歸因測試尚未執行；目前程序仍約 4 GiB、整機記憶體壓力高，不適合在其上繼續觸發可能再增加約 100 MiB 的歷史查詢。

## 本次重現指令

既有 session 重用單元測試亦已執行，1 項通過；沒有呼叫真實 DLL。

於 `capital_api` 目錄執行，皆不使用真實資料庫或群益 DLL：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 .\scripts\diagnose_history_idle_timeout_offline.py
.\.venv\Scripts\python.exe -B -X utf8 .\scripts\test_backfill_memory_offline.py
```
