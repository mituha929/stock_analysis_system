$utf8Encoding = [System.Text.UTF8Encoding]::new($false)

[Console]::InputEncoding = $utf8Encoding
[Console]::OutputEncoding = $utf8Encoding
$global:OutputEncoding = $utf8Encoding

if ($env:OS -eq "Windows_NT") {
    chcp.com 65001 | Out-Null
}

function Test-StockOllama {
    [CmdletBinding()]
    param(
        [Parameter()]
        [ValidatePattern("^[A-Za-z0-9._-]+$")]
        [string]$Symbol = "2330",

        [Parameter()]
        [ValidateNotNullOrEmpty()]
        [string]$Model = "qwen3:4b-instruct",

        [Parameter()]
        [ValidateSet(
            "1m",
            "5m",
            "15m",
            "60m",
            "day",
            "week",
            "month"
        )]
        [string]$Interval = "day",

        [Parameter()]
        [ValidateSet(
            "candles",
            "odd_lot_candle"
        )]
        [string]$CandleTable = "candles",

        [Parameter()]
        [ValidateRange(1, 60)]
        [int]$MaxDataAgeDays = 10,

        [Parameter()]
        [ValidateSet(
            "not_holding",
            "holding"
        )]
        [string]$PositionStatus = "not_holding",

        [Parameter()]
        [ValidateSet(
            "conservative",
            "balanced",
            "aggressive"
        )]
        [string]$RiskTolerance = "conservative",

        [Parameter()]
        [ValidateRange(0, 10000000)]
        [double]$AverageCost = 0,

        [Parameter()]
        [ValidateNotNullOrEmpty()]
        [string]$ApiBaseUrl = "http://127.0.0.1:8000",

        [Parameter()]
        [ValidateNotNullOrEmpty()]
        [string]$OllamaBaseUrl = "http://127.0.0.1:11434"
    )

    Write-Host ""
    Write-Host "輸入參數" -ForegroundColor Cyan

    [pscustomobject]@{
        Symbol         = $Symbol
        Model          = $Model
        Interval       = $Interval
        CandleTable    = $CandleTable
        MaxDataAgeDays = $MaxDataAgeDays
        PositionStatus = $PositionStatus
        RiskTolerance  = $RiskTolerance
        AverageCost    = if ($AverageCost -gt 0) { $AverageCost } else { "未提供" }
        ApiBaseUrl     = $ApiBaseUrl
        OllamaBaseUrl  = $OllamaBaseUrl
    } | Format-List

    $normalizedApiBaseUrl = $ApiBaseUrl.TrimEnd("/")
    $normalizedOllamaBaseUrl = $OllamaBaseUrl.TrimEnd("/")

    Write-Host "檢查 FastAPI" -ForegroundColor Cyan

    try {
        $health = Invoke-RestMethod `
            -Uri "$normalizedApiBaseUrl/api/v1/health" `
            -Method Get `
            -TimeoutSec 10

        Write-Host "FastAPI：在線" -ForegroundColor Green
        Write-Host ("狀態：{0}" -f $health.status)
    }
    catch {
        Write-Host "FastAPI：離線或無法連線" -ForegroundColor Red
        Write-Host $_.Exception.Message
        return
    }

    Write-Host ""
    Write-Host "檢查 Ollama" -ForegroundColor Cyan

    try {
        $ollamaVersion = Invoke-RestMethod `
            -Uri "$normalizedOllamaBaseUrl/api/version" `
            -Method Get `
            -TimeoutSec 10

        Write-Host "Ollama：在線" -ForegroundColor Green
        Write-Host ("版本：{0}" -f $ollamaVersion.version)
    }
    catch {
        Write-Host "Ollama：離線或無法連線" -ForegroundColor Red
        Write-Host $_.Exception.Message
        return
    }

    Write-Host ""
    Write-Host "檢查指定模型" -ForegroundColor Cyan

    try {
        $ollamaTags = Invoke-RestMethod `
            -Uri "$normalizedOllamaBaseUrl/api/tags" `
            -Method Get `
            -TimeoutSec 10

        $installedModels = @(
            $ollamaTags.models |
                ForEach-Object { [string]$_.name }
        )

        if ($installedModels -contains $Model) {
            Write-Host ("模型：{0}，已安裝" -f $Model) -ForegroundColor Green
        }
        else {
            Write-Host ("模型：{0}，尚未安裝" -f $Model) -ForegroundColor Red
            Write-Host "目前已安裝的模型："
            $installedModels | ForEach-Object { Write-Host ("- {0}" -f $_) }
            Write-Host ("請執行：ollama pull {0}" -f $Model) -ForegroundColor Yellow
            return
        }
    }
    catch {
        Write-Host "無法取得 Ollama 模型清單" -ForegroundColor Red
        Write-Host $_.Exception.Message
        return
    }

    Write-Host ""
    Write-Host "讀取 MySQL 技術分析資料" -ForegroundColor Cyan

    $encodedSymbol = [Uri]::EscapeDataString($Symbol)
    $encodedInterval = [Uri]::EscapeDataString($Interval)
    $encodedCandleTable = [Uri]::EscapeDataString($CandleTable)
    $analysisUrl = (
        "{0}/api/v1/analysis/{1}?history_interval={2}&candle_table={3}" -f
        $normalizedApiBaseUrl,
        $encodedSymbol,
        $encodedInterval,
        $encodedCandleTable
    )

    Write-Host $analysisUrl

    $httpClient = $null

    try {
        Add-Type -AssemblyName System.Net.Http -ErrorAction SilentlyContinue
        $httpClient = [System.Net.Http.HttpClient]::new()
        $httpClient.Timeout = [TimeSpan]::FromSeconds(30)

        $httpResponse = $httpClient.GetAsync($analysisUrl).GetAwaiter().GetResult()
        $responseBytes = $httpResponse.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
        $responseText = [System.Text.Encoding]::UTF8.GetString($responseBytes)

        if (-not $httpResponse.IsSuccessStatusCode) {
            try {
                $apiError = $responseText | ConvertFrom-Json
                throw ("HTTP {0}：{1}" -f [int]$httpResponse.StatusCode, $apiError.detail)
            }
            catch [System.Management.Automation.RuntimeException] {
                throw
            }
            catch {
                throw ("HTTP {0}：{1}" -f [int]$httpResponse.StatusCode, $responseText)
            }
        }

        $analysis = $responseText | ConvertFrom-Json
    }
    catch {
        Write-Host "股票分析資料讀取失敗" -ForegroundColor Red
        Write-Host $_.Exception.Message
        return
    }
    finally {
        if ($null -ne $httpClient) {
            $httpClient.Dispose()
        }
    }

    if (-not $analysis.symbol) {
        Write-Host "API 回傳內容缺少股票代號" -ForegroundColor Red
        return
    }

    if ([int]$analysis.candle_count -lt 2) {
        Write-Host "至少需要兩根 K 線才能進行技術分析" -ForegroundColor Red
        return
    }

    $directionLabels = @{
        strong_bullish = "強勢偏多"
        bullish        = "偏多"
        neutral        = "中性"
        bearish        = "偏空"
        strong_bearish = "強勢偏空"
    }

    $riskLabels = @{
        high     = "高"
        elevated = "偏高"
        normal   = "一般"
        low      = "低"
    }

    $agreementLabels = @{
        high   = "高"
        medium = "中"
        low    = "低"
    }

    $directionCode = [string]$analysis.summary.direction
    $riskCode = [string]$analysis.summary.risk
    $agreementCode = [string]$analysis.summary.agreement
    $directionLabel = $directionLabels[$directionCode]
    $riskLabel = $riskLabels[$riskCode]
    $agreementLabel = $agreementLabels[$agreementCode]

    if (-not $directionLabel) {
        $directionLabel = $directionCode
    }

    if (-not $riskLabel) {
        $riskLabel = $riskCode
    }

    if (-not $agreementLabel) {
        $agreementLabel = $agreementCode
    }

    $positionLabels = @{
        not_holding = "目前沒有持有"
        holding     = "目前已經持有"
    }

    $riskToleranceLabels = @{
        conservative = "保守"
        balanced     = "穩健"
        aggressive   = "積極"
    }

    $positionLabel = $positionLabels[$PositionStatus]
    $riskToleranceLabel = $riskToleranceLabels[$RiskTolerance]
    $warningCount = @($analysis.warnings).Count
    $agreementCount = [int]$analysis.summary.agreement_count
    $directionScore = [int]$analysis.scores.direction_score
    $riskScore = [int]$analysis.scores.risk_score

    if (
        $null -ne $analysis.action_evaluation -and
        $analysis.action_evaluation.available -ne $false -and
        $null -ne $analysis.action_evaluation.action_score
    ) {
        $actionScore = [int]$analysis.action_evaluation.action_score
        $actionLabel = [string]$analysis.action_evaluation.action_label
    }
    else {
        $riskPenalty = [Math]::Round($riskScore * 0.25)
        $warningPenalty = [Math]::Min(20, $warningCount * 10)
        $agreementAdjustment = switch ($agreementCount) {
            4 { 5 }
            3 { 0 }
            2 { -4 }
            1 { -8 }
            default { -10 }
        }
        $actionScore = [Math]::Max(
            0,
            [Math]::Min(
                100,
                [Math]::Round($directionScore - $riskPenalty - $warningPenalty + $agreementAdjustment)
            )
        )
        $actionLabel = "本機相容模式計算"
    }

    $buyThresholds = @{
        conservative = 85
        balanced     = 80
        aggressive   = 70
    }
    $buyThreshold = [int]$buyThresholds[$RiskTolerance]
    $isPositiveDirection = $directionCode -in @("strong_bullish", "bullish")
    $positiveGatePassed = (
        $isPositiveDirection -and
        $agreementCount -ge 3 -and
        $riskScore -lt 61 -and
        $warningCount -lt 2 -and
        $actionScore -ge $buyThreshold
    )

    $forNotHoldingReference = if ($positiveGatePassed) {
        "可考慮分批買進"
    }
    else {
        "暫不操作"
    }

    $forHoldingReference = if ($riskScore -ge 61 -or $actionScore -lt 30) {
        "可考慮分批減碼"
    }
    else {
        "持有觀察"
    }

    $operationReference = if ($PositionStatus -eq "holding") {
        $forHoldingReference
    }
    else {
        $forNotHoldingReference
    }

    if ($riskScore -ge 61 -or $warningCount -ge 2 -or $actionScore -lt 30) {
        $attentionLevel = "紅燈"
        $attentionExplanation = "程式偵測到較高風險、兩項以上警告，或操作分數偏低；不要只依這次結果做決定。"
    }
    elseif ($riskScore -ge 34 -or $warningCount -ge 1 -or $agreementCount -lt 4) {
        $attentionLevel = "黃燈"
        $attentionExplanation = "目前仍有風險警告或訊號沒有完全一致，適合先觀察並等待下一次資料更新。"
    }
    else {
        $attentionLevel = "綠燈"
        $attentionExplanation = "目前程式偵測到的風險與警告較少，但綠燈不代表保證獲利或沒有風險。"
    }

    $notHoldingText = if ($forNotHoldingReference -eq "可考慮分批買進") {
        "如果目前沒有持有，可以把它列入分批買進評估；不要一次投入全部預算。"
    }
    else {
        "如果目前沒有持有，先不要因這次技術訊號買進，等待下一次資料更新後再評估。"
    }

    $holdingText = if ($forHoldingReference -eq "可考慮分批減碼") {
        "如果目前已經持有，可以評估分批減碼；實際數量仍要依可承受損失決定。"
    }
    else {
        "如果目前已經持有，先持有觀察；不要只因單一技術訊號立刻全部賣出。"
    }

    $unrealizedPercent = $null
    if ($PositionStatus -eq "holding" -and $AverageCost -gt 0 -and [double]$analysis.price -gt 0) {
        $unrealizedPercent = [Math]::Round(
            (([double]$analysis.price - $AverageCost) / $AverageCost) * 100,
            2
        )
    }

    Write-Host "MySQL 技術分析資料：讀取成功" -ForegroundColor Green

    [pscustomobject]@{
        Symbol             = $analysis.symbol
        Name               = $analysis.name
        Interval           = $analysis.interval
        CandleTable        = $analysis.candle_table
        CandleCount        = $analysis.candle_count
        FirstCandleAt      = $analysis.first_candle_at
        LatestCandleAt     = $analysis.latest_candle_at
        LatestWrittenAt    = $analysis.latest_written_at
        Close              = $analysis.price
        PreviousClose      = $analysis.previous_close
        PriceChange        = $analysis.price_change
        PriceChangePercent = $analysis.price_change_percent
        Direction          = $directionLabel
        DirectionScore     = $analysis.scores.direction_score
        Risk               = $riskLabel
        RiskScore          = $analysis.scores.risk_score
        SignalAgreement    = $agreementLabel
        AgreementCount     = $analysis.summary.agreement_count
        WarningCount       = $warningCount
        PositionStatus     = $positionLabel
        RiskTolerance      = $riskToleranceLabel
        ActionScore        = $actionScore
        ActionLabel        = $actionLabel
        OperationReference = $operationReference
        AttentionLevel     = $attentionLevel
        AverageCost        = if ($AverageCost -gt 0) { $AverageCost } else { "未提供" }
        UnrealizedPercent  = if ($null -ne $unrealizedPercent) { "${unrealizedPercent}%" } else { "不計算" }
    } | Format-List

    if (@($analysis.reasons).Count -gt 0) {
        Write-Host "技術理由：" -ForegroundColor Yellow
        @($analysis.reasons) | ForEach-Object { Write-Host ("- {0}" -f $_) }
    }

    if (@($analysis.warnings).Count -gt 0) {
        Write-Host "風險警告：" -ForegroundColor Yellow
        @($analysis.warnings) | ForEach-Object { Write-Host ("- {0}" -f $_) }
    }

    Write-Host ""
    Write-Host "產生家人版白話操作參考" -ForegroundColor Cyan

    $explanationInput = [ordered]@{
        stock = [ordered]@{
            symbol = $analysis.symbol
            name = $analysis.name
            interval = $analysis.interval
            latest_candle_at = $analysis.latest_candle_at
        }
        evaluation = [ordered]@{
            direction = $directionLabel
            direction_score = $directionScore
            risk = $riskLabel
            risk_score = $riskScore
            signal_agreement = $agreementLabel
            agreement_count = $agreementCount
            action_score = $actionScore
            action_label = $actionLabel
            warning_count = $warningCount
        }
        family_context = [ordered]@{
            position_status = $positionLabel
            risk_tolerance = $riskToleranceLabel
            average_cost = if ($AverageCost -gt 0) { $AverageCost } else { $null }
            unrealized_percent = $unrealizedPercent
        }
        fixed_decision = [ordered]@{
            operation_reference = $operationReference
            for_not_holding_reference = $forNotHoldingReference
            for_holding_reference = $forHoldingReference
            attention_level = $attentionLevel
            attention_explanation = $attentionExplanation
            not_holding_text = $notHoldingText
            holding_text = $holdingText
        }
        technical_reasons = @($analysis.reasons)
        risk_warnings = @($analysis.warnings)
    }

    $explanationJson = $explanationInput | ConvertTo-Json -Depth 8

    $systemPrompt = @"
你是給完全不懂投資術語的家人閱讀的臺灣股票白話決策說明員。

程式已經完成技術計算、風險判斷、操作參考與注意燈號。你的工作只是在不改變結論的前提下，把原因翻譯成容易理解的臺灣繁體中文。你不能重新計算、修改、升級或推翻程式結果。

必須遵守：

1. overview、simple_reason、for_not_holding、for_holding與attention_explanation必須使用日常用語與短句，不可出現RSI、MACD、KD、均線、背離等技術名詞。
2. 技術名詞只能出現在technical_reasons或risk_warnings；第一次出現時，要立刻用一句白話解釋。
3. 如果出現「價格創較高高點」，要解釋為「後一個已確認的波段最高價，高於前一個波段最高價」。
4. 如果出現「頂背離」，要解釋為「價格高點變高，但指標高點沒有一起變高，代表價格與動能方向不一致」。
5. RSI頂背離與MACD頂背離必須分開說明。
6. 必須原樣保留fixed_decision內的operation_reference、for_not_holding_reference、for_holding_reference、attention_level、attention_explanation、not_holding_text與holding_text。
7. 必須保留輸入資料中的方向、分數、風險等級、理由和警告，不得自行改寫成不同結論。
8. 低風險不代表沒有警告；如果risk_warnings有內容，必須全部解釋。
9. 不得虛構輸入資料沒有的價格、日期、新聞、原因、指標、成本或持股數量。
10. 不得預測未來價格、反轉、反彈、回檔或漲跌方向。
11. 只有fixed_decision已提供的「可考慮分批買進、持有觀察、可考慮分批減碼、暫不操作」可以出現在操作說明中，不得自行產生其他操作指示。
12. 不得建議一次投入全部資金、借錢投資、融資、全數賣出，或給出未經程式提供的價位與數量。
13. 不得使用保證、一定會、穩賺或即將會等確定性字眼。
14. simple_reason最多兩句；每一項技術白話解釋最多兩句。
15. 只能回傳符合指定格式的JSON，不要輸出Markdown，不要顯示思考過程。
16. disclaimer必須固定為「此內容依歷史技術資料提供操作參考，不代表未來績效，也不能取代個人的資金與風險判斷。」
17. 禁止使用「看好、可能、將會、即將、回檔、反轉、反彈、失速、買氣飽和」等推測未來或延伸因果的文字。
18. 解釋頂背離時，最後必須說明「這只表示價格與指標方向不一致，無法單獨判斷後續漲跌」。
19. 錯誤範例：「可能要回檔」、「漲勢可能失速」、「這檔一定會漲」。
20. 正確範例：「後一個已確認的波段最高價較高，但指標高點沒有同步變高；這只表示價格與指標方向不一致，無法單獨判斷後續漲跌。」
"@

    $explanationSchema = @{
        type = "object"
        properties = [ordered]@{
            overview = @{
                type = "string"
            }
            operation_reference = @{
                type = "string"
                enum = @($operationReference)
            }
            simple_reason = @{
                type = "string"
            }
            for_not_holding = @{
                type = "string"
                enum = @($notHoldingText)
            }
            for_holding = @{
                type = "string"
                enum = @($holdingText)
            }
            attention_level = @{
                type = "string"
                enum = @($attentionLevel)
            }
            attention_explanation = @{
                type = "string"
                enum = @($attentionExplanation)
            }
            technical_reasons = @{
                type = "array"
                items = @{
                    type = "object"
                    properties = [ordered]@{
                        original = @{ type = "string" }
                        explanation = @{ type = "string" }
                    }
                    required = @("original", "explanation")
                    additionalProperties = $false
                }
            }
            risk_warnings = @{
                type = "array"
                items = @{
                    type = "object"
                    properties = [ordered]@{
                        original = @{ type = "string" }
                        explanation = @{ type = "string" }
                    }
                    required = @("original", "explanation")
                    additionalProperties = $false
                }
            }
            disclaimer = @{
                type = "string"
                enum = @("此內容依歷史技術資料提供操作參考，不代表未來績效，也不能取代個人的資金與風險判斷。")
            }
        }
        required = @(
            "overview",
            "operation_reference",
            "simple_reason",
            "for_not_holding",
            "for_holding",
            "attention_level",
            "attention_explanation",
            "technical_reasons",
            "risk_warnings",
            "disclaimer"
        )
        additionalProperties = $false
    }

    $messages = @(
        @{
            role = "system"
            content = $systemPrompt
        },
        @{
            role = "user"
            content = "請依照fixed_decision產生給完全不懂技術指標的家人閱讀的白話操作參考：`n$explanationJson`n/no_think"
        }
    )

    $ollamaRequest = @{
        model = $Model
        messages = $messages
        format = $explanationSchema
        stream = $false
        think = $false
        keep_alive = 0
        options = @{
            num_ctx = 4096
            temperature = 0.0
            num_predict = 700
            seed = 42
        }
    }

    $ollamaRequestJson = $ollamaRequest | ConvertTo-Json -Depth 15
    $ollamaHttpClient = $null

    try {
        $ollamaHttpClient = [System.Net.Http.HttpClient]::new()
        $ollamaHttpClient.Timeout = [TimeSpan]::FromSeconds(120)
        $requestBytes = [System.Text.Encoding]::UTF8.GetBytes($ollamaRequestJson)
        $requestContent = [System.Net.Http.ByteArrayContent]::new($requestBytes)
        $requestContent.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse("application/json; charset=utf-8")

        $ollamaResponse = $ollamaHttpClient.PostAsync(
            "$normalizedOllamaBaseUrl/api/chat",
            $requestContent
        ).GetAwaiter().GetResult()

        $ollamaResponseBytes = $ollamaResponse.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
        $ollamaResponseText = [System.Text.Encoding]::UTF8.GetString($ollamaResponseBytes)

        if (-not $ollamaResponse.IsSuccessStatusCode) {
            throw ("Ollama HTTP {0}：{1}" -f [int]$ollamaResponse.StatusCode, $ollamaResponseText)
        }

        $ollamaEnvelope = $ollamaResponseText | ConvertFrom-Json
        $plainExplanation = $ollamaEnvelope.message.content | ConvertFrom-Json

        $plainExplanation.overview = (
            "{0}（{1}）目前的操作參考是「{2}」，注意程度是{3}；這次使用的是{4}風險偏好與「{5}」情境。" -f
            $analysis.name,
            $analysis.symbol,
            $operationReference,
            $attentionLevel,
            $riskToleranceLabel,
            $positionLabel
        )
        $plainExplanation.operation_reference = $operationReference
        $plainExplanation.for_not_holding = $notHoldingText
        $plainExplanation.for_holding = $holdingText
        $plainExplanation.attention_level = $attentionLevel
        $plainExplanation.attention_explanation = $attentionExplanation
        $plainExplanation.disclaimer = "此內容依歷史技術資料提供操作參考，不代表未來績效，也不能取代個人的資金與風險判斷。"

        $simpleReasonForbiddenPattern = "RSI|MACD|KD|均線|背離|看好|可能|將會|即將|回檔|反轉|反彈|失速|買氣飽和|保證|一定會|穩賺|借錢|融資|全數"
        if (
            [string]::IsNullOrWhiteSpace([string]$plainExplanation.simple_reason) -or
            [string]$plainExplanation.simple_reason -match $simpleReasonForbiddenPattern
        ) {
            $plainExplanation.simple_reason = switch ($operationReference) {
                "可考慮分批買進" { "目前多數檢查結果方向一致，風險警告也在程式設定的範圍內，但仍適合分批處理。" }
                "可考慮分批減碼" { "目前程式偵測到的風險較高或操作分數偏低，分批降低部位是較保守的做法。" }
                "持有觀察" { "目前沒有足夠理由要求立刻賣出，先觀察下一次資料更新。" }
                default { "目前支持動作的條件還不完整，或仍有需要注意的警告，先不動作較保守。" }
            }
        }

        $safeReasonExplanations = @()
        foreach ($reason in @($analysis.reasons)) {
            if ($reason -match "均線呈多頭排列") {
                $safeExplanation = "短期均線高於中長期均線，代表目前不同期間的平均價格由短到長依序降低；這是程式對現有均線相對位置的描述。"
            }
            elseif ($reason -match "均線呈空頭排列") {
                $safeExplanation = "短期均線低於中長期均線，代表目前不同期間的平均價格由短到長依序升高；這是程式對現有均線相對位置的描述。"
            }
            else {
                $modelReason = @($plainExplanation.technical_reasons) |
                    Where-Object { $_.original -eq $reason } |
                    Select-Object -First 1
                $safeExplanation = if ($modelReason) {
                    [string]$modelReason.explanation
                }
                else {
                    "這是程式根據歷史K線計算出的技術理由。"
                }
            }

            $safeReasonExplanations += [pscustomobject]@{
                original = $reason
                explanation = $safeExplanation
            }
        }
        $plainExplanation.technical_reasons = $safeReasonExplanations

        $safeWarningExplanations = @()
        foreach ($warning in @($analysis.warnings)) {
            if ($warning -match "^RSI 頂背離") {
                $safeExplanation = "後一個已確認的波段最高價高於前一個波段最高價，但RSI高點沒有同步變高。RSI用來觀察價格動能，這只表示價格與指標方向不一致，無法單獨判斷後續漲跌。"
            }
            elseif ($warning -match "^MACD 頂背離") {
                $safeExplanation = "後一個已確認的波段最高價高於前一個波段最高價，但MACD高點沒有同步變高。MACD用來觀察趨勢與動能，這只表示價格與指標方向不一致，無法單獨判斷後續漲跌。"
            }
            else {
                $modelWarning = @($plainExplanation.risk_warnings) |
                    Where-Object { $_.original -eq $warning } |
                    Select-Object -First 1
                $safeExplanation = if ($modelWarning) {
                    [string]$modelWarning.explanation
                }
                else {
                    "這是程式根據歷史K線偵測到的風險警告，單一警告無法判斷後續漲跌。"
                }
            }

            $safeWarningExplanations += [pscustomobject]@{
                original = $warning
                explanation = $safeExplanation
            }
        }
        $plainExplanation.risk_warnings = $safeWarningExplanations

        $forbiddenExplanationPattern = "看好|可能|將會|即將|回檔|反轉|反彈|失速|買氣飽和|保證|一定會|穩賺|借錢|融資|全數賣出"
        $modelExplanationText = @(
            [string]$plainExplanation.simple_reason
            @($plainExplanation.technical_reasons) | ForEach-Object { [string]$_.explanation }
            @($plainExplanation.risk_warnings) | ForEach-Object { [string]$_.explanation }
        ) -join "`n"
        $forbiddenExplanationTerms = @(
            [regex]::Matches(
                $modelExplanationText,
                $forbiddenExplanationPattern
            ) |
                ForEach-Object { $_.Value } |
                Select-Object -Unique
        )

        if ($forbiddenExplanationTerms.Count -gt 0) {
            throw (
                "模型白話解說包含禁止內容：{0}" -f
                ($forbiddenExplanationTerms -join "、")
            )
        }
    }
    catch {
        Write-Host "家人版白話操作參考產生失敗" -ForegroundColor Red
        Write-Host $_.Exception.Message
        return
    }
    finally {
        if ($null -ne $ollamaHttpClient) {
            $ollamaHttpClient.Dispose()
        }
    }

    Write-Host "家人版操作參考：" -ForegroundColor Yellow
    Write-Host $plainExplanation.overview
    Write-Host ("操作參考：{0}" -f $plainExplanation.operation_reference) -ForegroundColor Cyan
    Write-Host ("一句話原因：{0}" -f $plainExplanation.simple_reason)
    Write-Host ("注意程度：{0}" -f $plainExplanation.attention_level)
    Write-Host ("注意說明：{0}" -f $plainExplanation.attention_explanation)

    Write-Host ""
    Write-Host "依持有狀態說明：" -ForegroundColor Yellow
    Write-Host ("- 尚未持有：{0}" -f $plainExplanation.for_not_holding)
    Write-Host ("- 已經持有：{0}" -f $plainExplanation.for_holding)

    if (@($plainExplanation.technical_reasons).Count -gt 0) {
        Write-Host ""
        Write-Host "技術理由白話說明：" -ForegroundColor Yellow
        foreach ($item in @($plainExplanation.technical_reasons)) {
            Write-Host ("- 原始內容：{0}" -f $item.original)
            Write-Host ("  白話說明：{0}" -f $item.explanation)
        }
    }

    if (@($plainExplanation.risk_warnings).Count -gt 0) {
        Write-Host ""
        Write-Host "風險警告白話說明：" -ForegroundColor Yellow
        foreach ($item in @($plainExplanation.risk_warnings)) {
            Write-Host ("- 原始內容：{0}" -f $item.original)
            Write-Host ("  白話說明：{0}" -f $item.explanation)
        }
    }

    Write-Host ""
    Write-Host $plainExplanation.disclaimer -ForegroundColor DarkYellow

    Write-Host ""
    Write-Host "連線、模型、MySQL 資料與家人版操作參考測試完成。" -ForegroundColor Green
}
