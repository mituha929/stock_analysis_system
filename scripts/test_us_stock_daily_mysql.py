"""Synthetic real-MySQL acceptance in an owned UUID schema; always clean it up."""
from dataclasses import replace
from datetime import date, timedelta
import json
from pathlib import Path
import re
import sys
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))

from app.services.mysql_instrument_repository import MySQLSettings
from app.services.us_stock_daily_repository import TABLE, UPDATES, USStockDailyRepository, StockDailyWriteError
from app.services.us_stock_daily_scheduler import USStockDailyScheduler, USStockDailySchedule
from test_us_stock_daily import batch, NOW, TARGET


def main():
    from dotenv import load_dotenv
    load_dotenv(PROJECT / ".env", override=False)
    settings = MySQLSettings.from_env()
    schema = "us_daily_test_" + uuid.uuid4().hex
    if not re.fullmatch(r"us_daily_test_[0-9a-f]{32}", schema) or schema == settings.database:
        raise RuntimeError("Unsafe test schema")
    import mysql.connector
    admin = mysql.connector.connect(host=settings.host, port=settings.port, user=settings.user,
                                    password=settings.password, connection_timeout=10)
    cursor = admin.cursor()
    created = False
    checks = []
    try:
        cursor.execute("CREATE DATABASE `"+schema+"` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        created = True
        repo = USStockDailyRepository(replace(settings, database=schema))
        assert repo.completed_symbols(TARGET) == set()
        checks.append("missing_completion_table_read_has_no_ddl")
        original = batch()
        saved = repo.save(original)
        assert saved["status"] == "completed" and saved["bar_count"] == 250
        checks.append("atomic_250_bars_and_daily_completion")

        def read(query, values=()):
            con = repo._connect_database(repo._connector());q = con.cursor(dictionary=True)
            try:
                q.execute(query, values)
                return q.fetchall()
            finally:
                con.rollback();q.close();con.close()

        actual = read("SELECT * FROM external_us_stock_daily WHERE symbol='TSM' ORDER BY market_date")
        assert len(actual) == 250
        for observed, expected in zip(actual, original.rows):
            assert all(observed[key] == value for key, value in expected.items())
        assert all(row["batch_id"] == saved["batch_id"] for row in actual)
        checks.extend(["separate_connection_exact_all_fields_readback", "large_unsigned_volume", "decimal_utc_json_roundtrip"])
        duplicate = repo.save(original)
        assert duplicate["status"] == "already_completed" and duplicate["batch_id"] == saved["batch_id"]
        assert len(read("SELECT id FROM external_us_stock_daily")) == 250
        assert repo.completed_symbols(TARGET) == {"TSM"}
        checks.append("same_market_date_restart_idempotency")

        next_day = date(2026,11,3)
        newer = batch(target=next_day, now=NOW+timedelta(days=1))
        assert repo.save(newer)["status"] == "completed"
        assert len(read("SELECT id FROM external_us_stock_daily WHERE symbol='TSM'")) == 251
        assert read("SELECT COUNT(*) AS n FROM external_us_stock_daily_updates")[0]["n"] == 2
        checks.extend(["overlap_updated_without_duplicates", "older_than_current_window_preserved"])

        class WrongReadCursor:
            def __init__(self, wrapped):self.wrapped=wrapped
            def __getattr__(self,key):return getattr(self.wrapped,key)
            def fetchall(self):
                rows=self.wrapped.fetchall()
                if rows and isinstance(rows[0],dict) and "source_record_json" in rows[0] and "id" not in rows[0]:
                    rows[0]={**rows[0],"volume":1}
                return rows
        class WrongReadConnection:
            def __init__(self,wrapped):self.wrapped=wrapped
            def __getattr__(self,key):return getattr(self.wrapped,key)
            def cursor(self,*args,**kwargs):return WrongReadCursor(self.wrapped.cursor(*args,**kwargs))
        broken=USStockDailyRepository(replace(settings,database=schema));connect=broken._connect_database
        broken._connect_database=lambda connector:WrongReadConnection(connect(connector))
        try:
            broken.save(batch("NVDA"))
        except StockDailyWriteError as error:
            assert not error.commit_uncertain
        else:raise AssertionError("Readback mismatch was committed")
        assert read("SELECT COUNT(*) AS n FROM external_us_stock_daily WHERE symbol='NVDA'")[0]["n"] == 0
        assert "NVDA" not in repo.completed_symbols(TARGET)
        checks.append("readback_error_rolls_back_prices_and_completion")

        class LostCommitReply:
            def __init__(self,wrapped):self.wrapped=wrapped;self.commits=0
            def __getattr__(self,key):return getattr(self.wrapped,key)
            def commit(self):
                self.commits+=1;self.wrapped.commit()
                if self.commits==2:raise OSError("synthetic lost commit reply")
        uncertain=USStockDailyRepository(replace(settings,database=schema));connect2=uncertain._connect_database
        uncertain._connect_database=lambda connector:LostCommitReply(connect2(connector))
        try:uncertain.save(batch("NVDA"))
        except StockDailyWriteError as error:assert error.commit_uncertain
        else:raise AssertionError("Expected ambiguous commit")
        assert "NVDA" in repo.completed_symbols(TARGET)
        assert repo.save(batch("NVDA"))["status"]=="already_completed"
        assert read("SELECT COUNT(*) AS n FROM external_us_stock_daily WHERE symbol='NVDA'")[0]["n"]==250
        checks.append("lost_commit_reply_recovers_without_duplicate_write")
        assert repo.save(batch("CHT"))["status"]=="completed"
        checks.append("one_stock_failure_does_not_block_other_stock")

        # Incompatible unique keys must fail rather than silently append duplicates.
        cursor.execute("ALTER TABLE `"+schema+"`.`"+UPDATES+"` DROP PRIMARY KEY")
        try:repo.save(batch("AMD"))
        except StockDailyWriteError as error:assert not error.commit_uncertain
        else:raise AssertionError("Missing completion key accepted")
        assert read("SELECT COUNT(*) AS n FROM external_us_stock_daily WHERE symbol='AMD'")[0]["n"]==0
        checks.append("incompatible_completion_schema_preserves_prices")
    finally:
        if created:
            if not re.fullmatch(r"us_daily_test_[0-9a-f]{32}",schema) or schema==settings.database:
                raise RuntimeError("Unsafe cleanup refused")
            cursor.execute("DROP DATABASE `"+schema+"`")
        cursor.close();admin.close()
    print(json.dumps(dict(status="passed",checks=checks,check_count=len(checks),test_schema_deleted=created,
                          production_data_modified=False),ensure_ascii=False,indent=2))


if __name__=="__main__":main()
