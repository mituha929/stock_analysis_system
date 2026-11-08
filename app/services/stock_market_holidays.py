"""TWSE stock holiday acquisition shared by listed/OTC/emerging stock markets.

Does not import or change the runtime trading calendar, MySQL, or Capital.
The runtime calendar independently reads the generated local merged file.
An empty official response means unavailable coverage, NOT an open market.
Reviewed official archive supplements and user overrides stay separate from
downloaded annual responses. Weekends are marked separately from announcements.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.request import Request, urlopen
from urllib.parse import urlparse

from app.services.taipei_closures import atomic_text

PROJECT = Path(__file__).resolve().parents[2]
API_BASES = ("https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule",
             "https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule")
WEEKEND_SOURCE = "https://www.twse.com.tw/zh/products/system/trading.html"
SNAPSHOT_PATH = PROJECT / "data" / "stock_market_holidays_official.json"
SUPPLEMENTS_PATH = PROJECT / "config" / "stock_market_holidays_supplements.json"
MANUAL_PATH = PROJECT / "config" / "stock_market_holidays_manual.json"
MERGED_PATH = PROJECT / "data" / "stock_market_holidays.json"
REPORT_PATH = PROJECT / "docs" / "STOCK_MARKET_HOLIDAY_DATES.md"
TAIPEI_TZ = timezone(timedelta(hours=8))
MARKETS = ("TWSE", "TPEx", "ESB")
LIMIT_BYTES = 2 * 1024 * 1024
ROW_FIELDS = ["日期", "名稱", "說明"]


class StockHolidayError(ValueError):
    pass


def today() -> date:
    return datetime.now(TAIPEI_TZ).date()


def iso_day(raw: object) -> date:
    try:
        if not isinstance(raw, str):
            raise ValueError("date must be a string")
        parsed = date.fromisoformat(raw)
        if parsed.isoformat() != raw:
            raise ValueError("use YYYY-MM-DD")
        return parsed
    except ValueError as error:
        raise StockHolidayError(f"日期無效，請使用 YYYY-MM-DD：{raw!r}") from error


def first_day(as_of: date, years: int = 10) -> date:
    if not 1 <= years <= 100:
        raise StockHolidayError("years 必須介於 1 與 100")
    try:
        return as_of.replace(year=as_of.year - years)
    except ValueError:
        return as_of.replace(year=as_of.year - years, day=28)


def load_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("必須是 version=1 的物件")
        return payload
    except (OSError, ValueError) as error:
        raise StockHolidayError(f"休市資料檔無效：{path}：{error}") from error


def save_json(path: Path, payload: dict) -> None:
    atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def classify_row(name: str, description: str, day: date | None = None) -> str:
    compact = re.sub(r"\s+", "", name)
    # 2021/2022 annual rows reuse "last trading day" as the NAME of following
    # settlement-only days. Only an explicit same-date statement overrides that
    # marker: a true last-trading-day row may mention OTHER closed dates.
    if re.search(r"(?:開始|最後)(?:之)?交易日$", compact):
        text = re.sub(r"\s+", "", description)
        if day and re.search(
            rf"(?<!\d)0?{day.month}月0?{day.day}日(?:當日)?(?:證券)?市場(?:無交易|不交易|休市)", text,
        ):
            return "closed"
        return "trading_marker"
    if re.search(r"(?:無交易|不交易|停止交易|休市)", compact):
        return "closed"
    if any(word in description for word in ("照常交易", "照常開市")) or "補行交易" in compact:
        return "special_open"
    if re.search(r"放假|補假|休市|不交易|無交易", description):
        return "closed"
    return "review"


def parse_annual(payload: object, year: int, source_url: str, *, raw_hash: str = "") -> dict:
    if not isinstance(payload, dict) or payload.get("stat") != "ok":
        raise StockHolidayError(f"{year} 年回應 stat 不正確")
    if payload.get("fields") != ROW_FIELDS or not isinstance(payload.get("data"), list):
        raise StockHolidayError(f"{year} 年回應欄位變動，停止更新")
    expected = f"{year}0101"
    if payload.get("date") != expected or payload.get("queryYear") != year:
        raise StockHolidayError(f"{year} 年請求卻收到其他年度資料，停止更新")
    if payload.get("total") != len(payload["data"]):
        raise StockHolidayError(f"{year} 年 total 與資料筆數不符")
    title = payload.get("title", "")
    if not isinstance(title, str) or not re.search(rf"(?<!\d){year - 1911}\s*年", title):
        raise StockHolidayError(f"{year} 年資料標題不符")
    records = []
    for row in payload["data"]:
        if not isinstance(row, list) or len(row) != 3 or not all(isinstance(value, str) for value in row):
            raise StockHolidayError(f"{year} 年含無效資料列，保留舊資料")
        day = iso_day(row[0])
        if day.year != year or not row[1].strip():
            raise StockHolidayError(f"{year} 年日期或名稱無效")
        records.append(dict(date=day.isoformat(), name=row[1].strip(), description=row[2].strip(),
                            kind=classify_row(row[1], row[2], day), source_url=source_url,
                            origin="twse_annual", markets=list(MARKETS)))
    if records and not any(row["kind"] == "closed" for row in records):
        raise StockHolidayError(f"{year} 年非空回應卻沒有可辨識的休市列，停止更新")
    return dict(year=year, status="available" if records else "unavailable",
                fetched_at=datetime.now(TAIPEI_TZ).isoformat(), source_url=source_url,
                source_sha256=raw_hash, response=payload, records=records,
                note="" if records else "官方年度介面回傳空陣列，不代表該年沒有休市日")


def fetch_year(year: int, *, timeout_seconds: float = 20) -> dict:
    attempts = []
    for base in API_BASES:
        source = f"{base}?date={year}0101&response=json"
        try:
            request = Request(source, headers={"User-Agent": "CapitalAPI-StockHolidays/1.0", "Accept": "application/json"})
            with urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read(LIMIT_BYTES + 1)
            if len(raw) > LIMIT_BYTES:
                raise StockHolidayError("回應超過 2 MiB")
            payload = json.loads(raw.decode("utf-8-sig"))
        except (OSError, ValueError) as error:
            attempts.append(dict(source_url=source, status="download_error", error=str(error)))
            continue
        parsed = parse_annual(payload, year, source, raw_hash=hashlib.sha256(raw).hexdigest())
        attempts.append(dict(source_url=source, status=parsed["status"]))
        if parsed["records"]:
            return dict(parsed, attempts=attempts)
        empty = parsed
    if any(item["status"] == "unavailable" for item in attempts):
        return dict(empty, attempts=attempts)
    raise StockHolidayError(f"{year} 年所有官方下載來源失敗：{attempts}")


def validate_override(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise StockHolidayError("人工紀錄必須是物件")
    required = {"date", "name", "is_closed", "reason", "source_url", "enabled"}
    if set(raw) != required:
        raise StockHolidayError("人工紀錄需包含 date/name/is_closed/reason/source_url/enabled")
    iso_day(raw["date"])
    if not all(isinstance(raw[key], str) for key in ("name", "reason", "source_url")):
        raise StockHolidayError("名稱、原因與來源必須是字串")
    if not raw["name"].strip() or not raw["reason"].strip():
        raise StockHolidayError("人工名稱與原因不可空白")
    if type(raw["is_closed"]) is not bool or type(raw["enabled"]) is not bool:
        raise StockHolidayError("is_closed 與 enabled 必須是 true/false")
    if raw["source_url"]:
        parsed = urlparse(raw["source_url"])
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise StockHolidayError("來源請使用完整的 http(s) 網址；未知來源可以留空")
    return dict(raw)


def notice_origin(source_url: str) -> str:
    host = urlparse(source_url).hostname or ""
    is_exchange = any(host == domain or host.endswith("." + domain) for domain in ("twse.com.tw", "tpex.org.tw"))
    return "reviewed_exchange_notice" if is_exchange else "reported_exchange_notice"


def load_manual(path: Path = MANUAL_PATH) -> dict:
    payload = load_json(path)
    validate_manual(payload)
    return payload


def validate_manual(payload: dict) -> None:
    if not isinstance(payload.get("records"), list):
        raise StockHolidayError("人工新增檔缺少 records 陣列")
    seen = set()
    for item in payload["records"]:
        record = validate_override(item)
        if record["date"] in seen:
            raise StockHolidayError(f"人工日期重複：{record['date']}")
        seen.add(record["date"])


def validate_supplements(payload: dict) -> None:
    if not isinstance(payload.get("annual_archives"), dict) or not isinstance(payload.get("closures"), list):
        raise StockHolidayError("官方補充檔缺少 annual_archives 或 closures")
    for year, archive in payload["annual_archives"].items():
        if not year.isdigit() or not isinstance(archive, dict):
            raise StockHolidayError("官方舊年表年度無效")
        if (not archive.get("source_url")
                or not archive.get("reviewed_on") or type(archive.get("covers_full_year")) is not bool):
            raise StockHolidayError(f"{year} 年補充來源缺少查證資訊")
        iso_day(archive["reviewed_on"])
        if not re.fullmatch(r"[0-9a-f]{64}", archive.get("reviewed_normalized_sha256", "")):
            raise StockHolidayError(f"{year} 年補充來源缺少已核對的摘要值")
        for row in archive_records(archive):
            if (not isinstance(row, dict) or set(row) != {"date", "name", "description", "kind"}
                    or iso_day(row["date"]).year != int(year)
                    or row["kind"] not in {"closed", "trading_marker", "special_open"}
                    or not all(isinstance(row[key], str) for key in ("name", "description"))):
                raise StockHolidayError(f"{year} 年補充表格紀錄無效")
    seen = set()
    for raw in payload["closures"]:
        record = validate_override(raw)
        if not record["source_url"] or record["date"] in seen:
            raise StockHolidayError("官方臨時休市日期重複或沒有來源")
        seen.add(record["date"])


def archive_records(archive: dict) -> list[dict]:
    if "records" in archive:
        if not isinstance(archive["records"], list):
            raise StockHolidayError("舊年表 records 必須是陣列")
        return archive["records"]
    if not isinstance(archive.get("groups"), list) or not archive["groups"]:
        raise StockHolidayError("舊年表缺少 records 或 groups")
    rows = []
    for group in archive["groups"]:
        if (not isinstance(group, dict) or set(group) != {"dates", "name", "description", "kind"}
                or not isinstance(group["dates"], list) or not group["dates"]):
            raise StockHolidayError("舊年表日期群組無效")
        rows.extend(dict(date=day, name=group["name"], description=group["description"], kind=group["kind"])
                    for day in group["dates"])
    if len({row["date"] for row in rows}) != len(rows):
        raise StockHolidayError("舊年表含重複日期")
    return rows


def archive_digest(html: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", "", html).encode("utf-8")).hexdigest()


def fetch_archive(year: int, archive: dict, *, timeout_seconds: float = 20) -> dict:
    """Retrieve the exact public table that was reviewed, not arbitrary HTML."""
    source = archive["source_url"]
    expected = f"https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_{year - 1911}.htm"
    if source != expected:
        raise StockHolidayError(f"{year} 年舊年表來源不符預期")
    request = Request(source, headers={"User-Agent": "CapitalAPI-StockHolidays/1.0"})
    with urlopen(request, timeout=timeout_seconds) as response:
        raw = response.read(LIMIT_BYTES + 1)
    if len(raw) > LIMIT_BYTES:
        raise StockHolidayError("舊年表回應超過 2 MiB")
    html = raw.decode("utf-8-sig")
    digest = archive_digest(html)
    if (f"Taipei Exchange Holiday Schedule {year}" not in html
            or digest != archive["reviewed_normalized_sha256"]):
        raise StockHolidayError(f"{year} 年舊年表與已核對版本不同，停止更新，請重新檢閱")
    return dict(source_url=source, fetched_at=datetime.now(TAIPEI_TZ).isoformat(),
                normalized_sha256=digest, html=html)


def build_calendar(snapshot: dict, supplements: dict, manual: dict, as_of: date, years: int = 10) -> dict:
    start = first_day(as_of, years)
    validate_supplements(supplements)
    validate_manual(manual)
    by_day = {}
    review = []
    coverage = []
    for year in range(start.year, as_of.year + 1):
        annual = snapshot.get("years", {}).get(str(year))
        archive = supplements["annual_archives"].get(str(year))
        available = annual is not None and annual.get("status") == "available"
        saved_archive = annual.get("archive", {}) if annual else {}
        archive_verified = bool(archive and saved_archive.get("source_url") == archive["source_url"]
                                and archive_digest(saved_archive.get("html", "")) == archive["reviewed_normalized_sha256"])
        fallback = bool(archive_verified and archive["covers_full_year"])
        rows = []
        if available:
            # Validate saved response again, including requested year and schema.
            rows.extend(parse_annual(annual["response"], year, annual["source_url"])["records"])
        if archive_verified:
            rows.extend(dict(row, source_url=archive["source_url"], origin="reviewed_official_archive",
                             markets=list(MARKETS)) for row in archive_records(archive)
                        if not available or row["kind"] == "special_open")
        status = "api" if available else "reviewed_archive" if fallback else "missing"
        coverage.append(dict(year=year, status=status,
                             fetched_at=annual.get("fetched_at") if annual else None,
                             source_url=annual["source_url"] if available else archive["source_url"] if archive else None,
                             note="" if available or fallback else "年度介面無資料，不能據此宣稱完整；未收錄平日保持待確認"))
        for row in rows:
            if not start <= iso_day(row["date"]) <= as_of:
                continue
            if row["kind"] == "review":
                review.append(row)
            else:
                by_day.setdefault(row["date"], []).append(row)
    # User-confirmed scope is the three STOCK markets, not futures/bond sessions.
    cursor = start
    records = []
    overrides = {row["date"]: (row, notice_origin(row["source_url"]))
                 for row in supplements["closures"] if row["enabled"]}
    overrides.update({row["date"]: (row, "manual") for row in manual["records"] if row["enabled"]})
    covered_years = {entry["year"] for entry in coverage if entry["status"] != "missing"}
    review_dates = {row["date"] for row in review}
    while cursor <= as_of:
        key = cursor.isoformat()
        weekend = cursor.weekday() >= 5
        events = by_day.get(key, [])
        closed = any(row["kind"] == "closed" for row in events)
        special_open = any(row["kind"] == "special_open" for row in events)
        marker = any(row["kind"] == "trading_marker" for row in events)
        if closed and (special_open or marker):
            raise StockHolidayError(f"{key} 官方紀錄同時標為開市與休市，停止建檔")
        if marker and weekend and not special_open:
            raise StockHolidayError(f"{key} 週末出現交易日標記，須人工確認是否補行交易")
        evidence = list(events)
        # Before 2019, some Saturdays were trading days. Without the annual
        # exception list those Saturdays stay unknown rather than auto-closed.
        weekend_closed = weekend and not (cursor.year < 2019 and cursor.weekday() == 5 and cursor.year not in covered_years)
        if key in review_dates and not closed:
            weekend_closed = False
        is_closed = (weekend_closed or closed) and not special_open
        category = "annual_holiday" if closed else "weekend" if weekend and not special_open else "special_open" if special_open else "trading_marker"
        if key in overrides:
            rule, origin = overrides[key]
            is_closed = rule["is_closed"]
            category = "manual_override" if origin == "manual" else "emergency_closure"
            evidence.append(dict(date=key, name=rule["name"], description=rule["reason"], kind="closed" if is_closed else "special_open",
                                 source_url=rule["source_url"], origin=origin, markets=list(MARKETS)))
        if is_closed or events or key in overrides:
            records.append(dict(date=key, weekday=cursor.weekday(), is_closed=is_closed, category=category,
                                is_weekend=weekend, markets=list(MARKETS), evidence=evidence,
                                weekend_source=WEEKEND_SOURCE if weekend else None))
        cursor += timedelta(days=1)
    closures = [row for row in records if row["is_closed"]]
    return dict(version=1, generated_at=datetime.now(TAIPEI_TZ).isoformat(), range_start=start.isoformat(), range_end=as_of.isoformat(),
                markets=list(MARKETS), market_labels={"TWSE": "上市", "TPEx": "上櫃", "ESB": "興櫃"},
                annual_coverage=coverage, missing_years=[entry["year"] for entry in coverage if entry["status"] == "missing"],
                annual_coverage_complete=len(covered_years) == len(coverage) and not review, emergency_coverage_complete=False,
                emergency_dates=[row["date"] for row in records if row["is_closed"] and row["category"] == "emergency_closure"],
                saturday_trading_dates=[row["date"] for row in records if not row["is_closed"] and row["is_weekend"]],
                coverage_note="年度表不保證收錄臨時休市；臨時事件僅依另附交易所公告補登。缺年度的未列日期不代表有交易；2019 年前週六保留補行交易例外。",
                runtime_calendar_applied=False, closed_date_count=len(closures),
                weekday_closed_count=sum(not row["is_weekend"] for row in closures),
                weekend_closed_count=sum(row["is_weekend"] for row in closures),
                closed_dates=[row["date"] for row in closures], records=records, review_required_records=review)


def calendar_status(calendar: dict, day: date) -> tuple[str, str]:
    """Standalone lookup: an uncovered weekday must stay unknown, not open."""
    if not iso_day(calendar["range_start"]) <= day <= iso_day(calendar["range_end"]):
        return "unknown", "日期不在查詢範圍"
    for row in calendar["records"]:
        if row["date"] == day.isoformat():
            return ("closed" if row["is_closed"] else "open"), row["category"]
    if day.year in calendar["missing_years"] or any(row["date"] == day.isoformat() for row in calendar["review_required_records"]):
        return "unknown", "官方年度資料缺漏或尚待分類"
    return "not_listed", "年度表未列休市；仍須考慮未收錄的臨時休市"


def render_report(calendar: dict) -> str:
    def safe(value):
        return str(value).replace("|", "／").replace("\r", "").replace("\n", " ")

    lines = ["# 台股休市日期（上市／上櫃／興櫃共用）", "",
             f"範圍：{calendar['range_start']}～{calendar['range_end']}（含起迄日）；整理時間：{calendar['generated_at']}。", "",
             f"已收錄休市 {calendar['closed_date_count']} 日：平日 {calendar['weekday_closed_count']} 日、週末 {calendar['weekend_closed_count']} 日（同日不重複計算）。", "",
             "**完整性限制：** " + calendar["coverage_note"],
             "此操作不修改 .env、MySQL 或回補工作；執行期交易日判斷會讀取此合併快照。人工維護見 [操作說明](STOCK_MARKET_HOLIDAYS.md)。", "",
             "## 年度來源與缺漏", "", "| 年度 | 狀態 | 平日休市 | 週末休市 | 來源 |", "| --- | --- | ---: | ---: | --- |"]
    labels = {"api": "官方 API 已取得", "reviewed_archive": "官方舊年表已人工核對", "missing": "缺少年度表，不代表沒有休市"}
    for entry in calendar["annual_coverage"]:
        url = entry["source_url"]
        rows = [row for row in calendar["records"] if row["is_closed"] and row["date"].startswith(str(entry["year"]))]
        weekdays = sum(not row["is_weekend"] for row in rows)
        weekends = sum(row["is_weekend"] for row in rows)
        lines.append(f"| {entry['year']} | {labels[entry['status']]} | {weekdays} | {weekends} | " + (f"[官方來源]({url})" if url else "待補") + " |")
    lines += ["", "## 週六補行交易例外（不列入休市）", "",
              "、".join(calendar["saturday_trading_dates"]) or "本範圍無已確認的週末交易例外。",
              "", "## 臨時休市來源層級", "",
              "年度表之外的休市逐筆附來源，不直接套用臺北市停班停課清單。來源為中央社轉述或公會公告時，保留該層級，不冒稱交易所原始公告。",
              "臨時休市公告的歷史完整性未宣稱已全部驗證，原始資料保留 emergency_coverage_complete=false。"]
    lines += ["", "## 公告休市日期（含與週末重疊的公告，非全部週末）", "",
              "| 日期 | 類型 | 名稱 | 說明 | 來源 |", "| --- | --- | --- | --- | --- |"]
    for row in calendar["records"]:
        if not row["is_closed"] or row["category"] == "weekend":
            continue
        for event in row["evidence"]:
            if event["kind"] != "closed":
                continue
            attribution = f"[來源]({event['source_url']})" if event["source_url"] else "人工新增，未填來源"
            lines.append(f"| {row['date']} | {row['category']} | {safe(event['name'])} | {safe(event['description'])} | {attribution} |")
    lines += ["", "## 全部已收錄休市日期（含週末）", "", "```text"]
    lines.extend(calendar["closed_dates"])
    lines += ["```", "", "## 待分類紀錄", ""]
    lines += [f"- {row['date']}：{safe(row['name'])}；{safe(row['description'])}" for row in calendar["review_required_records"]] or ["無未分類紀錄；不代表缺漏年度已補齊或臨時休市已全部收錄。"]
    return "\n".join(lines) + "\n"


def write_derived(snapshot: dict, supplements: dict, manual: dict, as_of: date, *, years: int = 10,
                  merged_path: Path = MERGED_PATH, report_path: Path = REPORT_PATH) -> dict:
    calendar = build_calendar(snapshot, supplements, manual, as_of, years)
    report = render_report(calendar)
    save_json(merged_path, calendar)
    atomic_text(report_path, report)
    return calendar


def refresh(as_of: date, *, years: int = 10, snapshot_path: Path = SNAPSHOT_PATH,
            supplements_path: Path = SUPPLEMENTS_PATH, manual_path: Path = MANUAL_PATH,
            merged_path: Path = MERGED_PATH, report_path: Path = REPORT_PATH, progress=None,
            pause_seconds: float = 0.6) -> dict:
    start = first_day(as_of, years)
    manual = load_manual(manual_path)
    supplements = load_json(supplements_path)
    validate_supplements(supplements)
    snapshot = load_json(snapshot_path) if snapshot_path.exists() else {"version": 1, "years": {}}
    updated = dict(snapshot, years=dict(snapshot["years"]))
    for year in range(start.year, as_of.year + 1):
        parsed = fetch_year(year)
        archive = supplements["annual_archives"].get(str(year))
        if archive:
            parsed["archive"] = fetch_archive(year, archive)
        previous = snapshot["years"].get(str(year))
        if previous and previous.get("status") == "available" and parsed["status"] != "available":
            raise StockHolidayError(f"{year} 年曾有資料但本次變空，停止更新並保留舊快照")
        if previous and len(parsed["records"]) < len(previous.get("records", [])):
            raise StockHolidayError(f"{year} 年資料筆數倒退，停止更新並保留舊快照，請確認官方是否修訂")
        updated["years"][str(year)] = parsed
        if progress:
            progress(year, parsed)
        if year != as_of.year:
            time.sleep(pause_seconds)
    calendar = build_calendar(updated, supplements, manual, as_of, years)
    report = render_report(calendar)
    save_json(snapshot_path, updated)
    save_json(merged_path, calendar)
    atomic_text(report_path, report)
    return calendar
