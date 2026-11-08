"""Isolated real-MySQL acceptance; synthetic quotes ONLY in an owned temporary schema."""
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
import json
import re
import sys
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from app.services.external_market_quotes import CNYES_PAGE, CMONEY_PAGE, INDEX_NAMES, MarketQuote, QuoteFetchError
from app.services.external_market_quote_repository import COLUMNS, TABLE, ExternalMarketQuoteRepository, QuoteWriteError, quote_row
from app.services.mysql_instrument_repository import MySQLSettings


def main():
    from dotenv import load_dotenv
    load_dotenv(PROJECT / ".env", override=False)
    settings = MySQLSettings.from_env()
    schema = "external_quote_test_" + uuid.uuid4().hex
    if not re.fullmatch(r"external_quote_test_[0-9a-f]{32}", schema) or schema == settings.database:
        raise RuntimeError("Unsafe test schema")
    import mysql.connector
    admin = mysql.connector.connect(host=settings.host, port=settings.port, user=settings.user,
                                    password=settings.password, connection_timeout=10)
    cursor = admin.cursor()
    created = False
    checks = []
    try:
        cursor.execute(f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        created = True
        repo = ExternalMarketQuoteRepository(replace(settings, database=schema))
        now = datetime.now(timezone.utc)
        quotes = [MarketQuote("cnyes", symbol, name, "index", CNYES_PAGE, now, now,
                              Decimal("100.12345678"), Decimal("-1.5"), Decimal("-1.48"),
                              Decimal("101"), Decimal("102"), Decimal("99"))
                  for symbol, name in INDEX_NAMES.items()]
        quotes.append(MarketQuote("cmoney", "TXF1", "測試台指期夜盤", "night", CMONEY_PAGE, now, None,
                                   Decimal("100"), Decimal("-1"), Decimal("-0.99"), Decimal("101"),
                                   Decimal("102"), Decimal("99"), Decimal("16.89"), 35705))
        first = repo.save_batch(quotes)
        rows = repo.read_batch(first)
        expected = sorted([quote_row(q) for q in quotes], key=lambda row: (row[0], row[1], row[3]))
        assert [tuple(row[c] for c in COLUMNS) for row in rows] == expected
        checks.extend(["five_rows_committed", "exact_decimal_roundtrip", "negative_change_preserved",
                       "percent_points_preserved", "utc_microseconds_preserved", "night_timestamp_null", "all_fields_postcommit_readback"])
        second = repo.save_batch(quotes)
        assert first != second and len(repo.read_batch(second)) == 5
        checks.append("append_history_separate_batches")
        try:
            repo.save_batch(quotes[:-1])
        except QuoteFetchError:
            pass
        else:
            raise AssertionError("Partial batch accepted")
        checks.append("partial_batch_rejected")

        # Intentionally fail the pre-commit readback AFTER the real INSERT.
        class BadReadCursor:
            def __init__(self, wrapped): self.wrapped = wrapped
            def __getattr__(self, key): return getattr(self.wrapped, key)
            def fetchall(self):
                self.wrapped.fetchall()  # Drain the real SELECT before rollback/close.
                return []
        class BadReadConnection:
            def __init__(self, wrapped): self.wrapped = wrapped
            def __getattr__(self, key): return getattr(self.wrapped, key)
            def cursor(self, *args, **kwargs): return BadReadCursor(self.wrapped.cursor(*args, **kwargs))
        failed_repo = ExternalMarketQuoteRepository(replace(settings, database=schema))
        real_connect = failed_repo._connect_database
        failed_repo._connect_database = lambda connector: BadReadConnection(real_connect(connector))
        try:
            failed_repo.save_batch(quotes)
        except QuoteWriteError as error:
            assert not error.commit_uncertain
        else:
            raise AssertionError("Readback mismatch committed")
        connection = repo._connect_database(repo._connector())
        verify = connection.cursor()
        try:
            verify.execute(f"SELECT COUNT(*) FROM `{TABLE}`")
            assert verify.fetchone()[0] == 10
        finally:
            verify.close()
            connection.close()
        checks.append("real_insert_rollback_leaves_only_ten_good_rows")

        # Per-product checks keep source failures independent and suppress equal snapshots.
        def count_snapshots():
            read = repo._connect_database(repo._connector())
            query = read.cursor()
            try:
                query.execute(f"SELECT COUNT(*) FROM `{TABLE}`")
                return query.fetchone()[0]
            finally:
                query.close(); read.close()
        quality = dict(market_session_key="NYSE:2026-11-02", close_pending=False, freshness="current_session")
        index_quote = quotes[0]
        result = repo.save_observation(index_quote, "isolated:slot1", quality)
        assert result["status"] == "unchanged" and count_snapshots() == 10
        checks.append("unchanged_quote_only_adds_check")
        assert repo.save_observation(index_quote, "isolated:slot1", quality)["status"] == "already_checked"
        checks.append("database_schedule_key_idempotency")
        newer = replace(index_quote, fetched_at=now+timedelta(seconds=2), quote_at=now+timedelta(seconds=1))
        assert repo.save_observation(newer, "isolated:slot2", quality)["status"] == "updated"
        assert count_snapshots() == 11
        checks.append("same_price_new_source_time_is_new_quote")
        regressed = replace(index_quote, fetched_at=now+timedelta(seconds=4))
        assert repo.save_observation(regressed, "isolated:slot3", quality)["status"] == "source_regressed"
        assert count_snapshots() == 11
        checks.append("regressed_source_time_does_not_replace_newer_quote")
        night_quality = dict(market_session_key="TAIFEX:2026-11-02",close_pending=False,freshness="unknown_time")
        assert repo.save_observation(quotes[-1], "isolated:night1", night_quality)["status"] == "updated"
        assert repo.save_observation(quotes[-1], "isolated:night2", night_quality)["status"] == "unchanged"
        assert count_snapshots() == 12
        checks.append("unknown_night_time_dedup_only_within_expected_session")
        assert repo.save_observation(quotes[-1], "isolated:night3", {**night_quality,"market_session_key":"TAIFEX:2026-11-03"})["status"] == "updated"
        assert count_snapshots() == 13
        checks.append("different_night_session_preserved_even_if_values_equal")
        repo.record_failure(("cmoney","TXF1","night"), "isolated:failure", now+timedelta(seconds=10), "QuoteFetchError")
        other = repo.save_observation(quotes[1], "isolated:independent", quality)
        assert other["status"] == "unchanged"
        checks.append("night_failure_does_not_block_index_check")
        latest = repo.latest()
        assert len(latest) == 5
        index_latest = next(r for r in latest if r["symbol"] == index_quote.symbol)
        assert index_latest["snapshot"]["quote_at_utc"] == newer.quote_at.replace(tzinfo=None)
        assert next(r for r in latest if r["symbol"] == "TXF1")["last_check"]["status"] == "failed"
        checks.append("latest_reports_snapshot_and_failed_check_separately")
        # A source parser failure never affects another product's committed quote.
        before_count = count_snapshots()
        class WrongObservationCursor:
            def __init__(self, wrapped): self.wrapped = wrapped
            def __getattr__(self, key): return getattr(self.wrapped,key)
            def fetchone(self):
                row = self.wrapped.fetchone()
                if isinstance(row,dict) and set(COLUMNS).issubset(row) and "id" not in row:
                    return {**row,"last_price":Decimal("9999")}
                return row
        class WrongObservationConnection:
            def __init__(self,wrapped): self.wrapped=wrapped
            def __getattr__(self,key): return getattr(self.wrapped,key)
            def cursor(self,*args,**kwargs): return WrongObservationCursor(self.wrapped.cursor(*args,**kwargs))
        broken = ExternalMarketQuoteRepository(replace(settings,database=schema))
        connect_observation = broken._connect_database
        broken._connect_database = lambda connector: WrongObservationConnection(connect_observation(connector))
        try:
            broken.save_observation(replace(newer,last_price=Decimal("100.22345678")),"isolated:rollback",quality)
        except QuoteWriteError as error:
            assert not error.commit_uncertain
        else:
            raise AssertionError("Individual readback mismatch committed")
        assert count_snapshots() == before_count
        checks.append("individual_snapshot_readback_failure_rolls_back")
    finally:
        if created:
            # Only the exact validated UUID schema created by this invocation is removed.
            if not re.fullmatch(r"external_quote_test_[0-9a-f]{32}", schema) or schema == settings.database:
                raise RuntimeError("Unsafe cleanup refused")
            cursor.execute(f"DROP DATABASE `{schema}`")
        cursor.close()
        admin.close()
    print(json.dumps({"status": "passed", "checks": checks, "test_schema_removed": created,
                      "production_rows_written": 0}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1)
