# V3.4 通知系統

## 使用

重啟 API 後開啟 `http://127.0.0.1:8000/notifications`，或從 `/dashboard` 點「通知中心」。

通知中心提供未讀總數、歷史分頁、只看未讀、單則已讀／未讀切換、全部標為已讀、條件判斷明細及「建立測試通知」。測試通知明確標示為測試，不建立提醒或股票觸發事件。

現有 Alert 發生 False→True 時，觸發事件與通知一起寫入原本的 SQLite（預設 `data/alerts.sqlite3`）。不需要手動同步或初始化資料表。升級前的 V3.3 事件會補入通知歷史，不補發系統通知。刪除提醒不刪除既有通知。

頁面每五秒自動更新通知清單、未讀數與派送狀態，保留已展開的明細及載入的歷史；切回頁面時也會更新。儀表板的通知中心連結同步更新未讀數。無需手動重新整理。

## Windows 系統通知（瀏覽器模式）

在 Chrome／Edge 等支援的桌面瀏覽器開啟通知中心，按「啟用系統通知」，由使用者允許網站通知。設定只儲存在該瀏覽器的此網站來源；localhost 和 127.0.0.1、不同埠各自獨立。

保持通知中心或分析儀表板開啟，每五秒領取一則最近五分鐘內的未讀通知，透過瀏覽器 Notifications API 提交給作業系統。在 Windows 上由瀏覽器提供系統通知；這不是原生常駐 Windows 服務，不支援關閉全部頁面後推送。API 還在運行時，關閉頁面不影響通知紀錄保存。

「停止系統通知」只停止此網站在該瀏覽器的後續派送，不刪除通知，也不更改瀏覽器網站權限。系統通知本身不會自動標已讀，點擊通知會開啟通知中心。

瀏覽器不支援、非安全環境或權限被封鎖時，頁面會說明原因，網頁內通知仍然可用。HTTP localhost 適合本地測試；遠端使用需符合瀏覽器安全來源要求。Windows 的通知設定、勿擾模式及瀏覽器政策可能影響顯示。

參考：[MDN requestPermission](https://developer.mozilla.org/en-US/docs/Web/API/Notification/requestPermission_static)、[Notifications API 使用方式](https://developer.mozilla.org/en-US/docs/Web/API/Notifications_API/Using_the_Notifications_API)。

## 去重、提交狀態及限制

- `event_id` 唯一約束保證同一觸發事件只產生一則通知；重啟或重新初始化不重複建立，也不重設已讀狀態。
- 系統通知使用 SQLite 原子領取，多分頁／多瀏覽器競爭時同一則只交給一方，屬於本地單使用者共用收件匣，不是每個裝置都各送一次。
- 每則最多領取一次，避免送出後網路中斷導致重試重複。領取後頁面崩潰可能漏掉系統彈出，通知紀錄仍保留；不提供自動補發，使用者可查看歷史或建立新的測試通知。
- `pending` 等待領取；`claimed` 已領取但尚未回報；`submitted` 表示瀏覽器建構通知成功，不保證作業系統已顯示或使用者看過；`failed` 記錄同步提交失敗；`expired` 超過五分鐘；`skipped_history` 為升級補入的舊紀錄。
- 「全部標為已讀」只涵蓋指定序號以前的資料，操作期間新到達的通知保留未讀。
- 本版没有 Email、LINE、Telegram、Discord、Service Worker／Web Push、原生 Windows 背景通知服務。

## API

共同前綴 `/api/v1/notifications`，Swagger 分組「V3 通知系統」。

| 方法／路徑 | 內容 |
|---|---|
| GET 空路徑 | `unread_only`、`before` 序號、`limit`（1–100）；回傳 items、unread_count、next_before |
| PATCH `/{id}/read` | `{"read": true}` 或 false |
| POST `/read-all` | `{"through_sequence": 123}`，不標記更新的通知 |
| POST `/test` | 建立測試通知，201 |
| POST `/desktop/claim` | 取得派送權與 claim_token；無項目則 notification=null |
| POST `/{id}/desktop-ack` | 傳 claim_token、status（submitted／failed）、選填 error；回傳 accepted |

一般測試使用 `/test` 或 GUI 即可。`desktop/claim` 會消耗一次派送權，不應當作查詢通知使用。

## 實作與驗證

新增 `app/services/notification_service.py`、`app/api/notifications.py`、`app/static/notifications.html/css/js`、`tests/test_notifications.py`；在 AlertService 交易內建立通知，於 FastAPI 登錄通知 API 與頁面，儀表板增加入口。未新增套件。

2026-09-14：102 項 Python 測試全部通過（新增 11 項），包括真實暫存 SQLite 的交易回滾、事件去重、升級補入、已讀保存、分頁、並行派送權與 HTTP API；`node --check app/static/notifications.js` 通過。

另外使用獨立預覽資料庫完成瀏覽器畫面、建立測試通知、已讀切換、未讀篩選實測。內嵌瀏覽器通知權限被封鎖，未修改其權限；Windows 作業系統實際彈出尚未驗證。未連接群益或修改行情資料。
