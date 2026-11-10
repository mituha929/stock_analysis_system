"""Calendar-based plans and durable progress for official market report updates."""
from calendar import monthrange
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import random
import sqlite3
import time
from uuid import uuid4

from scripts.collect_market_history import (
    PROJECT, TAIPEI, chip_jobs, financial_jobs, month_periods, quarter_periods, revenue_jobs,
)
from scripts.market_data_store import market_segment
from scripts.stage_market_data import record_key, verify_report

WORKFLOW_DDL = PROJECT / "sql" / "market_data_p3_mysql.sql"
GROUPS = {
    "daily": {"institutional_stock", "institutional_market", "margin", "margin_market"},
    "revenue": {"revenue"},
    "financial": {"income", "balance"},
}
TRANSIENT_ERRORS = {"URLError", "TimeoutError", "OSError", "ConnectionResetError"}


def source_id(job):
    return ":".join((job.dataset, job.market, market_segment(job)))


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")


def trading_days(calendar, end, count):
    first = date.fromisoformat(calendar["range_start"])
    if not first <= end <= date.fromisoformat(calendar["range_end"]):
        raise ValueError("trading_calendar_coverage_required")
    closed = set(calendar["closed_dates"])
    days = []
    while len(days) < count:
        if end < first:
            raise ValueError("trading_calendar_coverage_required")
        if end.weekday() < 5 and end.isoformat() not in closed:
            days.append(end.isoformat())
        end -= timedelta(days=1)
    return days


def financial_window(period, calendar):
    year, quarter = map(int, period.split("-Q"))
    month = quarter * 3
    end = date(year, month, monthrange(year, month)[1])
    override = calendar.get("financial_deadlines", {}).get(period)
    if override:
        deadline = date.fromisoformat(override)
    else:
        # Window for a market-wide pull. The longest ordinary deadlines cover
        # Q1/Q3 financial holding companies and Q2 first-listed companies.
        deadline = {
            1: end + timedelta(days=60),
            2: date(year, 8, 31),
            3: end + timedelta(days=60),
            4: date(year + 1, 3, 31),
        }[quarter]
        closed = set(calendar.get("filing_closed_dates", ()))
        while deadline.weekday() >= 5 or deadline.isoformat() in closed:
            deadline += timedelta(days=1)
    if deadline <= end:
        raise ValueError("financial_deadline_must_follow_quarter_end")
    return end + timedelta(days=1), deadline + timedelta(days=1)


def schedule_plan(now, calendar, groups, *, include_board=True):
    """Return (job, refresh) pairs and groups awaiting an updated calendar."""
    if now.utcoffset() is None:
        raise ValueError("schedule_time_requires_timezone")
    now = now.astimezone(TAIPEI)
    day = now.date()
    hour = now.hour
    due, deferred = [], []
    if now.minute >= 15:
        return due, deferred
    if "daily" in groups and hour in (9, 18, 20, 22) and day.weekday() < 5:
        try:
            today = trading_days(calendar, day, 1)[0]
            if today == day.isoformat():
                if hour == 9:
                    previous = trading_days(calendar, day - timedelta(days=1), 1)[0]
                    due.extend((job, False) for job in chip_jobs(previous, include_board=include_board))
                else:
                    due.extend((job, False) for job in chip_jobs(today, include_board=include_board))
                    if hour == 22:
                        for prior in trading_days(calendar, day - timedelta(days=1), 5):
                            due.extend((job, True) for job in chip_jobs(prior, include_board=include_board))
        except ValueError as exc:
            deferred.append({"group": "daily", "reason": str(exc)})
    last_day = day.day == monthrange(day.year, day.month)[1]
    if "revenue" in groups and hour == 20 and (day.day <= 15 or last_day):
        previous = day.replace(day=1) - timedelta(days=1)
        for period in month_periods(previous.strftime("%Y-%m"), 2 if last_day else 1):
            due.extend((job, last_day) for job in revenue_jobs(period))
    if "financial" in groups and hour == 21:
        previous_quarter_end = date(day.year, (day.month - 1) // 3 * 3 + 1, 1) - timedelta(days=1)
        period = f"{previous_quarter_end.year}-Q{(previous_quarter_end.month - 1) // 3 + 1}"
        periods = []
        for candidate in reversed(quarter_periods(period, 2)):
            first, final = financial_window(candidate, calendar)
            if first <= day <= final:
                periods.append(candidate)
        if day.day == 1:
            periods.extend(quarter_periods(period, 3)[:-1])
        for candidate in dict.fromkeys(periods):
            due.extend((job, True) for job in financial_jobs(candidate))
    return due, deferred


@contextmanager
def workflow_lock(store, target):
    if not store.mysql:
        with closing(sqlite3.connect(str(target) + ".lock", timeout=0)) as lock:
            try:
                lock.execute("BEGIN EXCLUSIVE")
            except sqlite3.OperationalError as exc:
                raise ValueError("market_data_workflow_lock_unavailable") from exc
            try:
                yield
            finally:
                lock.rollback()
        return
    name = "market_data:" + hashlib.sha256(str(target).encode()).hexdigest()[:48]
    with closing(store.connection.cursor()) as cursor:
        cursor.execute("SELECT GET_LOCK(%s, 0)", (name,))
        acquired = cursor.fetchone()[0]
    store.connection.rollback()
    if acquired != 1:
        raise ValueError("market_data_workflow_lock_unavailable")
    try:
        yield
    finally:
        with closing(store.connection.cursor()) as cursor:
            cursor.execute("SELECT RELEASE_LOCK(%s)", (name,))
            cursor.fetchone()
        store.connection.rollback()


def checkpoints(store):
    with closing(store.connection.cursor()) as cursor:
        cursor.execute("""
            SELECT report_key, last_status, last_attempt_at, last_success_at, coverage_verified
            FROM market_data_checkpoints
        """)
        rows = {row[0]: row[1:] for row in cursor.fetchall()}
    store.connection.rollback()
    return rows


def backfill_page(jobs, progress, limit):
    # Give new work a turn before retrying missing/failed reports.
    new = [job for job in jobs if job.key not in progress]
    retry = [job for job in jobs if job.key in progress and not backfill_done(progress[job.key])]
    return (new + retry)[:limit]


def backfill_done(status):
    return status is not None and (status[2] is not None or status[0] in {"archived", "unchanged"})


def scheduled_page(plan, progress, now, limit):
    due = []
    for job, refresh in plan:
        status = progress.get(job.key)
        if status:
            attempted = datetime.fromisoformat(str(status[1])).replace(tzinfo=timezone.utc)
            if attempted.astimezone(TAIPEI).strftime("%Y-%m-%dT%H") == now.astimezone(TAIPEI).strftime("%Y-%m-%dT%H"):
                continue
            if status[3] and not refresh:
                continue
        due.append(job)
    return due[:limit]


def record_checkpoint(store, job, result):
    success = result.get("is_current", False)
    # An old archived snapshot never changes the checkpoint for the active version.
    store.begin()
    try:
        with closing(store.connection.cursor()) as cursor:
            store.insert_identity(cursor, "checkpoints", (
                "report_key", "dataset", "market", "period", "segment", "last_status", "last_attempt_at",
            ), (job.key, job.dataset, job.market, job.period, market_segment(job), result["status"], utc_now()))
            store.execute(cursor, """
                UPDATE market_data_checkpoints SET last_status = ?, last_attempt_at = ?, last_error = ?
                WHERE report_key = ?
            """, (result["status"], utc_now(), result.get("error", "")[:255] or None, job.key))
            if success:
                store.execute(cursor, """
                    UPDATE market_data_checkpoints SET last_success_at = ?,
                        coverage_verified = CASE WHEN accepted_raw_sha256 = ? AND coverage_verified = 1
                            THEN 1 ELSE ? END,
                        accepted_raw_sha256 = ? WHERE report_key = ?
                """, (utc_now(), result["raw_sha256"], int(result["coverage_verified"]), result["raw_sha256"], job.key))
        store.connection.commit()
    except BaseException:
        store.connection.rollback()
        raise


def fetch_report(reader, job, *, live=False, sleep=time.sleep):
    attempts = 0
    for attempt in range(3 if live else 1):
        source, raw = reader.read(job)
        attempts = attempt + 1
        status = source.get("http_status")
        blocked = source.get("source_blocked") or source.get("error") in {"blocking_page", "host_stopped_after_block"}
        transient = status in range(500, 600) or (status is None and source.get("error") in TRANSIENT_ERRORS)
        if not (live and attempt < 2 and transient and not blocked):
            break
        sleep(2 ** (attempt + 1) + random.uniform(0, 0.5))
    return source, raw, attempts


def run_jobs(store, reader, jobs, *, live=False, approved=(), coverage=None):
    run_id = str(uuid4())
    results = []
    started = utc_now()
    for job in jobs:
        result = {"report_key": job.key, "source_id": source_id(job)}
        try:
            expected = (coverage or {}).get(job.key)
            if live and result["source_id"] not in approved:
                raise ValueError("source_use_not_approved")
            constituents = False
            if job.dataset == "margin_market":
                if job.market == "TWSE":
                    expected = ("financing_lots", "short_lots", "financing_amount")
                elif job.segment == "amount":
                    expected = ("financing_amount",)
                    if live and "margin_market:TPEx:" not in approved:
                        raise ValueError("date_reference_source_use_not_approved")
                else:
                    expected = (coverage or {}).get(f"margin_TPEx_{job.period}")
                    constituents = True
            if live and expected is None:
                raise ValueError("coverage_unverified")
            source, raw, attempts = fetch_report(reader, job, live=live)
            result.update(http_status=source.get("http_status"), attempts=attempts, raw_sha256=source.get("sha256"))
            report, path = verify_report(reader, job, source, raw)
            actual = {record_key(row) for row in report["records"]}
            result["row_count_observed"] = len(actual)
            if constituents:
                actual = set(report["constituents"])
            missing = set()
            financial = job.dataset in GROUPS["financial"]
            if expected is not None:
                expected_keys = set(expected)
                missing = expected_keys - actual
                result.update(expected_rows=len(expected_keys), missing_rows=len(missing))
                if missing and not financial:
                    raise ValueError(f"coverage_missing: {len(missing)} expected rows")
            result["coverage_verified"] = expected is not None and not missing
            if financial and (live or expected is not None):
                report["latest_quarter_partial"] = not result["coverage_verified"]
            result.update(store.import_report(job, report, path))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            result.update(status="pending", error=str(exc))
        except Exception as exc:
            result.update(status="database_error", error=type(exc).__name__)
        try:
            record_checkpoint(store, job, result)
            store.log_result(run_id, result)
        except Exception as exc:
            result["checkpoint_error"] = type(exc).__name__
        results.append(result)
        if result["status"] == "database_error" or "checkpoint_error" in result:
            break
    return {"run_id": run_id, "started_at_utc": started, "finished_at_utc": utc_now(), "results": results}
