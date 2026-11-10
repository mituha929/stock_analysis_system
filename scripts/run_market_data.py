"""Plan or execute bounded market-data updates, manual replays and cached backfill."""
import argparse
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime
import json
import math
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from scripts.collect_market_history import (
    DEFAULT_OUTPUT, PROJECT, REQUEST_INTERVAL, TAIPEI, Job, Reader, chip_jobs, financial_jobs, revenue_jobs, save_json,
)
from scripts.import_market_data import connect_mysql
from scripts.market_data_store import MarketDataStore, sqlite_schema
from scripts.market_data_workflow import (
    GROUPS, WORKFLOW_DDL, backfill_done, backfill_page, checkpoints, run_jobs,
    scheduled_page, schedule_plan, trading_days, workflow_lock,
)

OUTPUT = DEFAULT_OUTPUT.parent / "market_data_p345"
TEST_DB = OUTPUT / "workflow.sqlite3"
CALENDAR = PROJECT / "data" / "stock_market_holidays.json"
MAX_BATCH = 50


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def validate_period(value, group):
    if group == "daily":
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("daily_period_requires_YYYY_MM_DD")
    elif group == "revenue" and not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("revenue_period_requires_YYYY_MM")
    elif group == "financial" and not re.fullmatch(r"[0-9]{4}-Q[1-4]", value):
        raise ValueError("financial_period_requires_YYYY_QN")


def cached_jobs(args):
    manifest = [Job(**item) for item in read_json(DEFAULT_OUTPUT / "jobs.json")]
    by_key = {job.key: job for job in manifest}
    for job in manifest:
        if job.dataset == "margin":
            summary = replace(job, dataset="margin_market")
            by_key.setdefault(summary.key, summary)
    if args.mode == "manual":
        return [by_key[key] for key in dict.fromkeys(args.report)]
    validate_period(args.start, args.group)
    validate_period(args.end, args.group)
    eligible = [job for job in by_key.values() if job.dataset in GROUPS[args.group]]
    first, last = min(job.period for job in eligible), max(job.period for job in eligible)
    if not first <= args.start <= args.end <= last:
        raise ValueError(f"backfill_bounds_outside_verified_cache: {first} to {last}")
    jobs = [job for job in eligible if args.start <= job.period <= args.end]
    if not jobs:
        raise ValueError("no_cached_reports_in_requested_range")
    return jobs


def load_coverage(path):
    manifest = read_json(path)
    if not isinstance(manifest, dict) or any(
        not isinstance(rows, list) or not rows or not all(isinstance(row, str) and row.strip() for row in rows)
        or len(set(rows)) != len(rows) for rows in manifest.values()
    ):
        raise ValueError("coverage_requires_nonempty_unique_record_keys_per_report")
    return manifest


def manual_jobs(args, now):
    if args.report:
        return cached_jobs(args)
    validate_period(args.period, args.group)
    today = now.astimezone(TAIPEI).date()
    if args.group == "daily":
        day = date.fromisoformat(args.period)
        if day > today or trading_days(read_json(args.calendar), day, 1)[0] != args.period:
            raise ValueError("manual_period_requires_verified_trading_day")
    elif args.group == "revenue" and args.period >= today.strftime("%Y-%m"):
        raise ValueError("manual_month_not_ended")
    elif args.group == "financial" and args.period >= f"{today.year:04d}-Q{(today.month - 1) // 3 + 1}":
        raise ValueError("manual_quarter_not_ended")
    builders = {"daily": lambda period: chip_jobs(period, include_board=args.live or not args.run),
                "revenue": revenue_jobs, "financial": financial_jobs}
    return [job for job in builders[args.group](args.period) if not args.market or job.market == args.market]


def run(args):
    now = datetime.fromisoformat(args.at) if args.at else datetime.now(TAIPEI)
    if now.utcoffset() is None:
        raise ValueError("--at requires an explicit UTC offset")
    if args.mode == "scheduled":
        planned, deferred = schedule_plan(now, read_json(args.calendar), {args.group} if args.group else GROUPS,
                                          include_board=args.live or not args.run)
        jobs = [job for job, _ in planned]
    elif args.mode == "manual":
        jobs, deferred = manual_jobs(args, now), []
    else:
        jobs, deferred = cached_jobs(args), []
    if not args.run:
        print(json.dumps({"mode": "plan", "planned": len(jobs), "deferred": deferred,
                          "next_report_keys": [job.key for job in jobs[:args.limit]]}, ensure_ascii=False))
        return 0
    approved, coverage = [], {}
    if args.live:
        approved = read_json(args.approved_sources)
        if not isinstance(approved, list) or not approved or not all(isinstance(key, str) for key in approved):
            raise ValueError("approved_sources_requires_a_list_of_source_ids")
    if args.coverage:
        coverage = load_coverage(args.coverage)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    target = args.confirm_database if args.apply_mysql else TEST_DB.resolve()
    connection = connect_mysql(target) if args.apply_mysql else sqlite3.connect(TEST_DB, timeout=5)
    with closing(connection):
        store = MarketDataStore(connection, mysql=args.apply_mysql)
        with workflow_lock(store, target):
            if not args.apply_mysql:
                connection.executescript(sqlite_schema() + sqlite_schema(WORKFLOW_DDL))
            progress = checkpoints(store)
            if args.mode == "scheduled":
                selected = scheduled_page(planned, progress, now, args.limit)
            elif args.mode == "backfill":
                selected = backfill_page(jobs, progress, args.limit)
            else:
                selected = jobs[:args.limit]
            if args.live and selected:
                reader = Reader(OUTPUT / "live" / uuid4().hex, interval=args.interval,
                                retry_transient_errors=True, retry_server_statuses=range(500, 600))
                reader.block_file = OUTPUT / "blocked_hosts.json"
                reader.blocked = set(read_json(reader.block_file)) if reader.block_file.exists() else set()
            else:
                reader = Reader(DEFAULT_OUTPUT, offline=True)
            result = run_jobs(store, reader, selected, live=args.live, approved=approved, coverage=coverage)
            if args.mode == "backfill":
                progress = checkpoints(store)
                result["remaining_reports"] = sum(not backfill_done(progress.get(job.key)) for job in jobs)
            result.update(mode=args.mode, planned_reports=len(jobs), selected_reports=len(selected), deferred=deferred)
            folder = OUTPUT / ("mysql_runs" if args.apply_mysql else "sqlite_runs")
            folder.mkdir(exist_ok=True)
            save_json(folder / f"{result['run_id']}.json", result)
            errors = sum(r["status"] in {"pending", "database_error"} or "checkpoint_error" in r
                         for r in result["results"])
            print(json.dumps({k: v for k, v in result.items() if k != "results"} | {"pending_or_errors": errors},
                             ensure_ascii=False))
            return int(bool(errors or deferred))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("scheduled", "backfill", "manual"))
    parser.add_argument("--run", action="store_true", help="Execute the plan; offline SQLite by default")
    parser.add_argument("--group", choices=GROUPS)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--report", action="append")
    parser.add_argument("--period", help="Manual period, used with --group instead of --report")
    parser.add_argument("--market", choices=("TWSE", "TPEx"), help="Optional market for a manual period")
    parser.add_argument("--at", help="Simulated ISO timestamp with UTC offset for plan/replay")
    parser.add_argument("--calendar", type=Path, default=CALENDAR)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--live", action="store_true", help="Request explicitly approved official sources")
    parser.add_argument("--approved-sources", type=Path)
    parser.add_argument("--coverage", type=Path)
    parser.add_argument("--interval", type=float, default=REQUEST_INTERVAL)
    parser.add_argument("--apply-mysql", action="store_true")
    parser.add_argument("--confirm-database")
    args = parser.parse_args()
    if not 1 <= args.limit <= MAX_BATCH:
        parser.error(f"--limit must be 1 to {MAX_BATCH}")
    if not math.isfinite(args.interval) or args.interval < REQUEST_INTERVAL:
        parser.error("--interval must be at least 1 second")
    if args.mode == "backfill" and not (args.group and args.start and args.end):
        parser.error("backfill requires --group, --start and --end")
    if (args.start or args.end) and args.mode != "backfill":
        parser.error("--start and --end are for backfill")
    if args.mode == "manual":
        if bool(args.report) == bool(args.period) or (args.period and not args.group) or (args.market and not args.period):
            parser.error("manual requires --report or --group with --period")
    elif args.report or args.period or args.market:
        parser.error("--report, --period and --market are for manual mode")
    if args.report and len(args.report) > args.limit:
        parser.error("manual report count exceeds --limit")
    if args.apply_mysql != bool(args.confirm_database) or (args.apply_mysql and not args.run):
        parser.error("MySQL writes require --run --apply-mysql --confirm-database")
    if args.live and (not args.run or not args.approved_sources or not args.coverage or args.at or args.mode == "backfill"):
        parser.error("--live requires --run, source approval and coverage manifests, and a real-time scheduled/manual run")
    try:
        return run(args)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
