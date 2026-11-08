"""Validated, completed Yahoo daily bars for the fixed 15-stock research universe."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytz

from app.services.external_market_sessions import ExternalMarketSessions

SOURCE = "yahoo_finance"
BAR_COUNT = 250
PRECISION = Decimal("0.00000001")
NY = pytz.timezone("America/New_York")


class StockDailyError(ValueError):
    """Safe, non-secret validation reason, suitable for schedule status."""


@dataclass(frozen=True)
class Company:
    symbol: str
    name: str
    exchange: str = "NASDAQ"
    security_type: str = "STOCK"
    underlying: str | None = None
    ratio: int | None = None


COMPANIES = {company.symbol: company for company in (
    Company("TSM", "台積電ADR", "NYSE", "ADR", "2330", 5),
    Company("NVDA", "輝達"), Company("AMD", "超微"), Company("AVGO", "博通"),
    Company("MU", "美光"), Company("AAPL", "蘋果"), Company("QCOM", "高通"),
    Company("MSFT", "微軟"), Company("AMZN", "亞馬遜"), Company("GOOGL", "Alphabet A類股"),
    Company("ASML", "艾司摩爾", security_type="FOREIGN_ORDINARY_SHARE"),
    Company("AMAT", "應用材料"),
    Company("UMC", "聯電ADR", "NYSE", "ADR", "2303", 5),
    Company("ASX", "日月光投控ADR", "NYSE", "ADR", "3711", 2),
    Company("CHT", "中華電信ADR", "NYSE", "ADR", "2412", 10),
)}

# Current references verified during the original import; not historical ratio vintages.
RATIO_VERIFIED_ON = date(2026, 11, 2)
MAPPING_SOURCES = {
    "TSM": "https://investor.tsmc.com/english/faq",
    "UMC": "https://www.umc.com/en/Html/faqs",
    "ASX": "https://www.nasdaq.com/market-activity/stocks/asx",
    "CHT": "https://www.cht.com.tw/home/web/home/investors/faq-and-contactus/investors-faq",
}


def utc_naive(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise StockDailyError("timestamp_requires_timezone")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def completed_days(target, sessions, count=BAR_COUNT + 1):
    """A preceding close is required even for the oldest stored return."""
    if type(target) is not date or sessions.session("NYSE", target) is None:
        raise StockDailyError("target_is_not_trading_day")
    days = []
    for offset in range(count * 3):
        day = target - timedelta(days=offset)
        if sessions.session("NYSE", day):
            days.append(day)
            if len(days) == count:
                return tuple(reversed(days))
    raise StockDailyError("insufficient_verified_calendar")


def _price(value):
    try:
        if value is None or isinstance(value, bool):
            raise StockDailyError("missing_price")
        number = Decimal(str(value))
        if not number.is_finite() or not 0 < number < Decimal("1e16"):
            raise StockDailyError("invalid_price")
        result = number.quantize(PRECISION, rounding=ROUND_HALF_UP)
        if not 0 < result < Decimal("1e16"):
            raise StockDailyError("invalid_price_precision")
        return result
    except (InvalidOperation, TypeError) as error:
        raise StockDailyError("invalid_price") from error


@dataclass(frozen=True)
class StockDailyBatch:
    symbol: str
    market_date: date
    fetched_at: datetime
    rows: tuple[dict, ...]

    def validate(self, sessions=None):
        sessions = sessions or ExternalMarketSessions()
        if self.symbol not in COMPANIES or len(self.rows) != BAR_COUNT:
            raise StockDailyError("wrong_symbol_or_bar_count")
        expected = completed_days(self.market_date, sessions)[1:]
        if tuple(row["market_date"] for row in self.rows) != expected:
            raise StockDailyError("incomplete_or_duplicate_dates")
        fetched = utc_naive(self.fetched_at)
        for row in self.rows:
            opening, closing = sessions.session("NYSE", row["market_date"])
            if (row["symbol"] != self.symbol or row["source"] != SOURCE or row["session"] != "regular"
                    or row["fetched_at_utc"] != fetched or row["currency"] != "USD"
                    or row["session_open_utc"] != utc_naive(opening)
                    or row["session_close_utc"] != utc_naive(closing) or fetched < utc_naive(closing)):
                raise StockDailyError("invalid_batch_identity_or_time")
            prices = [_price(row[field]) for field in ("open_price", "high_price", "low_price", "close_price")]
            o, h, low, c = prices
            if not (low <= o <= h and low <= c <= h):
                raise StockDailyError("invalid_ohlc")
            _price(row["adjusted_close"])
            prior = _price(row["previous_close"])
            if (row["change_value"] != (c-prior).quantize(PRECISION, rounding=ROUND_HALF_UP)
                    or row["change_percent"] != ((c/prior-1)*100).quantize(PRECISION, rounding=ROUND_HALF_UP)):
                raise StockDailyError("invalid_return")
            if type(row["volume"]) is not int or not 0 <= row["volume"] <= 18446744073709551615:
                raise StockDailyError("invalid_volume")
        return self


def read_yahoo_json(url):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urlopen(request, timeout=15) as response:
        raw = response.read(4_000_001)
        if response.status != 200 or len(raw) > 4_000_000:
            raise StockDailyError("invalid_source_response")
    return json.loads(raw, parse_float=Decimal)


def parse_stock_history(payload, symbol, target, fetched_at, source_url, sessions=None):
    sessions = sessions or ExternalMarketSessions()
    if symbol not in COMPANIES:
        raise StockDailyError("unsupported_symbol")
    company = COMPANIES[symbol]
    days = completed_days(target, sessions)
    fetched = utc_naive(fetched_at)
    if fetched < utc_naive(sessions.session("NYSE", target)[1]) + timedelta(minutes=20):
        raise StockDailyError("session_not_ready")
    chart = payload.get("chart", {})
    if chart.get("error") is not None or not isinstance(chart.get("result"), list) or len(chart["result"]) != 1:
        raise StockDailyError("invalid_chart_result")
    data = chart["result"][0]
    meta = data.get("meta", {})
    exchanges = {"NYQ"} if company.exchange == "NYSE" else {"NMS", "NGM", "NCM", "NASDAQ"}
    if (meta.get("symbol") != symbol or meta.get("exchangeName") not in exchanges
            or meta.get("currency") != "USD" or meta.get("instrumentType") != "EQUITY"
            or meta.get("exchangeTimezoneName") != "America/New_York"):
        raise StockDailyError("wrong_security_metadata")
    stamp = meta.get("regularMarketTime")
    if (type(stamp) is not int or stamp > int(fetched_at.timestamp()) + 300
            or stamp < int(sessions.session("NYSE", target)[1].timestamp())):
        raise StockDailyError("source_has_not_reached_target_close")
    indicators = data.get("indicators", {})
    quotes, adjusted = indicators.get("quote"), indicators.get("adjclose")
    if not isinstance(quotes, list) or len(quotes) != 1 or not isinstance(adjusted, list) or len(adjusted) != 1:
        raise StockDailyError("missing_indicators")
    arrays = [data.get("timestamp")] + [quotes[0].get(k) for k in ("open", "high", "low", "close", "volume")] + [adjusted[0].get("adjclose")]
    if not all(isinstance(a, list) for a in arrays) or len({len(a) for a in arrays}) != 1:
        raise StockDailyError("unaligned_history")
    events = data.get("events", {})
    if not isinstance(events, dict) or set(events) - {"dividends", "splits", "capitalGains"}:
        raise StockDailyError("unknown_event_schema")
    dated_events = {}
    for kind, items in events.items():
        if not isinstance(items, dict):
            raise StockDailyError("invalid_event_map")
        for event_id, event in items.items():
            if not isinstance(event, dict) or type(event.get("date")) is not int:
                raise StockDailyError("invalid_event_date")
            day = datetime.fromtimestamp(event["date"], timezone.utc).astimezone(NY).date()
            dated_events.setdefault(day, []).append(dict(kind=kind, source_id=event_id, source_event=event))
    needed = set(days)
    bars = {}
    for stamp, o, h, low, c, volume, adj in zip(*arrays):
        if type(stamp) is not int:
            raise StockDailyError("invalid_bar_timestamp")
        at = datetime.fromtimestamp(stamp, timezone.utc)
        day = at.astimezone(NY).date()
        if day not in needed:
            continue  # In particular, exclude a newer, still-open session.
        if day in bars:
            raise StockDailyError("duplicate_market_date")
        opening, closing = sessions.session("NYSE", day)
        if not opening <= at <= closing:
            raise StockDailyError("bar_timestamp_outside_session")
        if type(volume) is not int or not 0 <= volume <= 18446744073709551615:
            raise StockDailyError("invalid_volume")
        prices = [_price(value) for value in (o, h, low, c, adj)]
        if not (prices[2] <= prices[0] <= prices[1] and prices[2] <= prices[3] <= prices[1]):
            raise StockDailyError("invalid_ohlc")
        bars[day] = dict(prices=prices, volume=volume, at=at,
                         raw=dict(timestamp=stamp, open=o, high=h, low=low, close=c, volume=volume, adjusted_close=adj))
    if set(bars) != needed:
        raise StockDailyError("missing_required_completed_sessions")
    rows = []
    for prior_day, day in zip(days, days[1:]):
        bar = bars[day]
        o, h, low, c, adj = bar["prices"]
        prior = bars[prior_day]["prices"][3]
        opening, closing = sessions.session("NYSE", day)
        rows.append(dict(source=SOURCE, symbol=symbol, name=company.name,
            source_name=str(meta.get("longName") or meta.get("shortName") or company.name),
            exchange=company.exchange, currency="USD", security_type=company.security_type, session="regular",
            underlying_market="TWSE" if company.underlying else None, underlying_symbol=company.underlying,
            adr_underlying_shares=Decimal(company.ratio) if company.ratio else None,
            adr_ratio_verified_on=RATIO_VERIFIED_ON if company.ratio else None,
            market_date=day, exchange_timezone="America/New_York", source_bar_time_utc=utc_naive(bar["at"]),
            session_open_utc=utc_naive(opening), session_close_utc=utc_naive(closing),
            open_price=o, high_price=h, low_price=low, close_price=c, previous_close=prior,
            change_value=(c-prior).quantize(PRECISION, rounding=ROUND_HALF_UP),
            change_percent=((c/prior-1)*100).quantize(PRECISION, rounding=ROUND_HALF_UP),
            volume=bar["volume"], volume_status="reported", adjusted_close=adj,
            adjustment_status="provider_adjclose_available", corporate_action_status="provider_events_fetched",
            source_url=source_url, fetched_at_utc=fetched,
            source_record_json=json.dumps(dict(raw_source_bar=bar["raw"], market_date=day.isoformat(),
                previous_market_date=prior_day.isoformat(), corporate_actions=dated_events.get(day, []),
                price_basis="provider_reported_daily_ohlc", adjusted_close_basis="provider_reported_adjusted_close",
                change_method="calculated_from_provider_daily_closes", stored_decimal_places=8, rounding="ROUND_HALF_UP",
                event_coverage="provider-returned events, not a complete corporate-action registry",
                adr_mapping_source=MAPPING_SOURCES.get(symbol), adr_ratio_scope="current_reference_not_historical_event_series"),
                ensure_ascii=False, default=str)))
    return StockDailyBatch(symbol, target, fetched_at, tuple(rows)).validate(sessions)


def fetch_stock_daily(symbol, target, *, sessions=None, reader=read_yahoo_json, now=None):
    sessions = sessions or ExternalMarketSessions()
    fixed_now = now
    now = now or datetime.now(timezone.utc)
    utc_naive(now)
    if symbol not in COMPANIES:
        raise StockDailyError("unsupported_symbol")
    days = completed_days(target, sessions)
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + symbol + "?" + urlencode(dict(
        period1=int(datetime.combine(days[0]-timedelta(days=2), datetime.min.time(), timezone.utc).timestamp()),
        period2=int(now.timestamp()), interval="1d", events="div,splits", includeAdjustedClose="true"))
    payload = reader(url)
    fetched = fixed_now or datetime.now(timezone.utc)
    return parse_stock_history(payload, symbol, target, fetched, url, sessions)
