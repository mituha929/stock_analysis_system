from datetime import date, datetime

from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = "ok"
    capital_status: str
    capital_message: str
    quote_ready: bool
    quote_connection_state: str
    quote_connection_count: int | None = None
    quote_out_limit: bool | None = None
    quote_status_code: int | None = None
    connection_events: list[tuple[int, int]] = Field(default_factory=list, description="僅國內行情：COM為(狀態,實際錯誤碼)；簡化DLL第二值0為佔位，詳見connection_diagnostics")
    connection_diagnostics: dict[str, Any] = Field(default_factory=dict)
    process_memory_working_set_mb: float | None = None
    process_memory_private_mb: float | None = None
    memory_protection_status: str = "unavailable"
    memory_warning_threshold_mb: int | None = None
    memory_stop_backfill_threshold_mb: int | None = None
    native_worker: dict[str, Any] | None = None


class ConnectRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)
    authority: int = Field(
        default=0,
        ge=0,
        le=3,
        description="0=正式環境、1=正式環境 SGX、2=測試環境、3=測試環境 SGX",
    )


class ConnectionResponse(BaseModel):
    status: str
    message: str
    announcement_callback_received: bool | None = None


class WatchlistItemRequest(BaseModel):
    symbol: str = Field(pattern=r"^[A-Za-z0-9]+$", min_length=1, max_length=20)
    page_no: int = Field(default=1, ge=0, le=32767)


class Quote(BaseModel):
    symbol: str
    name: str = ""
    price: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    prev_close: float | None = None
    bid: float | None = None
    bid_volume: int | None = None
    ask: float | None = None
    ask_volume: int | None = None
    volume: int | None = None
    updated_at: datetime


class PriceLevel(BaseModel):
    price: float | None
    volume: int


class Best5(BaseModel):
    symbol: str
    bids: list[PriceLevel]
    asks: list[PriceLevel]
    updated_at: datetime


class Tick(BaseModel):
    timestamp: datetime
    price: float | None
    volume: int
    bid: float | None = None
    ask: float | None = None


class Candle(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


class CandleResponse(BaseModel):
    symbol: str
    interval_minutes: int
    data: list[Candle]


class AnalysisResponse(BaseModel):
    """Temporary V2.2 API response, ready for the V2.4 GUI integration."""

    symbol: str
    name: str = Field(default="", description="商品主檔中的股票名稱")
    price: float
    previous_close: float = Field(description="前一根已儲存 K 線的收盤價")
    price_change: float = Field(description="最新收盤價相對前一根 K 線收盤價的差額")
    price_change_percent: float = Field(description="最新收盤價相對前一根 K 線收盤價的漲跌幅百分比")
    source: str
    interval: str
    candle_table: str
    first_candle_at: datetime
    latest_candle_at: datetime
    latest_written_at: datetime | None = Field(default=None, description="本次分析使用資料的最近寫入時間，UTC；不代表行情日期")
    timestamps: list[datetime] = Field(default_factory=list, description="依序對齊每個指標陣列的 K 線時間")
    candles: list[Candle] = Field(default_factory=list, description="依 timestamps 排列的 OHLCV K 線，可供前端繪製圖表")
    candle_count: int
    output_indicator_names: dict[str, str] = Field(
        default_factory=dict,
        description="本次輸出內容的程式欄位名稱與中文技術指標名稱對照表",
    )
    indicators: dict[str, Any]
    signals: dict[str, str]
    divergences: dict[str, dict[str, int | str]] = Field(default_factory=dict)
    scores: dict[str, int]
    summary: dict[str, Any]
    stock_evaluation: dict[str, Any] = Field(
        default_factory=dict,
        description="依趨勢、風險與各分類分數整理的股票評價；僅供技術分析參考，不構成投資建議",
    )
    action_evaluation: dict[str, Any] = Field(
        default_factory=dict,
        description="可追溯的技術候選評分、扣分明細與規則版本；不代表買賣建議",
    )
    reasons: list[str]
    warnings: list[str]


class HistoricalCandleResponse(BaseModel):
    symbol: str
    interval: str
    start_date: date
    end_date: date
    max_days: int
    data: list[Candle]
    excluded_periods: list[dict[str, Any]] = Field(default_factory=list)


class Instrument(BaseModel):
    """One tradeable product returned by Capital's RequestStockList API."""

    market_no: int
    category_no: int
    category_name: str
    symbol: str
    name: str
    order_symbol: str
    expiry_date: str | None = None


class StockListResponse(BaseModel):
    market_no: int
    market_name: str
    count: int
    returned_count: int
    data: list[Instrument]


class DatabaseInitResponse(BaseModel):
    database: str
    user: str
    message: str


class InstrumentSyncResponse(BaseModel):
    market_no: int
    market_name: str
    table_name: str
    fetched_count: int
    sync_run_id: int
    resumed: bool
    message: str


class HistoricalCandleSyncResponse(BaseModel):
    symbol: str
    interval: str
    table_name: str
    start_date: date
    end_date: date
    fetched_count: int
    upserted_count: int
    message: str
    excluded_periods: list[dict[str, Any]] = Field(default_factory=list)


class BackfillPreparationResponse(BaseModel):
    force: bool = False
    plan_saved: bool = False
    market_nos: list[int]
    instrument_table: str
    candle_table: str
    job_table: str
    interval: str
    start_date: date
    end_date: date
    target_bars: int
    eligible_symbols: int
    created_jobs: int
    resumed_jobs: int
    storage: dict[str, Any]
    message: str


class StaleBackfillPlan(BaseModel):
    market_no: int
    market_name: str
    interval: str
    job_table: str
    candle_table: str
    target_bars: int | None = None
    oldest_updated_at: datetime | None = None
    start_date: date | None = None
    end_date: date | None = None
    queued_jobs: int = 0
    initialization_jobs: int = 0
    incremental_jobs: int = 0
    action: str


class StaleBackfillResponse(BaseModel):
    requested_markets: list[int]
    requested_intervals: list[str]
    target_bars: int | None
    target_bars_by_interval: dict[str,int] = Field(default_factory=dict)
    resume: bool
    plans: list[StaleBackfillPlan]
    message: str


class BackfillJobCounts(BaseModel):
    pending: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    insufficient_history: int = 0
    waiting_review: int = 0


class BackfillStatusItem(BaseModel):
    market_no: int
    market_name: str
    interval: str
    target_bars: int | None = Field(default=None,ge=1,description="此週期的歷史補齊目標；不是資料儲存上限")
    instrument_table: str
    candle_table: str
    job_table: str
    storage_ready: bool
    active_instruments: int = 0
    tracked_symbols: int = 0
    coverage_percent: float = 0
    job_counts: BackfillJobCounts = Field(default_factory=BackfillJobCounts)
    average_fetch_seconds: float | None = Field(default=None,ge=0,description="同市場／週期各股最新已完成或歷史不足工作，首次執行的平均秒數；排除重試及續跑")
    average_fetch_sample_count: int = Field(default=0,ge=0,description="平均抓取時間採用的工作／商品檔數")
    pending_phases: dict[str,int] = Field(default_factory=dict,description="等待分類：latest最新、catchup長缺口、history舊歷史、unknown舊版待分類")
    checked_through: date | None = Field(default=None,description="有檢查紀錄商品的最舊檢查日；空資料成功或官方跳過亦可推進，不等於成交日期")
    checked_symbols: int = Field(default=0,ge=0)
    data_missing_symbols: int = Field(default=0,ge=0,description="完全沒有本週期K線的有效商品數")
    unfinished_symbols: int = Field(default=0,ge=0,description="最後K線標記日落在尚未結束週／月的商品數；不是完整性保證")
    data_through: date | None = None
    oldest_successful_update_at: datetime | None = None
    latest_successful_update_at: datetime | None = None
    latest_status_at: datetime | None = None
    latest_failure_symbol: str | None = None
    latest_failure_error: str | None = None
    latest_failure_at: datetime | None = None


class DailyBackfillScheduleStatus(BaseModel):
    refresh_bars_by_interval: dict[str, int] = Field(default_factory=dict)
    aggregate_week_month_from_day: bool = False
    enabled: bool
    state: str
    timezone: str
    local_time: str
    markets: list[int]
    intervals: list[str]
    target_bars: int
    target_bars_by_interval: dict[str, int] = Field(default_factory=dict)
    resume: bool
    next_run_at: datetime | None = None
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_message: str | None = None


class DailyBackfillRunLog(BaseModel):
    id: int
    status: str
    stage: str
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    board_instrument_count: int = 0
    odd_lot_instrument_count: int = 0
    queued_jobs: int = 0
    processed_jobs: int = 0
    completed_jobs: int = 0
    insufficient_jobs: int = 0
    failed_jobs: int = 0
    message: str | None = None
    error_message: str | None = None


class BackfillStatusResponse(BaseModel):
    generated_at: datetime
    requested_markets: list[int]
    requested_intervals: list[str]
    items: list[BackfillStatusItem]
    schedule: DailyBackfillScheduleStatus
    schedule_logs: list[DailyBackfillRunLog] = Field(default_factory=list)
    control: dict[str, Any] = Field(default_factory=dict)
    database_query_seconds: float = 0
    database_cache_age_seconds: float = 0
    database_cache_hit: bool = False
    database_refreshing: bool = False
    database_error: str | None = None


class BackfillRetryPlan(BaseModel):
    market_no: int
    market_name: str
    interval: str
    start_date: date
    end_date: date
    job_table: str
    candle_table: str
    queued_jobs: int


class BackfillRetryResponse(BaseModel):
    requested_markets: list[int]
    requested_intervals: list[str]
    queued_jobs: int
    plans: list[BackfillRetryPlan]
    message: str
