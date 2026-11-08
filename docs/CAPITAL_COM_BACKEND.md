# COM 主連線後端

## 設定與範圍

`CAPITAL_WORKER_QUOTE_BACKEND` 預設為 `com`；可明確設為 `dll` 退回原 SKDLLPython 實作。無效值會拒絕啟動，不會在登入失敗時自行切換另一套後端反覆登入。設定於 API／CLI 啟動時讀取；使用者仍須明確登入，程式不因 GET health 或此設定讀取 `.env` 自動登入。

API 仍透過既有 supervisor 啟動單一背景程序，不讓 FastAPI 持有原生 DLL。3072 MiB 輪替、4096 MiB 安全暫停、區間提交前的程序保護、失敗重試與手動暫停政策維持原樣；不變更 MySQL、回補範圍、掛牌邊界或斷點。這次沒有重啟 API 或啟動回補。

COM 後端使用 `CAPITAL_SKCOM_DLL`，不是 `CAPITAL_SKDLL_PY`。既有專案的此設定可沿用；未設定時，會在附帶套件的一層／兩層資料夾結構尋找符合 Python 位元數的 SKCOM.dll。實際指定的路徑無效時會失敗，不靜默換另一份 DLL。

## 連線與事件規則

背景程序同一條 STA 執行緒執行：

1. 取得共用 DLL 程序鎖，初始化 COM STA，建立 Center／Quote／Reply 三個物件。
2. 保留回呼及 `GetEvents` 連線的強參照，明確指定 Quote／Reply 事件介面。
3. 公告事件先註冊；只回覆 -1、紀錄收到時間，不保存帳號或公告原文。
4. `SetAuthority`、`SKCenterLib_Login` 成功後才呼叫 `SKQuoteLib_EnterMonitorLONG()`。
5. 以訊息泵收取事件；只有 `OnConnection(3003,0)` 才能設定報價就緒。3001不能取代3003；非零附帶代碼、3002與3033會撤銷就緒狀態。
6. 事件只改狀態／收資料；主迴圈負責原生行情快照、訂閱及重連後的訂閱還原，不在事件裡直接呼叫 COM。
7. 結束時標記 cleanup，忽略晚到的3003，LeaveMonitor、解除事件、釋放物件、CoUninitialize，再退出自有背景程序。不中斷其他程式，不手動刪 DLL 鎖檔。

COM 與簡化 DLL 的事件不能混用：前者 `OnConnection(kind,error_code)`，後者 `OnConnection(loginID,code)`。COM 診斷來源為 `SKQuoteLib`，保留真正的附帶錯誤碼；DLL 來源為 `SKQuote`，舊相容事件的第二值0是佔位。共同的時間線仍最多50筆，新增 `backend`、`owner_thread_id`、`sdk_connection_value`，不包含帳密或原始登入ID。

`worker_snapshot.quote_backend` 顯示選定後端；`connection_diagnostics.backend=com` 表示目前診斷來自 COM。health 的數字狀態查詢不會把沒有3003的連線假裝成已就緒。

## 行情功能

COM 訂閱、取消訂閱、行情快照與 Tick 都留在擁有物件的執行緒；待處理行情佇列上限2000筆，每圈最多處理200筆。Tick日期使用台北時區。此佇列與既有 Tick 快取一樣是有界即時快取，不是逐筆成交永久歷史檔。

K線使用同一個 Quote 物件與既有 `request_history_candles` 驗證／有限重連流程，不另開歷史 COM Session 或再次登入；保留明確排除區間、負成交量驗證與原子提交的上層工作程序。背景程序本身不開 MySQL。

商品清單使用官方 `RequestStockList`／`OnNotifyCommodityListWithTypeNo`，按文件的 `%類別代碼%類別名稱%` 與商品欄位解析，等待 `##` 結束通知；類別94、98仍排除。格式未知、資料為空、未完成或超過安全上限時整次失敗，不能用部分清單覆寫現有商品。

## 2026/10/23 實機驗收

執行命令：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 scripts\test_quote_login_id_live.py --allow-live-login --backend com --allow-api-offline
```

`--allow-live-login` 是必要的明確開關。測試讀取 `.env` 帳密，只執行一次登入、空自選股、停用背景重試，測完結束自有程序，不要求正式 API 登入。`--allow-api-offline` 僅在本機連線被拒絕時適用；逾時不能當成 API 關閉。它以唯讀模式讀取既有控制 SQLite，要求回補已暫停並持有共用工作鎖，前後比對控制內容的SHA256，不建立／更新控制資料表。

台北時間23:22驗收結果：

- SetAuthority、Login、EnterMonitorLONG皆回傳0。
- 收到3001後再收到 `(3003,0)`，實際quote_ready=true；登入／背景程序啟動至就緒約5.891秒。
- 僅一次登入，沒有歷史K線請求、訂閱、下單或MySQL連線。
- 背景程序正常退出，exit0、forced_exits=0。API前後都未執行，控制檔SHA256及暫停狀態完全一致。
- 診斷報告：[quote-com-20261023T152241676664Z.json](../output/diagnostics/quote-com-20261023T152241676664Z.json)。

這證明本次直接COM路徑可以完成報價就緒，不能推論其他網路問題皆已排除，也不把單次連線測試當成長時間回補或4GB輪替的實機驗收。新後端的K線、商品清單與訂閱功能有隔離測試；本次未以真實行情再抓資料驗證，更未寫正式MySQL。

完整隔離回歸664項通過；測試入口同時禁止載入真實COM、簡化DLL及建立正式MySQL連線。

## 依據

2026/10/24 另完成三檔七週期的真實抓取／隔離MySQL寫入，以及正式supervisor提交後輪替、訂閱恢復及精確斷點續抓；完整離線回歸687項通過。10/23已確認官方颱風休市，實際資料到10/22正常。詳細範圍、門檻設定與原始報告見 [COM小範圍與輪替驗收](COM_ACCEPTANCE_20261024.md)。

- [群益官方報價連線範例](https://www.capital.com.tw/apiforum/docs/atlObjFunction/domesticQuote/connectQuote)
- 隨附 SDK2.13.58 COM 元件手冊：Quote事件、商品清單格式、同一物件K線請求。
- [Microsoft COM STA規則](https://learn.microsoft.com/windows/win32/com/single-threaded-apartments)
- 使用者提供的整合與重連文章提供結構對照；不匯入文章的下單／回報業務。
