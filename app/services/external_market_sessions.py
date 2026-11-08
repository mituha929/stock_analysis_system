"""Reviewed exchange calendars; expected sessions never invent source trade dates."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
import json
import os
from pathlib import Path

import pytz

PROJECT = Path(__file__).resolve().parents[2]
PRODUCTS = tuple(("cnyes", symbol, "index") for symbol in ("DJI", "INX", "IXIC", "SOX")) + (("cmoney", "TXF1", "night"),)


def product_key(product):
    return ":".join(product)


@lru_cache(maxsize=4)
def _read_calendar(path, modified, size):
    if size > 1_000_000:
        raise ValueError("外部市場日曆過大")
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if value.get("version") != 1 or set(value["markets"]) != {"NYSE", "TAIFEX"}:
        raise ValueError("外部市場日曆格式錯誤")
    for name, market in value["markets"].items():
        if market["timezone"] != ("America/New_York" if name == "NYSE" else "Asia/Taipei"):
            raise ValueError("日曆時區錯誤")
        years = market["verified_years"]
        if not years or any(type(year) is not int for year in years) or not market["source_urls"]:
            raise ValueError("缺少已核對年度／來源")
        for day in market["closed_dates"]:
            if date.fromisoformat(day).year not in years:
                raise ValueError("休市日期不在已核對年度內")
        for day, override in market.get("session_overrides", {}).items():
            if date.fromisoformat(day).year not in years or not set(override) <= {"open", "close", "closed"}:
                raise ValueError("特殊時段格式錯誤")
            for field in ("open", "close"):
                if field in override:
                    time.fromisoformat(override[field])
            if "closed" in override and type(override["closed"]) is not bool:
                raise ValueError("closed 必須是布林值")
    return value["markets"]


@dataclass(frozen=True)
class CapturePlan:
    market: str
    session_key: str
    opens: datetime
    closes: datetime
    scheduled_at: datetime
    phase: str

    @property
    def key(self):
        return self.session_key + ":" + ("close" if self.phase == "close" else self.scheduled_at.isoformat(timespec="minutes"))


class ExternalMarketSessions:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv("CAPITAL_EXTERNAL_QUOTES_CALENDAR_FILE", str(PROJECT / "config" / "external_market_sessions.json")))
        if not self.path.is_absolute():
            self.path = PROJECT / self.path

    def markets(self):
        stat = self.path.stat()
        return _read_calendar(str(self.path), stat.st_mtime_ns, stat.st_size)

    def session(self, market, day):
        config = self.markets()[market]
        if day.year not in config["verified_years"]:
            raise ValueError(f"{market} {day.year} 日曆尚未核對")
        override = config.get("session_overrides", {}).get(day.isoformat(), {})
        if override.get("closed") is True or (override.get("closed") is not False and
                (day.weekday() >= 5 or day.isoformat() in config["closed_dates"])):
            return None
        zone = pytz.timezone(config["timezone"])
        opens = time.fromisoformat(override.get("open", "09:30" if market == "NYSE" else "15:00"))
        closes = time.fromisoformat(override.get("close", "16:00" if market == "NYSE" else "05:00"))
        start = zone.localize(datetime.combine(day, opens))
        end = zone.localize(datetime.combine(day + timedelta(days=closes <= opens), closes))
        return start, end

    def plan(self, product, now, interval_minutes=30, close_delay_minutes=20):
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("排程時間必須包含時區")
        market = "NYSE" if product[0] == "cnyes" else "TAIFEX"
        zone = pytz.timezone("America/New_York" if market == "NYSE" else "Asia/Taipei")
        local = now.astimezone(zone)
        # Check this year before looking back, so an unknown new year isn't silently inferred.
        if local.year not in self.markets()[market]["verified_years"]:
            raise ValueError(f"{market} {local.year} 日曆尚未核對")
        for offset in range(32):
            day = local.date() - timedelta(days=offset)
            try:
                session = self.session(market, day)
            except ValueError:
                break
            if not session or local < session[0]:
                continue
            start, end = session
            if local < end:
                steps = int((local-start).total_seconds() // (interval_minutes*60))
                scheduled = start + timedelta(minutes=steps*interval_minutes)
                phase = "intraday"
            else:
                scheduled = end + timedelta(minutes=close_delay_minutes)
                phase = "close" if local >= scheduled else "close_wait"
            return CapturePlan(market, f"{market}:{day.isoformat()}", start, end, scheduled, phase)
        return None


def quote_quality(quote_at, now, plan, *, max_age_seconds=2700, close_retry_minutes=90):
    """A close-time timestamp check is not a guarantee of an official settlement."""
    age = max(0, int((now-quote_at).total_seconds())) if quote_at else None
    during = plan.opens <= now < plan.closes
    if quote_at is None:
        status = "unknown_time"
    elif quote_at > now + timedelta(minutes=5):
        status = "future_time"
    elif quote_at < plan.opens:
        status = "stale"
    elif quote_at > plan.closes + timedelta(minutes=close_retry_minutes):
        status = "outside_session_time"
    elif during and age > max_age_seconds:
        status = "delayed"
    else:
        status = "current_session" if during else "latest_session"
    reached_close = quote_at is not None and quote_at >= plan.closes and status in {"current_session", "latest_session"}
    pending = (plan.phase == "close" and quote_at is not None and not reached_close
               and now < plan.closes + timedelta(minutes=close_retry_minutes))
    return dict(freshness=status, quote_age_seconds=age, quote_time_known=quote_at is not None,
                market_session_key=plan.session_key, market_state="open" if during else "closed",
                session_open=plan.opens.isoformat(), session_close=plan.closes.isoformat(),
                source_time_reached_close=reached_close, close_pending=pending,
                close_status=("pending_source_update" if pending else "source_time_verified" if reached_close
                              else "time_unknown" if quote_at is None else "unverified"),
                official_close_confirmed=False, data_kind="quote_snapshot", source_trading_date=None,
                usable_for_expected_session=status in {"current_session", "latest_session"})
