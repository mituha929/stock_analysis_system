"""MySQL persistence for the Capital product master list."""

from __future__ import annotations

import os
import re
import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable

from app.schemas.models import Candle, Instrument
from app.services.backfill_eligibility import backfill_symbol_predicate


class MySQLRepositoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class MySQLSettings:
    host: str
    port: int
    user: str
    password: str
    database: str

    @classmethod
    def from_env(cls) -> "MySQLSettings":
        password = os.getenv("MYSQL_PASSWORD", "")
        if not password or password == "請填入MySQL密碼":
            raise MySQLRepositoryError("請先在 .env 設定 MYSQL_PASSWORD")
        database = os.getenv("MYSQL_DATABASE", "capital_stock_analysis")
        if not re.fullmatch(r"[A-Za-z0-9_]+", database):
            raise MySQLRepositoryError("MYSQL_DATABASE 只能包含英文字母、數字與底線")
        try:
            port = int(os.getenv("MYSQL_PORT", "3306"))
        except ValueError as error:
            raise MySQLRepositoryError("MYSQL_PORT 必須是整數") from error
        return cls(
            host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            port=port,
            user=os.getenv("MYSQL_USER", "capital_api"),
            password=password,
            database=database,
        )


class MySQLInstrumentRepository:
    """Creates and maintains the MySQL tables used for instrument snapshots."""

    def __init__(self, settings: MySQLSettings | None = None) -> None:
        self.settings = settings or MySQLSettings.from_env()
        self._lock = threading.Lock()

    def initialize(self) -> None:
        """Create the configured database and tables if they do not exist."""
        connector = self._connector()
        # A normal API account deliberately has no CREATE DATABASE privilege.
        # If an administrator already initialized this schema, verifying both
        # required tables is enough and avoids a needless privileged command.
        if self._has_required_tables(connector):
            return
        try:
            connection = connector.connect(
                host=self.settings.host, port=self.settings.port,
                user=self.settings.user, password=self.settings.password,
            )
            cursor = connection.cursor()
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{self.settings.database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            cursor.close()
            connection.close()

            connection = self._connect_database(connector)
            cursor = connection.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS instrument_sync_runs (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    market_no SMALLINT NOT NULL,
                    started_at DATETIME(6) NOT NULL,
                    completed_at DATETIME(6) NULL,
                    fetched_count INT UNSIGNED NOT NULL DEFAULT 0,
                    snapshot_hash CHAR(64) NOT NULL,
                    next_offset INT UNSIGNED NOT NULL DEFAULT 0,
                    status VARCHAR(20) NOT NULL,
                    error_message TEXT NULL,
                    PRIMARY KEY (id),
                    INDEX idx_sync_runs_market_started (market_no, started_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS instruments (
                    market_no SMALLINT NOT NULL,
                    symbol VARCHAR(32) NOT NULL,
                    name VARCHAR(128) NOT NULL,
                    category_no INT NOT NULL,
                    category_name VARCHAR(128) NOT NULL,
                    order_symbol VARCHAR(32) NOT NULL,
                    expiry_date VARCHAR(32) NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    last_sync_run_id BIGINT UNSIGNED NOT NULL,
                    first_seen_at DATETIME(6) NOT NULL,
                    last_seen_at DATETIME(6) NOT NULL,
                    updated_at DATETIME(6) NOT NULL,
                    PRIMARY KEY (market_no, symbol),
                    INDEX idx_instruments_category (market_no, category_no),
                    INDEX idx_instruments_active (market_no, is_active),
                    CONSTRAINT fk_instruments_sync_run
                        FOREIGN KEY (last_sync_run_id) REFERENCES instrument_sync_runs(id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS candles (
                    symbol VARCHAR(32) NOT NULL,
                    interval_code VARCHAR(8) NOT NULL,
                    candle_time DATETIME NOT NULL,
                    open_price DECIMAL(18,6) NOT NULL,
                    high_price DECIMAL(18,6) NOT NULL,
                    low_price DECIMAL(18,6) NOT NULL,
                    close_price DECIMAL(18,6) NOT NULL,
                    volume BIGINT UNSIGNED NOT NULL,
                    source VARCHAR(32) NOT NULL DEFAULT 'capital_history',
                    fetched_at DATETIME(6) NOT NULL,
                    PRIMARY KEY (symbol, interval_code, candle_time),
                    INDEX idx_candles_lookup (symbol, interval_code, candle_time)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS candle_backfill_jobs (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    market_no SMALLINT NOT NULL,
                    symbol VARCHAR(32) NOT NULL,
                    interval_code VARCHAR(8) NOT NULL,
                    start_date DATE NOT NULL,
                    end_date DATE NOT NULL,
                    status VARCHAR(24) NOT NULL DEFAULT 'pending',
                    attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
                    received_count INT UNSIGNED NULL,
                    last_error TEXT NULL,
                    started_at DATETIME(6) NULL,
                    completed_at DATETIME(6) NULL,
                    updated_at DATETIME(6) NOT NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uq_candle_backfill_job (market_no, symbol, interval_code, start_date, end_date),
                    INDEX idx_candle_backfill_status (status, market_no, id),
                    INDEX idx_candle_backfill_latest (market_no, interval_code, symbol, updated_at, id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            connection.commit()
            cursor.close()
            connection.close()
        except Exception as error:
            raise MySQLRepositoryError(f"MySQL 初始化失敗：{error}") from error

    def _has_required_tables(self, connector) -> bool:
        connection = None
        cursor = None
        try:
            connection = self._connect_database(connector)
            cursor = connection.cursor()
            cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name IN ('instruments', 'instrument_sync_runs', 'candles', 'candle_backfill_jobs')",
                (self.settings.database,),
            )
            return {str(row[0]) for row in cursor.fetchall()} == {
                "instruments", "instrument_sync_runs", "candles", "candle_backfill_jobs",
            }
        except Exception:
            return False
        finally:
            if cursor is not None:
                cursor.close()
            if connection is not None:
                connection.close()

    def sync_market(
        self, market_no: int, instruments: Iterable[Instrument], batch_size: int = 500,
        table_name: str = "instruments",
    ) -> tuple[int, int, bool]:
        """Persist a market snapshot in committed batches, with resume support.

        A retry re-fetches the list from Capital.  If its fingerprint matches a
        previously interrupted run, writes resume at ``next_offset`` instead
        of duplicating already committed batches.
        """
        rows = list(instruments)
        if not 1 <= batch_size <= 1_000:
            raise MySQLRepositoryError("batch_size 必須介於 1 到 1000")
        table_name = self._validate_table_name(table_name)
        snapshot_hash = self._snapshot_hash(rows)
        with self._lock:
            connector = self._connector()
            connection = None
            cursor = None
            try:
                connection = self._connect_database(connector)
                cursor = connection.cursor()
                if table_name != "instruments":
                    self._ensure_instrument_table(cursor, table_name)
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                cursor.execute(
                    "SELECT id, next_offset FROM instrument_sync_runs "
                    "WHERE market_no = %s AND snapshot_hash = %s AND status = 'running' "
                    "ORDER BY id DESC LIMIT 1",
                    (market_no, snapshot_hash),
                )
                previous_run = cursor.fetchone()
                resumed = previous_run is not None
                if previous_run is not None:
                    sync_run_id, next_offset = int(previous_run[0]), int(previous_run[1])
                else:
                    # A changed list cannot safely reuse the old offset.
                    cursor.execute(
                        "UPDATE instrument_sync_runs SET status = 'superseded', completed_at = %s, "
                        "error_message = '收到不同版本的商品清單，改由新的同步作業處理' "
                        "WHERE market_no = %s AND status = 'running'",
                        (now, market_no),
                    )
                    cursor.execute(
                        "INSERT INTO instrument_sync_runs "
                        "(market_no, started_at, fetched_count, snapshot_hash, next_offset, status) "
                        "VALUES (%s, %s, %s, %s, 0, 'running')",
                        (market_no, now, len(rows), snapshot_hash),
                    )
                    sync_run_id, next_offset = int(cursor.lastrowid), 0
                connection.commit()

                for start in range(next_offset, len(rows), batch_size):
                    batch = rows[start:start + batch_size]
                    cursor.executemany(f"""
                        INSERT INTO `{table_name}` (
                            market_no, symbol, name, category_no, category_name,
                            order_symbol, expiry_date, is_active, last_sync_run_id,
                            first_seen_at, last_seen_at, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            name = VALUES(name),
                            category_no = VALUES(category_no),
                            category_name = VALUES(category_name),
                            order_symbol = VALUES(order_symbol),
                            expiry_date = VALUES(expiry_date),
                            is_active = TRUE,
                            last_sync_run_id = VALUES(last_sync_run_id),
                            last_seen_at = VALUES(last_seen_at),
                            updated_at = VALUES(updated_at)
                    """, [
                        (
                            item.market_no, item.symbol, item.name, item.category_no,
                            item.category_name, item.order_symbol, item.expiry_date,
                            sync_run_id, now, now, now,
                        )
                        for item in batch
                    ])
                    completed_offset = start + len(batch)
                    cursor.execute(
                        "UPDATE instrument_sync_runs SET next_offset = %s WHERE id = %s",
                        (completed_offset, sync_run_id),
                    )
                    # This commit is the checkpoint.  A process crash after
                    # it can resume from the following batch.
                    connection.commit()
                # The request is a full snapshot.  Only after all rows were
                # inserted successfully may absent symbols be marked inactive.
                cursor.execute(
                    f"UPDATE `{table_name}` SET is_active = FALSE, updated_at = %s "
                    "WHERE market_no = %s AND last_sync_run_id <> %s",
                    (now, market_no, sync_run_id),
                )
                cursor.execute(
                    "UPDATE instrument_sync_runs SET completed_at = %s, status = 'completed' WHERE id = %s",
                    (now, sync_run_id),
                )
                connection.commit()
                return sync_run_id, len(rows), resumed
            except Exception as error:
                if connection is not None:
                    connection.rollback()
                raise MySQLRepositoryError(f"MySQL 商品清單同步失敗：{error}") from error
            finally:
                if cursor is not None:
                    cursor.close()
                if connection is not None:
                    connection.close()

    def _connect_database(self, connector):
        return connector.connect(
            host=self.settings.host, port=self.settings.port,
            user=self.settings.user, password=self.settings.password,
            database=self.settings.database,
            connection_timeout=10, read_timeout=30, write_timeout=30,
        )

    @staticmethod
    def _validate_table_name(table_name: str) -> str:
        normalized = table_name.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", normalized):
            raise MySQLRepositoryError("table_name 只能使用英文字母、數字與底線，且必須以英文字母開頭")
        return normalized

    @staticmethod
    def _ensure_instrument_table(cursor, table_name: str) -> None:
        """Create a compatible product-master table only when it is missing."""
        cursor.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = %s",
            (table_name,),
        )
        if cursor.fetchone() is not None:
            return
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS `{table_name}` (
                market_no SMALLINT NOT NULL,
                symbol VARCHAR(32) NOT NULL,
                name VARCHAR(128) NOT NULL,
                category_no INT NOT NULL,
                category_name VARCHAR(128) NOT NULL,
                order_symbol VARCHAR(32) NOT NULL,
                expiry_date VARCHAR(32) NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                last_sync_run_id BIGINT UNSIGNED NOT NULL,
                first_seen_at DATETIME(6) NOT NULL,
                last_seen_at DATETIME(6) NOT NULL,
                updated_at DATETIME(6) NOT NULL,
                PRIMARY KEY (market_no, symbol),
                INDEX idx_category (market_no, category_no),
                INDEX idx_active (market_no, is_active)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

    @staticmethod
    def _connector():
        try:
            import mysql.connector
            return mysql.connector
        except ImportError as error:
            raise MySQLRepositoryError(
                "尚未安裝 mysql-connector-python；請在 capital_api 執行 pip install -r requirements.txt"
            ) from error

    @staticmethod
    def _snapshot_hash(rows: list[Instrument]) -> str:
        data = [
            [
                item.market_no, item.category_no, item.category_name,
                item.symbol, item.name, item.order_symbol, item.expiry_date,
            ]
            for item in rows
        ]
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class MySQLCandleRepository(MySQLInstrumentRepository):
    """Persistence operations for historical OHLCV candles."""

    DEFAULT_CANDLE_TABLE = "candles"
    DEFAULT_JOB_TABLE = "candle_backfill_jobs"
    DEFAULT_INSTRUMENT_TABLE = "instruments"

    def get_instrument_name(self, symbol: str, instrument_table: str) -> str:
        """Return the active instrument name without depending on live quote data."""
        instrument_table = self._validate_table_name(instrument_table)
        connector = self._connector()
        connection = cursor = None
        try:
            connection = self._connect_database(connector)
            cursor = connection.cursor()
            cursor.execute(
                f"SELECT name FROM `{instrument_table}` WHERE symbol = %s AND is_active = TRUE "
                "ORDER BY market_no LIMIT 1",
                (symbol,),
            )
            row = cursor.fetchone()
            return str(row[0]) if row else ""
        except Exception as error:
            raise MySQLRepositoryError(f"讀取商品名稱失敗：{error}") from error
        finally:
            if cursor is not None:
                cursor.close()
            if connection is not None:
                connection.close()

    def ensure_backfill_storage(self, candle_table: str, job_table: str) -> tuple[str, str]:
        """Create named K-line and resume-job tables when they are missing."""
        candle_table = self._validate_table_name(candle_table)
        job_table = self._validate_table_name(job_table)
        with self._lock:
            connector = self._connector()
            connection = None
            cursor = None
            try:
                connection = self._connect_database(connector)
                cursor = connection.cursor()
                if candle_table != self.DEFAULT_CANDLE_TABLE:
                    self._ensure_candle_table(cursor, candle_table)
                if job_table != self.DEFAULT_JOB_TABLE:
                    self._ensure_backfill_job_table(cursor, job_table)
                connection.commit()
                return candle_table, job_table
            except Exception as error:
                if connection is not None:
                    connection.rollback()
                raise MySQLRepositoryError(f"建立 K 線回補資料表失敗：{error}") from error
            finally:
                if cursor is not None:
                    cursor.close()
                if connection is not None:
                    connection.close()

    def upsert_candles(
        self, symbol: str, interval: str, candles: Iterable[Candle], table_name: str = "candles",
    ) -> int:
        rows = list(candles)
        if not rows:
            return 0
        from app.services.candle_validation import validate_candle_volumes
        validate_candle_volumes(rows)
        table_name = self._validate_table_name(table_name)
        with self._lock:
            connector = self._connector()
            connection = None
            cursor = None
            try:
                connection = self._connect_database(connector)
                cursor = connection.cursor()
                if table_name != "candles":
                    self._ensure_candle_table(cursor, table_name)
                fetched_at = datetime.now(timezone.utc).replace(tzinfo=None)
                cursor.executemany(f"""
                    INSERT INTO `{table_name}` (
                        symbol, interval_code, candle_time, open_price, high_price,
                        low_price, close_price, volume, source, fetched_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'capital_history', %s)
                    ON DUPLICATE KEY UPDATE
                        open_price = VALUES(open_price),
                        high_price = VALUES(high_price),
                        low_price = VALUES(low_price),
                        close_price = VALUES(close_price),
                        volume = VALUES(volume),
                        source = VALUES(source),
                        fetched_at = VALUES(fetched_at)
                """, [
                    (
                        symbol.upper(), interval, candle.timestamp.replace(tzinfo=None),
                        candle.open, candle.high, candle.low, candle.close,
                        candle.volume, fetched_at,
                    )
                    for candle in rows
                ])
                connection.commit()
                return len(rows)
            except Exception as error:
                if connection is not None:
                    connection.rollback()
                raise MySQLRepositoryError(f"MySQL K 線同步失敗：{error}") from error
            finally:
                if cursor is not None:
                    cursor.close()
                if connection is not None:
                    connection.close()

    def get_candles(
        self, symbol: str, interval: str, table_name: str = "candles", limit: int = 10_000,
    ) -> list[Candle]:
        return self.get_candles_with_metadata(symbol, interval, table_name, limit)[0]

    def get_candles_with_metadata(
        self, symbol: str, interval: str, table_name: str = "candles", limit: int = 10_000,
    ) -> tuple[list[Candle], datetime | None]:
        """Read one symbol's stored OHLCV series in chronological order."""
        if limit < 2 or limit > 100_000:
            raise MySQLRepositoryError("K 線讀取筆數必須介於 2 到 100000")
        table_name = self._validate_table_name(table_name)
        with self._lock:
            connector = self._connector()
            connection = None
            cursor = None
            try:
                connection = self._connect_database(connector)
                cursor = connection.cursor()
                cursor.execute(
                    f"SELECT candle_time, open_price, high_price, low_price, close_price, volume, fetched_at "
                    f"FROM `{table_name}` WHERE symbol = %s AND interval_code = %s "
                    + ("AND source<>'capital_day_partial' " if interval in {"week", "month"} else "") +
                    "ORDER BY candle_time DESC LIMIT %s",
                    (symbol.upper(), interval, limit),
                )
                # Query newest first so LIMIT preserves the most useful history;
                # the analysis engine requires chronological input.
                rows = list(reversed(cursor.fetchall()))
                if interval in {"week","month"}:
                    from app.services.backfill_policy import completed_end, complete_aggregate
                    as_of, periods = completed_end(interval), {}
                    rows = [r for r in rows if complete_aggregate(interval,r[0].date(),r[6],as_of,periods)]
                candles = [
                    Candle(
                        timestamp=candle_time,
                        open=float(open_price),
                        high=float(high_price),
                        low=float(low_price),
                        close=float(close_price),
                        volume=int(volume),
                    )
                    for candle_time, open_price, high_price, low_price, close_price, volume, _fetched_at in rows
                ]
                written = [row[6] for row in rows if row[6] is not None]
                # fetched_at is stored as UTC without timezone in MySQL.
                latest_written = max(written).replace(tzinfo=timezone.utc) if written else None
                return candles, latest_written
            except MySQLRepositoryError:
                raise
            except Exception as error:
                raise MySQLRepositoryError(f"讀取 MySQL K 線失敗：{error}") from error
            finally:
                if cursor is not None:
                    cursor.close()
                if connection is not None:
                    connection.close()

    @staticmethod
    def _ensure_candle_table(cursor, table_name: str) -> None:
        cursor.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = %s",
            (table_name,),
        )
        if cursor.fetchone() is not None:
            return
        cursor.execute(f"""
            CREATE TABLE `{table_name}` (
                symbol VARCHAR(32) NOT NULL,
                interval_code VARCHAR(8) NOT NULL,
                candle_time DATETIME NOT NULL,
                open_price DECIMAL(18,6) NOT NULL,
                high_price DECIMAL(18,6) NOT NULL,
                low_price DECIMAL(18,6) NOT NULL,
                close_price DECIMAL(18,6) NOT NULL,
                volume BIGINT UNSIGNED NOT NULL,
                source VARCHAR(32) NOT NULL DEFAULT 'capital_history',
                fetched_at DATETIME(6) NOT NULL,
                PRIMARY KEY (symbol, interval_code, candle_time),
                INDEX idx_lookup (symbol, interval_code, candle_time)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

    @staticmethod
    def _ensure_backfill_job_table(cursor, table_name: str) -> None:
        cursor.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = %s",
            (table_name,),
        )
        if cursor.fetchone() is not None:
            return
        cursor.execute(f"""
            CREATE TABLE `{table_name}` (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                market_no SMALLINT NOT NULL,
                symbol VARCHAR(32) NOT NULL,
                interval_code VARCHAR(8) NOT NULL,
                start_date DATE NOT NULL,
                end_date DATE NOT NULL,
                status VARCHAR(24) NOT NULL DEFAULT 'pending',
                attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
                received_count INT UNSIGNED NULL,
                last_error TEXT NULL,
                started_at DATETIME(6) NULL,
                completed_at DATETIME(6) NULL,
                updated_at DATETIME(6) NOT NULL,
                PRIMARY KEY (id),
                UNIQUE KEY uq_backfill_job (market_no, symbol, interval_code, start_date, end_date),
                INDEX idx_backfill_status (status, market_no, id),
                INDEX idx_backfill_latest (market_no, interval_code, symbol, updated_at, id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

    def create_backfill_jobs(
        self, market_nos: Iterable[int], interval: str, start_date: date, end_date: date,
        target_bars: int, candle_table: str = DEFAULT_CANDLE_TABLE, job_table: str = DEFAULT_JOB_TABLE,
        instrument_table: str = DEFAULT_INSTRUMENT_TABLE, force: bool = False,
    ) -> int:
        """Queue only missing, incomplete, or stale K-line data.

        The query deliberately excludes the known Capital category mappings for
        beneficiary certificates and warrants, including OTC category 198.
        A job previously marked ``insufficient_history`` is not retried again
        on the same data-update day; this prevents new listings from looping.
        """
        markets = sorted({int(market_no) for market_no in market_nos})
        if not markets:
            raise MySQLRepositoryError("至少要指定一個市場別")
        if target_bars < 1:
            raise MySQLRepositoryError("目標 K 線根數必須大於 0")
        candle_table = self._validate_table_name(candle_table)
        job_table = self._validate_table_name(job_table)
        instrument_table = self._validate_table_name(instrument_table)
        placeholders = ", ".join(["%s"] * len(markets))
        with self._lock:
            connector = self._connector()
            connection = None
            cursor = None
            try:
                connection = self._connect_database(connector)
                cursor = connection.cursor()
                cursor.execute(
                    f"SELECT instruments.market_no, instruments.symbol, "
                    "COUNT(candle_data.candle_time), MAX(candle_data.fetched_at) "
                    f"FROM `{instrument_table}` AS instruments "
                    f"LEFT JOIN `{candle_table}` AS candle_data ON candle_data.symbol = instruments.symbol "
                    "AND candle_data.interval_code = %s "
                    f"WHERE instruments.is_active = TRUE AND instruments.market_no IN ({placeholders}) "
                    "AND instruments.category_no NOT IN (94, 98) "
                    "AND NOT (instruments.market_no = 1 AND instruments.category_no = 198) "
                    f"AND {backfill_symbol_predicate('instruments')} "
                    "GROUP BY instruments.market_no, instruments.symbol "
                    "ORDER BY FIELD(instruments.market_no, 0, 1, 4, 5, 6), instruments.symbol",
                    (interval, *markets),
                )
                # Odd-lot markets may contain the same underlying symbols as
                # their normal market.  One K-line set per symbol is enough.
                unique_symbols: set[str] = set()
                job_rows = []
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                refresh_rows = []
                requeue_statuses = ("completed", "insufficient_history", "failed") if force else (
                    "completed", "insufficient_history",
                )
                for market_no, symbol, candle_count, latest_fetched_at in cursor.fetchall():
                    normalized = str(symbol).upper()
                    if normalized in unique_symbols:
                        continue
                    unique_symbols.add(normalized)
                    if not force and not self._needs_candle_refresh(
                        int(candle_count), latest_fetched_at, target_bars, end_date,
                    ):
                        continue
                    job_rows.append((int(market_no), normalized, interval, start_date, end_date, now))
                    refresh_rows.append((
                        now, int(market_no), normalized, interval, start_date, end_date, *requeue_statuses,
                    ))
                if not job_rows:
                    return 0
                cursor.executemany(f"""
                    INSERT IGNORE INTO `{job_table}` (
                        market_no, symbol, interval_code, start_date, end_date, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                """, job_rows)
                created = cursor.rowcount
                # If a user deleted K-lines, or yesterday's completed data is
                # now stale, revive that same range's finished job.  Failed
                # jobs remain failed unless the user supplies --retry-failed.
                cursor.executemany(f"""
                    UPDATE `{job_table}`
                    SET status = 'pending', received_count = 0, completed_at = NULL,
                        updated_at = %s
                    WHERE market_no = %s AND symbol = %s AND interval_code = %s
                      AND start_date = %s AND end_date = %s
                      AND status IN ({", ".join(["%s"] * len(requeue_statuses))})
                """, refresh_rows)
                connection.commit()
                return int(created)
            except MySQLRepositoryError:
                raise
            except Exception as error:
                if connection is not None:
                    connection.rollback()
                raise MySQLRepositoryError(f"建立 K 線回補工作失敗：{error}") from error
            finally:
                if cursor is not None:
                    cursor.close()
                if connection is not None:
                    connection.close()

    def get_oldest_backfill_updates(
        self, market_nos: Iterable[int], intervals: Iterable[str], job_table: str = DEFAULT_JOB_TABLE,
    ) -> dict[tuple[int, str], datetime | None]:
        """Get the least-recent *per-symbol* successful update for each pair.

        A symbol can have many historical jobs.  Its old completed job must
        not keep a whole market stale after a newer successful refresh, so we
        first take ``MAX(updated_at)`` per symbol and only then take ``MIN``
        across the market.  Failed/pending jobs do not count as successful
        data updates.
        """
        markets = sorted({int(market_no) for market_no in market_nos})
        interval_list = list(dict.fromkeys(str(interval) for interval in intervals))
        if not markets or not interval_list:
            raise MySQLRepositoryError("市場別與 K 線週期不可為空")
        job_table = self._validate_table_name(job_table)
        market_marks = ", ".join(["%s"] * len(markets))
        interval_marks = ", ".join(["%s"] * len(interval_list))
        result = {(market, interval): None for market in markets for interval in interval_list}
        with self._lock:
            connector = self._connector()
            connection = None
            cursor = None
            try:
                connection = self._connect_database(connector)
                cursor = connection.cursor()
                cursor.execute(
                    "SELECT market_no, interval_code, MIN(symbol_updated_at) FROM ("
                    f"SELECT market_no, symbol, interval_code, MAX(updated_at) AS symbol_updated_at FROM `{job_table}` "
                    f"WHERE market_no IN ({market_marks}) AND interval_code IN ({interval_marks}) "
                    "AND status IN ('completed', 'insufficient_history') "
                    f"AND {backfill_symbol_predicate()} "
                    "GROUP BY market_no, symbol, interval_code"
                    ") AS latest_symbol_updates "
                    "GROUP BY market_no, interval_code",
                    (*markets, *interval_list),
                )
                for market_no, interval, oldest in cursor.fetchall():
                    result[(int(market_no), str(interval))] = oldest
                return result
            except Exception as error:
                raise MySQLRepositoryError(f"讀取 K 線回補更新時間失敗：{error}") from error
            finally:
                if cursor is not None:
                    cursor.close()
                if connection is not None:
                    connection.close()

    @staticmethod
    def _needs_candle_refresh(
        candle_count: int, latest_fetched_at: datetime | None, target_bars: int, end_date: date,
    ) -> bool:
        """Whether local data is absent, short, or was not written on ``end_date``."""
        if candle_count < target_bars or latest_fetched_at is None:
            return True
        return latest_fetched_at.date() < end_date

    def get_candle_storage_summary(
        self, market_nos: Iterable[int], intervals: Iterable[str], candle_table: str = DEFAULT_CANDLE_TABLE,
        instrument_table: str = DEFAULT_INSTRUMENT_TABLE,
    ) -> dict[str, object]:
        """Return data presence and latest update timestamps for the backfill scope."""
        markets = sorted({int(market_no) for market_no in market_nos})
        interval_list = list(dict.fromkeys(str(interval) for interval in intervals))
        if not markets or not interval_list:
            raise MySQLRepositoryError("市場別與 K 線週期不可為空")
        candle_table = self._validate_table_name(candle_table)
        instrument_table = self._validate_table_name(instrument_table)
        market_marks = ", ".join(["%s"] * len(markets))
        interval_marks = ", ".join(["%s"] * len(interval_list))
        eligible_where = (
            f"is_active = TRUE AND market_no IN ({market_marks}) "
            "AND category_no NOT IN (94, 98) "
            f"AND NOT (market_no = 1 AND category_no = 198) AND {backfill_symbol_predicate()}"
        )
        with self._lock:
            connector = self._connector()
            connection = None
            cursor = None
            try:
                connection = self._connect_database(connector)
                cursor = connection.cursor()
                cursor.execute(
                    f"SELECT COUNT(DISTINCT symbol) FROM `{instrument_table}` WHERE {eligible_where}", tuple(markets),
                )
                eligible_symbols = int(cursor.fetchone()[0])
                cursor.execute(
                    "SELECT candle_data.interval_code, COUNT(*), COUNT(DISTINCT candle_data.symbol), "
                    "MAX(candle_data.candle_time), MAX(candle_data.fetched_at) "
                    f"FROM `{candle_table}` AS candle_data INNER JOIN ("
                    f"SELECT DISTINCT symbol FROM `{instrument_table}` WHERE {eligible_where}"
                    ") AS eligible ON eligible.symbol = candle_data.symbol "
                    f"WHERE candle_data.interval_code IN ({interval_marks}) "
                    "GROUP BY candle_data.interval_code",
                    (*markets, *interval_list),
                )
                by_interval = {
                    interval: {
                        "candle_count": 0, "symbol_count": 0,
                        "latest_candle_at": None, "latest_fetched_at": None,
                    }
                    for interval in interval_list
                }
                for interval, candle_count, symbol_count, latest_candle_at, latest_fetched_at in cursor.fetchall():
                    by_interval[str(interval)] = {
                        "candle_count": int(candle_count), "symbol_count": int(symbol_count),
                        "latest_candle_at": latest_candle_at, "latest_fetched_at": latest_fetched_at,
                    }
                return {"eligible_symbols": eligible_symbols, "by_interval": by_interval}
            except Exception as error:
                raise MySQLRepositoryError(f"讀取 K 線資料表狀態失敗：{error}") from error
            finally:
                if cursor is not None:
                    cursor.close()
                if connection is not None:
                    connection.close()

    @staticmethod
    def _ensure_daily_backfill_run_table(cursor) -> None:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_backfill_runs (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                status VARCHAR(24) NOT NULL,
                stage VARCHAR(48) NOT NULL,
                configuration_json LONGTEXT NOT NULL,
                board_instrument_count INT UNSIGNED NOT NULL DEFAULT 0,
                odd_lot_instrument_count INT UNSIGNED NOT NULL DEFAULT 0,
                queued_jobs INT UNSIGNED NOT NULL DEFAULT 0,
                processed_jobs INT UNSIGNED NOT NULL DEFAULT 0,
                completed_jobs INT UNSIGNED NOT NULL DEFAULT 0,
                insufficient_jobs INT UNSIGNED NOT NULL DEFAULT 0,
                failed_jobs INT UNSIGNED NOT NULL DEFAULT 0,
                message TEXT NULL,
                error_message TEXT NULL,
                started_at DATETIME(6) NOT NULL,
                completed_at DATETIME(6) NULL,
                updated_at DATETIME(6) NOT NULL,
                PRIMARY KEY (id),
                INDEX idx_daily_backfill_started (started_at),
                INDEX idx_daily_backfill_status (status, started_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

    def start_daily_backfill_run(self, configuration: dict[str, object]) -> int:
        """Create the persistent parent log before a scheduled workflow starts."""
        with self._lock:
            connector = self._connector()
            connection = cursor = None
            try:
                connection = self._connect_database(connector)
                cursor = connection.cursor()
                self._ensure_daily_backfill_run_table(cursor)
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                cursor.execute(
                    "UPDATE daily_backfill_runs SET status = 'error', stage = 'interrupted', "
                    "message = 'API 在前一次自動更新完成前停止；該次工作已中斷', "
                    "error_message = 'API process stopped before the scheduled run completed', "
                    "completed_at = %s, updated_at = %s WHERE status = 'running'",
                    (now, now),
                )
                cursor.execute(
                    "INSERT INTO daily_backfill_runs "
                    "(status, stage, configuration_json, started_at, updated_at) "
                    "VALUES ('running', 'syncing_board_instruments', %s, %s, %s)",
                    (json.dumps(configuration, ensure_ascii=False, separators=(",", ":")), now, now),
                )
                run_id = int(cursor.lastrowid)
                connection.commit()
                return run_id
            except Exception as error:
                if connection is not None:
                    connection.rollback()
                raise MySQLRepositoryError(f"建立每日自動更新 Log 失敗：{error}") from error
            finally:
                if cursor is not None:
                    cursor.close()
                if connection is not None:
                    connection.close()

    def update_daily_backfill_run(
        self, run_id: int, *, status: str, stage: str,
        board_instrument_count: int | None = None,
        odd_lot_instrument_count: int | None = None,
        queued_jobs: int | None = None, processed_jobs: int | None = None,
        completed_jobs: int | None = None, insufficient_jobs: int | None = None,
        failed_jobs: int | None = None, message: str | None = None,
        error_message: str | None = None, finished: bool = False,
    ) -> None:
        """Update a known run using a fixed field whitelist."""
        optional_values = {
            "board_instrument_count": board_instrument_count,
            "odd_lot_instrument_count": odd_lot_instrument_count,
            "queued_jobs": queued_jobs,
            "processed_jobs": processed_jobs,
            "completed_jobs": completed_jobs,
            "insufficient_jobs": insufficient_jobs,
            "failed_jobs": failed_jobs,
            "message": message,
            "error_message": error_message,
        }
        assignments = ["status = %s", "stage = %s"]
        parameters: list[object] = [status, stage]
        for column, value in optional_values.items():
            if value is not None:
                assignments.append(f"{column} = %s")
                parameters.append(value)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if finished:
            assignments.append("completed_at = %s")
            parameters.append(now)
        assignments.append("updated_at = %s")
        parameters.extend((now, int(run_id)))
        with self._lock:
            connector = self._connector()
            connection = cursor = None
            try:
                connection = self._connect_database(connector)
                cursor = connection.cursor()
                cursor.execute(
                    f"UPDATE daily_backfill_runs SET {', '.join(assignments)} WHERE id = %s",
                    tuple(parameters),
                )
                if cursor.rowcount != 1:
                    raise MySQLRepositoryError(f"找不到每日自動更新 Log：{run_id}")
                connection.commit()
            except MySQLRepositoryError:
                if connection is not None:
                    connection.rollback()
                raise
            except Exception as error:
                if connection is not None:
                    connection.rollback()
                raise MySQLRepositoryError(f"更新每日自動更新 Log 失敗：{error}") from error
            finally:
                if cursor is not None:
                    cursor.close()
                if connection is not None:
                    connection.close()

    def list_daily_backfill_runs(self, limit: int = 10) -> list[dict[str, object]]:
        """Read recent persistent scheduler logs without creating storage."""
        bounded_limit = max(1, min(int(limit), 100))
        with self._lock:
            connector = self._connector()
            connection = cursor = None
            try:
                connection = self._connect_database(connector)
                cursor = connection.cursor(dictionary=True)
                cursor.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() AND table_name = 'daily_backfill_runs'"
                )
                if cursor.fetchone() is None:
                    return []
                cursor.execute(
                    "SELECT id, status, stage, started_at, updated_at, completed_at, board_instrument_count, "
                    "odd_lot_instrument_count, queued_jobs, processed_jobs, completed_jobs, "
                    "insufficient_jobs, failed_jobs, message, error_message "
                    "FROM daily_backfill_runs ORDER BY id DESC LIMIT %s",
                    (bounded_limit,),
                )
                return [dict(row) for row in cursor.fetchall()]
            except Exception as error:
                raise MySQLRepositoryError(f"讀取每日自動更新 Log 失敗：{error}") from error
            finally:
                if cursor is not None:
                    cursor.close()
                if connection is not None:
                    connection.close()

    def get_backfill_dashboard_status(
        self, market_nos: Iterable[int], intervals: Iterable[str], candle_table: str,
        job_table: str, instrument_table: str,
    ) -> dict[str, object]:
        """Return a lightweight status snapshot using each symbol's latest job.

        Historical ranges remain in the job table for audit and resume support.
        Counting only the latest row per market/symbol/interval prevents an old
        failed or pending range from making a subsequently successful symbol
        look unfinished on the status page.
        """
        markets = sorted({int(market_no) for market_no in market_nos})
        interval_list = list(dict.fromkeys(str(interval) for interval in intervals))
        if not markets or not interval_list:
            raise MySQLRepositoryError("市場別與 K 線週期不可為空")
        candle_table = self._validate_table_name(candle_table)
        job_table = self._validate_table_name(job_table)
        instrument_table = self._validate_table_name(instrument_table)
        market_marks = ", ".join(["%s"] * len(markets))
        interval_marks = ", ".join(["%s"] * len(interval_list))
        items = {
            (market, interval): {
                "storage_ready": False,
                "active_instruments": 0,
                "tracked_symbols": 0,
                "job_counts": {
                    "pending": 0, "running": 0, "completed": 0,
                    "failed": 0, "insufficient_history": 0,
                },
                "data_through": None,
                "oldest_successful_update_at": None,
                "latest_successful_update_at": None,
                "latest_status_at": None,
                "latest_failure_symbol": None,
                "latest_failure_error": None,
                "latest_failure_at": None,
            }
            for market in markets for interval in interval_list
        }
        with self._lock:
            connector = self._connector()
            connection = None
            cursor = None
            try:
                connection = self._connect_database(connector)
                cursor = connection.cursor()
                cursor.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() AND table_name IN (%s, %s, %s)",
                    (instrument_table, candle_table, job_table),
                )
                existing_tables = {str(row[0]) for row in cursor.fetchall()}
                storage_ready = {instrument_table, candle_table, job_table}.issubset(existing_tables)
                for item in items.values():
                    item["storage_ready"] = storage_ready
                if instrument_table in existing_tables:
                    cursor.execute(
                        f"SELECT market_no, COUNT(*) FROM `{instrument_table}` "
                        f"WHERE is_active = TRUE AND market_no IN ({market_marks}) "
                        "AND category_no NOT IN (94, 98) "
                        "AND NOT (market_no = 1 AND category_no = 198) "
                        f"AND {backfill_symbol_predicate()} "
                        "GROUP BY market_no",
                        tuple(markets),
                    )
                    active_by_market = {int(market): int(count) for market, count in cursor.fetchall()}
                    for (market, _interval), item in items.items():
                        item["active_instruments"] = active_by_market.get(market, 0)
                if job_table not in existing_tables or instrument_table not in existing_tables:
                    return {"storage_ready": storage_ready, "items": items}

                ranked_sql = (
                    "WITH ranked_jobs AS ("
                    "SELECT jobs.id, jobs.market_no, jobs.symbol, jobs.interval_code, jobs.start_date, "
                    "jobs.end_date, jobs.status, jobs.last_error, jobs.updated_at, "
                    "ROW_NUMBER() OVER (PARTITION BY jobs.market_no, jobs.symbol, jobs.interval_code "
                    "ORDER BY jobs.updated_at DESC, jobs.id DESC) AS job_rank "
                    f"FROM `{job_table}` AS jobs INNER JOIN `{instrument_table}` AS instruments "
                    "ON instruments.market_no = jobs.market_no AND instruments.symbol = jobs.symbol "
                    f"WHERE jobs.market_no IN ({market_marks}) "
                    f"AND jobs.interval_code IN ({interval_marks}) AND jobs.status <> 'superseded' AND instruments.is_active = TRUE "
                    "AND instruments.category_no NOT IN (94, 98) "
                    f"AND NOT (instruments.market_no = 1 AND instruments.category_no = 198) AND {backfill_symbol_predicate('instruments')}"
                    ") "
                )
                parameters = (*markets, *interval_list)
                cursor.execute(
                    ranked_sql
                    + "SELECT market_no, interval_code, status, COUNT(*), MIN(updated_at), "
                      "MAX(updated_at), MIN(end_date), MAX(end_date) "
                      "FROM ranked_jobs WHERE job_rank = 1 "
                      "GROUP BY market_no, interval_code, status",
                    parameters,
                )
                successful = {"completed", "insufficient_history"}
                for market, interval, job_status, count, oldest, latest, first_end, last_end in cursor.fetchall():
                    key = (int(market), str(interval))
                    item = items[key]
                    status_name = str(job_status)
                    count_value = int(count)
                    if status_name in item["job_counts"]:
                        item["job_counts"][status_name] = count_value
                    item["tracked_symbols"] += count_value
                    if item["latest_status_at"] is None or (latest is not None and latest > item["latest_status_at"]):
                        item["latest_status_at"] = latest
                    if status_name in successful:
                        if item["data_through"] is None or (first_end is not None and first_end < item["data_through"]):
                            item["data_through"] = first_end
                        if item["oldest_successful_update_at"] is None or (
                            oldest is not None and oldest < item["oldest_successful_update_at"]
                        ):
                            item["oldest_successful_update_at"] = oldest
                        if item["latest_successful_update_at"] is None or (
                            latest is not None and latest > item["latest_successful_update_at"]
                        ):
                            item["latest_successful_update_at"] = latest

                cursor.execute(
                    ranked_sql
                    + "SELECT market_no, interval_code, symbol, last_error, updated_at "
                      "FROM ranked_jobs WHERE job_rank = 1 AND status = 'failed' "
                      "ORDER BY updated_at DESC, id DESC",
                    parameters,
                )
                failure_keys: set[tuple[int, str]] = set()
                for market, interval, symbol, last_error, updated_at in cursor.fetchall():
                    key = (int(market), str(interval))
                    if key in failure_keys:
                        continue
                    failure_keys.add(key)
                    items[key]["latest_failure_symbol"] = str(symbol)
                    items[key]["latest_failure_error"] = str(last_error or "") or None
                    items[key]["latest_failure_at"] = updated_at
                return {"storage_ready": storage_ready, "items": items}
            except Exception as error:
                raise MySQLRepositoryError(f"讀取 K 線回補狀態失敗：{error}") from error
            finally:
                if cursor is not None:
                    cursor.close()
                if connection is not None:
                    connection.close()

    def get_pending_backfill_groups(
        self, market_nos: Iterable[int], intervals: Iterable[str], job_table: str = DEFAULT_JOB_TABLE,
        instrument_table: str = DEFAULT_INSTRUMENT_TABLE,
    ) -> list[dict[str, object]]:
        """Read original pending ranges, so a paused run can resume on a later day."""
        markets = sorted({int(market) for market in market_nos})
        interval_list = list(dict.fromkeys(str(interval) for interval in intervals))
        if not markets or not interval_list:
            return []
        job_table = self._validate_table_name(job_table)
        instrument_table = self._validate_table_name(instrument_table)
        market_marks = ", ".join(["%s"] * len(markets))
        interval_marks = ", ".join(["%s"] * len(interval_list))
        with self._lock:
            connection = cursor = None
            try:
                connection = self._connect_database(self._connector())
                cursor = connection.cursor(dictionary=True)
                cursor.execute(
                    "SELECT jobs.market_no, jobs.interval_code AS `interval`, jobs.start_date, jobs.end_date, "
                    "COUNT(*) AS queued_jobs "
                    f"FROM `{job_table}` AS jobs INNER JOIN `{instrument_table}` AS instruments "
                    "ON instruments.market_no = jobs.market_no AND instruments.symbol = jobs.symbol "
                    f"WHERE jobs.market_no IN ({market_marks}) AND jobs.interval_code IN ({interval_marks}) "
                    "AND jobs.status = 'pending' AND instruments.is_active = TRUE "
                    "AND instruments.category_no NOT IN (94, 98) "
                    "AND NOT (instruments.market_no = 1 AND instruments.category_no = 198) "
                    f"AND {backfill_symbol_predicate('instruments')} "
                    "GROUP BY jobs.market_no, jobs.interval_code, jobs.start_date, jobs.end_date "
                    "ORDER BY jobs.end_date, jobs.start_date, jobs.interval_code",
                    (*markets, *interval_list),
                )
                return [dict(row) for row in cursor.fetchall()]
            except Exception as error:
                raise MySQLRepositoryError(f"讀取待續跑 K 線工作失敗：{error}") from error
            finally:
                if cursor is not None:
                    cursor.close()
                if connection is not None:
                    connection.close()

    def requeue_latest_failed_backfill_jobs(
        self, market_nos: Iterable[int], intervals: Iterable[str], job_table: str = DEFAULT_JOB_TABLE,
        instrument_table: str = DEFAULT_INSTRUMENT_TABLE,
    ) -> list[dict[str, object]]:
        """Move only currently-failed latest jobs back to pending.

        Older failed ranges are audit history and are deliberately left alone
        when a newer job for the same symbol and interval has succeeded.
        """
        markets = sorted({int(market_no) for market_no in market_nos})
        interval_list = list(dict.fromkeys(str(interval) for interval in intervals))
        if not markets or not interval_list:
            raise MySQLRepositoryError("市場別與 K 線週期不可為空")
        job_table = self._validate_table_name(job_table)
        instrument_table = self._validate_table_name(instrument_table)
        market_marks = ", ".join(["%s"] * len(markets))
        interval_marks = ", ".join(["%s"] * len(interval_list))
        with self._lock:
            connector = self._connector()
            connection = None
            cursor = None
            try:
                connection = self._connect_database(connector)
                connection.start_transaction()
                cursor = connection.cursor()
                cursor.execute(
                    "WITH ranked_jobs AS ("
                    "SELECT jobs.id, jobs.market_no, jobs.interval_code, jobs.start_date, jobs.end_date, jobs.status, "
                    "ROW_NUMBER() OVER (PARTITION BY jobs.market_no, jobs.symbol, jobs.interval_code "
                    "ORDER BY jobs.updated_at DESC, jobs.id DESC) AS job_rank "
                    f"FROM `{job_table}` AS jobs INNER JOIN `{instrument_table}` AS instruments "
                    "ON instruments.market_no = jobs.market_no AND instruments.symbol = jobs.symbol "
                    f"WHERE jobs.market_no IN ({market_marks}) "
                    f"AND jobs.interval_code IN ({interval_marks}) AND instruments.is_active = TRUE "
                    "AND instruments.category_no NOT IN (94, 98) "
                    f"AND NOT (instruments.market_no = 1 AND instruments.category_no = 198) AND {backfill_symbol_predicate('instruments')}"
                    ") SELECT id, market_no, interval_code, start_date, end_date "
                    "FROM ranked_jobs WHERE job_rank = 1 AND status = 'failed' "
                    "ORDER BY market_no, interval_code, start_date, end_date, id",
                    (*markets, *interval_list),
                )
                failed_rows = list(cursor.fetchall())
                if not failed_rows:
                    connection.commit()
                    return []
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                cursor.executemany(
                    f"UPDATE `{job_table}` SET status = 'pending', received_count = 0, "
                    "last_error = NULL, started_at = NULL, completed_at = NULL, updated_at = %s "
                    "WHERE id = %s AND status = 'failed'",
                    [(now, int(row[0])) for row in failed_rows],
                )
                groups: dict[tuple[int, str, date, date], int] = {}
                for _job_id, market, interval, start_date, end_date in failed_rows:
                    key = (int(market), str(interval), start_date, end_date)
                    groups[key] = groups.get(key, 0) + 1
                connection.commit()
                return [
                    {
                        "market_no": market, "interval": interval,
                        "start_date": start_date, "end_date": end_date, "queued_jobs": count,
                    }
                    for (market, interval, start_date, end_date), count in sorted(groups.items())
                ]
            except Exception as error:
                if connection is not None:
                    connection.rollback()
                raise MySQLRepositoryError(f"重新排入失敗的 K 線工作失敗：{error}") from error
            finally:
                if cursor is not None:
                    cursor.close()
                if connection is not None:
                    connection.close()

    def reset_running_backfill_jobs(
        self, interval: str, start_date: date, end_date: date, job_table: str = DEFAULT_JOB_TABLE,
        market_no: int | None = None,
    ) -> int:
        """Return interrupted jobs to pending; use only with one worker."""
        job_table = self._validate_table_name(job_table)
        with self._lock:
            connector = self._connector()
            connection = None
            cursor = None
            try:
                connection = self._connect_database(connector)
                cursor = connection.cursor()
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                market_condition = " AND market_no = %s" if market_no is not None else ""
                cursor.execute(
                    f"UPDATE `{job_table}` SET status = 'pending', updated_at = %s "
                    "WHERE status = 'running' AND interval_code = %s AND start_date = %s AND end_date = %s"
                    f" AND {backfill_symbol_predicate()}{market_condition}",
                    (now, interval, start_date, end_date, *((market_no,) if market_no is not None else ())),
                )
                changed = cursor.rowcount
                connection.commit()
                return int(changed)
            except Exception as error:
                if connection is not None:
                    connection.rollback()
                raise MySQLRepositoryError(f"重設 K 線回補進度失敗：{error}") from error
            finally:
                if cursor is not None:
                    cursor.close()
                if connection is not None:
                    connection.close()

    def claim_backfill_job(
        self, interval: str, start_date: date, end_date: date, include_failed: bool = False,
        job_table: str = DEFAULT_JOB_TABLE, market_no: int | None = None,
    ) -> dict[str, object] | None:
        statuses = ("pending", "failed") if include_failed else ("pending",)
        job_table = self._validate_table_name(job_table)
        placeholders = ", ".join(["%s"] * len(statuses))
        market_condition = "AND market_no = %s " if market_no is not None else ""
        with self._lock:
            connector = self._connector()
            connection = None
            cursor = None
            try:
                connection = self._connect_database(connector)
                connection.start_transaction()
                cursor = connection.cursor(dictionary=True)
                cursor.execute(
                    f"SELECT id, market_no, symbol, interval_code, start_date, end_date "
                    f"FROM `{job_table}` WHERE status IN ({placeholders}) "
                    "AND interval_code = %s AND start_date = %s AND end_date = %s "
                    f"AND {backfill_symbol_predicate()} {market_condition}ORDER BY id LIMIT 1 FOR UPDATE",
                    (*statuses, interval, start_date, end_date, *((market_no,) if market_no is not None else ())),
                )
                job = cursor.fetchone()
                if job is None:
                    connection.commit()
                    return None
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                cursor.execute(
                    f"UPDATE `{job_table}` SET status = 'running', attempt_count = attempt_count + 1, "
                    "started_at = %s, updated_at = %s, last_error = NULL WHERE id = %s",
                    (now, now, job["id"]),
                )
                connection.commit()
                return dict(job)
            except Exception as error:
                if connection is not None:
                    connection.rollback()
                raise MySQLRepositoryError(f"取得下一個 K 線回補工作失敗：{error}") from error
            finally:
                if cursor is not None:
                    cursor.close()
                if connection is not None:
                    connection.close()

    def finish_backfill_job(
        self, job_id: int, received_count: int, one_year_minimum: int,
        job_table: str = DEFAULT_JOB_TABLE,
    ) -> str:
        # Keep this within candle_backfill_jobs.status VARCHAR(24).
        status = "completed" if received_count >= one_year_minimum else "insufficient_history"
        job_table = self._validate_table_name(job_table)
        with self._lock:
            connector = self._connector()
            connection = None
            cursor = None
            try:
                connection = self._connect_database(connector)
                cursor = connection.cursor()
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                cursor.execute(
                    f"UPDATE `{job_table}` SET status = %s, received_count = %s, completed_at = %s, "
                    "updated_at = %s WHERE id = %s",
                    (status, received_count, now, now, job_id),
                )
                connection.commit()
                return status
            except Exception as error:
                if connection is not None:
                    connection.rollback()
                raise MySQLRepositoryError(f"更新 K 線回補結果失敗：{error}") from error
            finally:
                if cursor is not None:
                    cursor.close()
                if connection is not None:
                    connection.close()

    def pause_backfill_job(
        self, job_id: int, error_message: str, job_table: str = DEFAULT_JOB_TABLE,
        *, received_count: int = 0,
    ) -> None:
        """Return ONLY the owned running row to pending after a connection outage.

        Keep attempts and the pause reason for diagnosis. Already-upserted
        windows remain in the candle table and are safely upserted on resume.
        """
        job_table = self._validate_table_name(job_table)
        with self._lock:
            connector = self._connector()
            connection = cursor = None
            try:
                connection = self._connect_database(connector)
                cursor = connection.cursor()
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                cursor.execute(
                    f"UPDATE `{job_table}` SET status = 'pending', received_count = %s, "
                    "started_at = NULL, completed_at = NULL, last_error = %s, updated_at = %s "
                    "WHERE id = %s AND status = 'running'",
                    (received_count, error_message[:4000], now, job_id),
                )
                if cursor.rowcount != 1:
                    raise MySQLRepositoryError("暫停 K 線回補失敗：工作已不在 running 狀態")
                connection.commit()
            except Exception as error:
                if connection is not None:
                    connection.rollback()
                raise MySQLRepositoryError(f"保留 K 線回補等待工作失敗：{error}") from error
            finally:
                if cursor is not None:
                    cursor.close()
                if connection is not None:
                    connection.close()

    def fail_backfill_job(self, job_id: int, error_message: str, job_table: str = DEFAULT_JOB_TABLE) -> None:
        job_table = self._validate_table_name(job_table)
        with self._lock:
            connector = self._connector()
            connection = None
            cursor = None
            try:
                connection = self._connect_database(connector)
                cursor = connection.cursor()
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                cursor.execute(
                    f"UPDATE `{job_table}` SET status = 'failed', last_error = %s, updated_at = %s WHERE id = %s",
                    (error_message[:4000], now, job_id),
                )
                connection.commit()
            except Exception as error:
                if connection is not None:
                    connection.rollback()
                raise MySQLRepositoryError(f"記錄 K 線回補失敗原因失敗：{error}") from error
            finally:
                if cursor is not None:
                    cursor.close()
                if connection is not None:
                    connection.close()
