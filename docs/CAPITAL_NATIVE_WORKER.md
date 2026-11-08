# 群益 DLL 背景程序與自動輪替

## 正式行為

FastAPI 與批次 CLI 共用 `CapitalWorkerService`。登入、商品主檔、歷史 COM 連線及報價／Best5／Tick 訂閱全部在唯一的 native worker；API／CLI 的協調程序不載入群益 DLL，持有 MySQL 連線、回補工作鎖與持久斷點。

預設流程：

1. 背景程序私有記憶體達 **3072 MiB**，標記待輪替，不立即殺程序。
2. 已接收的 K 線與下一查詢日期在同一 MySQL 交易提交。提交前禁止輪替，亦不再接受其他會增加負載的新歷史／商品查詢。
3. 提交成功後正常結束 native worker，確認舊程序退出，才建立替代程序。
4. 用本次登入時保存在記憶體中的帳密重新登入，等待報價主機就緒並恢復期望訂閱。
5. 原 API／CLI 抓取流程繼續下一區間，不重排全部工作，也不從頭重抓。

API 與 MySQL 不因背景程序輪替而重啟。輪替期間仍可讀取已存 MySQL；即時報價短暫不可用，重新連線會清除舊程序的 Tick 快取，避免重播資料與舊 Tick 混合計算。

## 設定（`.env`，重啟 API 或 CLI 後生效）

```dotenv
CAPITAL_WORKER_RECYCLE_MB=3072
CAPITAL_WORKER_CONNECT_TIMEOUT_SECONDS=30
CAPITAL_WORKER_RPC_TIMEOUT_SECONDS=180
CAPITAL_WORKER_STOP_TIMEOUT_SECONDS=20
CAPITAL_WORKER_POLL_SECONDS=1
CAPITAL_WORKER_RETRY_SECONDS=15
CAPITAL_WORKER_HISTORY_TIMEOUT_SECONDS=30
API_MEMORY_WARNING_MB=2048
API_MEMORY_STOP_BACKFILL_MB=4096
```

單位實際為 MiB。輪替門檻必須低於 API 的停止門檻，預留單次歷史查詢的尖峰餘裕；設定無效則啟動失敗，不默默忽略。API 本身的 4096 MiB 安全暫停與低水位恢復觀察仍保留。3 GiB 是背景程序的輪替觸發值，並非 OS 硬性記憶體上限；請求期間可能短暫超過，收到區間後仍先保存再輪替。

請以單一 Uvicorn worker 執行，勿使用多 API workers 同時登入。既有 DLL 機器鎖依然生效，不能同時啟動另一個群益原生程式搶用登入。

## 登入與斷線的相容性變更

- API 初次啟動不會暗讀 `.env` 自動登入，仍由 `/api/v1/capital/connect` 明確提供帳密；已成功登入後，背景程序輪替才可自動重登入。
- 錯誤的初次帳密不保留、不持續自動重試；使用者可重新輸入。
- `/api/v1/capital/disconnect` 現在結束 native worker 並清除協調程序的帳密，不再保留舊的 DLL Session；重新連線需登入。
- API 重啟後不會從 SQLite、日誌或磁碟佇列取回帳密。既有 `.env` 仍可由使用者或明確授權的維護腳本用於登入。
- CLI 的 `--resume` 與 `.env` 登入習慣保留，但 DLL 已搬到子程序；舊 `CapitalKLineSession` 僅供相容與離線測試，不是 CLI 預設執行路徑。

## 失敗與保護

- 寫入失敗時解除接收資料的 pin，但不冒稱已提交；MySQL 交易回滾與既有資料庫暫停照常執行，原斷點不前進。
- worker 意外退出、IPC 中斷或逾時以連線問題暫停，不把股票誤標為資料失敗。使用已驗證帳密的重新連線採 15、30、60…最多 300 秒退避。
- 正常退出失敗時，僅可終止 supervisor 自己建立的子程序；替代 worker 必須等舊程序已退出，絕不強解另一個程式的 DLL 鎖或終止 API／MySQL。
- 持有未提交區間時不進行自動輪替。手動暫停不會被自動恢復功能解除；輪替只管理群益連線，不覆寫回補控制旗標。
- 個別訂閱失敗顯示於 `subscription_errors`，不把未恢復的訂閱冒充成功，也不阻止其他已可用的歷史抓取。
- 獨立程序仍可能收到 SDK 的真實斷線、帳號限制或無效資料；此功能不修造成交量，不放寬 MySQL 型態，也不會重新排入先前隔離的 TSLQ 異常工作。
- 歷史 session 的閒置期限另已修正為從請求完成後開始計算，避免查詢耗時被誤算成閒置時間。

## 查看狀態

`GET /api/v1/health` 新增 `native_worker`，可區分 API 與 DLL worker 的 PID、私有記憶體、輪替門檻、`recycle_due`、未確認區間數、已輪替次數、強制退出次數及訂閱錯誤。`/data-status` 的記憶體區塊也會顯示這些資料；原記憶體欄位與樣本仍代表協調程序，不能與 worker 用量混為一談。

## 驗證

完整離線回歸（隔離控制檔、禁止真實 DLL／MySQL）：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 scripts/run_worker_tests_offline.py
```

本次完整回歸共 **386 項全部通過**；前端 `data-status.js` 亦通過 JavaScript 語法檢查。

正式 supervisor 的真實驗收：`scripts/test_capital_supervisor_live.py`。2026-10-19 22:11～22:12 使用 **400 MiB 測試門檻**，同一協調程序 PID 14108 先後自動輪替 `2108 → 6652 → 15496`，查詢 2330 兩天各 270 根。兩次輪替前均確認 MySQL 尚未提交時舊程序仍活著，只有成功提交後才換 PID；舊程序退出碼均為 0，沒有強制終止。

輪替前 native 私有記憶體約 457.9／458.5 MiB，替代程序約 240.4／240.2 MiB；協調程序約 30.1～30.2 MiB。合計 540 根、540 個不重複時間，正式計畫與所選斷點比對未變。原始結果：[result.json](../output/diagnostics/dll-recycle-ba636ed97564/result.json)。測試資料只在新建隔離 schema，並保留供追查。

這是對正式自動輪替路徑的短程驗收，不是 4 GiB 壓力或長期全市場穩態保證。輪替可回收程序內原生配置，但記憶體增長的全部內部根因仍未完全歸因。

## 正式服務切換紀錄（2026-10-19，Asia/Taipei）

經使用者同意，22:19～22:20 完成安全暫停、確認工作鎖與 DLL 鎖釋放後重啟 API，再明確使用 `.env` 登入及恢復原計畫。35 組計畫的 SHA-256 比對一致，MySQL 未重啟，沒有重排所有工作。

- 舊 API PID 27700，切換前私有記憶體 2550.0 MiB。
- 新 API PID 13464，native worker PID 16992；確認 worker 的父程序為該 API，API 未載入群益 SDK 模組。
- 22:20:06：API 67.9 MiB，native worker 263.2 MiB，連線就緒且正在抓取。
- 22:23:33：API 83.7 MiB，native worker 462.6 MiB；正式門檻為 **3072 MiB**，無待提交區間、無錯誤、無強制退出。
- 已完成切換後第一輪 100 個區間，工作計數由 34102 增至 34173，隨後按原有最新資料優先／歷史補齊限量規則等待下輪，並非手動暫停。
- 原斷點工作 564475（00875／週 K）由 pending、70 根續抓至 completed、337 根；TSLQ 異常工作 569074 仍為 failed，未重新排入。

切換前後的原始健康狀態、計畫摘要及取樣保存在 [切換紀錄](../output/diagnostics/native-cutover-20261019-221946.json)。啟動日誌未見異常。正式環境當時尚未達 3 GiB；自動輪替本身的已驗證證據為前述降低測試門檻的真實 DLL 驗收，不宣稱正式服務已在 3 GiB 完成輪替。
