"""Append validated external quote batches without touching existing K-line tables."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import uuid

from app.services.external_market_quotes import MarketQuote, validate_batch
from app.services.mysql_instrument_repository import MySQLInstrumentRepository, MySQLRepositoryError


TABLE = "external_market_quote_snapshots"
COLUMNS = ("source", "symbol", "name", "session", "source_url", "fetched_at_utc", "quote_at_utc",
           "quote_time_status", "last_price", "change_value", "change_percent", "open_price",
           "high_price", "low_price", "amount_yi", "total_volume", "raw_json")


class QuoteWriteError(MySQLRepositoryError):
    def __init__(self, batch_id: str, commit_attempted: bool, cause: Exception):
        self.batch_id = batch_id
        self.commit_uncertain = commit_attempted
        super().__init__(f"外部報價寫入失敗（{type(cause).__name__}）；未自動重試")


def _utc(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value else None


def quote_row(q: MarketQuote) -> tuple:
    return (q.source, q.symbol, q.name, q.session, q.source_url, _utc(q.fetched_at), _utc(q.quote_at),
            "source_timestamp" if q.quote_at else "not_provided", q.last_price, q.change_value,
            q.change_percent, q.open_price, q.high_price, q.low_price, q.amount_yi, q.total_volume, q.raw)


class ExternalMarketQuoteRepository(MySQLInstrumentRepository):
    @staticmethod
    def _ensure_checks(cursor):
        cursor.execute("""CREATE TABLE IF NOT EXISTS external_market_quote_checks (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            source VARCHAR(16) NOT NULL,symbol VARCHAR(16) NOT NULL,session VARCHAR(16) NOT NULL,
            schedule_key VARCHAR(96) NOT NULL,status VARCHAR(24) NOT NULL,
            attempt_count INT UNSIGNED NOT NULL DEFAULT 1,
            checked_at_utc DATETIME(6) NOT NULL,quote_at_utc DATETIME(6) NULL,
            snapshot_id BIGINT UNSIGNED NULL,batch_id CHAR(32) NULL,
            quality_json LONGTEXT NOT NULL,last_error VARCHAR(512) NULL,
            UNIQUE KEY uq_external_check(source,symbol,session,schedule_key),
            INDEX idx_external_check_latest(source,symbol,session,checked_at_utc,id),
            INDEX idx_external_check_snapshot(snapshot_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")

    @staticmethod
    def _ensure_snapshots(cursor):
        cursor.execute(f"""CREATE TABLE IF NOT EXISTS `{TABLE}` (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,batch_id CHAR(32) NOT NULL,
            source VARCHAR(16) NOT NULL,symbol VARCHAR(16) NOT NULL,name VARCHAR(64) NOT NULL,
            session VARCHAR(16) NOT NULL,source_url VARCHAR(256) NOT NULL,
            fetched_at_utc DATETIME(6) NOT NULL,quote_at_utc DATETIME(6) NULL,
            quote_time_status VARCHAR(24) NOT NULL,last_price DECIMAL(24,8) NOT NULL,
            change_value DECIMAL(24,8) NOT NULL,change_percent DECIMAL(24,8) NOT NULL,
            open_price DECIMAL(24,8) NOT NULL,high_price DECIMAL(24,8) NOT NULL,
            low_price DECIMAL(24,8) NOT NULL,amount_yi DECIMAL(24,8) NULL,
            total_volume BIGINT UNSIGNED NULL,raw_json LONGTEXT NOT NULL,
            UNIQUE KEY uq_external_batch(batch_id,source,symbol,session),
            INDEX idx_external_latest(source,symbol,session,fetched_at_utc,id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")

    def save_observation(self, quote: MarketQuote, schedule_key: str, quality: dict) -> dict:
        """One product's check and optional new snapshot commit atomically."""
        quote.validate()
        if not schedule_key or len(schedule_key) > 96 or not quality.get("market_session_key"):
            raise ValueError("缺少排程／市場時段鍵")
        expected = quote_row(quote)
        product = (quote.source, quote.symbol, quote.session)
        batch_id = uuid.uuid4().hex
        connection = cursor = None
        acquired = committed = False
        lock_name = "extq:" + hashlib.sha256((self.settings.database + repr(product)).encode()).hexdigest()[:56]
        try:
            connection = self._connect_database(self._connector())
            cursor = connection.cursor(dictionary=True)
            cursor.execute("SELECT GET_LOCK(%s,0) AS acquired", (lock_name,))
            acquired = cursor.fetchone()["acquired"] == 1
            if not acquired:
                raise RuntimeError("external_quote_busy")
            self._ensure_snapshots(cursor)
            self._ensure_checks(cursor)
            connection.commit()
            connection.start_transaction()
            cursor.execute("SELECT * FROM external_market_quote_checks WHERE source=%s AND symbol=%s AND session=%s AND schedule_key=%s", (*product, schedule_key))
            checked = cursor.fetchone()
            if checked and checked["status"] in {"updated", "unchanged", "source_regressed"}:
                previous_quality = json.loads(checked["quality_json"])
                if not previous_quality.get("close_pending"):
                    connection.rollback()
                    return dict(status="already_checked", snapshot_id=checked["snapshot_id"],
                                batch_id=checked["batch_id"], quality=previous_quality,
                                checked_at=checked["checked_at_utc"].replace(tzinfo=timezone.utc).isoformat(),
                                quote_at=checked["quote_at_utc"].replace(tzinfo=timezone.utc).isoformat() if checked["quote_at_utc"] else None)
            cursor.execute(f"SELECT * FROM `{TABLE}` WHERE source=%s AND symbol=%s AND session=%s ORDER BY fetched_at_utc DESC,id DESC LIMIT 1", product)
            previous = cursor.fetchone()
            compared = [column for column in COLUMNS if column not in {"fetched_at_utc", "raw_json"}]
            same = previous is not None and all(previous[column] == expected[COLUMNS.index(column)] for column in compared)
            if same and quote.quote_at is None:
                cursor.execute("SELECT quality_json FROM external_market_quote_checks WHERE snapshot_id=%s ORDER BY checked_at_utc DESC,id DESC LIMIT 1", (previous["id"],))
                previous_check = cursor.fetchone()
                same = bool(previous_check and json.loads(previous_check["quality_json"]).get("market_session_key") == quality["market_session_key"])
            regressed = (previous is not None and quote.quote_at is not None and previous["quote_at_utc"] is not None
                         and _utc(quote.quote_at) < previous["quote_at_utc"])
            if same or regressed:
                snapshot_id, batch_id, status = previous["id"], previous["batch_id"], "unchanged"
                if regressed:
                    status = "source_regressed"
            else:
                names = ",".join("`" + column + "`" for column in COLUMNS)
                cursor.execute(f"INSERT INTO `{TABLE}` (batch_id,{names}) VALUES ({','.join(['%s']*(len(COLUMNS)+1))})", (batch_id, *expected))
                snapshot_id, status = cursor.lastrowid, "updated"
                cursor.execute(f"SELECT {names} FROM `{TABLE}` WHERE id=%s", (snapshot_id,))
                actual = cursor.fetchone()
                if tuple(actual[column] for column in COLUMNS) != expected:
                    raise ValueError("snapshot_readback_mismatch")
            self._write_check(cursor, product, schedule_key, status, quote.fetched_at, quote.quote_at,
                              snapshot_id, batch_id, quality, None)
            committed = True
            connection.commit()
            return dict(status=status, snapshot_id=snapshot_id, batch_id=batch_id, quality=quality)
        except Exception as error:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
            raise QuoteWriteError(batch_id, committed, error) from error
        finally:
            if acquired and cursor is not None:
                try:
                    cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
                    cursor.fetchall()
                except Exception:
                    pass
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    @staticmethod
    def _write_check(cursor, product, key, status, checked_at, quote_at, snapshot_id, batch_id, quality, error):
        cursor.execute("""INSERT INTO external_market_quote_checks
            (source,symbol,session,schedule_key,status,checked_at_utc,quote_at_utc,snapshot_id,batch_id,quality_json,last_error)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE status=VALUES(status),attempt_count=attempt_count+1,
                checked_at_utc=VALUES(checked_at_utc),quote_at_utc=VALUES(quote_at_utc),
                snapshot_id=VALUES(snapshot_id),batch_id=VALUES(batch_id),
                quality_json=VALUES(quality_json),last_error=VALUES(last_error)""",
            (*product, key, status, _utc(checked_at), _utc(quote_at), snapshot_id, batch_id,
             json.dumps(quality, ensure_ascii=False), error))

    def record_failure(self, product, key, checked_at, error_type, quality=None):
        from app.services.external_market_quotes import EXPECTED_KEYS
        if product not in EXPECTED_KEYS:
            raise ValueError("不在允許商品內")
        connection = cursor = None
        try:
            connection = self._connect_database(self._connector())
            cursor = connection.cursor(dictionary=True)
            self._ensure_checks(cursor)
            connection.commit()
            self._write_check(cursor, product, key, "failed", checked_at, None, None, None, quality or {}, error_type[:128])
            connection.commit()
        finally:
            if cursor is not None:
                cursor.close()
            if connection is not None:
                connection.close()

    def latest(self):
        """Read one indexed latest snapshot/check per product, without DDL."""
        from app.services.external_market_sessions import PRODUCTS
        connection = cursor = None
        result = []
        try:
            connection = self._connect_database(self._connector())
            connection.start_transaction(readonly=True, consistent_snapshot=True)
            cursor = connection.cursor(dictionary=True)
            cursor.execute("SELECT table_name AS name FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name IN (%s,%s)", (TABLE, "external_market_quote_checks"))
            existing = {row["name"] for row in cursor.fetchall()}
            for product in PRODUCTS:
                snapshot = check = None
                if TABLE in existing:
                    cursor.execute(f"SELECT * FROM `{TABLE}` WHERE source=%s AND symbol=%s AND session=%s ORDER BY fetched_at_utc DESC,id DESC LIMIT 1", product)
                    snapshot = cursor.fetchone()
                    if snapshot:
                        snapshot.pop("raw_json", None)
                if "external_market_quote_checks" in existing:
                    cursor.execute("SELECT * FROM external_market_quote_checks WHERE source=%s AND symbol=%s AND session=%s ORDER BY checked_at_utc DESC,id DESC LIMIT 1", product)
                    check = cursor.fetchone()
                    if check:
                        check["quality"] = json.loads(check.pop("quality_json"))
                result.append(dict(source=product[0], symbol=product[1], session=product[2], snapshot=snapshot, last_check=check))
            return result
        finally:
            if connection is not None:
                connection.rollback()
            if cursor is not None:
                cursor.close()
            if connection is not None:
                connection.close()

    def save_batch(self, quotes: list[MarketQuote]) -> str:
        # All source parsing and all five validations must finish BEFORE connecting/DDL.
        validate_batch(quotes)
        batch_id = uuid.uuid4().hex
        connection = cursor = None
        commit_attempted = False
        try:
            connection = self._connect_database(self._connector())
            cursor = connection.cursor()
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS `{TABLE}` (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    batch_id CHAR(32) NOT NULL,
                    source VARCHAR(16) NOT NULL, symbol VARCHAR(16) NOT NULL,
                    name VARCHAR(64) NOT NULL, session VARCHAR(16) NOT NULL,
                    source_url VARCHAR(256) NOT NULL,
                    fetched_at_utc DATETIME(6) NOT NULL, quote_at_utc DATETIME(6) NULL,
                    quote_time_status VARCHAR(24) NOT NULL,
                    last_price DECIMAL(24,8) NOT NULL, change_value DECIMAL(24,8) NOT NULL,
                    change_percent DECIMAL(24,8) NOT NULL, open_price DECIMAL(24,8) NOT NULL,
                    high_price DECIMAL(24,8) NOT NULL, low_price DECIMAL(24,8) NOT NULL,
                    amount_yi DECIMAL(24,8) NULL, total_volume BIGINT UNSIGNED NULL,
                    raw_json LONGTEXT NOT NULL,
                    UNIQUE KEY uq_external_batch (batch_id, source, symbol, session),
                    INDEX idx_external_latest (source, symbol, session, fetched_at_utc, id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            connection.commit()  # MySQL DDL is separate from the atomic five-row transaction.
            connection.start_transaction()
            names = ", ".join(f"`{col}`" for col in COLUMNS)
            cursor.executemany(
                f"INSERT INTO `{TABLE}` (batch_id, {names}) VALUES ({', '.join(['%s'] * (len(COLUMNS) + 1))})",
                [(batch_id, *quote_row(q)) for q in quotes],
            )
            cursor.execute(f"SELECT {names} FROM `{TABLE}` WHERE batch_id = %s ORDER BY source, symbol, session", (batch_id,))
            expected = sorted([quote_row(q) for q in quotes], key=lambda row: (row[0], row[1], row[3]))
            if cursor.fetchall() != expected:
                raise MySQLRepositoryError("報價讀回與原始五筆資料不符；已取消本批寫入")
            commit_attempted = True
            connection.commit()
            return batch_id
        except Exception as error:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
            # Connector messages can contain connection information: keep CLI output safe.
            raise QuoteWriteError(batch_id, commit_attempted, error) from error
        finally:
            if cursor is not None:
                cursor.close()
            if connection is not None:
                connection.close()

    def read_batch(self, batch_id: str) -> list[dict]:
        connection = cursor = None
        try:
            connection = self._connect_database(self._connector())
            connection.start_transaction(readonly=True)
            cursor = connection.cursor(dictionary=True)
            cursor.execute(f"SELECT id, batch_id, {', '.join('`'+c+'`' for c in COLUMNS)} "
                           f"FROM `{TABLE}` WHERE batch_id = %s ORDER BY source, symbol", (batch_id,))
            return cursor.fetchall()
        finally:
            if connection is not None:
                connection.rollback()
            if cursor is not None:
                cursor.close()
            if connection is not None:
                connection.close()
