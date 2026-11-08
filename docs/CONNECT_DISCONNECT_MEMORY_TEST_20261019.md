# 真實 API 登入、斷線與記憶體測試

測試時間：2026-10-19 15:32:28～15:33:16，Asia/Taipei。

## 結果

已透過正式 API 各執行一次登入與斷線。登入使用 `capital_api/.env` 的 `CAPITAL_USER_ID`、`CAPITAL_PASSWORD`、`CAPITAL_AUTHORITY`；帳密只在記憶體及本機 HTTP JSON 請求中使用，未列印、未另存檔。

- `POST /api/v1/capital/connect`：HTTP 200，約 3.281 秒回覆 `connecting`；後續 health 輪詢確認 `quote_ready=true`、`quote_connection_state=connected`，並非只以 HTTP 200 認定連線成功。
- `POST /api/v1/capital/disconnect`：HTTP 200，回覆耗時四捨五入至毫秒為 0.000 秒。斷線後 health 確認 `quote_ready=false`、`quote_connection_state=disconnected`。
- DLL 鎖：登入前可取得，連線期間無法取得，斷線後立即及 5／15／35 秒均可取得。測試只以非阻塞方式短暫取得再釋放鎖，未強行解除其他程序的鎖。
- 記憶體：斷線 35 秒後，私有記憶體只較連線穩定時減少約 0.594 MiB，未回到登入前水準。

**本輪確認主行情連線與斷線成功、DLL 鎖確實釋放，但單純 disconnect 沒有明顯回收已配置記憶體。**

## 量測數據

整輪量測同一個實際 API 程序 PID 27700；未重啟 API、未強制 GC、未修剪工作集。

| 階段 | 台北時間 | 私有記憶體 MiB | 實體工作集 MiB | quote_ready | DLL 鎖可取得 |
| --- | --- | ---: | ---: | --- | --- |
| 登入前 | 15:32:28 | 75.715 | 94.734 | false | 是 |
| 報價主機就緒 | 15:32:36 | 291.512 | 314.824 | true | 否 |
| 就緒後再觀察 5 秒 | 15:32:41 | 296.074 | 319.434 | true | 否 |
| 斷線後立即 | 15:32:41 | 294.930 | 318.348 | false | 是 |
| 斷線後 5 秒 | 15:32:46 | 294.934 | 318.379 | false | 是 |
| 斷線後 15 秒 | 15:32:56 | 294.934 | 318.484 | false | 是 |
| 斷線後 35 秒 | 15:33:16 | 295.480 | 319.051 | false | 是 |

- 登入至連線穩定的私有記憶體增量：220.359 MiB。
- 斷線 35 秒後相較連線穩定時的變化：-0.594 MiB。
- 斷線 35 秒後仍比登入前多：219.765 MiB。
- 外部程序記憶體採樣：775 筆，0 筆採樣錯誤；設定間隔 50 毫秒，實際排程可能較慢。
- 採樣得到的私有記憶體峰值：296.074 MiB，短暫尖峰仍可能漏取。

私有記憶體代表程序的私有提交用量，實體工作集是當時駐留 RAM 的部分；兩者不可相加。背景活動可能造成小幅波動，因此不能將斷線後約 0.6 MiB 的變化精確歸因到某個 DLL 內部動作。

## 程式行為與限制

現有 `CapitalQuoteService.disconnect()` 呼叫 `stop()`，關閉歷史 client、要求主行情斷線，再釋放程序持有的鎖；服務仍保留登入 Session、DLL wrapper 與記憶體中的帳密。因此「鎖已釋放」與「所有 DLL 配置已回收」不是同一件事。本次未拆分量測 DLL、快取、Python allocator 各自保留的配置。

本輪沒有發出歷史 K 線查詢、商品同步或 K 線寫入請求，沒有重建／結束實際 API 程序。這不能取代歷史查詢後的釋放測試，也不能證明之前累積到約 4 GiB 的程序一定能立即完成斷線。

## 測試結束狀態

- API PID 27700 保持運作。
- 群益主行情保持斷線，DLL 鎖空閒。
- 回補 `worker_active=false`。
- 為防止登入後自動恢復正式回補，測試先呼叫 `POST /api/v1/backfill/pause`，結束後維持 `pause_requested=true`，沒有自行恢復回補。
- 未修改正式服務程式、`.env`、資料表結構或既有 K 線。

## 測試腳本

新增 `scripts/measure_connect_disconnect_memory.py`。它只允許本機固定 API 位址，從 `.env` 讀取帳密並遮蔽輸出，不重試逾時的 POST，且在登入後發生測試錯誤時最多進行一次斷線清理。

若重跑，必須換成當時真正監聽 8000 的 API PID，且 API 應處於尚未登入、沒有回補工作的狀態；不要沿用已重啟前的舊 PID。

```powershell
.\.venv\Scripts\python.exe -B -X utf8 .\scripts\measure_connect_disconnect_memory.py --pid 27700 --env-file .env --observe-seconds 35
```
