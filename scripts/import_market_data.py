"""Import verified report versions from the local cache. SQLite is the default."""
import argparse
from contextlib import closing
import json
import re
import sqlite3
from uuid import uuid4

from scripts.collect_market_history import DEFAULT_OUTPUT, PROJECT, Reader, save_json
from scripts.market_data_store import MYSQL_DDL, MarketDataStore, sqlite_schema
from scripts.stage_market_data import load_report, pilot_jobs

OUTPUT = DEFAULT_OUTPUT.parent / "market_data_p2"


def connect_mysql(expected_database):
    from dotenv import load_dotenv
    import mysql.connector

    from app.services.mysql_instrument_repository import MySQLSettings

    load_dotenv(PROJECT / ".env", override=False)
    settings = MySQLSettings.from_env()
    if expected_database != settings.database:
        raise ValueError("confirmed_database_does_not_match_configuration")
    connection = mysql.connector.connect(
        host=settings.host, port=settings.port, user=settings.user, password=settings.password,
        database=settings.database, charset="utf8mb4", autocommit=False, connection_timeout=10,
    )
    try:
        required = set(re.findall(r"CREATE TABLE IF NOT EXISTS (market_data_\w+)", MYSQL_DDL.read_text(encoding="utf-8")))
        with closing(connection.cursor()) as cursor:
            cursor.execute("SELECT DATABASE()")
            if cursor.fetchone()[0] != expected_database:
                raise ValueError("unexpected_connected_database")
            cursor.execute("SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s",
                           (expected_database,))
            engines = dict(cursor.fetchall())
            if any(engines.get(table) != "InnoDB" for table in required):
                raise ValueError("approved_mysql_migration_required")
        connection.rollback()
    except BaseException:
        connection.close()
        raise
    return connection


def import_batches(store, reader, jobs):
    run_id = str(uuid4())
    results = []
    for job in jobs:
        result = {"report_key": job.key}
        try:
            report, raw_path = load_report(reader, job)
            result["raw_sha256"] = report["source"]["sha256"]
            result.update(store.import_report(job, report, raw_path))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            result.update(status="rejected", error=str(exc))
        except Exception as exc:
            result.update(status="database_error", error=type(exc).__name__)
        try:
            store.log_result(run_id, result)
        except Exception as exc:
            result["log_error"] = type(exc).__name__
        results.append(result)
        if result["status"] == "database_error" or "log_error" in result:
            break
    return run_id, results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--print-mysql-ddl", action="store_true", help="Print the migration SQL for review")
    action.add_argument("--apply-mysql", action="store_true", help="Write approved reports to a pre-provisioned MySQL schema")
    parser.add_argument("--confirm-database", help="Exact MYSQL_DATABASE to write")
    parser.add_argument("--report", action="append", help="Select a report key from the P1 pilot (repeatable)")
    args = parser.parse_args()
    if args.print_mysql_ddl:
        print(MYSQL_DDL.read_text(encoding="utf-8"), end="")
        return 0
    if args.apply_mysql and not (args.confirm_database and args.report):
        parser.error("--apply-mysql requires --confirm-database and explicit --report selection")
    if args.confirm_database and not args.apply_mysql:
        parser.error("--confirm-database requires --apply-mysql")
    pilot = {job.key: job for job in pilot_jobs()}
    selected = list(dict.fromkeys(args.report or pilot))
    if any(key not in pilot for key in selected):
        parser.error("unknown report key; valid keys: " + ", ".join(pilot))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    try:
        connection = connect_mysql(args.confirm_database) if args.apply_mysql else sqlite3.connect(OUTPUT / "import.sqlite3")
        with closing(connection):
            if not args.apply_mysql:
                connection.executescript(sqlite_schema())
            run_id, results = import_batches(MarketDataStore(connection, mysql=args.apply_mysql),
                                             Reader(DEFAULT_OUTPUT, offline=True), [pilot[key] for key in selected])
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__}))
        return 1
    errors = sum(result["status"] in {"rejected", "database_error"} or "log_error" in result for result in results)
    summary = {
        "run_id": run_id, "database": "mysql" if args.apply_mysql else "sqlite",
        "reports": len(results), "requested_reports": len(selected), "errors": errors,
        "promoted": sum(result["status"] == "promoted" for result in results),
        "unchanged": sum(result["status"] == "unchanged" for result in results),
        "rows": sum(result.get("rows", 0) for result in results),
        "financial_facts": sum(result.get("facts", 0) for result in results),
        "flagged_rows": sum(result.get("flagged_rows", 0) for result in results),
    }
    save_json(OUTPUT / ("mysql_results.json" if args.apply_mysql else "results.json"),
              {"summary": summary, "reports": results})
    print(json.dumps(summary))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
