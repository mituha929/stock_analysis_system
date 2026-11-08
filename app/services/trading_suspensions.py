"""Conservative official halt evidence, not an exhaustive tradable-stock list.

An absent record is UNKNOWN. Only fresh, explicit full-day evidence may avoid
a Capital request; cached evidence never changes the API verification cursor.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from app.services.taiwan_trading_calendar import trading_day_status

logger = logging.getLogger(__name__)
SOURCES = {
    "twse": "https://openapi.twse.com.tw/v1/exchangeReport/TWTAWU",
    "tpex_history": "https://www.tpex.org.tw/openapi/v1/tpex_spendi_history",
    "tpex_status": "https://www.tpex.org.tw/openapi/v1/tpex_cmode",
}
_LOCK = threading.Lock()


def official_date(value):
    raw = re.sub(r"[/.-]", "", str(value or "").strip())
    if not re.fullmatch(r"\d{7,8}", raw):
        raise ValueError("停復牌資料的日期格式不明")
    year = int(raw[:-4])
    return date(year + 1911 if year < 1911 else year, int(raw[-4:-2]), int(raw[-2:]))


def official_time(value):
    raw = str(value or "").strip().replace(":", "")
    return time(int(raw[:2]), int(raw[2:4]), int(raw[4:6] or 0)) if re.fullmatch(r"\d{4}(\d{2})?", raw) else None


@dataclass(frozen=True)
class Halt:
    market: int
    symbol: str
    start: date
    end: date
    source: str
    observed_on: date
    resumed_on: date | None = None


def _period(symbol, market, start_date, start_time, resume_date, resume_time, source, today, open_current=False):
    start = official_date(start_date)
    began = official_time(start_time)
    resume = official_date(resume_date) if str(resume_date or "").strip() else None
    # Unknown/partial first sessions are NOT skipped. A resumption day is also
    # retained unless the official time is after all regular/odd-lot sessions.
    first = start if began is not None and began <= time(9) else start + timedelta(days=1)
    if resume:
        resumed = official_time(resume_time)
        last = resume if resumed is not None and resumed >= time(15) else resume - timedelta(days=1)
    else:
        # TPEx history sometimes publishes halt and resume as separate rows;
        # an unpaired historical row is not evidence of a years-long halt.
        last = today if open_current else start
    last = min(last, today)
    return Halt(market, symbol, first, last, source, today, resume) if first <= last else None


def parse_official(source, rows, today):
    if not isinstance(rows, list) or len(rows) > 50000 or any(not isinstance(r, dict) for r in rows):
        raise ValueError("停復牌 API 不是可辨識的資料列陣列")
    result = []
    if source == "tpex_status":
        for row in rows:
            # Require the specific field; altered/periodic/margin trading is
            # deliberately NOT a suspension signal.
            observed = official_date(row["Date"])
            flag = unicodedata.normalize("NFKC", row["SuspensionOfTrading"]).strip().upper()
            if flag == "Y" and observed == today:
                result.append(Halt(1, str(row["SecuritiesCompanyCode"]), observed, observed, SOURCES[source], today))
    elif source == "twse":
        for row in rows:
            item = _period(str(row["Code"]), 0, row["TradingHaltDate"], row["TradingHaltTime"],
                           row["TradingResumptionDate"], row["TradingResumptionTime"], SOURCES[source], today, True)
            if item: result.append(item)
    elif source == "tpex_history":
        events = {}
        for row in rows:
            symbol = str(row["SecuritiesCompanyCode"])
            for field, clock, kind in (("DateOfSuspendedTrading", "TimeOfSuspendedTrading", "halt"),
                                      ("DateOfResumedTrading", "TimeOfResumedTrading", "resume")):
                if str(row[field]).strip():
                    events.setdefault(symbol, []).append((official_date(row[field]), str(row[clock]), kind))
        for symbol, values in events.items():
            pending = None
            for day, clock, kind in sorted(values):
                if kind == "halt":
                    if pending is None: pending = (day, clock)
                elif pending is not None:
                    item = _period(symbol, 1, pending[0].isoformat(), pending[1], day.isoformat(), clock,
                                   SOURCES[source], today)
                    if item: result.append(item)
                    pending = None
            if pending:
                item = _period(symbol, 1, pending[0].isoformat(), pending[1], None, None, SOURCES[source], today)
                if item: result.append(item)
    else:
        raise ValueError("未知的停復牌來源")
    if any(not re.fullmatch(r"[A-Za-z0-9]{2,12}", item.symbol) for item in result):
        raise ValueError("停復牌代碼格式錯誤")
    return result


class SuspensionCalendar:
    def __init__(self, records=(), status=None):
        self.records = {}
        for item in records:
            self.records.setdefault((item.market, item.symbol), []).append(item)
        self.status = status or {"state": "unavailable", "message": "無可靠停復牌資料，採有上限的 API 查詢"}

    def full_window(self, market, symbol, start, end):
        base_market = {5: 0, 6: 1}.get(market, market)
        records = self.records.get((base_market, symbol), ())
        sources = set()
        traded_days = 0
        day = start
        while day <= end:
            if trading_day_status(day)[0]:
                traded_days += 1
                evidence = [r for r in records if r.start <= day <= r.end and r.observed_on == date.today()]
                if not evidence:
                    return None
                sources.update(r.source for r in evidence)
            day += timedelta(days=1)
        return ",".join(sorted(sources)) if traded_days else "trading_calendar"


def _download(url):
    with urlopen(Request(url, headers={"Accept": "application/json", "User-Agent": "CapitalAPI/1.0"}), timeout=5) as response:
        content = response.read(4 * 1024 * 1024 + 1)
    if len(content) > 4 * 1024 * 1024: raise ValueError("停復牌回應過大")
    return json.loads(content.decode("utf-8-sig"))


def load_suspensions(today=None, *, path=None, fetcher=None):
    """Once/day/source, including failed attempts. Failure retains but disables old evidence."""
    today = today or date.today()
    # A pre-close snapshot cannot certify a whole session. Refresh it once
    # more after close; otherwise later same-day resumption could be missed.
    session = "closed" if today != date.today() or datetime.now().time() >= time(14,30) else "preclose"
    if os.getenv("CAPITAL_SUSPENSION_SYNC_ENABLED", "true").lower() in {"0", "false"}:
        return SuspensionCalendar(status={"state": "disabled", "message": "停復牌同步未啟用；採有上限的 API 查詢"})
    root = Path(__file__).resolve().parents[2]
    cache_path = path or Path(os.getenv("CAPITAL_SUSPENSION_CACHE_FILE", str(root / "data" / "trading_suspensions.json")))
    if not cache_path.is_absolute(): cache_path = root / cache_path
    fetcher = fetcher or _download
    with _LOCK:
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            if cache.get("version") != 1 or not isinstance(cache.get("sources"), dict): raise ValueError("版本錯誤")
        except (OSError, ValueError, TypeError):
            cache = {"version": 1, "sources": {}}
        due = [name for name in SOURCES if cache["sources"].get(name, {}).get("attempted_on") != today.isoformat()
               or cache["sources"].get(name, {}).get("attempted_session") != session]
        def refresh(name):
            try:
                rows = parse_official(name, fetcher(SOURCES[name]), today)
                if session == "preclose":
                    rows = [Halt(r.market,r.symbol,r.start,min(r.end,today-timedelta(days=1)),r.source,r.observed_on,r.resumed_on)
                            for r in rows if r.start < today]
                return name, {"attempted_on": today.isoformat(), "attempted_session":session,"fetched_on": today.isoformat(),
                              "error": None, "records": json.loads(json.dumps([asdict(r) for r in rows], default=str))}
            except Exception as error:
                previous = dict(cache["sources"].get(name, {}))
                previous.update(attempted_on=today.isoformat(), attempted_session=session,error=f"{type(error).__name__}: {error}"[:500])
                logger.warning("停復牌來源 %s 更新失敗；不依過期資料跳過：%s", name, error)
                return name, previous
        if due:
            with ThreadPoolExecutor(max_workers=3) as pool:
                cache["sources"].update(pool.map(refresh, due))
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=cache_path.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump(cache, stream, ensure_ascii=False, default=str)
                try: os.replace(temporary, cache_path)
                finally: temporary.unlink(missing_ok=True)
            except OSError as error:
                logger.warning("停復牌快取未能保存：%s", error)
        records, states = [], {}
        for name, source in cache["sources"].items():
            fresh = source.get("fetched_on") == today.isoformat() and not source.get("error")
            states[name] = {"fresh": fresh, "fetched_on": source.get("fetched_on"), "error": source.get("error")}
            if fresh:
                try:
                    parsed = [Halt(int(r["market"]), r["symbol"], date.fromisoformat(r["start"]), date.fromisoformat(r["end"]),
                                   r["source"], date.fromisoformat(r["observed_on"]),
                                   date.fromisoformat(r["resumed_on"]) if r.get("resumed_on") else None) for r in source.get("records", [])]
                    records.extend(parsed)
                except (ValueError, KeyError, TypeError): states[name]["fresh"] = False
        return SuspensionCalendar(records, {"state": "ready" if all(v["fresh"] for v in states.values()) else "partial",
            "sources": states, "checked_on": today.isoformat(),
            "message": "未列名不代表正常；興櫃及未確認停牌採限量查詢；Yahoo 不作為唯一跳過依據"})
