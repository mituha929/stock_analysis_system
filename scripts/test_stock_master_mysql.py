"""Isolated MySQL acceptance; only the UUID test schema created here is removed."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import asyncio
import hashlib
import json
import re
import sys
import uuid
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from app.services.mysql_instrument_repository import MySQLSettings
from app.services.stock_master_sources import TAIPEI, StockRecord, StockSnapshot, StockSourceError
from app.services.stock_master_repository import StockMasterRepository, StockSyncError


def run_checks(repo):
    checks = []
    now = datetime.now(timezone.utc)
    today = now.astimezone(TAIPEI).date()
    old = today - timedelta(days=1000)
    def record(code, market="TWSE", listed=old):
        return StockRecord(code, "測試" + code, market, listed, "半導體業", "24", code.zfill(8))
    records = {"TWSE": [record("0050"), record("2330"), record("1101"), record("1234", listed=None), record("9999", listed=today+timedelta(days=1))],
               "TPEx": [record(code,"TPEx") for code in ("6488","3105","8069","3529","9998")]}
    def fetch():
        return [StockSnapshot(market, today, tuple(rows)) for market, rows in records.items()]
    def sync(**kwargs):
        return repo.synchronize(fetcher=fetch, trigger="test", now=now, **kwargs)
    def scalar(sql, params=()):
        connection = repo._connect_database(repo._connector())
        cursor = connection.cursor()
        try:
            cursor.execute(sql, params)
            return cursor.fetchone()[0]
        finally:
            connection.rollback()
            cursor.close()
            connection.close()
    first = sync()
    assert first["new"] == 9 and first["scheduled"] == 1
    checks.append("both_markets_atomic_initial_import")
    assert repo.profile("0050")["code"] == "0050"
    checks.append("leading_zero_code_preserved")
    assert repo.profile("1234")["listing_date"] is None
    checks.append("missing_date_is_null_not_incorporation")
    try:
        repo.profile("9999")
    except KeyError:
        pass
    else:
        raise AssertionError("Future issuer entered current master")
    assert repo.listing_plans("9999")["items"][0]["planned_listing_date"] == (today+timedelta(days=1)).isoformat()
    checks.append("future_listing_separate_from_master")
    history_count = scalar("SELECT COUNT(*) FROM stock_master_history")
    updated_at = repo.profile("2330")["updated_at"]
    again = sync()
    assert again["new"] == 0 and again["changed"] == 0 and again["unchanged"] == 9
    assert scalar("SELECT COUNT(*) FROM stock_master") == 9
    assert scalar("SELECT COUNT(*) FROM stock_master_history") == history_count
    assert repo.profile("2330")["updated_at"] == updated_at
    checks.extend(["repeat_upsert_no_duplicate", "unchanged_profile_timestamp_stable", "unchanged_not_duplicate_history"])
    corrected = old + timedelta(days=1)
    records["TWSE"][1] = replace(records["TWSE"][1], name="測試更名", listing_date=corrected)
    changed = sync()
    assert changed["changed"] == 1 and repo.profile("2330")["listing_date"] == corrected.isoformat()
    history = repo.history("2330")["items"]
    assert history[0]["before"]["listing_date"] == old.isoformat() and history[0]["after"]["name"] == "測試更名"
    checks.append("corrections_preserve_before_after_history")
    records["TWSE"][1] = replace(records["TWSE"][1], listing_date=None)
    retained = sync()
    profile = repo.profile("2330")
    assert retained["retained_dates"] == 1 and profile["listing_date"] == corrected.isoformat()
    assert profile["source"]["date_status"] == "retained_previous_official" and profile["source"]["reported_listing_date"] is None
    checks.append("blank_cannot_erase_known_official_date")

    # Two-market observation, not an invented delisting date or original trading date.
    records["TWSE"] = [row for row in records["TWSE"] if row.code != "1101"]
    records["TWSE"].append(record("8069", listed=today))
    records["TPEx"] = [row for row in records["TPEx"] if row.code != "8069"]
    moved = sync()
    assert moved["newly_absent"] == 2
    assert repo.profile("1101")["source"]["presence_status"] == "not_in_latest"
    profile = repo.profile("8069")
    assert profile["market"] == "TWSE" and profile["listing_date"] == today.isoformat()
    assert repo.profile("8069", "TPEx")["listing_date"] == old.isoformat()
    assert len(profile["market_history"]) == 2
    checks.extend(["absence_retained_not_deleted_or_claimed_delisted", "otc_to_listed_dates_separate", "historical_market_query"])

    rows_before, changes_before = scalar("SELECT COUNT(*) FROM stock_master"), scalar("SELECT COUNT(*) FROM stock_master_history")
    import app.services.stock_master_repository as module
    real_write, calls = module._write_row, []
    def broken_write(*args):
        real_write(*args)
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("intentional test SQL failure AFTER real writes")
    records["TWSE"][0] = replace(records["TWSE"][0], name="不應提交的名稱")
    with patch.object(module,"_write_row",side_effect=broken_write):
        try:
            sync()
        except StockSyncError as error:
            assert not error.commit_uncertain
            failed_run = error.run_id
        else:
            raise AssertionError("Broken transaction committed")
    assert scalar("SELECT COUNT(*) FROM stock_master") == rows_before
    assert scalar("SELECT COUNT(*) FROM stock_master_history") == changes_before
    assert repo.profile("0050")["name"] == "測試0050"
    assert next(r for r in repo.sync_runs(100)["items"] if r["id"] == failed_run)["status"] == "failed"
    records["TWSE"][0] = replace(records["TWSE"][0], name="測試0050")
    checks.extend(["real_profile_and_history_writes_rolled_back_together", "failure_reason_persists_after_rollback"])

    try:
        repo.synchronize(fetcher=lambda: (_ for _ in ()).throw(StockSourceError("TPEx 官方欄位改變")), trigger="test", now=now)
    except StockSyncError as error:
        source_failure_run = error.run_id
    assert scalar("SELECT COUNT(*) FROM stock_master") == rows_before
    assert "TPEx" in next(r for r in repo.sync_runs(100)["items"] if r["id"] == source_failure_run)["error_message"]
    checks.append("source_failure_keeps_data_and_records_error")
    reduced = [replace(s, records=s.records[:1]) for s in fetch()]
    try:
        repo.synchronize(fetcher=lambda: reduced, trigger="test", now=now)
    except StockSyncError:
        pass
    else:
        raise AssertionError("Large roster shrink accepted")
    checks.append("roster_shrink_protection")
    regressed = [replace(s, source_date=today-timedelta(days=1)) for s in fetch()]
    try:
        repo.synchronize(fetcher=lambda: regressed, trigger="test", now=now)
    except StockSyncError:
        pass
    else:
        raise AssertionError("Older source overwritten current data")
    checks.append("source_report_date_regression_rejected")
    key = "weekly:" + today.isoformat()
    scheduled = repo.synchronize(fetcher=fetch, trigger="weekly", schedule_key=key, now=now)
    assert scheduled["status"] == "completed"
    with patch.object(module, "fetch_snapshots", side_effect=AssertionError("must not refetch")):
        repeated = repo.synchronize(fetcher=lambda: (_ for _ in ()).throw(AssertionError("must not fetch")), trigger="weekly", schedule_key=key, now=now)
    assert repeated["status"] == "already_completed"
    checks.append("weekly_success_deduplicated_across_repository_instances")
    con = repo._connect_database(repo._connector())
    cur = con.cursor()
    lock = "stock-master:"+hashlib.sha256(repo.settings.database.encode()).hexdigest()[:32]
    try:
        cur.execute("SELECT GET_LOCK(%s,0)",(lock,))
        assert cur.fetchone()[0] == 1
        assert sync()["status"] == "busy"
    finally:
        cur.execute("SELECT RELEASE_LOCK(%s)",(lock,))
        cur.fetchone()
        cur.close()
        con.close()
    checks.append("cross_process_advisory_lock_prevents_overlap")

    # Real SQL -> real ASGI routing/serialization, without starting a server or DLL.
    from app.main import app
    from app.api.stock_profiles import repository
    async def get(path):
        output=[]
        async def receive():return {"type":"http.request","body":b"","more_body":False}
        async def send(message):output.append(message)
        await app({"type":"http","asgi":{"version":"3.0"},"http_version":"1.1","method":"GET","scheme":"http",
                   "path":path,"raw_path":path.encode(),"query_string":b"","headers":[],"client":("127.0.0.1",1),
                   "server":("test",80),"root_path":""},receive,send)
        return output[0]["status"],json.loads(b"".join(m.get("body",b"") for m in output[1:]))
    app.dependency_overrides[repository] = lambda: repo
    try:
        status, body = asyncio.run(get("/api/stocks/8069/profile"))
        assert status == 200 and body["market"] == "TWSE" and len(body["market_history"]) == 2
        assert body["updated_at"].endswith("+08:00")
    finally:
        app.dependency_overrides.pop(repository,None)
    checks.append("fastapi_profile_real_mysql_and_taipei_serialization")
    return checks


def main():
    from dotenv import load_dotenv
    load_dotenv(PROJECT/".env",override=False)
    settings=MySQLSettings.from_env()
    schema="stock_master_test_"+uuid.uuid4().hex
    if not re.fullmatch(r"stock_master_test_[0-9a-f]{32}",schema) or schema==settings.database:
        raise RuntimeError("Unsafe schema")
    import mysql.connector
    con=mysql.connector.connect(host=settings.host,port=settings.port,user=settings.user,password=settings.password,connection_timeout=10)
    cursor=con.cursor()
    created=False
    try:
        cursor.execute(f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        created=True
        checks=run_checks(StockMasterRepository(replace(settings,database=schema)))
    finally:
        if created:
            if not re.fullmatch(r"stock_master_test_[0-9a-f]{32}",schema) or schema==settings.database:
                raise RuntimeError("Unsafe cleanup refused")
            cursor.execute(f"DROP DATABASE `{schema}`")
        cursor.close()
        con.close()
    print(json.dumps({"status":"passed","checks":checks,"test_schema_removed":created,"production_rows_written":0},ensure_ascii=False))


if __name__=="__main__":
    try:
        main()
    except Exception as error:
        # No credentials/connector tracebacks, but include safe test failure class.
        import traceback
        frame=traceback.extract_tb(error.__traceback__)[-1]
        print(json.dumps({"status":"failed","error_type":type(error).__name__,"line":frame.lineno,
                          "function":frame.name,"reason":str(error) if isinstance(error,StockSyncError) else None}),file=sys.stderr)
        raise SystemExit(1)
