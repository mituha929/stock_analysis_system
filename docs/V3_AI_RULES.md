# V3.8：本地 AI 自然語言規則解析

V3.9 已補強驗證並新增不呼叫模型的 `/api/v1/ai/rules/validate`。V3.10～3.11已新增[AI選股預覽與確認介面](V3_AI_PREVIEW.md)，目前prompt版本為 `v3.10-1`。錯誤位置契約詳見 [V3.9 規則驗證與測試](V3_AI_RULE_VALIDATION.md)。下方 V3.8 實測紀錄保留當時版本。

本階段依開發流程「最終開發順序」編號。將繁體中文轉成現有 Rule／RuleGroup 草稿，提供 assumptions 與待釐清項目。獨立於 `Test-StockOllama.ps1` 的單股家人版操作解說。

## 啟動與設定

重新啟動 FastAPI，在 `/docs` →「V3.8 自然語言規則解析」使用 `POST /api/v1/ai/rules/parse`。Ollama 需已啟動且模型已安裝；不需要登入群益。

以下為環境變數的預設值（也已加入 `.env.example`）；不必修改既有 `.env` 即可使用本機模型：

```dotenv
OLLAMA_ENABLED=true
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_RULE_MODEL=qwen3:4b-instruct
OLLAMA_RULE_TIMEOUT_SECONDS=60
OLLAMA_RULE_KEEP_ALIVE=0
OLLAMA_RULE_NUM_CTX=4096
```

設定由伺服器環境提供，請求不能覆寫模型或 URL。只有收到解析請求才呼叫 Ollama，不在 API 啟動時載入模型。HTTP 使用 UTF-8、不經系統代理、不跟隨重新導向。模型參數為 `think=false`、`stream=false`、`temperature=0`、`seed=42`、`num_predict=700`。相同參數仍不保證模型每次輸出完全相同。

預設 `keep_alive=0`，本系統每次模型請求完成後便由 Ollama 卸載模型，釋放模型占用的 RAM／VRAM。Ollama 服務及磁碟上的模型檔案保留；下次使用會重新載入，首次回應可能較慢。若環境另有明確設定 `OLLAMA_RULE_KEEP_ALIVE`，該值優先，設為 `0` 才會於使用後立即卸載。這不會停止股票 API、群益連線或回補。

「2454 是否可以買進」的單股預覽／確認／證據整理流程，見 [單股進場條件觀察](SINGLE_STOCK_ENTRY.md)。

## 使用範例

```json
{
  "text": "股價高於MA20，且20日量比至少1.5倍，風險分數不超過60",
  "history_interval": "day"
}
```

回應範例（elapsed_ms 為實際耗時）：

```json
{
  "original_text": "股價高於MA20，且20日量比至少1.5倍，風險分數不超過60",
  "history_interval": "day",
  "model": "qwen3:4b-instruct",
  "prompt_version": "v3.9-1",
  "status": "draft",
  "rule": {
    "logic": "AND",
    "rules": [
      {"field": "price", "operator": ">", "value": {"field": "ma20"}},
      {"field": "volume_ratio_20d", "operator": ">=", "value": 1.5},
      {"field": "risk_score", "operator": "<=", "value": 60}
    ]
  },
  "assumptions": [],
  "unresolved_conditions": [],
  "requires_confirmation": true,
  "elapsed_ms": 12000
}
```

PowerShell（整段貼上；明確使用 UTF-8 傳送與讀取）：

```powershell
Add-Type -AssemblyName System.Net.Http
$client = [System.Net.Http.HttpClient]::new()
$client.Timeout = [TimeSpan]::FromSeconds(90)
try {
    $body = @{ text = "股價高於MA20，且風險分數不超過60"; history_interval = "day" } | ConvertTo-Json
    $content = [System.Net.Http.StringContent]::new($body, [System.Text.Encoding]::UTF8, "application/json")
    $response = $client.PostAsync("http://127.0.0.1:8000/api/v1/ai/rules/parse", $content).GetAwaiter().GetResult()
    $bytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
    Write-Host ([System.Text.Encoding]::UTF8.GetString($bytes))
    Write-Host ("HTTP: " + [int]$response.StatusCode)
} finally {
    $client.Dispose()
}
```

## 解讀與限制

- 支援單一規則、巢狀 AND／OR、欄位比較、between 與 cross_above／cross_below；以既有 `app/rules/fields.py` 動態建立白名單與生成 Schema。規則最多10層、100節點。
- 文字1–2000字，去除前後空白後不能為空；週期預設day，支援1m、5m、15m、60m、day、week、month。
- prompt 將一般「多頭」包含 bullish 與 strong_bullish；「強勢偏多」只對應 strong_bullish。
- 「比較強」「放量」「風險不要太高」分別建議 direction_score >= 70、volume_ratio_20d >= 1.5、risk_score <= 60，模型須列入 assumptions。這些是可修改的解讀，非策略績效承諾。
- ma20、volume_ratio_20d、breakout_20d_high 的20代表所選週期20根K線。日與週等混合條件不能默默改寫成同一週期。
- 本版不解析市場／產業／指定股票／自選清單／排序／日期區間，也不修改已有策略；包含這些要求時應回傳待釐清原因。後续整合再建立完整 ScreenerRequest。
- 不支援新聞、財報、未來價格機率、個人持股成本、虧損本金百分比或減碼比例等欄位。這些屬另一個功能，不能用 risk_score 代替個人損益。
- 存在 unresolved_conditions 時，即使模型同時產生部分合法規則，程式仍強制回傳 rule=null、status=needs_clarification。null規則不能執行。
- 所有成功回應固定 requires_confirmation=true。Schema通過只證明規則合法，不能保證模型理解正確；確認介面屬後續V3.10／V3.11。目前僅供Swagger或腳本人工閱讀。
- 此端點不讀行情、不接資料庫、不建立策略／提醒、不呼叫群益，也不執行選股。不要把解析回應自動串接 `/screener/run`。

## 錯誤回應

服務錯誤為 `{"detail":{"code":"...","message":"..."}}`；請求格式錯誤沿用FastAPI標準422。

| HTTP | code | 意義 |
|---|---|---|
| 409 | ai_busy | 同一API行程已有解析；鎖不涵蓋多worker或PowerShell直接呼叫Ollama |
| 422 | unsupported_field / invalid_rule | 模型規則不合法，不回傳可執行草稿 |
| 422 | invalid_assumption | 模型假設引用的原詞不在使用者輸入中 |
| 502 | invalid_json / invalid_response | 非JSON、重複key或回應不完整 |
| 502 | incomplete_response | 輸出被截斷；請拆分條件 |
| 502 | response_too_large / ollama_http_error | 過大回應或Ollama HTTP異常 |
| 503 | ai_disabled / invalid_config | 功能停用或設定錯誤 |
| 503 | model_not_found / ollama_unavailable | 模型／端點不存在或Ollama無法連線 |
| 504 | ollama_timeout | 超過設定等待時間 |

目前不自動修復、猜測或重試失敗的模型輸出。超過700輸出token的複雜條件可能被拒絕；10層／100節點是驗證上限，不代表此模型能在輸出額度內生成全部。

## 驗證

模擬測試（不啟動完整API生命週期或外部服務）：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_ai_rules.py
```

真實本機模型測試（只呼叫Ollama，無需重啟8000伺服器）：

```powershell
.\.venv\Scripts\python.exe .\scripts\test_ai_rule_parser.py --text "股價高於MA20，且20日量比至少1.5倍，風險分數不超過60"
.\.venv\Scripts\python.exe .\scripts\test_ai_rule_parser.py --text "找比較強、放量、風險不要太高的股票"
.\.venv\Scripts\python.exe .\scripts\test_ai_rule_parser.py --text "虧損本金10%時賣出一半"
```

依序檢查：明確比較正確、模糊條件逐項列出assumptions、不支援的個人持股條件回傳null與原因。退出碼0代表服務回傳正常，仍須確認語意；needs_clarification也屬正常回應。

API欄位參考：[Ollama官方Chat API](https://docs.ollama.com/api/chat)。

### 2026-09-26 驗證紀錄

新增16項自動測試；與規則、提醒、通知、選股、儲存策略合計90項通過。涵蓋規則可交給既有引擎運算、深度／節點限制、錯誤型態、假設原詞引用、JSON截斷／重複key、離線／逾時／404、HTTP輸入與併發鎖釋放。

使用本機 `qwen3:4b-instruct`、prompt v3.8-2 驗證明確三條件（約23秒）、模糊三條件（約12秒）、巢狀AND／OR與between（約15秒），規則內容符合測試輸入。個人虧損百分比要求回傳rule=null及needs_clarification。

已知模型限制：即使prompt要求明確條件不列為假設，4B模型偶爾仍會把明確條件重列在assumptions，也可能多列不精確的待釐清理由。結構驗證與原詞檢查無法證明語意正確；保留原輸入與全部解讀供人工確認。此版不代表所有自然語句或提示注入都能被正確理解。
