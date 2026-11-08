from contextlib import asynccontextmanager
import asyncio
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from app.api.routes import router, run_scheduled_backfill, run_scheduled_day_integrity
from app.api.rules import router as rules_router
from app.api.alerts import router as alerts_router
from app.api.notifications import router as notifications_router
from app.api.screener import router as screener_router
from app.api.saved_screeners import router as saved_screeners_router
from app.api.ai_rules import router as ai_rules_router
from app.api.stock_profiles import router as stock_profiles_router
from app.api.external_market_quotes import router as external_market_quotes_router
from app.services.external_market_quote_scheduler import ExternalMarketQuoteScheduler
from app.services.us_stock_daily_scheduler import USStockDailyScheduler
from app.services.stock_master_scheduler import WeeklyStockMasterScheduler
from app.services.saved_screener_store import SavedScreenerStore
from app.services.alert_service import AlertService
from app.services.alert_observation import observe_alert
from app.services.capital_worker_service import CapitalWorkerService as CapitalQuoteService
from app.services.quote_cache import QuoteCache
from app.services.backfill_scheduler import DailyBackfillConfig, DailyBackfillScheduler
from app.services.backfill_control import BackfillControl, BackfillRecovery
from app.services.capital_history_service import configured_history_limits
from app.api.routes import _resume_saved_backfill

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configured_history_limits()
    app.state.capital_service = CapitalQuoteService(QuoteCache(), control=BackfillControl())
    # Route-level queue for all operations that can invoke the native DLL.
    # This intentionally belongs to the active event loop, not a module global.
    app.state.capital_dll_async_lock = asyncio.Lock()
    alert_path = os.getenv("CAPITAL_ALERT_DB", str(Path(__file__).resolve().parent.parent / "data" / "alerts.sqlite3"))
    alerts = None
    backfill_scheduler = None
    recovery = None
    stock_master_scheduler = None
    external_market_quote_scheduler = None
    us_stock_daily_scheduler = None
    try:
        backfill_scheduler = DailyBackfillScheduler(
            DailyBackfillConfig.from_env(),
            lambda config: run_scheduled_backfill(app.state.capital_service, config, scheduled_due=True),
            maintenance_callback=lambda config: run_scheduled_day_integrity(app.state.capital_service, config),
        )
        app.state.backfill_scheduler = backfill_scheduler
        alerts = AlertService(alert_path, lambda alert: observe_alert(alert, app.state.capital_service, alerts.clock()))
        app.state.alert_service = alerts
        app.state.saved_screener_store = SavedScreenerStore(alerts)
        alerts.start()
        backfill_scheduler.start()
        stock_master_scheduler = WeeklyStockMasterScheduler()
        app.state.stock_master_scheduler = stock_master_scheduler
        stock_master_scheduler.start()
        external_market_quote_scheduler = ExternalMarketQuoteScheduler()
        app.state.external_market_quote_scheduler = external_market_quote_scheduler
        external_market_quote_scheduler.start()
        us_stock_daily_scheduler = USStockDailyScheduler()
        app.state.us_stock_daily_scheduler = us_stock_daily_scheduler
        us_stock_daily_scheduler.start()
        recovery = BackfillRecovery(app.state.capital_service, _resume_saved_backfill, app.state.capital_service.cache)
        recovery.start()
        yield
    finally:
        if us_stock_daily_scheduler is not None:
            await asyncio.to_thread(us_stock_daily_scheduler.stop)
        if external_market_quote_scheduler is not None:
            await asyncio.to_thread(external_market_quote_scheduler.stop)
        if stock_master_scheduler is not None:
            await asyncio.to_thread(stock_master_scheduler.stop)
        if recovery is not None:
            recovery.stop()
        if backfill_scheduler is not None:
            await asyncio.to_thread(backfill_scheduler.stop)
        if alerts is not None:
            await asyncio.to_thread(alerts.stop)
        app.state.capital_service.stop()


app = FastAPI(
    title="群益 Capital API 股票分析系統",
    version="0.1.0",
    description="唯讀即時行情 API；群益 COM callback 寫入快取，HTTP 端點讀取快取。",
    lifespan=lifespan,
)

origins = [item.strip() for item in os.getenv("CAPITAL_CORS_ORIGINS", "").split(",") if item.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.include_router(router)
app.include_router(rules_router)
app.include_router(alerts_router)
app.include_router(notifications_router)
app.include_router(screener_router)
app.include_router(saved_screeners_router)
app.include_router(ai_rules_router)
app.include_router(external_market_quotes_router)
app.include_router(stock_profiles_router, prefix="/api")
app.include_router(stock_profiles_router, prefix="/api/v1", include_in_schema=False)
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")


@app.get("/dashboard", include_in_schema=False)
def analysis_dashboard() -> FileResponse:
    """Serve the V2.4 same-origin technical-analysis dashboard."""
    return FileResponse(Path(__file__).resolve().parent / "static" / "analysis.html")


@app.get("/notifications", include_in_schema=False)
def notification_center() -> FileResponse:
    return FileResponse(Path(__file__).resolve().parent / "static" / "notifications.html")


@app.get("/v3", include_in_schema=False)
def v3_validation_dashboard() -> FileResponse:
    """Serve the integrated V3.1–V3.6 verification workspace."""
    return FileResponse(Path(__file__).resolve().parent / "static" / "v3.html")


@app.get("/data-status", include_in_schema=False)
def data_status_dashboard() -> FileResponse:
    """Serve the K-line maintenance and daily-schedule dashboard."""
    return FileResponse(Path(__file__).resolve().parent / "static" / "data-status.html")
