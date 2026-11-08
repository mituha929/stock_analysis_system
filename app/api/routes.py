import time
import threading
import logging
import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, AsyncIterator, Callable

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse

from app.schemas.models import (
    AnalysisResponse, BackfillPreparationResponse, BackfillRetryPlan, BackfillRetryResponse,
    BackfillStatusItem, BackfillStatusResponse, Best5, Candle, CandleResponse, ConnectRequest,
    ConnectionResponse, DailyBackfillRunLog, DailyBackfillScheduleStatus, DatabaseInitResponse, HealthResponse,
    HistoricalCandleResponse, HistoricalCandleSyncResponse, InstrumentSyncResponse, Quote,
    StaleBackfillPlan, StaleBackfillResponse, StockListResponse, Tick, WatchlistItemRequest,
)
from app.services.backfill_scheduler import DailyBackfillConfig
from app.services.capital_dll_quote_service import CapitalApiError, CapitalQuoteService, STOCK_LIST_MARKETS
from app.services.capital_history_service import CapitalHistoryError, INTERVAL_OPTIONS, max_history_days, validate_history_range
from app.services.capital_history_connection import is_history_connection_error
from app.services.candle_validation import CandleDataError
from app.services.history_exclusions import exclusion_info
from app.services.mysql_instrument_repository import MySQLInstrumentRepository, MySQLRepositoryError
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.backfill_policy import completed_end, initial_start
from app.services.backfill_targets import configured_targets, target_for, prioritize_day
from app.services.backfill_control import BackfillControl
from app.services.capital_process_lock import CapitalProcessLockError
from app.services.backfill_status_cache import BackfillStatusCache
from app.services.backfill_engine import BackfillRunner
from app.services.empty_history_review import pending_review_text
from app.services.backfill_memory import BackfillMemoryGuard, BackfillMemoryPause, mark_memory_pause, memory_resume_ready
from app.services.process_memory import current_process_memory, memory_limit_mb, memory_status
from app.services.taiwan_trading_calendar import (
    TaiwanTradingCalendarError,
    latest_trading_day_on_or_before,
    trading_day_status,
)
from app.signals import analyze_technical

router = APIRouter(prefix="/api/v1")


class UTF8JSONResponse(JSONResponse):
    """Windows PowerShell 5 uses the declared charset to decode JSON text."""
    media_type = "application/json; charset=utf-8"

BOARD_LOT_MARKETS = [0, 1, 4]
ODD_LOT_MARKETS = [5, 6]
BOARD_LOT_CANDLE_TABLE = "candles"
BOARD_LOT_JOB_TABLE = "candle_backfill_jobs"
BOARD_LOT_INSTRUMENT_TABLE = "instruments"
ODD_LOT_CANDLE_TABLE = "odd_lot_candle"
ODD_LOT_JOB_TABLE = "odd_lot_candle_backfill_jobs"
ODD_LOT_INSTRUMENT_TABLE = "odd_lot_instruments"
ALL_HISTORY_INTERVALS = tuple(prioritize_day(INTERVAL_OPTIONS))
BACKGROUND_KLINE_REQUEST_INTERVAL_SECONDS = 0.8
MEMORY_WARNING_MB = memory_limit_mb("API_MEMORY_WARNING_MB", 2048)
MEMORY_STOP_BACKFILL_MB = memory_limit_mb("API_MEMORY_STOP_BACKFILL_MB", 4096)
MINIMUM_EVALUATION_CANDLES = 20
BACKFILL_WORKER_LOCK = threading.Lock()
STATUS_CACHE_INIT_LOCK = threading.Lock()


class DailyTakeoverPause(Exception):
    """Stop an instrument sync after its current durable batch."""

MARKET_BACKFILL_SOURCES = {
    0: ("上市", BOARD_LOT_INSTRUMENT_TABLE, BOARD_LOT_CANDLE_TABLE, BOARD_LOT_JOB_TABLE),
    1: ("上櫃", BOARD_LOT_INSTRUMENT_TABLE, BOARD_LOT_CANDLE_TABLE, BOARD_LOT_JOB_TABLE),
    4: ("興櫃", BOARD_LOT_INSTRUMENT_TABLE, BOARD_LOT_CANDLE_TABLE, BOARD_LOT_JOB_TABLE),
    5: ("上市零股", ODD_LOT_INSTRUMENT_TABLE, ODD_LOT_CANDLE_TABLE, ODD_LOT_JOB_TABLE),
    6: ("上櫃零股", ODD_LOT_INSTRUMENT_TABLE, ODD_LOT_CANDLE_TABLE, ODD_LOT_JOB_TABLE),
}


def backfill_start_date(interval: str, end_date: date, target_bars: int) -> date:
    """Match the backfill script's buffered calendar lookback."""
    days_per_bar = {
        "1m": 10 / 360, "5m": 16 / 360, "15m": 45 / 360,
        "60m": 160 / 360, "day": 1.55,
        "week": 10.5, "month": 31.5,
    }[interval]
    return end_date - timedelta(days=max(7, int(target_bars * days_per_bar)))


def parse_refresh_intervals(value: str) -> list[str]:
    normalized = value.strip().lower()
    if normalized == "all":
        return list(ALL_HISTORY_INTERVALS)
    intervals = list(dict.fromkeys(item.strip() for item in normalized.split(",") if item.strip()))
    if not intervals or any(interval not in INTERVAL_OPTIONS for interval in intervals):
        allowed = ", ".join(ALL_HISTORY_INTERVALS)
        raise HTTPException(status_code=422, detail=f"intervals 僅支援 all 或 {allowed}")
    return prioritize_day(intervals)


def parse_refresh_markets(value: str) -> list[int]:
    """Parse the market selection accepted by the automatic backfill route."""
    normalized = value.strip().lower()
    if normalized == "all":
        return list(MARKET_BACKFILL_SOURCES)
    try:
        markets = list(dict.fromkeys(int(item.strip()) for item in normalized.split(",") if item.strip()))
    except ValueError as error:
        raise HTTPException(status_code=422, detail="markets 僅支援 all 或以逗號分隔的 0、1、4、5、6") from error
    if not markets or any(market not in MARKET_BACKFILL_SOURCES for market in markets):
        raise HTTPException(status_code=422, detail="markets 僅支援 all 或以逗號分隔的 0、1、4、5、6")
    return markets


def database_utc(value: datetime | None) -> datetime | None:
    """MySQL stores this project's UTC timestamps without a timezone suffix."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def history_windows(interval: str, start_date: date, end_date: date) -> list[tuple[date, date]]:
    """Split a stale range by the configured inclusive calendar-day limit."""
    maximum = max_history_days(interval)
    result = []
    start = start_date
    while start <= end_date:
        end = start + timedelta(days=min(maximum - 1, (end_date - start).days))
        result.append((start, end))
        start = end + timedelta(days=1)
    return result


def service(request: Request) -> CapitalQuoteService:
    return request.app.state.capital_service


async def capital_dll_access(request: Request) -> AsyncIterator[None]:
    """Queue native Capital operations within this FastAPI process.

    The service itself owns the cross-process lock for a connected session;
    this asyncio lock additionally keeps concurrent HTTP requests orderly.
    """
    async with request.app.state.capital_dll_async_lock:
        yield


@router.get("/health", response_model=HealthResponse, tags=["系統"], summary="取得系統與群益連線狀態", description="回傳 API、群益報價主機與商品基本資料的目前連線狀態。")
def health(capital: CapitalQuoteService = Depends(service)) -> HealthResponse:
    try:
        quote_connection_state = capital.refresh_quote_connection_state()
    except CapitalApiError:
        quote_connection_state = capital.quote_connection_state
    memory = current_process_memory()
    return HealthResponse(
        capital_status=capital.status,
        capital_message=capital.status_message,
        quote_ready=capital.quote_ready,
        quote_connection_state=quote_connection_state,
        quote_connection_count=capital.quote_connection_count,
        quote_out_limit=capital.quote_out_limit,
        quote_status_code=capital.quote_status_code,
        connection_events=capital.connection_events,
        connection_diagnostics=capital.connection_diagnostics if getattr(type(capital), "connection_diagnostics", None) else {},
        process_memory_working_set_mb=memory.working_set_mb,
        process_memory_private_mb=memory.private_usage_mb,
        memory_protection_status=memory_status(memory, MEMORY_WARNING_MB, MEMORY_STOP_BACKFILL_MB),
        memory_warning_threshold_mb=MEMORY_WARNING_MB,
        memory_stop_backfill_threshold_mb=MEMORY_STOP_BACKFILL_MB,
        native_worker=capital.worker_snapshot() if getattr(type(capital), "worker_snapshot", None) else None,
    )


@router.post("/capital/connect", response_model=ConnectionResponse, tags=["群益連線"], summary="登入並連線群益報價主機", description="使用群益帳號登入，並建立國內行情報價主機連線。")
def connect(
    payload: ConnectRequest,
    capital: CapitalQuoteService = Depends(service),
    _dll_access: None = Depends(capital_dll_access, scope="function"),
) -> ConnectionResponse:
    try:
        connection_status, message, announcement_received = capital.connect(payload.user_id, payload.password, payload.authority)
        return ConnectionResponse(
            status=connection_status,
            message=message,
            announcement_callback_received=announcement_received,
        )
    except CapitalApiError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error


@router.post("/capital/disconnect", response_model=ConnectionResponse, tags=["群益連線"], summary="中斷群益報價主機連線", description="停止目前群益報價連線，並釋放跨行程 DLL 鎖。")
def disconnect(
    capital: CapitalQuoteService = Depends(service),
    _dll_access: None = Depends(capital_dll_access, scope="function"),
) -> ConnectionResponse:
    try:
        return ConnectionResponse(status="ready", message=capital.disconnect())
    except CapitalApiError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error


@router.get("/watchlist", response_model=list[str], tags=["即時報價"], summary="取得自選商品清單", description="回傳目前 API 已儲存的自選商品代號。")
def get_watchlist(capital: CapitalQuoteService = Depends(service)) -> list[str]:
    return capital.watchlist()


@router.post("/watchlist", response_model=ConnectionResponse, status_code=status.HTTP_201_CREATED, tags=["即時報價"], summary="新增自選商品並訂閱報價", description="將商品加入自選清單；群益連線完成後會訂閱即時報價與 Tick。")
def add_watchlist(
    payload: WatchlistItemRequest,
    capital: CapitalQuoteService = Depends(service),
    _dll_access: None = Depends(capital_dll_access, scope="function"),
) -> ConnectionResponse:
    try:
        return ConnectionResponse(status="subscribed", message=capital.subscribe(payload.symbol, payload.page_no))
    except CapitalApiError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error


@router.delete("/watchlist/{symbol}", response_model=ConnectionResponse, tags=["即時報價"], summary="移除自選商品", description="取消商品的即時報價與 Tick 訂閱，並從自選清單移除。")
def remove_watchlist(
    symbol: str,
    capital: CapitalQuoteService = Depends(service),
    _dll_access: None = Depends(capital_dll_access, scope="function"),
) -> ConnectionResponse:
    try:
        return ConnectionResponse(status="removed", message=capital.unsubscribe(symbol))
    except CapitalApiError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error


@router.get("/watchlist/quotes", response_model=list[Quote], tags=["即時報價"], summary="取得自選商品即時報價", description="從記憶體快取回傳所有自選商品的最新報價，不會額外呼叫群益 DLL。")
def watchlist_quotes(capital: CapitalQuoteService = Depends(service)) -> list[Quote]:
    return capital.cache.get_quotes(capital.watchlist())


@router.get("/quotes/{symbol}", response_model=Quote, tags=["即時報價"], summary="取得單一商品即時報價", description="從記憶體快取回傳指定商品的最新成交與五檔摘要價格。")
def quote(symbol: str, capital: CapitalQuoteService = Depends(service)) -> Quote:
    result = capital.cache.get_quote(symbol.upper())
    if result is None:
        raise HTTPException(status_code=404, detail="尚未收到此股票的即時報價")
    return result


@router.get("/quotes/{symbol}/best5", response_model=Best5, tags=["即時報價"], summary="取得五檔委買委賣", description="從記憶體快取回傳指定商品的五檔買賣價量。")
def best5(symbol: str, capital: CapitalQuoteService = Depends(service)) -> Best5:
    result = capital.cache.get_best5(symbol.upper())
    if result is None:
        raise HTTPException(status_code=404, detail="尚未收到此股票的五檔報價")
    return result


@router.get("/quotes/{symbol}/ticks", response_model=list[Tick], tags=["即時報價"], summary="取得最近 Tick 明細", description="回傳指定商品在記憶體中保留的最近逐筆成交資料。")
def ticks(
    symbol: str,
    limit: int = Query(default=100, ge=1, le=500, description="回傳最近幾筆 Tick"),
    capital: CapitalQuoteService = Depends(service),
) -> list[Tick]:
    return capital.cache.get_ticks(symbol.upper(), limit=limit)


@router.get("/instruments", response_model=StockListResponse, tags=["商品主檔"], summary="查詢群益商品清單", description="向群益查詢指定市場別的商品清單；此端點只預覽，不寫入資料庫。")
def instruments(
    market_no: int = Query(..., description="0=上市、1=上櫃、4=興櫃、5=盤中零股上市、6=盤中零股上櫃"),
    limit: int = Query(default=500, ge=1, le=10_000, description="最多回傳幾檔商品；預設 500"),
    capital: CapitalQuoteService = Depends(service),
    _dll_access: None = Depends(capital_dll_access, scope="function"),
) -> StockListResponse:
    """Fetch Capital's product master list for one domestic market."""
    try:
        data = capital.fetch_stock_list(market_no)
        return StockListResponse(
            market_no=market_no,
            market_name=STOCK_LIST_MARKETS[market_no],
            count=len(data),
            returned_count=min(len(data), limit),
            data=data[:limit],
        )
    except CapitalApiError as error:
        status_code = status.HTTP_422_UNPROCESSABLE_ENTITY if market_no not in STOCK_LIST_MARKETS else status.HTTP_503_SERVICE_UNAVAILABLE
        raise HTTPException(status_code=status_code, detail=str(error)) from error


@router.post("/database/init", response_model=DatabaseInitResponse, tags=["資料庫管理"], summary="初始化 MySQL 資料庫", description="建立或驗證專案使用的 MySQL 資料庫與預設資料表。")
def initialize_database() -> DatabaseInitResponse:
    """Create the configured MySQL database and product-master tables."""
    try:
        repository = MySQLInstrumentRepository()
        repository.initialize()
        return DatabaseInitResponse(
            database=repository.settings.database,
            user=repository.settings.user,
            message=f"MySQL 商品主檔資料庫已初始化（目前登入帳號：{repository.settings.user}）",
        )
    except MySQLRepositoryError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error


def _sync_instrument_markets(
    market_nos: list[int], table_name: str, batch_size: int, capital: CapitalQuoteService,
) -> list[InstrumentSyncResponse]:
    """Fetch and persist each market in a fixed board-lot or odd-lot group."""
    try:
        with BACKFILL_WORKER_LOCK:
            return _sync_instrument_markets_unlocked(
                market_nos, table_name, batch_size, capital,
            )
    except CapitalApiError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    except MySQLRepositoryError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error


def _sync_instrument_markets_unlocked(
    market_nos: list[int], table_name: str, batch_size: int, capital: CapitalQuoteService,
    memory_guard=None,
) -> list[InstrumentSyncResponse]:
    """Synchronize one market group while the caller owns the workflow lock."""
    repository = MySQLInstrumentRepository()
    results = []
    for market_no in market_nos:
        if memory_guard:
            snapshot = BackfillControl().snapshot()
            marker = snapshot.get("manual_daily_run") or {}
            if marker.get("phase") == "waiting_stop" or (snapshot.get("scheduled_daily_priority") or {}).get("phase") == "waiting_stop":
                raise DailyTakeoverPause("已保存目前商品批次；準備切換每日更新")
        if memory_guard: memory_guard.require(f"before_instruments_{market_no}")
        data = capital.fetch_stock_list(market_no)
        if not data:
            raise CapitalApiError(
                f"{STOCK_LIST_MARKETS[market_no]}商品主檔回傳 0 筆，為避免把既有商品誤標為停用，已中止同步"
            )
        stop_after_sync = memory_guard.check(f"after_instrument_fetch_{market_no}_before_write") if memory_guard else False
        sync_run_id, fetched_count, resumed = repository.sync_market(
            market_no, data, batch_size, table_name,
        )
        data.clear()
        del data
        if memory_guard:
            snapshot = BackfillControl().snapshot()
            marker = snapshot.get("manual_daily_run") or {}
            if marker.get("phase") == "waiting_stop" or (snapshot.get("scheduled_daily_priority") or {}).get("phase") == "waiting_stop":
                raise DailyTakeoverPause("已保存目前商品批次；準備切換每日更新")
        if stop_after_sync:
            raise BackfillMemoryPause(f"商品 {market_no} 已保存；程序記憶體達 {memory_guard.stop} MB 保護門檻")
        if memory_guard: memory_guard.require(f"after_instruments_{market_no}")
        results.append(InstrumentSyncResponse(
            market_no=market_no,
            market_name=STOCK_LIST_MARKETS[market_no],
            table_name=table_name,
            fetched_count=fetched_count,
            sync_run_id=sync_run_id,
            resumed=resumed,
            message="商品清單已同步至 MySQL",
        ))
    return results


@router.post(
    "/board-lot/instruments/sync", response_model=list[InstrumentSyncResponse], tags=["商品主檔"],
    summary="同步整張商品清單至 MySQL",
    description="依序向群益取得市場 0、1、4 的商品清單，固定以可續傳方式寫入 instruments 資料表。",
)
def sync_board_lot_instruments(
    batch_size: int = Query(default=500, ge=1, le=1000, description="每批寫入 MySQL 的商品數；預設 500"),
    capital: CapitalQuoteService = Depends(service),
    _dll_access: None = Depends(capital_dll_access, scope="function"),
) -> list[InstrumentSyncResponse]:
    return _sync_instrument_markets(BOARD_LOT_MARKETS, BOARD_LOT_INSTRUMENT_TABLE, batch_size, capital)


@router.post(
    "/odd-lot/instruments/sync", response_model=list[InstrumentSyncResponse], tags=["商品主檔"],
    summary="同步零股商品清單至 MySQL",
    description="依序向群益取得市場 5、6 的商品清單，固定以可續傳方式寫入 odd_lot_instruments 資料表。",
)
def sync_odd_lot_instruments(
    batch_size: int = Query(default=500, ge=1, le=1000, description="每批寫入 MySQL 的商品數；預設 500"),
    capital: CapitalQuoteService = Depends(service),
    _dll_access: None = Depends(capital_dll_access, scope="function"),
) -> list[InstrumentSyncResponse]:
    return _sync_instrument_markets(ODD_LOT_MARKETS, ODD_LOT_INSTRUMENT_TABLE, batch_size, capital)


@router.get("/candles/{symbol}", response_model=CandleResponse, tags=["即時報價"], summary="由即時 Tick 組成分 K", description="使用記憶體中已接收的 Tick 資料，計算指定分鐘週期的即時 K 線。")
def candles(
    symbol: str,
    interval_minutes: int = Query(default=1, ge=1, le=60),
    capital: CapitalQuoteService = Depends(service),
) -> CandleResponse:
    supported = {1, 5, 15, 60}
    if interval_minutes not in supported:
        raise HTTPException(status_code=422, detail="interval_minutes 僅支援 1、5、15、60")
    normalized = symbol.upper()
    return CandleResponse(
        symbol=normalized,
        interval_minutes=interval_minutes,
        data=capital.cache.get_candles(normalized, interval_minutes),
    )


@router.get("/history/candles/{symbol}", response_model=HistoricalCandleResponse, tags=["歷史 K 線"], summary="查詢單一商品歷史 K 線", description="向群益查詢指定日期範圍的歷史 K 線，結果只放入記憶體快取，不寫入 MySQL。")
def historical_candles(
    symbol: str,
    interval: str = Query(default="day", pattern=r"^(1m|5m|15m|60m|day|week|month)$"),
    start_date: date | None = None,
    end_date: date | None = None,
    capital: CapitalQuoteService = Depends(service),
    _dll_access: None = Depends(capital_dll_access, scope="function"),
) -> HistoricalCandleResponse:
    """Fetch one symbol's historical K-lines with a bounded calendar range."""
    normalized = symbol.upper()
    end = end_date or date.today()
    maximum = max_history_days(interval)
    start = start_date or end - timedelta(days=maximum - 1)
    try:
        validate_history_range(interval, start, end)
    except CapitalHistoryError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    try:
        data = capital.fetch_historical_candles(normalized, interval, start, end)
    except CandleDataError as error:
        raise HTTPException(status_code=422,detail=dict(message=str(error),data_error=error.details)) from error
    except CapitalApiError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    return HistoricalCandleResponse(symbol=normalized, interval=interval, start_date=start, end_date=end,
        max_days=maximum, data=data, excluded_periods=exclusion_info(normalized,interval,start,end))


@router.post("/history/candles/{symbol}/sync", response_model=HistoricalCandleSyncResponse, tags=["歷史 K 線"], summary="查詢並儲存歷史 K 線", description="向群益取得單一商品的歷史 K 線，並寫入指定的 MySQL K 線資料表。")
def sync_historical_candles(
    symbol: str,
    interval: str = Query(default="day", pattern=r"^(1m|5m|15m|60m|day|week|month)$"),
    table_name: str = Query(default="candles", pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$", description="要寫入的 K 線表名；不存在時會建立"),
    start_date: date | None = None,
    end_date: date | None = None,
    capital: CapitalQuoteService = Depends(service),
    _dll_access: None = Depends(capital_dll_access, scope="function"),
) -> HistoricalCandleSyncResponse:
    """Fetch historical K-lines from Capital and upsert them into MySQL."""
    normalized = symbol.upper()
    end = end_date or date.today()
    maximum = max_history_days(interval)
    start = start_date or end - timedelta(days=maximum - 1)
    try:
        validate_history_range(interval, start, end)
        data = capital.fetch_historical_candles(normalized, interval, start, end, cache_result=False)
        committed = False
        try:
            upserted_count = MySQLCandleRepository().upsert_candles(normalized, interval, data, table_name)
            committed = True
        finally:
            if getattr(type(capital), "history_checkpoint", None) is not None:
                capital.history_checkpoint(committed)
    except CapitalHistoryError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (CapitalApiError, MySQLRepositoryError) as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    return HistoricalCandleSyncResponse(
        symbol=normalized,
        interval=interval,
        table_name=table_name,
        start_date=start,
        end_date=end,
        fetched_count=len(data),
        upserted_count=upserted_count,
        message="歷史 K 線已同步至 MySQL",
        excluded_periods=exclusion_info(normalized,interval,start,end),
    )


def _sync_historical_candles_to_table(
    symbol: str, interval: str, start_date: date | None, end_date: date | None,
    table_name: str, capital: CapitalQuoteService,
) -> HistoricalCandleSyncResponse:
    normalized = symbol.upper()
    end = end_date or date.today()
    maximum = max_history_days(interval)
    start = start_date or end - timedelta(days=maximum - 1)
    try:
        validate_history_range(interval, start, end)
        data = capital.fetch_historical_candles(normalized, interval, start, end, cache_result=False)
        committed = False
        try:
            upserted_count = MySQLCandleRepository().upsert_candles(normalized, interval, data, table_name)
            committed = True
        finally:
            if getattr(type(capital), "history_checkpoint", None) is not None:
                capital.history_checkpoint(committed)
    except CapitalHistoryError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (CapitalApiError, MySQLRepositoryError) as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    return HistoricalCandleSyncResponse(
        symbol=normalized, interval=interval, table_name=table_name,
        start_date=start, end_date=end, fetched_count=len(data), upserted_count=upserted_count,
        message="歷史 K 線已同步至 MySQL",
        excluded_periods=exclusion_info(normalized,interval,start,end),
    )


@router.post("/board-lot/history/candles/{symbol}/sync", response_model=HistoricalCandleSyncResponse, tags=["整張 K 線"], summary="同步單一整張商品歷史 K 線", description="向群益取得指定整張商品的歷史 K 線，固定寫入 candles 資料表。")
def sync_board_lot_historical_candles(
    symbol: str,
    interval: str = Query(default="day", pattern=r"^(1m|5m|15m|60m|day|week|month)$"),
    start_date: date | None = None,
    end_date: date | None = None,
    capital: CapitalQuoteService = Depends(service),
    _dll_access: None = Depends(capital_dll_access, scope="function"),
) -> HistoricalCandleSyncResponse:
    """Fetch one board-lot symbol and store it in the standard K-line table."""
    return _sync_historical_candles_to_table(
        symbol, interval, start_date, end_date, BOARD_LOT_CANDLE_TABLE, capital,
    )


@router.post("/odd-lot/history/candles/{symbol}/sync", response_model=HistoricalCandleSyncResponse, tags=["零股 K 線"], summary="同步單一零股商品歷史 K 線", description="向群益取得指定零股商品的歷史 K 線，固定寫入 odd_lot_candle 資料表。")
def sync_odd_lot_historical_candles(
    symbol: str,
    interval: str = Query(default="day", pattern=r"^(1m|5m|15m|60m|day|week|month)$"),
    start_date: date | None = None,
    end_date: date | None = None,
    capital: CapitalQuoteService = Depends(service),
    _dll_access: None = Depends(capital_dll_access, scope="function"),
) -> HistoricalCandleSyncResponse:
    """Fetch one odd-lot symbol and store it in ``odd_lot_candle``."""
    return _sync_historical_candles_to_table(
        symbol, interval, start_date, end_date, ODD_LOT_CANDLE_TABLE, capital,
    )


def _prepare_backfill(
    market_nos: list[int], instrument_table: str, candle_table: str, job_table: str, interval: str,
    target_bars: int | None, start_date: date | None, end_date: date | None,
    force: bool = False,
) -> BackfillPreparationResponse:
    target_source = "config" if target_bars is None else "override"
    target_bars = target_for(interval,target_bars)
    end = completed_end(interval, end_date)
    if start_date is not None and start_date > end:
        raise HTTPException(status_code=422, detail="start_date 不可晚於最後已完成交易日")
    try:
        repository = MySQLCandleRepository()
        if force:
            control = BackfillControl()
            if control.worker_active() or not BACKFILL_WORKER_LOCK.acquire(blocking=False):
                raise HTTPException(status_code=409,detail="回補仍在執行或準備中；請先安全暫停並等待工作停止，再重抓全部資料")
            try:
                with control.worker():
                    saved = control.snapshot()
                    if (saved.get("resume_pending") or saved.get("state") in {"running","queued","planning"}) and not saved.get("pause_requested"):
                        raise HTTPException(status_code=409,detail="已有續跑要求，請先安全暫停再建立重抓範圍")
                    control.pause(True)
                    first = start_date or initial_start(interval,end,target_bars)
                    summaries, work = [], []
                    for market in market_nos:
                        summary, plan = _prepare_requery_group(repository,market,interval,end,target_bars,
                            candle_table,job_table,instrument_table,first,target_source)
                        summaries.append(summary)
                        work.append(plan)
                    control.save_prepared_plans(work)
            finally:
                BACKFILL_WORKER_LOCK.release()
        else:
            with BackfillControl().worker():
                summaries = [repository.plan_incremental(
                    market, interval, end, target_bars, candle_table, job_table, instrument_table,
                    explicit_start=start_date,
                ) for market in market_nos]
        storage = repository.get_candle_storage_summary(market_nos, [interval], candle_table, instrument_table)
    except CapitalProcessLockError as error:
        raise HTTPException(status_code=409,detail="其他程序仍持有回補工作鎖，請先安全暫停再準備重抓") from error
    except MySQLRepositoryError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return BackfillPreparationResponse(
        force=force,plan_saved=force,
        market_nos=market_nos, instrument_table=instrument_table, candle_table=candle_table, job_table=job_table,
        interval=interval, start_date=min((s["start_date"] for s in summaries), default=end), end_date=end,
        target_bars=target_bars, eligible_symbols=int(storage["eligible_symbols"]),
        created_jobs=sum(s["queued_jobs"] for s in summaries), resumed_jobs=0,
        storage=storage["by_interval"][interval],
        message="已準備完整歷史範圍重抓工作；既有資料保留，請按繼續回補啟動" if force else "已依實際 K 線建立初始化／增量工作；執行回補腳本即可從區間斷點接續",
    )


def _prepare_requery_group(repository, market, interval, end, target, candles, jobs, instruments,
                           first=None, target_source="config"):
    """Common implementation for both prepare endpoints and full regrab."""
    first = first or initial_start(interval,end,target)
    summary = repository.plan_batch(market,interval,end,target,candles,jobs,instruments,
        force=True,explicit_start=first,recover_running=True)
    plan = dict(market_no=market,interval=interval,start_date=summary["start_date"],
        end_date=end,as_of_date=end,explicit_start=first,target_bars=target,
        target_source="override",prepared_target_source=target_source,candle_table=candles,job_table=jobs,
        instrument_table=instruments,batch_v2=True,force=False,fixed_range=True,
        planned_on=date.today().isoformat(),planner_version=3,
        planned_jobs=summary["queued_jobs"],summary=summary)
    return summary, plan


@router.post("/backfill/regrab-all", status_code=202, tags=["K 線自動更新"],
    summary="互斥準備全部市場及週期，成功後立即回補（休市日亦可）")
def regrab_all_backfill(background_tasks: BackgroundTasks, request: Request) -> dict[str, Any]:
    control = BackfillControl()
    lease = None
    if not BACKFILL_WORKER_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409,detail="已有回補執行或準備中；請先安全暫停並等待停止")
    try:
        try:
            lease = control.acquire_worker()
        except CapitalProcessLockError as error:
            raise HTTPException(status_code=409,detail="API 或獨立腳本已有回補執行中；本次不重排工作") from error
        state = control.snapshot()
        if (state.get("manual_daily_run") or {}).get("phase") in {"waiting_stop", "parking", "ready"}:
            raise HTTPException(status_code=409,detail="手動每日更新正在安全切換，請等待完成")
        if (state.get("scheduled_daily_priority") or {}).get("phase") in {"waiting_stop", "ready", "running"}:
            raise HTTPException(status_code=409,detail="排程每日更新正在優先切換或執行；請等待完成")
        if state.get("preparation_pending") or ((state.get("resume_pending") or
                state.get("state") in {"running","queued","planning","preparing"}) and not state.get("pause_requested")):
            raise HTTPException(status_code=409,detail="已有回補執行、準備或排隊中；請先安全暫停並等待停止")
        capital = service(request)
        if not capital.quote_ready:
            raise HTTPException(status_code=503,detail="群益主行情尚未就緒；本次未重排，請先完成連線")
        if not memory_resume_ready(control):
            raise HTTPException(status_code=409,detail="記憶體尚未符合恢復條件；本次未重排工作")
        token = uuid.uuid4().hex
        state = control.update(source="api",state="preparing",stage="preparing",manual_run=True,
            preparation_pending=True,preparation_complete=False,preparation_done=0,preparation_total=35,
            regrab_token=token,pause_requested=False,resume_pending=False,pause_kind=None,
            pause_reason="已取得獨占工作鎖；正在準備全部市場及週期，全部成功後自動開始")
        # Transfer BOTH locks to the background task. There is no unprotected
        # gap between the 35 groups or between preparation and the first fetch.
        background_tasks.add_task(_regrab_all_owned,capital,control,lease,token)
        lease = None
        return {"message":"已受理完整重抓；正在背景準備全部 35 個市場／週期，成功後直接開始抓取，無須再按繼續回補", "control":state}
    except BaseException:
        if lease is not None:
            lease.release()
        BACKFILL_WORKER_LOCK.release()
        raise


def _regrab_all_owned(capital, control, lease, token):
    done = 0
    try:
        repository = MySQLCandleRepository()
        guard = BackfillMemoryGuard(control,getattr(capital,"cache",None))
        plans = []
        for interval in ALL_HISTORY_INTERVALS:
            end, target = completed_end(interval), target_for(interval)
            for market, (_name,instruments,candles,jobs) in MARKET_BACKFILL_SOURCES.items():
                if control.snapshot().get("pause_requested"):
                    control.update(state="paused",stage="prepare_interrupted",preparation_pending=False,
                        preparation_complete=False,resume_pending=False,pause_kind="manual",
                        pause_reason=f"完整重抓準備已安全暫停（{done}/35）；請重新按重抓全部資料完成準備，不啟動部分範圍")
                    return
                guard.require("before_regrab_planning")
                control.update(current_market=market,current_interval=interval,
                    pause_reason=f"正在準備市場 {market}／{interval}；已完成 {done}/35 個族群")
                _, plan = _prepare_requery_group(repository,market,interval,end,target,candles,jobs,instruments)
                plans.append(plan)
                done += 1
                control.update(preparation_done=done)
                guard.require("after_regrab_planning")
        state = control.finish_regrab(plans,token)
        if state.get("pause_requested"):
            return
        if not memory_resume_ready(control):
            control.update(state="paused",pause_reason="完整範圍已準備；等待記憶體恢復後自動續跑")
            return
        if not capital.quote_ready:
            control.update(state="waiting_connection",pause_reason="完整範圍已準備；等待群益連線後自動續跑")
            return
        _run_stale_backfill_unlocked(capital,control.saved_plans(),1200,owns_worker_lock=True)
    except Exception as error:
        prepared = bool(control.snapshot().get("preparation_complete"))
        if isinstance(error,BackfillMemoryPause):
            mark_memory_pause(control)
        control.update(state="paused",stage="prepare_failed",preparation_pending=False,
            preparation_complete=prepared,pause_requested=True,resume_pending=False,
            pause_kind="memory" if isinstance(error,BackfillMemoryPause) else "prepare_error",
            pause_reason=(f"完整重抓執行中斷；{error}；斷點保留，可繼續回補" if prepared else
                f"完整重抓未完成（{done}/35）；{error}；請確認原因後重新重抓，不啟動部分範圍"))
        logging.getLogger(__name__).exception("完整重抓失敗；已停止，現有資料與斷點保留")
    finally:
        try:
            lease.release()
        finally:
            BACKFILL_WORKER_LOCK.release()


@router.post("/board-lot/backfill/prepare", response_model=BackfillPreparationResponse, tags=["整張 K 線"], summary="建立整張 K 線回補工作", description="為上市、上櫃與興櫃商品建立可續傳的 K 線回補工作；此端點不直接抓取 K 線。")
def prepare_board_lot_backfill(
    interval: str = Query(default="day", pattern=r"^(1m|5m|15m|60m|day|week|month)$"),
    target_bars: int | None = Query(default=None, ge=1, le=10_000,description="留空依週期目標：一般1200、週560、月100；指定則覆寫"),
    start_date: date | None = None,
    end_date: date | None = None,
    force: Annotated[bool,Query(description="true=重查設定歷史範圍，即使已有資料亦重新排入；準備後保持暫停，不直接抓取")] = False,
) -> BackfillPreparationResponse:
    """Create resumable backfill jobs for board-lot listed, OTC, and emerging products."""
    return _prepare_backfill(
        BOARD_LOT_MARKETS, BOARD_LOT_INSTRUMENT_TABLE, BOARD_LOT_CANDLE_TABLE, BOARD_LOT_JOB_TABLE,
        interval, target_bars, start_date, end_date, force,
    )


@router.post("/odd-lot/backfill/prepare", response_model=BackfillPreparationResponse, tags=["零股 K 線"], summary="建立零股 K 線回補工作", description="為上市零股與上櫃零股建立可續傳的 K 線回補工作；此端點不直接抓取 K 線。")
def prepare_odd_lot_backfill(
    interval: str = Query(default="day", pattern=r"^(1m|5m|15m|60m|day|week|month)$"),
    target_bars: int | None = Query(default=None, ge=1, le=10_000,description="留空依週期目標：一般1200、週560、月100；指定則覆寫"),
    start_date: date | None = None,
    end_date: date | None = None,
    force: Annotated[bool,Query(description="true=重查設定歷史範圍並保存暫停計畫；不刪除既有K線或直接抓取")] = False,
) -> BackfillPreparationResponse:
    """Create resumable backfill jobs for markets 5 and 6 in separate tables."""
    return _prepare_backfill(
        ODD_LOT_MARKETS, ODD_LOT_INSTRUMENT_TABLE, ODD_LOT_CANDLE_TABLE, ODD_LOT_JOB_TABLE,
        interval, target_bars, start_date, end_date, force,
    )


@router.get("/backfill/control", tags=["K 線自動更新"], summary="查詢回補進度與暫停原因，不依賴 MySQL")
def backfill_control_status() -> dict[str, Any]:
    control = BackfillControl()
    state = control.snapshot()
    return {**state, "control_version":7, "worker_active":control.worker_active(), "saved_plan_count":len(state.get("plans",[]))}


@router.post("/backfill/pause", tags=["K 線自動更新"], summary="在查詢區間寫入後安全暫停回補")
def pause_backfill() -> dict[str, Any]:
    result = BackfillControl().pause(True)
    return {"message":"已請求暫停；當前區間完整寫入後停止，進度保留", "control":result}


@router.post("/backfill/resume", tags=["K 線自動更新"], summary="從已保存進度續跑；無本機計畫時依所選市場重建")
def resume_backfill(background_tasks: BackgroundTasks, request: Request, response: Response,
                    markets: str = "all", intervals: str = "all", target_bars: int | None = Query(default=None,ge=1,le=10_000)) -> dict[str, Any]:
    control = BackfillControl()
    try:
        if (control.snapshot().get("manual_daily_run") or {}).get("phase") in {"waiting_stop", "parking", "ready"}:
            raise HTTPException(status_code=409,detail="每日更新正在安全切換，請等待切換完成")
        if (control.snapshot().get("scheduled_daily_priority") or {}).get("phase") == "waiting_stop":
            raise HTTPException(status_code=409,detail="排程每日更新正在等待慢速補齊安全停止")
        # Never clear another API/CLI worker's pause or overwrite its scope.
        with control.worker():
            state = control.snapshot()
            if state.get("preparation_pending") or state.get("preparation_complete") is False:
                raise HTTPException(status_code=409,detail="完整重抓尚未準備完成；請先完成重抓全部資料，不啟動部分工作")
            control.update(manual_run=True)
            if not memory_resume_ready(control):
                response.status_code = 202
                state = control.pause(False)
                return {"message":"記憶體尚未符合恢復條件；須低於恢復門檻並穩定一段時間，工作與斷點保留，不重排", "control":state}
            plans = control.saved_plans()
            if plans and state.get("state") == "completed":
                return {"message":"目前沒有已保存的待續跑工作", "control":state}
            scope = None if plans else {"markets":parse_refresh_markets(markets),"intervals":parse_refresh_intervals(intervals)}
            target = int(state.get("target_bars") or target_bars or 1200) if plans else target_bars or 1200
            state, created = control.request_resume(scope,target,target_source="config" if target_bars is None else "override")
            response.status_code = 202
            if not created:
                return {"message":"續跑要求已排入，請查看目前回補進度，不需重複點擊", "control":state}
            capital = service(request)
            if not capital.quote_ready:
                state = control.update(state="waiting_connection",pause_reason="等待使用者登入群益並連上報價主機")
                return {"message":"等待群益登入與報價主機連線，完成後自動續跑", "control":state}
            background_tasks.add_task(_resume_saved_backfill,capital,plans,target)
            message = "已排入區間斷點續傳" if plans else "沒有本機保存計畫；已在背景依所選市場與週期、實際資料重建增量／初始化計畫"
            return {"message":message+"；手動回補休市日亦可執行，僅抓已完成交易期間", "control":state}
    except CapitalProcessLockError as error:
        raise HTTPException(status_code=409,detail="API 或獨立腳本已有回補執行中；不重複啟動，請等待安全暫停完成") from error


def _resume_saved_backfill(capital, plans, target_bars):
    """Slow planning belongs in the background, not in the resume response."""
    control = BackfillControl()
    state = control.snapshot()
    if (state.get("manual_daily_run") or {}).get("phase") in {"waiting_stop", "parking", "ready"}:
        return _resume_manual_daily_takeover(capital)
    if (state.get("scheduled_daily_priority") or {}).get("phase") in {"waiting_stop", "ready"}:
        return _resume_scheduled_daily_priority(capital)
    if state.get("pause_requested") or not memory_resume_ready(control):
        return
    pending = state.get("pending_schedule")
    if pending:
        config = replace(DailyBackfillConfig.from_env(),markets=tuple(pending["markets"]),
                         intervals=tuple(pending["intervals"]),resume=True,
                         as_of_date=date.fromisoformat(pending["as_of_date"]) if pending.get("as_of_date") else None,
                         target_bars_by_interval=pending["target_bars_by_interval"],
                         aggregate_week_month_from_day=bool(pending.get("aggregate_week_month_from_day", False)),
                         **({"refresh_bars_by_interval":pending["refresh_bars_by_interval"]}
                            if pending.get("refresh_bars_by_interval") else {}))
        run_scheduled_backfill(capital,config,manual=bool(state.get("manual_run")),
            **({"force_completed_refresh":True} if pending.get("force_completed_refresh") else {}),
            **({"fresh_daily":True} if pending.get("fresh_daily") else {}),
            **({"skip_day_integrity":True} if pending.get("skip_day_integrity") else {}))
        return
    with BACKFILL_WORKER_LOCK:
        try:
            with control.worker():
                _resume_saved_backfill_owned(capital,control,plans,target_bars)
        except CapitalProcessLockError:
            # The owner may be a CLI process which won the race after HTTP
            # acknowledgement. Do not change any of that owner's state.
            return


def _resume_saved_backfill_owned(capital, control, plans, target_bars):
    state = control.snapshot()
    # HTTP/recovery may have observed an older scope before the lease was
    # acquired. The durable scope under this lease is authoritative.
    plans = control.saved_plans()
    if not memory_resume_ready(control):
        return
    if state.get("pause_requested"):
        control.update(state="paused",pause_kind="manual",pause_reason="使用者要求安全暫停")
        return
    if state.get("preparation_pending") or state.get("preparation_complete") is False:
        return
    if state.get("state") in {"completed","error","limited"}:
        return
    if not state.get("manual_run") and not trading_day_status(date.today())[0]:
        control.update(state="waiting_trading_day",pause_reason="今日休市，等待下一交易日")
        return
    if not capital.quote_ready:
        control.update(state="waiting_connection",pause_reason="等待使用者登入群益並連上報價主機")
        return
    try:
        if not plans:
            scope = state.get("resume_scope")
            if not scope:
                control.update(state="error",resume_pending=False,pause_reason="找不到待續跑範圍，請先建立更新計畫")
                return
            control.update(state="planning",stage="planning",pause_reason="正在依實際 K 線資料建立工作")
            targets = None if state.get("resume_target_source") == "config" else target_bars
            _,plans,_,_ = _plan_stale_backfill(scope["markets"],scope["intervals"],targets,True,
                owns_worker_lock=True,allow_non_trading_day=bool(state.get("manual_run")))
            if not plans:
                control.update(state="completed",resume_pending=False,resume_scope=None,
                               pause_reason="所選市場與週期已更新至已完成交易日，沒有待執行工作")
                return
            control.update(plans=plans,resume_scope=None,pause_reason=None)
        result = _run_stale_backfill_unlocked(capital,plans,target_bars,owns_worker_lock=True)
        priority = control.snapshot().get("scheduled_daily_priority") or {}
        if priority.get("phase") == "running":
            control.finish_scheduled_priority(priority["id"])
        if priority.get("phase") != "running" and any(plan.get("force_completed_refresh") for plan in plans):
            _record_manual_daily_result(control, _backfill_result_state(result),
                result.get("pause_reason") or "原每日更新工作已接續；請查看目前回補狀態")
    except Exception as error:
        kind = "database" if isinstance(error,MySQLRepositoryError) else "connection" if is_history_connection_error(error) else "error"
        control.update(state="paused" if kind != "error" else "error",resume_pending=False,
                       pause_kind=kind,pause_reason=str(error))
        if ((control.snapshot().get("scheduled_daily_priority") or {}).get("phase") != "running"
                and any(plan.get("force_completed_refresh") for plan in plans)):
            _record_manual_daily_result(control, "error" if kind == "error" else "paused", str(error))
        import logging
        logging.getLogger(__name__).exception("續跑準備失敗；已保存進度保留")


def _run_stale_backfill(
    capital: CapitalQuoteService, plans: list[dict[str, Any]], target_bars: int,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if not BACKFILL_WORKER_LOCK.acquire(blocking=False):
        return dict(paused=True,pause_kind="busy",pause_reason="已有回補執行或準備中",processed=0,completed=0,insufficient_history=0,failed=0)
    try:
        with BackfillControl().worker():
            return _run_stale_backfill_unlocked(capital, plans, target_bars, progress_callback,owns_worker_lock=True)
    except CapitalProcessLockError:
        return dict(paused=True,pause_kind="busy",pause_reason="其他程序已有回補執行中",processed=0,completed=0,insufficient_history=0,failed=0)
    finally:
        BACKFILL_WORKER_LOCK.release()


def _run_stale_backfill_unlocked(
    capital: CapitalQuoteService, plans: list[dict[str, Any]], target_bars: int,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    *, owns_worker_lock: bool = False,
) -> dict[str, Any]:
    # Upgrade saved legacy group descriptors without touching their candles.
    # Actual per-stock planning is performed only when the worker enters each
    # group, never while all other groups are still waiting in the queue.
    for plan in plans:
        if not plan.get("batch_v2"):
            plan.update(batch_v2=True,planned_on=None,
                instrument_table=MARKET_BACKFILL_SOURCES[plan["market_no"]][1],
                target_bars=plan.get("target_bars",target_bars))
    result = BackfillRunner(
        capital, repository=MySQLCandleRepository(), progress=progress_callback,
    ).run(plans, target_bars, **({"owns_worker_lock":True} if owns_worker_lock else {}))
    if plans and plans[0].get("run_id"):
        try:
            message = result.get("pause_reason", f"本輪正常抓取已結束；{result['deferred']} 筆工作保留待補" if result.get("deferred") else "本輪 K 線抓取已結束")
            if result.get('pending_review'):
                message += '；'+pending_review_text(result)
            waiting = bool(result.get("deferred") or result.get('audit_pending_symbols'))
            reviews = bool(result.get('pending_review'))
            MySQLCandleRepository().update_daily_backfill_run(
                plans[0]["run_id"], status="paused" if result.get("paused") or waiting else ("error" if result["failed"] else "waiting_review" if reviews else "completed"),
                stage="waiting_backfill" if waiting else result.get("pause_kind") or ("waiting_review" if reviews else "completed"),
                processed_jobs=result["processed"], completed_jobs=result["completed"],
                insufficient_jobs=result["insufficient_history"], failed_jobs=result["failed"],
                message=message, finished=True,
            )
        except MySQLRepositoryError:
            pass
    return result


def _plan_stale_backfill(
    requested_markets: list[int], requested_intervals: list[str],
    target_bars: int | dict[str, int] | None, use_resume: bool,
    today: date | None = None, owns_worker_lock: bool = False, allow_non_trading_day: bool = False,
    aggregate_from_day: bool = False,
) -> tuple[list[StaleBackfillPlan], list[dict[str, Any]], date, str]:
    current_day = today or date.today()
    is_trading_day, reason = trading_day_status(current_day)
    latest = completed_end("day", today)
    if not is_trading_day and not allow_non_trading_day:
        return [], [], latest, reason
    if not owns_worker_lock and (BackfillControl().snapshot().get("manual_daily_run") or {}).get("phase") in {"waiting_stop", "parking", "ready"}:
        raise RuntimeError("手動每日更新正在安全切換，不建立其他工作")
    if not owns_worker_lock and (BackfillControl().snapshot().get("scheduled_daily_priority") or {}).get("phase") in {"waiting_stop", "ready", "running"}:
        raise RuntimeError("排程每日更新正在優先切換或執行，不建立其他工作")
    if not owns_worker_lock and BackfillControl().worker_active():
        raise RuntimeError("已有回補工作執行中；可在資料狀態頁安全暫停後再建立計畫")
    plans, queued = [], []
    for market in requested_markets:
        market_name, instruments, candles, jobs = MARKET_BACKFILL_SOURCES[market]
        for interval in requested_intervals:
            target = target_for(interval,target_bars)
            derived = aggregate_from_day and interval in {"week", "month"}
            if derived:
                from app.services.daily_candle_aggregation import daily_aggregate_end
                end_date = daily_aggregate_end(today)
            else:
                end_date = completed_end(interval, today)
            plans.append(StaleBackfillPlan(
                market_no=market, market_name=market_name, interval=interval,
                job_table=jobs, candle_table=candles,
                target_bars=target,
                start_date=None, end_date=end_date,
                action="planning_at_group_start",
            ))
            queued.append({
                    "market_no":market, "interval":interval, "start_date":None,
                    "end_date":end_date, "job_table":jobs, "candle_table":candles,
                    "instrument_table":instruments,
                    "target_bars":target,
                    "target_source":"config" if target_bars is None or isinstance(target_bars,dict) else "override",
                    "batch_v2":True,"planned_on":None,"force":not use_resume,"as_of_date":today,
                    **({"aggregate_from_day": True} if derived else {}),
                })
    return plans, queued, latest, reason


@router.post("/backfill/refresh-stale", response_model=StaleBackfillResponse, status_code=status.HTTP_202_ACCEPTED, tags=["K 線自動更新"], summary="檢查並背景更新過期 K 線", description="依每檔股票實際 K 線與已提交進度建立初始化／增量更新；週末與休市日不啟動。可用 markets 指定 0=上市、1=上櫃、4=興櫃、5=上市零股、6=上櫃零股；resume=1 續跑同一範圍的未完成工作，resume=0 則重新排入該範圍工作。")
def refresh_stale_backfill(
    background_tasks: BackgroundTasks,
    response: Response,
    markets: str = Query(default="all", description="all 或以逗號分隔的市場別：0=上市、1=上櫃、4=興櫃、5=上市零股、6=上櫃零股"),
    intervals: str = Query(default="all", description="all 或以逗號分隔，例如 1m,5m,day"),
    target_bars: int | None = Query(default=None, ge=1, le=10_000,description="留空依 .env 各週期目標：一般1200、週560、月100；指定則統一覆寫所選週期"),
    resume: int = Query(default=1, ge=0, le=1, description="是否使用斷點續傳：1=續跑既有未完成工作（預設），0=重新排入本次範圍的工作"),
    capital: CapitalQuoteService = Depends(service),
    _dll_access: None = Depends(capital_dll_access, scope="function"),
) -> StaleBackfillResponse:
    """Refresh a market only when one of its symbols is behind.

    The response is immediate; actual Capital requests run in the background
    and their progress remains visible in the relevant job table.
    """
    requested_markets = parse_refresh_markets(markets)
    requested_intervals = parse_refresh_intervals(intervals)
    use_resume = bool(resume)
    if not capital.quote_ready:
        raise HTTPException(status_code=503, detail="群益主行情尚未就緒，請先完成報價主機連線")
    try:
        plans, queued_work, latest_trading_day, day_reason = _plan_stale_backfill(
            requested_markets, requested_intervals, target_bars, use_resume,
        )
    except (MySQLRepositoryError, RuntimeError) as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    if queued_work:
        background_tasks.add_task(_run_stale_backfill, capital, queued_work, target_bars or 1200)
        mode = "斷點續傳" if use_resume else "重新回補"
        priority_note = "先處理所有市場日 K 的最新與歷史工作，尚待補齊時其他週期等待；之後各週期先補最新缺口，再限量補齊較舊歷史" if "day" in requested_intervals else "先補最新缺口，再限量補齊較舊歷史"
        message = f"已排入族群（{mode}；{day_reason}）；各族群開工時才批次檢查逐檔進度及已完成交易日，工作數尚未統計；{priority_note}"
    else:
        response.status_code = status.HTTP_200_OK
        message = f"所有已有工作紀錄的分類都已更新至最近交易日 {latest_trading_day.isoformat()}（{day_reason}），未呼叫群益 DLL"
    return StaleBackfillResponse(
        requested_markets=requested_markets,
        requested_intervals=requested_intervals, target_bars=target_bars, resume=use_resume,
        target_bars_by_interval={i:target_for(i,target_bars) for i in requested_intervals},
        plans=plans, message=message,
    )


@router.get(
    "/backfill/status", response_model=BackfillStatusResponse, tags=["K 線自動更新"],
    summary="查看 K 線回補狀態",
    description="唯讀統計各市場與週期目前最新工作的完成、等待、執行與失敗數量，並回傳每日排程狀態。",
)
def backfill_status(
    request: Request,
    markets: str = Query(default="all", description="all 或以逗號分隔的市場別：0、1、4、5、6"),
    intervals: str = Query(default="all", description="all 或以逗號分隔，例如 1m,day"),
    refresh: bool = False,
) -> BackfillStatusResponse:
    begin = time.monotonic()
    requested_markets = parse_refresh_markets(markets)
    requested_intervals = parse_refresh_intervals(intervals)
    with STATUS_CACHE_INIT_LOCK:
        cache = getattr(request.app.state, "backfill_status_cache", None)
        if cache is None:
            cache = BackfillStatusCache()
            request.app.state.backfill_status_cache = cache
    try:
        value, cache_age, refreshing, database_error = cache.peek(
            (tuple(sorted(requested_markets)), tuple(requested_intervals)),
            lambda: _load_backfill_status(requested_markets, requested_intervals),
            force=refresh,
        )
    except MySQLRepositoryError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    scheduler = getattr(request.app.state, "backfill_scheduler", None)
    if scheduler is None:
        schedule = DailyBackfillScheduleStatus(
            enabled=False, state="unavailable", timezone="Asia/Taipei", local_time="--:--",
            markets=[], intervals=[], target_bars=0, resume=True,
            last_message="每日回補排程服務尚未啟動；請重新啟動 API",
        )
    else:
        schedule = DailyBackfillScheduleStatus(**scheduler.snapshot())
    items, schedule_logs, query_seconds = value if value is not None else ([],[],0.0)
    response = BackfillStatusResponse(
        generated_at=datetime.now(timezone.utc), requested_markets=requested_markets,
        requested_intervals=requested_intervals, items=items, schedule=schedule,
        schedule_logs=schedule_logs, control=backfill_control_status(),
        database_query_seconds=query_seconds, database_cache_age_seconds=cache_age,
        database_cache_hit=value is not None, database_refreshing=refreshing,database_error=database_error,
    )
    import logging
    logging.getLogger(__name__).info("資料狀態查詢：total=%.3fs database=%.3fs cache=%s age=%.3fs",
                                   time.monotonic()-begin, query_seconds, value is not None, cache_age)
    return response


@router.get('/backfill/empty-reviews', tags=['K 線自動更新'], summary='查詢有效空回應觀察與待查證佇列（唯讀）')
def empty_history_reviews(markets: str = 'all', limit: int = Query(default=200, ge=1, le=1000),
                          offset: int = Query(default=0, ge=0), include_resolved: bool = False):
    requested = parse_refresh_markets(markets)
    try:
        result = MySQLCandleRepository().empty_history_report(requested, limit=limit, offset=offset,
                                                             include_resolved=include_resolved)
    except MySQLRepositoryError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return dict(generated_at=datetime.now(timezone.utc),
                note='空回應只代表疑似無資料；不宣稱無交易、不算補齊。此端點不抓取、不登入、不建立資料表。', **result)


@router.get('/backfill/day-integrity', tags=['K 線自動更新'], summary='逐商品、日期列出日 K 驗收缺口（唯讀）')
def day_integrity_status(markets: str = 'all', limit: int = Query(default=200, ge=1, le=1000),
                         offset: int = Query(default=0, ge=0), include_resolved: bool = False):
    requested = parse_refresh_markets(markets)
    try:
        result = MySQLCandleRepository().day_integrity_report(requested, limit=limit, offset=offset,
                                                            include_resolved=include_resolved)
    except MySQLRepositoryError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return dict(generated_at=datetime.now(timezone.utc), requested_markets=requested, limit=limit, offset=offset,
                complete_statuses=['complete', 'complete_available_history'],
                note='完整1200根與已核實的可用歷史不足分開標示；空回應不證明不存在。此端點不啟動抓取。', **result)


def _load_backfill_status(requested_markets, requested_intervals):
    begin = time.monotonic()
    repository = MySQLCandleRepository()
    items: list[BackfillStatusItem] = []
    schedule_logs: list[DailyBackfillRunLog] = []
    try:
        groups = {}
        snapshots = {}
        for market_no in requested_markets:
            _, instruments, candles, jobs = MARKET_BACKFILL_SOURCES[market_no]
            groups.setdefault((instruments,candles,jobs), []).append(market_no)
        for (instruments,candles,jobs), markets_in_group in groups.items():
            snapshots.update(repository.get_backfill_dashboard_status(
                markets_in_group, requested_intervals, candles, jobs, instruments,
            )["items"])
        for market_no in requested_markets:
            market_name, instrument_table, candle_table, job_table = MARKET_BACKFILL_SOURCES[market_no]
            for interval in requested_intervals:
                raw = snapshots[(market_no, interval)]
                active = int(raw["active_instruments"])
                tracked = int(raw["tracked_symbols"])
                items.append(BackfillStatusItem(
                    market_no=market_no,
                    market_name=market_name,
                    interval=interval,
                    target_bars=target_for(interval),
                    instrument_table=instrument_table,
                    candle_table=candle_table,
                    job_table=job_table,
                    storage_ready=bool(raw["storage_ready"]),
                    active_instruments=active,
                    tracked_symbols=tracked,
                    coverage_percent=round((tracked / active * 100) if active else 0, 2),
                    job_counts=raw["job_counts"],
                    average_fetch_seconds=raw.get("average_fetch_seconds"),
                    average_fetch_sample_count=int(raw.get("average_fetch_sample_count",0)),
                    pending_phases=raw.get("pending_phases",{}),
                    checked_through=raw.get("checked_through"),
                    checked_symbols=int(raw.get("checked_symbols",0)),
                    data_missing_symbols=int(raw.get("data_missing_symbols",0)),
                    unfinished_symbols=int(raw.get("unfinished_symbols",0)),
                    data_through=raw["data_through"],
                    oldest_successful_update_at=database_utc(raw["oldest_successful_update_at"]),
                    latest_successful_update_at=database_utc(raw["latest_successful_update_at"]),
                    latest_status_at=database_utc(raw["latest_status_at"]),
                    latest_failure_symbol=raw["latest_failure_symbol"],
                    latest_failure_error=raw["latest_failure_error"],
                    latest_failure_at=database_utc(raw["latest_failure_at"]),
                ))
        schedule_logs = [
            DailyBackfillRunLog(
                **{
                    **row,
                    "started_at": database_utc(row["started_at"]),
                    "updated_at": database_utc(row["updated_at"]),
                    "completed_at": database_utc(row["completed_at"]),
                }
            )
            for row in repository.list_daily_backfill_runs(limit=10)
        ]
    except MySQLRepositoryError:
        raise
    return items, schedule_logs, round(time.monotonic()-begin,3)


@router.post(
    "/backfill/retry-failed", response_model=BackfillRetryResponse,
    status_code=status.HTTP_202_ACCEPTED, tags=["K 線自動更新"],
    summary="重新執行目前失敗的 K 線工作",
    description="只將每檔商品各週期最新且仍為 failed 的工作重新排入；較舊的失敗歷史不會重跑。",
)
def retry_failed_backfill(
    background_tasks: BackgroundTasks,
    response: Response,
    markets: str = Query(default="all", description="all 或以逗號分隔的市場別：0、1、4、5、6"),
    intervals: str = Query(default="all", description="all 或以逗號分隔，例如 1m,day"),
    target_bars: int | None = Query(default=None, ge=1, le=10_000,description="留空依各週期設定的目標根數"),
    capital: CapitalQuoteService = Depends(service),
    _dll_access: None = Depends(capital_dll_access, scope="function"),
) -> BackfillRetryResponse:
    requested_markets = parse_refresh_markets(markets)
    requested_intervals = parse_refresh_intervals(intervals)
    if not capital.quote_ready:
        raise HTTPException(status_code=503, detail="群益主行情尚未就緒，請先完成報價主機連線")
    try:
        with BackfillControl().worker():
            repository = MySQLCandleRepository()
            plans, queued_work = [], []
            try:
                if trading_day_status(date.today())[0]:
                    for market in requested_markets:
                        name,instruments,candles,jobs = MARKET_BACKFILL_SOURCES[market]
                        repository.ensure_incremental_storage(candles,jobs)
                        for interval in requested_intervals:
                            symbols = repository.failed_symbols(market,interval,jobs,instruments)
                            if not symbols:
                                continue
                            end = completed_end(interval)
                            target = target_for(interval,target_bars)
                            group = repository.plan_incremental(market,interval,end,target,candles,jobs,instruments,
                                force=True,only_symbols=symbols)
                            if group["queued_jobs"]:
                                plans.append(BackfillRetryPlan(market_no=market,market_name=name,interval=interval,
                                    start_date=group["start_date"],end_date=end,job_table=jobs,candle_table=candles,
                                    queued_jobs=group["queued_jobs"]))
                                queued_work.append(dict(market_no=market,interval=interval,start_date=group["start_date"],
                                    end_date=end,job_table=jobs,candle_table=candles,target_bars=target,
                                    target_source="config" if target_bars is None else "override",
                                    only_symbols=symbols,force=True))
            except MySQLRepositoryError as error:
                raise HTTPException(status_code=503, detail=str(error)) from error
    except CapitalProcessLockError as error:
        raise HTTPException(status_code=409,detail="API 或獨立腳本已有回補執行中；本次不重排失敗工作") from error
    count = sum(plan.queued_jobs for plan in plans)
    if queued_work:
        background_tasks.add_task(_run_stale_backfill,capital,queued_work,target_bars or 1200)
        message = f"已重新排入 {count} 筆目前最新失敗商品，依實際資料日期接續"
    else:
        response.status_code = 200
        message = "指定範圍無待重試工作，或今日為休市日"
    return BackfillRetryResponse(requested_markets=requested_markets,requested_intervals=requested_intervals,
        queued_jobs=count,plans=plans,message=message)


@router.post("/backfill/daily/run", status_code=status.HTTP_202_ACCEPTED,
             response_class=UTF8JSONResponse,
             tags=["K 線自動更新"], summary="立即執行一輪每日更新",
             description="安全保存目前區間、將其他待執行工作移出佇列，再啟動每日更新。")
def run_daily_backfill_now(background_tasks: BackgroundTasks, request: Request) -> dict[str, Any]:
    control = BackfillControl()
    scheduler = getattr(request.app.state, "backfill_scheduler", None)
    config = scheduler.config if scheduler is not None else DailyBackfillConfig.from_env()
    intervals = config.configured_intervals
    settings = dict(markets=list(config.markets),intervals=list(intervals),target_bars=config.target_bars,
        target_bars_by_interval=config.target_bars_by_interval,
        refresh_bars_by_interval=config.refresh_bars_by_interval,
        aggregate_week_month_from_day=config.aggregate_week_month_from_day,timezone=config.timezone)
    try:
        saved, created = control.request_manual_daily(uuid.uuid4().hex, settings)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    task = saved["manual_daily_run"]
    if created:
        background_tasks.add_task(_resume_manual_daily_takeover, service(request))
    effective = task.get("settings") or settings
    return {"message":"已請求安全保存目前區間；停止後移出舊佇列並啟動每日更新" if created
                      else "每日切換或更新已在進行；不重複建立工作",
            "request_id":task["id"],"created":created,"replaces_request_id":task.get("replaces_request_id"),
            "state":task["state"],"markets":effective["markets"],
            "intervals":effective["intervals"],"refresh_bars_by_interval":effective["refresh_bars_by_interval"],
            "aggregate_week_month_from_day":effective["aggregate_week_month_from_day"]}


def _resume_manual_daily_takeover(capital):
    """Continue a persisted stop/park/start transition, holding both worker locks."""
    control = BackfillControl()
    if not BACKFILL_WORKER_LOCK.acquire(blocking=False):
        return
    lease = None
    try:
        try:
            lease = control.acquire_worker()
        except CapitalProcessLockError:
            return
        saved = control.snapshot()
        task = saved.get("manual_daily_run") or {}
        token, phase = task.get("id"), task.get("phase")
        if not token or phase not in {"waiting_stop", "parking", "ready"}:
            return
        if (phase == "waiting_stop" and (task.get("old_save_failed") or
                (task.get("original_state") in {"running", "planning", "preparing"}
                 and saved.get("pause_kind") == "database"))):
            control.update_manual_daily(token,{"waiting_stop"},phase="failed",state="error",
                message="原工作在保存目前區間時發生資料庫錯誤；未移出舊佇列，請先處理錯誤並續跑")
            return
        if not capital.quote_ready:
            control.update_manual_daily(token,{phase},state="waiting_connection",message="已停止舊工作；等待使用者登入群益")
            return
        memory = current_process_memory()
        memory_state = memory_status(memory, MEMORY_WARNING_MB, MEMORY_STOP_BACKFILL_MB)
        if memory_state == "backfill_stopped" and saved.get("pause_kind") != "memory":
            mark_memory_pause(control)
            control.update(state="paused",pause_kind="memory",pause_reason="每日切換等待記憶體恢復")
        if memory_state in {"backfill_stopped", "unavailable"} or not memory_resume_ready(control,memory=memory):
            control.update_manual_daily(token,{phase},state="waiting_memory",message="已停止舊工作；等待記憶體恢復門檻")
            return
        if phase in {"waiting_stop", "parking"}:
            if not control.update_manual_daily(token,{"waiting_stop", "parking"},phase="parking",
                                               state="parking",message="正在保存並移出舊的待執行工作"):
                return
            from app.services.manual_daily_queue import archive_and_supersede
            count = archive_and_supersede(MySQLCandleRepository(), saved.get("plans") or [], token,
                                          saved.get("pending_schedule"))
            saved = control.finish_manual_daily_parking(token,count)
            task = saved.get("manual_daily_run") or {}
            if task.get("phase") != "ready":
                return
        else:
            task = control.snapshot().get("manual_daily_run") or {}
            if task.get("phase") != "ready":
                return
        options = task["settings"]
        config = replace(DailyBackfillConfig.from_env(),markets=tuple(options["markets"]),
            intervals=tuple(options["intervals"]),target_bars=int(options["target_bars"]),
            target_bars_by_interval=options["target_bars_by_interval"],
            refresh_bars_by_interval=options["refresh_bars_by_interval"],
            aggregate_week_month_from_day=bool(options["aggregate_week_month_from_day"]),resume=True)
        result = _run_scheduled_backfill_owned(capital,config,manual=True,
                                               force_completed_refresh=True,skip_day_integrity=True)
        _record_manual_daily_result(control,*result)
    except MySQLRepositoryError as error:
        state = control.snapshot().get("manual_daily_run") or {}
        if state.get("id"):
            control.update_manual_daily(state["id"],{"waiting_stop","parking","ready"},
                                        state="waiting_database",message=f"等待資料庫：{error}")
        logging.getLogger(__name__).exception("手動每日切換等待資料庫")
    except Exception as error:
        logging.getLogger(__name__).exception("手動每日切換失敗；原資料保留")
        state = control.snapshot().get("manual_daily_run") or {}
        if state.get("id"):
            control.update_manual_daily(state["id"],{"waiting_stop","parking","ready","running"},
                                        phase="failed",state="error",message=str(error))
    finally:
        if lease is not None:
            lease.release()
        BACKFILL_WORKER_LOCK.release()


def _record_manual_daily_result(control, state, message):
    previous = control.snapshot().get("manual_daily_run") or {}
    if previous.get("phase"):
        values = dict(state=state,message=message)
        if state in {"completed", "waiting_review", "limited", "error"}:
            values["phase"] = "failed" if state == "error" else "done"
        control.update_manual_daily(previous.get("id"),{"ready","running"},**values)
    else:
        control.update(manual_daily_run={**previous, "state":state, "message":message,
                                         "updated_at":datetime.now(timezone.utc).isoformat()})


def _backfill_result_state(result):
    if result.get("paused"):
        return "paused"
    if result.get("deferred") or result.get("audit_pending_symbols"):
        return "waiting_backfill"
    if result.get("failed"):
        return "error"
    if result.get("pending_review"):
        return "waiting_review"
    return "completed"


def run_scheduled_backfill(
    capital: CapitalQuoteService, config: DailyBackfillConfig, *, manual: bool = False,
    force_completed_refresh: bool = False, skip_day_integrity: bool = False,
    fresh_daily: bool = False, scheduled_due: bool = False,
) -> tuple[str, str]:
    """Daily scheduler callback; it never attempts a hidden Capital login."""
    if not manual and not force_completed_refresh:
        control = BackfillControl()
        settings = dict(markets=list(config.markets), intervals=list(config.intervals),
                        target_bars=config.target_bars, target_bars_by_interval=config.target_bars_by_interval,
                        refresh_bars_by_interval=config.refresh_bars_by_interval,
                        aggregate_week_month_from_day=config.aggregate_week_month_from_day,
                        as_of_date=(config.as_of_date or date.today()).isoformat())
        saved, created = control.request_scheduled_priority(settings)
        priority = saved.get("scheduled_daily_priority") or {}
        if created or priority.get("phase") in {"waiting_stop", "ready"}:
            _resume_scheduled_daily_priority(capital)
            return "queued", "每日更新已排入；目前抓取將在區間保存後安全暫停"
    if not BACKFILL_WORKER_LOCK.acquire(blocking=False):
        return "waiting_worker", "已有回補執行或準備中；保留當日排程，稍後重試"
    try:
        with BackfillControl().worker():
            if (BackfillControl().snapshot().get("manual_daily_run") or {}).get("phase") in {"waiting_stop", "parking", "ready"}:
                return "paused", "手動每日更新正在安全切換；排程不覆寫其工作"
            result = _run_scheduled_backfill_owned(capital,config,manual=manual,
                **({"force_completed_refresh":True} if force_completed_refresh or scheduled_due else {}),
                **({"fresh_daily":True} if fresh_daily or scheduled_due else {}),
                **({"skip_day_integrity":True} if skip_day_integrity else {}))
            priority = BackfillControl().snapshot().get("scheduled_daily_priority") or {}
            if force_completed_refresh and priority.get("phase") != "running":
                _record_manual_daily_result(BackfillControl(), *result)
            if priority.get("phase") == "running":
                BackfillControl().finish_scheduled_priority(priority["id"])
            return result
    except CapitalProcessLockError:
        return "waiting_worker", "API 或獨立腳本已有回補執行中；保留當日排程，稍後重試"
    finally:
        BACKFILL_WORKER_LOCK.release()


def _resume_scheduled_daily_priority(capital):
    """Switch only after the prior worker relinquishes both exclusive locks."""
    control = BackfillControl()
    if not BACKFILL_WORKER_LOCK.acquire(blocking=False):
        return
    try:
        with control.worker():
            state = control.snapshot()
            task = state.get("scheduled_daily_priority") or {}
            if task.get("phase") not in {"waiting_stop", "ready"} or (task.get("phase") == "ready" and state.get("pause_requested")):
                return
            if task.get("phase") == "waiting_stop" and state.get("pause_kind") == "database":
                control.start_scheduled_priority(task["id"])
                return
            if not capital.quote_ready or not memory_resume_ready(control):
                return
            if task.get("phase") == "waiting_stop" and not control.start_scheduled_priority(task["id"]):
                return
            options = task["settings"]
            config = replace(DailyBackfillConfig.from_env(), markets=tuple(options["markets"]),
                intervals=tuple(options["intervals"]), target_bars=int(options["target_bars"]),
                as_of_date=date.fromisoformat(options["as_of_date"]) if options.get("as_of_date") else None,
                target_bars_by_interval=options["target_bars_by_interval"],
                refresh_bars_by_interval=options["refresh_bars_by_interval"],
                aggregate_week_month_from_day=bool(options["aggregate_week_month_from_day"]), resume=True)
            _run_scheduled_backfill_owned(capital, config, skip_day_integrity=True,
                                         force_completed_refresh=True, fresh_daily=True)
            control.finish_scheduled_priority(task["id"])
    except CapitalProcessLockError:
        return
    except Exception as error:
        logging.getLogger(__name__).exception("排程每日更新切換失敗；原慢速補齊斷點保留")
        control.update(state="paused", pause_kind="database" if isinstance(error, MySQLRepositoryError) else "error",
                       pause_reason=f"每日優先切換失敗；原慢速補齊斷點保留：{error}")
    finally:
        BACKFILL_WORKER_LOCK.release()


def run_scheduled_day_integrity(capital, config):
    """Separate holiday maintenance; no master sync or normal-job replanning."""
    if not BACKFILL_WORKER_LOCK.acquire(blocking=False):
        return 'waiting_backfill', '已有抓取執行中；休市日日 K 驗收等待，不搶占工作'
    try:
        control = BackfillControl()
        with control.worker():
            from app.services.day_candle_integrity_maintenance import run_day_maintenance
            return run_day_maintenance(capital, config, control, MySQLCandleRepository, MARKET_BACKFILL_SOURCES)
    except CapitalProcessLockError:
        return 'waiting_backfill', 'API 或 CLI 工作仍在執行；休市日日 K 驗收稍後再試'
    finally:
        BACKFILL_WORKER_LOCK.release()


def _run_scheduled_backfill_owned(capital, config, *, manual=False,
                                  force_completed_refresh=False, skip_day_integrity=False, fresh_daily=False):
    interval_text = ",".join(config.intervals)
    if not capital.quote_ready:
        return (
            "waiting_connection",
            f"本次週期 {interval_text}；正在等待使用者完成群益登入與報價主機連線",
        )
    control = BackfillControl()
    saved = control.snapshot()
    if saved.get("preparation_pending") or saved.get("preparation_complete") is False:
        return "paused", "完整重抓尚未準備完成；排程不覆寫其工作"
    if saved.get("pause_requested"):
        return "paused", "使用者已暫停回補；保留既有工作與斷點"
    if saved.get("state") not in {"completed","error","limited"} and any(p.get("fixed_range") for p in saved.get("plans",[])):
        return "paused", "先完成已準備的完整重抓範圍；每日排程不覆蓋其斷點"
    if saved.get("pause_kind") == "memory":
        if not memory_resume_ready(control):
            return "paused", "記憶體保護等待恢復門檻與穩定時間；不重新同步或重排工作"
        if saved.get("plans") and not saved.get("pending_schedule"):
            return "paused", "先由恢復檢查接續原工作；本次不建立重複排程"
    if (saved.get('day_first') and saved.get('plans') and not saved.get('normal_round_complete')
            and saved.get('state') not in {'completed','error','limited'}):
        # Upgrade a previously day-blocked round by running its ORIGINAL
        # saved scope through the new latest-first pipeline. Do not replace
        # dates, checkpoint rows, target overrides or its daily run ID.
        result = _run_stale_backfill_unlocked(capital,control.saved_plans(),int(saved.get('target_bars') or 1200),
                                            owns_worker_lock=True)
        message = result.get('pause_reason') or '已接續原範圍；正常抓取先跑所有週期，待補齊留到最後'
        if result.get('pending_review'):
            message += '；'+pending_review_text(result)
        return ('paused' if result.get('paused') else 'limited' if result.get('limited') else
                'waiting_backfill' if result.get('deferred') or result.get('audit_pending_symbols') else
                'waiting_review' if result.get('pending_review') else 'error' if result.get('failed') else 'completed'),message
    repository = MySQLCandleRepository()
    pending = saved.get("pending_schedule") or {}
    run_id = pending.get("run_id") or repository.start_daily_backfill_run({
        "markets": list(config.markets),
        "intervals": list(config.intervals),
        "target_bars_by_interval": config.target_bars_by_interval,
        "refresh_bars_by_interval": config.refresh_bars_by_interval,
        "aggregate_week_month_from_day": config.aggregate_week_month_from_day,
        "resume": config.resume,
        "timezone": config.timezone,
        "local_time": config.local_time.strftime("%H:%M"),
        "as_of_date": config.as_of_date.isoformat() if config.as_of_date else None,
    })
    pending_state = dict(run_id=run_id,markets=list(config.markets),intervals=list(config.intervals),
                   target_bars_by_interval=config.target_bars_by_interval,
                   refresh_bars_by_interval=config.refresh_bars_by_interval,
                   aggregate_week_month_from_day=config.aggregate_week_month_from_day,
                   **({"as_of_date":config.as_of_date.isoformat()} if config.as_of_date else {}),
                   **({"force_completed_refresh":True} if force_completed_refresh else {}),
                   **({"fresh_daily":True} if fresh_daily else {}),
                   **({"skip_day_integrity":True} if skip_day_integrity else {}))
    takeover = control.snapshot().get("manual_daily_run") or {}
    priority = control.snapshot().get("scheduled_daily_priority") or {}
    if priority.get("phase") == "ready":
        if not control.start_scheduled_daily(priority["id"], pending_state):
            repository.update_daily_backfill_run(run_id,status="paused",stage="scheduled_switch",
                message="使用者在新每日工作啟動前要求暫停",finished=True)
            return "paused", "使用者要求暫停；未開始每日工作"
    elif force_completed_refresh and takeover.get("phase") == "ready":
        if not control.start_manual_daily(takeover["id"],pending_state):
            repository.update_daily_backfill_run(run_id,status="paused",stage="manual",
                message="使用者在新每日工作啟動前要求暫停",finished=True)
            return "paused", "使用者要求暫停；未開始每日工作"
    else:
        control.update(source="api",manual_run=manual,pending_schedule=pending_state,
                       **({"state":"running", "stage":"syncing_board_instruments", "pause_reason":None}
                          if force_completed_refresh else {}))
    guard = BackfillMemoryGuard(control,getattr(capital,"cache",None))
    stage = "syncing_board_instruments"
    board_count = odd_lot_count = 0
    try:
        guard.require("before_board_instruments")
        board_results = _sync_instrument_markets_unlocked(
            BOARD_LOT_MARKETS, BOARD_LOT_INSTRUMENT_TABLE, 500, capital,memory_guard=guard,
        )
        guard.require("after_board_instruments")
        board_count = sum(item.fetched_count for item in board_results)
        stage = "syncing_odd_lot_instruments"
        repository.update_daily_backfill_run(
            run_id, status="running", stage=stage,
            board_instrument_count=board_count,
            message=f"整張商品主檔同步完成，共 {board_count} 檔；正在同步零股商品主檔",
        )
        odd_lot_results = _sync_instrument_markets_unlocked(
            ODD_LOT_MARKETS, ODD_LOT_INSTRUMENT_TABLE, 500, capital,memory_guard=guard,
        )
        guard.require("after_odd_lot_instruments")
        odd_lot_count = sum(item.fetched_count for item in odd_lot_results)
        sync_message = (
            f"商品主檔同步完成（整張 {board_count} 檔、零股 {odd_lot_count} 檔）；"
            f"本次 K 線週期 {interval_text}；日常回抓目標 "
            + ",".join(f"{i}:{config.refresh_bars_by_interval[i]}" for i in config.intervals)
        )
        stage = "planning_kline"
        repository.update_daily_backfill_run(
            run_id, status="running", stage=stage,
            board_instrument_count=board_count,
            odd_lot_instrument_count=odd_lot_count,
            message=f"{sync_message}；正在建立 K 線更新工作",
        )
        plans, queued_work, latest_trading_day, day_reason = _plan_stale_backfill(
            list(config.markets), list(config.intervals), config.target_bars_by_interval,
            config.resume, owns_worker_lock=True,allow_non_trading_day=manual,
            aggregate_from_day=config.aggregate_week_month_from_day,
            **({"today":config.as_of_date} if config.as_of_date else {}),
        )
        guard.require("after_schedule_planning")
        queued_jobs = sum(plan.queued_jobs for plan in plans)
        for item in queued_work:
            item["run_id"] = run_id
            item["refresh_bars"] = config.refresh_bars_by_interval[item["interval"]]
            if force_completed_refresh:
                item["force_completed_refresh"] = True
            if fresh_daily:
                item["fresh_daily"] = True
            if skip_day_integrity:
                item["skip_day_integrity"] = True
        control.update(pending_schedule=None,plans=queued_work,pause_kind=None)
        if not queued_work:
            control.update(state="completed",pause_kind=None,pause_reason=None,resume_pending=False)
            no_history = sum(plan.action == "no_job_history" for plan in plans)
            if no_history:
                message = (
                    f"{sync_message}；有 {no_history} 個市場／週期尚無首次回補紀錄，"
                    "自動更新不會替代首次完整回補"
                )
                repository.update_daily_backfill_run(
                    run_id, status="skipped", stage="no_job_history",
                    board_instrument_count=board_count,
                    odd_lot_instrument_count=odd_lot_count,
                    queued_jobs=queued_jobs, message=message, finished=True,
                )
                return "skipped", message
            message = (
                f"{sync_message}；K 線已更新至最近交易日 "
                f"{latest_trading_day.isoformat()}（{day_reason}）"
            )
            repository.update_daily_backfill_run(
                run_id, status="completed", stage="completed",
                board_instrument_count=board_count,
                odd_lot_instrument_count=odd_lot_count,
                queued_jobs=queued_jobs, message=message, finished=True,
            )
            return "completed", message

        stage = "backfilling_kline"
        repository.update_daily_backfill_run(
            run_id, status="running", stage=stage,
            board_instrument_count=board_count,
            odd_lot_instrument_count=odd_lot_count,
            queued_jobs=queued_jobs,
            message=f"{sync_message}；K 線背景工作執行中",
        )

        def persist_progress(progress: dict[str, Any]) -> None:
            nonlocal queued_jobs
            queued_jobs = int(progress.get("queued_jobs",queued_jobs))
            repository.update_daily_backfill_run(
                run_id, status="running", stage="backfilling_kline",
                board_instrument_count=board_count,
                odd_lot_instrument_count=odd_lot_count,
                queued_jobs=queued_jobs,
                processed_jobs=progress["processed"],
                completed_jobs=progress["completed"],
                insufficient_jobs=progress["insufficient_history"],
                failed_jobs=progress["failed"],
                message=(
                    f"{sync_message}；K 線更新中：已處理 {progress['processed']}／{queued_jobs}，"
                    f"成功 {progress['completed']}、不足 {progress['insufficient_history']}、"
                    f"失敗 {progress['failed']}"
                ),
            )

        result = _run_stale_backfill_unlocked(
            capital, queued_work, config.target_bars,
            progress_callback=persist_progress, owns_worker_lock=True,
        )
        paused = bool(result.get("paused"))
        deferred = int(result.get("deferred",0))
        waiting = bool(deferred or result.get('audit_pending_symbols'))
        reviews = int(result.get('pending_review', 0))
        state = "paused" if paused else ("error" if result["failed"] else "completed")
        if waiting and not paused: state = "paused"
        elif reviews and not paused: state = 'waiting_review'
        message = (
            f"{sync_message}；排程 K 線更新完成：最近交易日 {latest_trading_day.isoformat()}；"
            f"處理 {result['processed']}、成功 {result['completed']}、"
            f"不足 {result['insufficient_history']}、失敗 {result['failed']}"
        )
        if paused:
            pause_label = {"manual":"使用者要求安全暫停", "memory":"觸發記憶體保護", "database":"資料庫暫時無法使用", "data":"來源資料異常，等待同區間重試"}.get(result.get("pause_kind"), "群益連線恢復失敗")
            message = (
                f"{sync_message}；{pause_label}，已暫停 K 線更新；"
                f"成功 {result['completed']}、不足 {result['insufficient_history']}、"
                f"失敗 {result['failed']}；目前及剩餘工作保留等待，可在解除暫停或恢復條件後續跑；"
                f"原因：{result['pause_reason']}"
            )
        elif waiting:
            message = (f"{sync_message}；{result['pause_reason']}" if result.get("pause_reason") else
                       f"{sync_message}；最新資料優先輪完成；{deferred} 筆慢速補齊工作保留等待，稍後依區間斷點續跑（不是失敗）")
        elif reviews:
            message = f'{sync_message}；本輪正常抓取已結束；{pending_review_text(result)}'
        if reviews and (paused or waiting):
            message += '；'+pending_review_text(result)
        repository.update_daily_backfill_run(
            run_id, status=state,
            stage=(result.get("pause_kind") or "connection_paused") if paused else "waiting_backfill" if waiting else 'waiting_review' if reviews else ("completed" if state == "completed" else "kline_failed"),
            board_instrument_count=board_count,
            odd_lot_instrument_count=odd_lot_count,
            queued_jobs=queued_jobs,
            processed_jobs=result["processed"], completed_jobs=result["completed"],
            insufficient_jobs=result["insufficient_history"], failed_jobs=result["failed"],
            message=message,
            error_message=(
                result["pause_reason"] if paused else
                (f"{result['failed']} 筆 K 線工作失敗；請查看對應工作表" if result["failed"] else None)
            ),
            finished=True,
        )
        return state, message
    except DailyTakeoverPause as error:
        message = f"{error}；原工作保留至移出佇列完成"
        control.update(state="paused",pause_kind="manual",pause_reason=message,source="api")
        repository.update_daily_backfill_run(run_id,status="paused",stage="manual_switch",
            board_instrument_count=board_count,odd_lot_instrument_count=odd_lot_count,
            message=message,finished=True)
        return "paused",message
    except BackfillMemoryPause as error:
        mark_memory_pause(control)
        message = f"{error}；商品同步／規劃尚未完成，不開始 K 線更新；恢復後接續同一排程"
        control.update(state="paused",pause_kind="memory",pause_reason=message,source="api")
        repository.update_daily_backfill_run(run_id,status="paused",stage="memory",
            board_instrument_count=board_count,odd_lot_instrument_count=odd_lot_count,
            message=message,error_message=str(error),finished=True)
        return "paused",message
    except Exception as error:
        takeover = control.snapshot().get("manual_daily_run") or {}
        if takeover.get("phase") == "waiting_stop":
            message = f"原工作保存失敗，未移出舊佇列：{error}"
            control.update_manual_daily(takeover["id"],{"waiting_stop"},old_save_failed=True,message=message)
            control.update(state="paused",pause_kind="database" if isinstance(error,MySQLRepositoryError) else "connection",
                           pause_reason=message)
            try:
                repository.update_daily_backfill_run(run_id,status="paused",stage="manual_switch_error",
                    message=message,error_message=str(error),finished=True)
            except MySQLRepositoryError:
                logging.getLogger(__name__).exception("無法保存原每日工作錯誤紀錄")
            return "paused", message
        if (control.snapshot().get("scheduled_daily_priority") or {}).get("phase") != "running":
            control.update(pending_schedule=None)
        if stage in {"syncing_board_instruments", "syncing_odd_lot_instruments"}:
            message = f"商品主檔同步失敗，未執行 K 線更新：{error}"
        else:
            message = f"每日 K 線更新在 {stage} 階段失敗：{error}"
        try:
            repository.update_daily_backfill_run(
                run_id, status="error", stage=stage,
                board_instrument_count=board_count,
                odd_lot_instrument_count=odd_lot_count,
                message=message, error_message=str(error), finished=True,
            )
        except MySQLRepositoryError as log_error:
            raise RuntimeError(f"{message}；Log 寫入也失敗：{log_error}") from error
        raise RuntimeError(message) from error


@router.get("/analysis/{symbol}", response_model=AnalysisResponse, tags=["技術分析"], summary="執行技術指標與股票評價", description="固定讀取 MySQL K 線，預設日 K。以 history_interval 選擇週期、candle_table 選擇資料表、output_keys 選擇指標。回傳 K 線起訖時間、指標對齊時間與最近寫入時間；不會呼叫群益 DLL。")
def analysis(
    symbol: str,
    history_interval: Annotated[str, Query(pattern=r"^(1m|5m|15m|60m|day|week|month)$", description="MySQL 已儲存的 K 線週期，預設日 K")] = "day",
    candle_table: Annotated[str, Query(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$", description="整張為 candles，零股為 odd_lot_candle")] = BOARD_LOT_CANDLE_TABLE,
    output_keys: Annotated[str | None, Query(description="以逗號指定輸出項目；例如 ma,macd,rsi,stock_evaluation。未指定時輸出全部")] = None,
) -> AnalysisResponse:
    """Run the V2.1–V2.3 pipeline on the selected K-line storage.

    Indicators requiring more history return ``null`` until enough K-lines
    are available.
    """
    selected_output_keys = _parse_analysis_output_keys(output_keys)
    normalized = symbol.upper()
    try:
        repository = MySQLCandleRepository()
        candle_data, latest_written_at = repository.get_candles_with_metadata(
            normalized, history_interval, candle_table,
        )
        instrument_table = ODD_LOT_INSTRUMENT_TABLE if candle_table == ODD_LOT_CANDLE_TABLE else BOARD_LOT_INSTRUMENT_TABLE
        instrument_name = repository.get_instrument_name(normalized, instrument_table)
    except MySQLRepositoryError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    if not candle_data:
        detail = f"MySQL 資料表 {candle_table} 尚無 {normalized} 的 {history_interval} K 線"
        raise HTTPException(status_code=404, detail=detail)
    if len(candle_data) < 2:
        raise HTTPException(status_code=422, detail="至少需要兩根 K 線才能進行技術分析")
    result = analyze_technical(candle_data)
    indicator_data = {
        key: value for key, value in result["indicators"].items()
        if key != "candles" and key in selected_output_keys
    }
    evaluation_available = len(candle_data) >= MINIMUM_EVALUATION_CANDLES
    if evaluation_available:
        evaluation_scores = result["scores"]
        evaluation_summary = result["summary"]
        stock_evaluation = _build_stock_evaluation(evaluation_summary, evaluation_scores)
        action_evaluation = result["action_evaluation"]
        warnings = result["warnings"]
    else:
        insufficient_message = _insufficient_evaluation_message(history_interval)
        evaluation_scores = {}
        evaluation_summary = {
            "status": "insufficient_data",
            "message": insufficient_message,
            "candle_count": len(candle_data),
            "minimum_candle_count": MINIMUM_EVALUATION_CANDLES,
        }
        stock_evaluation = {
            "name": "股票評價",
            "status": "insufficient_data",
            "available": False,
            "message": insufficient_message,
            "candle_count": len(candle_data),
            "minimum_candle_count": MINIMUM_EVALUATION_CANDLES,
        }
        action_evaluation = {
            "available": False,
            "scoring_version": "v1",
            "message": insufficient_message,
            "disclaimer": "此評分僅依歷史技術指標計算，不代表未來績效，也不是買賣建議。",
        }
        warnings = list(dict.fromkeys([*result["warnings"], insufficient_message]))
    return AnalysisResponse(
        symbol=normalized,
        name=instrument_name,
        price=candle_data[-1].close,
        previous_close=candle_data[-2].close,
        price_change=candle_data[-1].close - candle_data[-2].close,
        price_change_percent=(candle_data[-1].close - candle_data[-2].close) / candle_data[-2].close * 100 if candle_data[-2].close else 0.0,
        source="mysql",
        interval=history_interval,
        candle_table=candle_table,
        first_candle_at=candle_data[0].timestamp,
        latest_candle_at=candle_data[-1].timestamp,
        latest_written_at=latest_written_at,
        timestamps=[c.timestamp for c in candle_data],
        candles=[
            Candle(timestamp=c.timestamp, open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume)
            for c in candle_data
        ],
        candle_count=len(candle_data),
        output_indicator_names={key: _ANALYSIS_OUTPUT_INDICATOR_NAMES[key] for key in selected_output_keys},
        indicators=indicator_data,
        signals=result["signals"] if "signals" in selected_output_keys else {},
        divergences=result["divergences"] if "divergences" in selected_output_keys else {},
        scores=evaluation_scores if "scores" in selected_output_keys else {},
        summary=evaluation_summary if "stock_evaluation" in selected_output_keys else {},
        stock_evaluation=stock_evaluation if "stock_evaluation" in selected_output_keys else {},
        action_evaluation=action_evaluation if "stock_evaluation" in selected_output_keys else {},
        reasons=result["reasons"],
        warnings=warnings,
    )


_ANALYSIS_OUTPUT_INDICATOR_NAMES = {
    "ma": "移動平均線（MA）",
    "ema": "指數移動平均線（EMA）",
    "macd": "指數平滑異同移動平均線（MACD）",
    "dmi_adx": "動向指標／平均趨向指標（DMI／ADX）",
    "kd": "隨機指標（KD）",
    "rsi": "相對強弱指標（RSI）",
    "roc": "變動率指標（ROC）",
    "bollinger": "布林通道（Bollinger Bands）",
    "atr": "平均真實波幅（ATR）",
    "volume": "成交量指標",
    "signals": "技術訊號",
    "divergences": "RSI／MACD 背離",
    "scores": "分類評分",
    "stock_evaluation": "股票評價",
}


def _parse_analysis_output_keys(raw_output_keys: str | None) -> list[str]:
    """Return selected API output keys while preserving the documented order."""
    if raw_output_keys is None:
        return list(_ANALYSIS_OUTPUT_INDICATOR_NAMES)
    requested = {key.strip() for key in raw_output_keys.split(",") if key.strip()}
    if not requested:
        raise HTTPException(status_code=422, detail="output_keys 至少要指定一個輸出項目")
    unsupported = requested - _ANALYSIS_OUTPUT_INDICATOR_NAMES.keys()
    if unsupported:
        choices = ", ".join(_ANALYSIS_OUTPUT_INDICATOR_NAMES)
        raise HTTPException(
            status_code=422,
            detail=f"不支援的 output_keys：{', '.join(sorted(unsupported))}；可使用：{choices}",
        )
    return [key for key in _ANALYSIS_OUTPUT_INDICATOR_NAMES if key in requested]


def _build_stock_evaluation(summary: dict[str, object], scores: dict[str, int]) -> dict[str, object]:
    """Convert machine-readable V2 scores into labels convenient for an API UI."""
    direction = str(summary["direction"])
    risk = str(summary["risk"])
    agreement = str(summary["agreement"])
    direction_labels = {
        "strong_bullish": "強勢偏多",
        "bullish": "偏多",
        "neutral": "中性／觀望",
        "bearish": "偏空",
        "strong_bearish": "強勢偏空",
    }
    risk_labels = {"high": "高", "elevated": "偏高", "normal": "一般", "low": "低"}
    agreement_labels = {"high": "高", "medium": "中", "low": "低"}
    return {
        "name": "股票評價",
        "rating": direction_labels.get(direction, direction),
        "risk": risk_labels.get(risk, risk),
        "signal_agreement": agreement_labels.get(agreement, agreement),
        "agreement_count": summary["agreement_count"],
        "direction_score": scores["direction_score"],
        "risk_score": scores["risk_score"],
        "disclaimer": "此評價僅依歷史技術指標計算，不構成投資建議。",
    }


def _insufficient_evaluation_message(interval: str) -> str:
    """Return a user-facing explanation for the 20-K-line evaluation rule."""
    if interval == "day":
        return "未滿20個交易日，無法提供評價"
    interval_labels = {
        "1m": "1 分 K", "5m": "5 分 K", "15m": "15 分 K",
        "60m": "60 分 K", "week": "週K", "month": "月K",
    }
    return f"未滿20根{interval_labels[interval]}，無法提供評價"
