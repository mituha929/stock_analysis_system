# 國內行情事件分流與連線診斷

目前主連線預設為COM，實機已成功收到 `(3003,0)`；新後端設定、事件格式與23:22驗收見 [COM主連線](CAPITAL_COM_BACKEND.md)。下列SKQuote來源與空ID備援說明專指仍可明確選用的 `dll` 後端。COM的事件來源為SKQuoteLib、第二個參數是實際錯誤碼，不套用DLL的來源字串判斷或登入ID備援。

## 修正範圍

依隨 SDK 2.13.58 附的 `Python策略王DLL元件使用說明.docx`，共用 `OnConnection(loginID, code)` 回呼中，國內行情來源為 `SKQuote`，其他來源可能是登入帳號、回報或其他主機。

- 只有 `SKQuote` 的事件可更新國內行情狀態。接受等價的 ASCII bytes、大小寫與前後空白；不把任意包含 SKQuote 的來源當成國內行情。
- `3001` 只代表建立連線，清除過期的就緒狀態並等待 `3003`；仍須收到 `SKQuote/3003` 才准許行情操作。
- 其他通道的 `3001/3002/3003/3033/5001` 只進入診斷，不設為就緒、不觸發自選股恢復，也不把已就緒行情誤設斷線。
- `SKQuote/3002`、`SKQuote/3033` 仍立即清除就緒狀態；不為通過測試而忽略真正的行情斷線。
- 主動清理前會標記 cleanup，清理期間的事件不再改變行情狀態。晚到的 `3003` 不會把已停止程序的行情標成就緒。
- 等待就緒的截止檢查前先處理待送達事件，並再次核對就緒狀態，避免 `3003` 恰好與截止時間同時到達卻被誤判逾時。

報價連線仍優先依文件呼叫 `ManageServerConnection("", 0, 1)`。官方隨附 `PythonTester.py` 則傳入登入 ID，與文件文字不同；實機診斷曾確認 `Login=0` 後空 ID 呼叫立即回傳 `1000`。因此，僅在已成功登入同一帳號且該呼叫回傳 `1000` 時，改用同次登入的 ID 再試一次；不再次呼叫 Login、不換帳號、不改連線狀態或目標、不循環重試。其他錯誤與登入本身失敗不觸發此相容處理；第二次失敗保留其真正錯誤。中斷連線使用同次嘗試最後使用的 ID 模式。

診斷只記 `login_id_mode=empty/authenticated_user`，不記真實 ID；新增 `quote_login_id_fallback` 事件標示相容處理。成功回傳0仍須等到 `SKQuote/3003` 才能判定報價就緒，不能以其他通道3003取代。初次登入授權、30秒預設就緒等待、失敗後安全結束背景程序、回補斷點及記憶體輪替規則維持不變。

## 查詢方式

重新啟動 API 並由使用者登入後，唯讀查詢：

```text
GET /api/v1/health
```

- `connection_diagnostics`：最近背景程序回傳的診斷快照。
- `native_worker.connection_diagnostics`：同一份最近快照；清理後可能顯示 `stopped`。
- `native_worker.last_connect_failure`：最近一次連線失敗、**清理前**的獨立快照。查看內層 `connection_diagnostics`，不要用清理產生的斷線當成原始故障原因。新登入嘗試開始時清除舊失敗，成功後維持 null。

連線 RPC 尚在進行時，health 可能只有背景程序啟動時的快照；完成或失敗回應後才取得這次完整事件序列，不代表在等待中沒有任何 SDK 活動。單純 GET health 不會重新登入或啟動抓取。

每個診斷快照包含 `attempt`、`started_at`、`phase`、`last_quote_code`、總 `event_count` 與最近最多50筆 `events`。每筆有 UTC `at`、相對嘗試起點的 `elapsed_ms`、事件種類與所屬階段。

重要事件種類：

| kind | 內容 |
| --- | --- |
| `sdk_start`／`sdk_return` | `Login`、開啟／中斷國內行情呼叫；return 記錄 SDK 實際整數 `return_code` |
| `sdk_exception` | 呼叫拋出例外的類型，不保存含帳密風險的原始例外內容 |
| `connection_event` | 遮罩後來源、通道、原始 `code`、是否套用 `applied`、是否清理中 `during_cleanup` |
| `announcement_received` | 只記收到公告的時間，不保存公告原文或登入 ID |
| `wait_quote_timeout` | 等待 SKQuote/3003 逾時；不把其他通道的3001當成已連上國內行情 |
| `cleanup_start` | 程式開始主動清理，不能作為伺服器先斷線的證據 |

一般事件來源用 `channel=other`、`source=[REDACTED]`（無來源則 unknown），不輸出真實登入 ID。國內行情為 `channel=domestic_quote`、`source=SKQuote`。

此 SDK 回呼只有來源與狀態代碼，**沒有額外 nErrorCode**，所以新事件資料的 `error_code` 是 null。舊 `connection_events` 為保持客戶端相容，仍用 `(code,0)` 格式，但只收國內行情事件；第二值0只是歷史佔位，不能當成 SDK 回報「沒有錯誤」。

紀錄使用獨立鎖，不持有 DLL 呼叫鎖等待回呼；使用有界記憶體快照與既有控制狀態，不建立新資料表或無上限歷史清單。細項也可隨 Python DEBUG 日誌輸出，不額外新增永久檔案記錄器。

## 驗證與部署限制

```powershell
.\.venv\Scripts\python.exe -B -X utf8 scripts\run_worker_tests_offline.py test_capital_connection_diagnostics test_capital_native_worker test_connect_idempotency test_history_connection_recovery
```

測試涵蓋事件分流、真正行情斷線、晚到事件、截止時間競態、來源遮罩、50筆上限、health 回傳，以及真實子程序 IPC 的失敗快照保留。SDK、登入帳密與行情均為模擬，不載入真實群益 DLL，不接觸正式 MySQL。

程式與測試通過不代表本次實際 SDK 逾時已完全排除。正式 API 必須重新啟動才會載入新程式；本次修改不會自動重啟、使用 .env 重新登入、解除手動暫停或啟動回補。之後做真實登入驗收時，應先退出官方測試程式，避免並行連線干擾判讀。

另提供明確授權才執行的連線驗收：`scripts/test_quote_login_id_live.py --allow-live-login`。它從 `.env` 讀取帳密，使用獨立的正式 supervisor 與空記憶體自選股，停用背景重試；只要求一次登入與連線，最後結束自有背景程序。執行前要求本機 API 已斷線、回補已暫停且無執行中工作，不重啟或中斷 API、不查歷史行情、不建立 MySQL 連線、不修改正式斷點；前後唯讀查核 API 狀態，輸出遮罩後 JSON 至 `output/diagnostics`。此驗收不會替正在運作的 API 載入新程式。

### 2026/10/23 實機驗收結果

台北時間22:42～22:43只執行一次 `.env` 登入。Login回傳0、空ID的ManageServerConnection回傳0，收到SKQuote/3001，但其後30秒未收到3003而逾時。因此本次未重現1000，也未觸發「登入ID備援」；不能據此宣稱備援已經實機驗證成功或就緒問題已修復。未再追加登入或繞過3003檢查。自有背景程序正常退出（exit0、forced_exits=0），API PID27228及回補暫停狀態前後一致，未發出歷史行情請求或建立MySQL連線。

遮罩後報告：`output/diagnostics/quote-login-id-20261023T144308559561Z.json`。包含清理前的獨立失敗快照；清理所產生的3002已標示during_cleanup，不當成主機先斷線。完整離線回歸642項通過，其中相容處理涵蓋只重試一次、登入失敗不呼叫報價、其他錯誤不備援、仍需3003、相同Session續連與對應模式清理、診斷不洩漏帳密。
