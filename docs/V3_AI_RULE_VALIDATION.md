# V3.9：AI 規則驗證

V3.8 負責將自然語言轉成草稿，V3.9 負責檢查草稿是否合法。本階段不新增選股執行、策略保存或確認介面；`requires_confirmation` 固定為 `true`。

## 已完成

- 欄位與比較欄位必須在白名單；布林及趨勢欄位只允許 `==`、`!=`。
- 數字必須為有限數值，不接受數字字串、布林、null 或超出浮點表示能力的整數；布林必須為 JSON `true`／`false`。
- 分數、RSI、KD、ADX 門檻為 0–100，含兩端。
- 股價、MA、EMA、成交量、均量、量比、ATR 的門檻不得小於 0；不任意限制上限。
- MACD、ROC、漲跌幅、OBV、布林通道保留負數支援。範圍限制針對規則的數字門檻，不會修改已儲存行情。
- `between` 必須是兩個有限數字，下限不得大於上限，兩端都檢查範圍；禁止默默交換或截斷數值。
- 欄位參照必須同資料型態。這不代表所有同型態欄位的比較都有分析意義，例如價格與成交量仍需人工判斷，不檢查金融語意或單位。
- 群組必須是非空 AND／OR，禁止混用葉節點與群組欄位、禁止額外指令欄位；最多 10 層及 100 節點，根節點及群組都計入。
- 回傳最多 50 筆錯誤，以 `code`、`path`、繁體中文 `message` 定位，不回傳錯誤輸入值或模型原始輸出。
- 欄位探索、程式驗證、模型生成 Schema 共用欄位與範圍資料。模型 grammar 僅協助生成，不能取代伺服器端檢查（尤其上下限順序、全樹節點數與語意）。

既有 Rule 模型也共用新的葉節點驗證。因此手寫提醒／選股／已保存策略若包含負的價格或量比門檻，重新讀取或使用時會被拒絕；請改正條件，程式不會自動修改、刪除或遷移既有策略。合法既有條件維持原行為。

## 端點

重啟 FastAPI 後開啟 `/docs`，新增「V3.9 規則驗證」：

`POST /api/v1/ai/rules/validate`

只驗證 JSON 規則，不需要 Ollama 在線，也不讀寫 MySQL、SQLite 或呼叫群益。請貼入：

```json
{
  "rule": {
    "logic": "AND",
    "rules": [
      {"field": "price", "operator": ">", "value": {"field": "ma20"}},
      {"field": "rsi14", "operator": "between", "value": [40, 60]}
    ]
  }
}
```

預期 HTTP 200，回傳 `valid: true`、原規則與 `requires_confirmation: true`。此端點只接收 `rule`，不是整份 `/parse` 回應；不接受 null 草稿。沒有可執行條件的草稿必須先釐清。

改成 RSI `[40, 120]`，預期 HTTP 422：

```json
{
  "detail": {
    "code": "invalid_rule",
    "message": "規則未通過驗證，請依 errors 的位置與原因修正。",
    "errors": [
      {
        "code": "out_of_range",
        "path": "/rule/rules/1/value/1",
        "message": "數值必須介於 0 與 100（含兩端）。"
      }
    ]
  }
}
```

`path` 是 JSON Pointer：陣列從 0 開始，所以上例指第二條規則的第二個界限。物件鍵中的 `/` 與 `~` 分別編碼成 `~1`、`~0`。

`POST /api/v1/ai/rules/parse` 已自動套用相同驗證，失敗時不回傳可執行草稿，也不自動重試或猜測修復。模型假設未逐字引用原文時，回傳 `invalid_assumption` 與 `/assumptions/序號/source_text`。

`GET /api/v1/rules/fields` 的每個欄位新增 `minimum`、`maximum`。`null` 表示沒有固定界限，不表示可以傳入 null 值。

請求最外層缺少 `rule`、額外參數等格式問題仍採 FastAPI 標準 422 格式；規則內容與模型輸出驗證錯誤使用上述 `detail.errors` 格式。

## 手動測試清單

| 規則變更 | 預期 |
|---|---|
| RSI between `[40,60]` | 200 |
| RSI between `[40,120]` | 422 `out_of_range` |
| price 的 value 改成 `"100"` | 422 `invalid_type` |
| field 改成 `future_price` | 422 `unsupported_field` |
| value 改成 `{"field":"trend"}` 與 price 比較 | 422 `incompatible_field` |
| 布林欄位使用 `>` | 422 `unsupported_operator` |
| between `[60,40]` | 422 `invalid_range` |
| AND 的 rules 改成 `[]` | 422 `invalid_children` |
| `macd_histogram < -1` | 200，負 MACD 值合法 |
| `volume_ratio_20d > -1` | 422 `out_of_range` |

以上操作不觸發回補、通知、策略保存或選股。`valid=true` 不表示符合原文、資料足夠或股票值得買賣。條件預覽與確認执行仍屬後續 V3.10／V3.11。

## 終端機自動測試

在 `capital_api` 目錄執行，不需要啟動 Ollama 或群益：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_ai_rule_validation.py
```

完整的本階段相關回歸：

```powershell
$env:PYTHONPATH = '.;.\tests'
.\.venv\Scripts\python.exe -m unittest test_ai_rule_validation test_ai_rules test_rules test_alerts test_notifications test_screener test_saved_screeners test_indicators test_signals test_divergence test_analysis_route
```

2026-09-26：新增 17 項測試，相關回歸共 126 項通過。測試使用模擬服務或暫存 SQLite，不觸碰正式資料庫，不啟動完整 API 生命週期或群益 DLL。

本機 `qwen3:4b-instruct`、prompt `v3.9-1` 實測：「股價大於100」約 5.4 秒回傳 `price > 100`；三條件測試有一項假設未逐字引用原文，被正確拒絕並指出 `/assumptions/2/source_text`。模型仍可能把明確條件重列成假設，或引用不精確；不得因為結構通過就取消人工確認。

本版不需新增環境變數、安裝依賴或修改資料表；既有伺服器需重啟才載入新程式碼。
