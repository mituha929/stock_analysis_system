"""Private, opt-in read API for chips, revenue and financial statements."""
from collections import deque
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import os
import secrets
from threading import Lock
from time import monotonic
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.services.market_data_queries import MarketDataQueries
from app.services.mysql_instrument_repository import MySQLSettings

bearer = HTTPBearer(auto_error=False)
TAIPEI = timezone(timedelta(hours=8))
recent_requests = deque()
rate_lock = Lock()


def enabled():
    return os.getenv("CAPITAL_MARKET_DATA_API_ENABLED", "").lower() == "true"


def authorize(response: Response, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    if not enabled():
        raise HTTPException(404, "Market data API disabled")
    token = os.getenv("CAPITAL_MARKET_DATA_API_TOKEN", "")
    if len(token) < 24:
        raise HTTPException(503, "Market data API is not configured")
    if credentials is None or not secrets.compare_digest(token.encode(), credentials.credentials.encode()):
        raise HTTPException(401, "Invalid bearer token", headers={"WWW-Authenticate": "Bearer"})
    with rate_lock:
        now = monotonic()
        while recent_requests and recent_requests[0] <= now - 60:
            recent_requests.popleft()
        if len(recent_requests) >= 60:
            raise HTTPException(429, "Market data query limit reached", headers={"Retry-After": "60"})
        recent_requests.append(now)
    response.headers["Cache-Control"] = "private, no-store"


router = APIRouter(prefix="/api/v1", tags=["籌碼與基本面"], dependencies=[Depends(authorize)])


@contextmanager
def mysql_reader():
    import mysql.connector

    try:
        settings = MySQLSettings.from_env()
        db = mysql.connector.connect(host=settings.host, port=settings.port, user=settings.user, password=settings.password,
                                     database=settings.database, connection_timeout=5, charset="utf8mb4")
        with closing(db):
            db.start_transaction(readonly=True, isolation_level="REPEATABLE READ")
            try:
                yield MarketDataQueries(db, mysql=True)
            finally:
                db.rollback()
    except (mysql.connector.Error, RuntimeError) as exc:
        raise HTTPException(503, "Market data storage unavailable") from exc


@lru_cache(maxsize=64)
def cached_query(method, args, kwargs, time_bucket):
    with mysql_reader() as reader:
        return getattr(reader, method)(*args, **dict(kwargs))


class MySQLMarketDataQueries:
    def current(self, *args, **kwargs):
        return cached_query("current", args, tuple(sorted(kwargs.items())), int(monotonic() // 5))

    def latest_revenue_month(self, stock_code, market):
        return cached_query("latest_revenue_month", (stock_code, market), (), int(monotonic() // 5))

    def status(self, *, page):
        with mysql_reader() as reader:
            return reader.status(page=page)


def repository():
    return MySQLMarketDataQueries()


def pagination(limit: Annotated[int, Query(ge=1, le=100)] = 50,
               offset: Annotated[int, Query(ge=0, le=5000)] = 0):
    return limit, offset


def day_range(start: date, end: date):
    if not 0 <= (end - start).days <= 365:
        raise HTTPException(422, "Date range must be ordered and at most 366 days inclusive")
    return start.isoformat(), end.isoformat()


def market_name(market):
    return "TPEx" if market in ("TPEx", "TPEX") else market


Market = Literal["TWSE", "TPEx", "TPEX"]
Code = Annotated[str, Path(pattern=r"^(?:[0-9]{4,6}|[0-9]{5}[A-Z])$")]
Page = Annotated[tuple[int, int], Depends(pagination)]
Days = Annotated[tuple[str, str], Depends(day_range)]
Repo = Annotated[MarketDataQueries, Depends(repository)]


@router.get("/stocks/{code}/institutional")
def stock_institutional(code: Code, days: Days, page: Page, repo: Repo, market: Market | None = None):
    return repo.current("institutional_stock", *days, stock_code=code, market=market_name(market), page=page)


@router.get("/markets/{market}/institutional")
def market_institutional(market: Market, days: Days, page: Page, repo: Repo,
                         scope: Literal["stocks", "securities", "published"] | None = None):
    market = market_name(market)
    if market == "TWSE" and scope in (None, "published"):
        scope = "published"
    elif market != "TPEx" or scope not in ("stocks", "securities"):
        raise HTTPException(422, "TPEx requires stocks or securities scope; TWSE uses published scope")
    return repo.current("institutional_market", *days, market=market, segment=scope, page=page)


@router.get("/stocks/{code}/margin")
def stock_margin(code: Code, days: Days, page: Page, repo: Repo, market: Market | None = None):
    return repo.current("margin", *days, stock_code=code, market=market_name(market), page=page)


@router.get("/markets/{market}/margin")
def market_margin(market: Market, days: Days, page: Page, repo: Repo):
    return repo.current("margin_market", *days, market=market_name(market), page=page)


@router.get("/stocks/{code}/revenue")
def stock_revenue(code: Code, page: Page, repo: Repo, market: Market | None = None,
                  months: Annotated[int, Query(ge=1, le=60)] = 12, through: str | None = None):
    market = market_name(market)
    end = through or repo.latest_revenue_month(code, market)
    if end is None:
        now = datetime.now(TAIPEI).date()
        end = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    try:
        valid = date.fromisoformat(end + "-01")
        if valid.strftime("%Y-%m") != end:
            raise ValueError
        first = valid.year * 12 + valid.month - months
        if first < 12:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(422, "through must be YYYY-MM") from exc
    start = f"{first // 12:04d}-{first % 12 + 1:02d}"
    return repo.current("revenue", start, end, stock_code=code, market=market, page=page)


@router.get("/stocks/{code}/financials")
def stock_financials(code: Code, page: Page, repo: Repo,
                     year: Annotated[int, Query(ge=1912, le=9999)], quarter: Annotated[int, Query(ge=1, le=4)],
                     statement: Literal["income_statement", "balance_sheet"], market: Market | None = None):
    dataset = "income" if statement == "income_statement" else "balance"
    period = f"{year:04d}-Q{quarter}"
    return repo.current(dataset, period, period, stock_code=code, market=market_name(market), page=page)


@router.get("/data/status")
def data_status(page: Page, repo: Repo):
    return repo.status(page=page)
