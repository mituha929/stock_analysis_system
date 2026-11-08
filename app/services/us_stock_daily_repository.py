"""Atomic daily prices + durable once-per-symbol/session completion, with no deletes."""
from datetime import datetime, timezone
import hashlib
import uuid

from app.services.mysql_instrument_repository import MySQLInstrumentRepository, MySQLRepositoryError
from app.services.us_stock_daily import SOURCE, BAR_COUNT, COMPANIES, utc_naive

TABLE = "external_us_stock_daily"
UPDATES = "external_us_stock_daily_updates"


class StockDailyWriteError(MySQLRepositoryError):
    def __init__(self, batch_id, commit_uncertain, cause):
        self.batch_id, self.commit_uncertain = batch_id, commit_uncertain
        super().__init__(f"美股日K寫入失敗（{type(cause).__name__}），既有資料保留")


class USStockDailyRepository(MySQLInstrumentRepository):
    @staticmethod
    def _ensure_schema(cursor, columns):
        cursor.execute("""CREATE TABLE IF NOT EXISTS external_us_stock_daily (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            source VARCHAR(16) NOT NULL,symbol VARCHAR(16) NOT NULL,name VARCHAR(64) NOT NULL,source_name VARCHAR(160) NOT NULL,
            exchange VARCHAR(16) NOT NULL,currency CHAR(3) NOT NULL,security_type VARCHAR(32) NOT NULL,session VARCHAR(16) NOT NULL,
            underlying_market VARCHAR(16) NULL,underlying_symbol VARCHAR(16) NULL,
            adr_underlying_shares DECIMAL(18,8) NULL,adr_ratio_verified_on DATE NULL,
            market_date DATE NOT NULL,exchange_timezone VARCHAR(48) NOT NULL,
            source_bar_time_utc DATETIME NOT NULL,session_open_utc DATETIME NOT NULL,session_close_utc DATETIME NOT NULL,
            open_price DECIMAL(24,8) NOT NULL,high_price DECIMAL(24,8) NOT NULL,low_price DECIMAL(24,8) NOT NULL,
            close_price DECIMAL(24,8) NOT NULL,previous_close DECIMAL(24,8) NOT NULL,
            change_value DECIMAL(24,8) NOT NULL,change_percent DECIMAL(24,8) NOT NULL,
            volume BIGINT UNSIGNED NULL,volume_status VARCHAR(48) NOT NULL,
            adjusted_close DECIMAL(24,8) NULL,adjustment_status VARCHAR(48) NOT NULL,corporate_action_status VARCHAR(48) NOT NULL,
            source_url VARCHAR(512) NOT NULL,fetched_at_utc DATETIME(6) NOT NULL,batch_id CHAR(32) NOT NULL,source_record_json LONGTEXT NOT NULL,
            UNIQUE KEY uq_external_us_stock_day(source,symbol,market_date,session),
            INDEX idx_external_us_stock_date(market_date,symbol)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS external_us_stock_daily_updates (
            source VARCHAR(16) NOT NULL,symbol VARCHAR(16) NOT NULL,market_date DATE NOT NULL,
            batch_id CHAR(32) NOT NULL,bar_count SMALLINT UNSIGNED NOT NULL,
            first_market_date DATE NOT NULL,last_market_date DATE NOT NULL,
            fetched_at_utc DATETIME(6) NOT NULL,completed_at_utc DATETIME(6) NOT NULL,
            PRIMARY KEY(source,symbol,market_date), INDEX idx_stock_daily_completed(source,market_date,symbol)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
        for table, required in ((TABLE, set(columns) | {"id"}), (UPDATES, {"source", "symbol", "market_date", "batch_id", "bar_count", "first_market_date", "last_market_date", "fetched_at_utc", "completed_at_utc"})):
            cursor.execute("SHOW COLUMNS FROM `" + table + "`")
            if not required <= {row["Field"] for row in cursor.fetchall()}:
                raise ValueError("incompatible_stock_daily_schema")
            cursor.execute("SELECT ENGINE AS engine FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=%s", (table,))
            engine = cursor.fetchone()
            if not engine or next(iter(engine.values())).upper() != "INNODB":
                raise ValueError("stock_daily_requires_innodb")
        for table, expected in ((TABLE, ("source", "symbol", "market_date", "session")),
                                (UPDATES, ("source", "symbol", "market_date"))):
            cursor.execute("SHOW INDEX FROM `" + table + "`")
            unique = {}
            for row in cursor.fetchall():
                if not row["Non_unique"]:
                    unique.setdefault(row["Key_name"], []).append((row["Seq_in_index"], row["Column_name"]))
            if expected not in [tuple(c for _, c in sorted(values)) for values in unique.values()]:
                raise ValueError("missing_stock_daily_unique_key")

    def completed_symbols(self, market_date):
        connection = cursor = None
        try:
            connection = self._connect_database(self._connector())
            cursor = connection.cursor(dictionary=True)
            cursor.execute("SELECT 1 FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=%s", (UPDATES,))
            if not cursor.fetchone():
                return set()
            cursor.execute("SELECT symbol FROM external_us_stock_daily_updates WHERE source=%s AND market_date=%s AND bar_count=%s", (SOURCE, market_date, BAR_COUNT))
            return {row["symbol"] for row in cursor.fetchall()} & set(COMPANIES)
        finally:
            if connection is not None:
                connection.rollback()
            if cursor is not None:
                cursor.close()
            if connection is not None:
                connection.close()

    def save(self, batch, *, sessions=None):
        batch.validate(sessions)  # No connection or DDL before validation succeeds.
        batch_id = uuid.uuid4().hex
        rows = [{**row, "batch_id": batch_id} for row in batch.rows]
        columns = tuple(rows[0])
        names = ",".join("`" + name + "`" for name in columns)
        connection = cursor = None
        acquired = commit_attempted = False
        lock = "usdaily:" + hashlib.sha256((self.settings.database + ":" + batch.symbol).encode()).hexdigest()[:55]
        try:
            connection = self._connect_database(self._connector())
            cursor = connection.cursor(dictionary=True)
            cursor.execute("SELECT GET_LOCK(%s,0) AS acquired", (lock,))
            acquired = cursor.fetchone()["acquired"] == 1
            if not acquired:
                return {"status": "busy"}
            self._ensure_schema(cursor, columns)
            connection.commit()  # Metadata reads can also start a transaction.
            connection.start_transaction()
            cursor.execute("SELECT batch_id,bar_count FROM external_us_stock_daily_updates WHERE source=%s AND symbol=%s AND market_date=%s", (SOURCE, batch.symbol, batch.market_date))
            previous = cursor.fetchone()
            if previous:
                if previous["bar_count"] != BAR_COUNT:
                    raise ValueError("incompatible_daily_completion")
                connection.rollback()
                return dict(status="already_completed", **previous)
            cursor.execute("SELECT MAX(fetched_at_utc) AS newest FROM external_us_stock_daily WHERE source=%s AND symbol=%s AND market_date BETWEEN %s AND %s", (SOURCE, batch.symbol, rows[0]["market_date"], batch.market_date))
            newest = cursor.fetchone()["newest"]
            if newest and newest > utc_naive(batch.fetched_at):
                raise ValueError("source_batch_older_than_existing_data")
            updates = ",".join("`"+name+"`=VALUES(`"+name+"`)" for name in columns if name not in {"source", "symbol", "market_date", "session"})
            cursor.executemany("INSERT INTO external_us_stock_daily ("+names+") VALUES ("+",".join(["%s"]*len(columns))+") ON DUPLICATE KEY UPDATE "+updates,
                               [tuple(row[name] for name in columns) for row in rows])
            cursor.execute("SELECT "+names+" FROM external_us_stock_daily WHERE source=%s AND symbol=%s AND batch_id=%s ORDER BY market_date", (SOURCE, batch.symbol, batch_id))
            actual = cursor.fetchall()
            if len(actual) != BAR_COUNT or any(any(a[key] != expected[key] for key in columns) for a, expected in zip(actual, rows)):
                raise ValueError("stock_daily_readback_mismatch")
            cursor.execute("""INSERT INTO external_us_stock_daily_updates
                (source,symbol,market_date,batch_id,bar_count,first_market_date,last_market_date,fetched_at_utc,completed_at_utc)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (SOURCE, batch.symbol, batch.market_date, batch_id, BAR_COUNT, rows[0]["market_date"], batch.market_date,
                 utc_naive(batch.fetched_at), datetime.now(timezone.utc).replace(tzinfo=None)))
            commit_attempted = True
            connection.commit()
            return dict(status="completed", batch_id=batch_id, bar_count=BAR_COUNT,
                        first_market_date=rows[0]["market_date"].isoformat(), last_market_date=batch.market_date.isoformat())
        except Exception as error:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
            raise StockDailyWriteError(batch_id, commit_attempted, error) from error
        finally:
            if acquired and cursor is not None:
                try:
                    cursor.execute("SELECT RELEASE_LOCK(%s)", (lock,))
                    cursor.fetchall()
                except Exception:
                    pass
            if cursor is not None:
                cursor.close()
            if connection is not None:
                connection.close()
