"""Same-origin loopback access to dashboard reports and index candles."""
from contextlib import closing
from pathlib import Path
import re
import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from app.api.market_data import Code, Market, market_name
from app.services.market_data_queries import MarketDataQueries
from app.services.taiwan_market_indices import INDEX_PRODUCTS

LOCAL_DB = Path(__file__).resolve().parents[2] / "tests" / "output" / "market_data_p345" / "workflow.sqlite3"
router = APIRouter(prefix="/api/local/dashboard", include_in_schema=False)
Dataset = Literal["institutional_stock", "institutional_market", "margin", "revenue", "income", "balance"]
Period = Annotated[str | None, Query(max_length=10)]
Scope = Literal["stocks", "securities"]


def require_local(request):
    host = request.headers.get("host", "")
    if (
        request.client is None
        or request.client.host not in {"127.0.0.1", "::1"}
        or not re.fullmatch(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]+)?", host, re.IGNORECASE)
        or any(name in request.headers for name in ("forwarded", "x-forwarded-for", "x-forwarded-host"))
        or request.headers.get("sec-fetch-site") not in (None, "same-origin", "none")
        or request.headers.get("origin") not in (None, f"{request.url.scheme}://{host}")
    ):
        raise HTTPException(403, "請從本機的 dashboard 頁面查詢。")


def selected_period(periods, period):
    if period is not None and period not in periods:
        raise HTTPException(422, "這個資料期間不在目前的報表中。")
    return period or (periods[0] if periods else None)


def read_report(dataset, market, period, scope, code=None):
    try:
        with closing(sqlite3.connect(LOCAL_DB.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.execute("BEGIN")
            repo = MarketDataQueries(db)
            if dataset == "institutional_market":
                market = market or (repo.stock_market(code) if code else None)
                segment = scope if market == "TPEx" else "published"
                stock_code = None
            else:
                segment = None
                stock_code = code
            periods = (
                [] if dataset == "institutional_market" and market is None else
                repo.periods(dataset, market, stock_code=stock_code, segment=segment)
            )
            selected = selected_period(periods, period)
            rows = repo.current(dataset, selected, selected, stock_code=stock_code, market=market,
                                segment=segment)["data"] if selected else []
    except sqlite3.Error as exc:
        raise HTTPException(503, "目前無法讀取本機歷史報表，請確認回填資料庫已準備好。") from exc
    except ValueError as exc:
        if str(exc) != "ambiguous_market":
            raise
        raise HTTPException(422, "這個代號有多個市場的資料，請先選擇上市或上櫃。") from exc
    markets = {row["market"] for row in rows}
    resolved = market or (next(iter(markets)) if len(markets) == 1 else None)
    return {"dataset": dataset, "stock_code": code, "market": resolved, "scope": segment,
            "periods": periods, "period": selected, "data": rows}


@router.get("/market-data/{code}")
def market_data_preview(
    request: Request, response: Response, code: Code, dataset: Dataset = "institutional_stock",
    market: Market | None = None, period: Period = None, scope: Scope = "stocks",
):
    require_local(request)
    response.headers["Cache-Control"] = "private, no-store"
    return read_report(dataset, market_name(market), period, scope, code)


@router.get("/market-summary/{market}")
async def market_summary(
    request: Request, response: Response, market: Market,
    dataset: Literal["index", "institutional_market"] = "index", period: Period = None, scope: Scope = "stocks",
):
    require_local(request)
    response.headers["Cache-Control"] = "private, no-store"
    market = market_name(market)
    if dataset == "institutional_market":
        return await run_in_threadpool(read_report, dataset, market, period, scope)
    async with request.app.state.capital_dll_async_lock:
        rows, stale = await run_in_threadpool(request.app.state.index_snapshots.read, market)
    if not rows:
        raise HTTPException(503, "群益指數日 K 暫時無法取得，請確認群益行情已連線後再查詢。")
    periods = sorted(rows, reverse=True)
    selected = selected_period(periods, period)
    name, symbol = INDEX_PRODUCTS[market]
    return {"dataset": dataset, "market": market, "periods": periods, "period": selected,
            "name": name, "symbol": symbol, "source": "群益 API", "stale": stale, "data": [rows[selected]]}
