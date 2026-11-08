# SKDLLPython 後端修補（2026/10/24）

## 範圍與依據

依使用者提供的 SDK 2.13.58 `SKDLLPython.py` 與 `Python策略王DLL元件使用說明.docx`，修正本專案的 ctypes 整合層。沒有修改官方 Python 範例、DLL 二進位檔、登錄設定或 `.env`。目前預設仍為 `CAPITAL_WORKER_QUOTE_BACKEND=com`，不會自動切換或中斷既有 COM 連線。

這次修補不等於已證明先前「Login 成功、收到 3001，但沒有 3003」的原因已完全排除。必須以修補後的真實 DLL 連線驗收確認；不能把離線模擬事件或 COM 已成功連線當成 DLL 實機成功。

## 已修改

1. 載入 wrapper 前先取得共用 SDK 程序鎖；若其他 API 正在使用，不載入第二份原生元件。自動路徑搜尋支援官方套件一層／兩層目錄，明確指定的無效路徑仍會失敗。
2. 在載入前設定 wrapper 相鄰 `libs` 的程序內 DLL 相依元件搜尋環境。保留 `os.add_dll_directory` 的 handle 至停止；PATH 去重，不隨重連無限增長。不變更系統 PATH。
3. 五個報價相關回呼註冊函式的 `argtypes` 使用 wrapper 自己定義的 callback type，`restype=None`；不混用 COM 事件簽章，也不註冊下單或交易回報事件。保留 vendor module 及 wrapper 持有的原生回呼參照。
4. DLL 原生操作回到初始化執行緒。`OnConnection` 不再開背景訂閱執行緒；`OnNotifyQuoteLONG` 只排入有界通知，避免在 DLL 尚未返回時鎖互等或再次進入 SDK。主迴圈 `poll()` 處理訊息、恢復訂閱及取價；佇列最多 2000 筆，每圈最多處理 200 筆，不是逐筆歷史保存。
5. 初次公告等待與歷史連線恢復等待改為有截止時間的訊息泵，不只阻塞等待 Event。仍須真正收到國內 `SKQuote/3003` 才設定就緒；不延長原有就緒期限，不以 3001 代替。
6. 商品清單依手冊先呼叫該市場的 `LoadCommodity(market_no)`，成功才 `RequestStockList(market_no)`；前置載入失敗不回傳空清單覆蓋現有商品。這個前置條件不代表應在登入時猜測呼叫 `LoadCommodity(12)`，也不是已證實的 3003 修復。
7. 報價開啟失敗會清除「連線中」狀態，讓同一個已接受的登入 Session 可再次嘗試連線，不會永遠被當成仍在連線而略過。斷線會清除已訂閱標記，就緒後由主迴圈恢復。既有 1000 錯誤限定一次的登入 ID 相容處理維持不變。

DLL 診斷新增 `backend=dll`、`native_owner_thread_id`、`dll_search_configured`、`callback_abi_configured`、`pending_quotes`。COM 保留自己的診斷欄位，不顯示 DLL 專屬資訊。帳密與公告原文不寫入診斷。

不變更 MySQL、斷點、回補範圍、掛牌邊界、手動暫停、記憶體輪替門檻或每日排程。DLL 後端原有的獨立歷史 COM client 仍保留；這次未改為新的 K 線實作，也沒有新增即時行情獨立 IPC 推送通道。

## 離線驗證

在 `capital_api` 執行：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 scripts\run_worker_tests_offline.py
```

完整 677 項測試通過，包含新增 13 項純假 DLL 測試：鎖與搜尋環境的載入順序、回呼 ABI、模組／handle 生命週期、錯誤清理、同 Session 重連、回呼不等待 DLL 鎖、跨執行緒拒絕原生操作、訊息泵送達公告／3003、商品載入順序與失敗保護、有界通知及停止後忽略通知。既有 COM、歷史連線恢復、回補及資料驗證測試同時通過。

測試入口封鎖真實 DLL／COM 載入與正式 MySQL 連線，使用隔離的控制資料庫，不登入、不改正式斷點、不重啟 API。

## 實機驗收條件

使用者已允許一次 `.env` 實機登入，但明確要求「不動現有 COM 連線」。本次確認現有 COM 仍就緒，因此**沒有執行 DLL 真實登入**，也沒有消耗該次登入嘗試。

之後只有在既有 API 已斷線、群益背景程序已退出、回補已暫停且沒有執行中工作時，才可執行：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 scripts\test_quote_login_id_live.py --allow-live-login --backend dll
```

若 API 本身已由使用者關閉，可加 `--allow-api-offline`；這只接受明確的本機連線被拒絕，不把 HTTP 逾時當成關閉，並以唯讀模式核對既有控制檔及持有工作鎖。測試不會呼叫正式 API 的斷線／登入端點，不刪鎖、不重啟、不解除暫停、不發歷史查詢、不訂閱、不寫 MySQL。一次測試只要求一次 Login，最後只結束自己建立的程序，將遮罩報告存於 `output/diagnostics`。

現有 API 未重新啟動，不會自動載入這些程式碼。請勿為了驗收同時再開官方測試程式或另一組群益連線。
