"""Official stock profiles in the existing MySQL schema, separate from candles."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
import uuid

from app.services.mysql_instrument_repository import MySQLInstrumentRepository, MySQLRepositoryError
from app.services.stock_master_sources import SOURCES, TAIPEI, StockSourceError, fetch_snapshots, validate_code, validate_snapshots

TABLES = {"stock_master", "stock_master_history", "stock_master_sync_runs", "stock_listing_plans"}
DATA_COLUMNS = ("code", "name", "market", "listing_date", "industry", "updated_at", "industry_code", "issuer_id",
                "source_date", "source_url", "first_seen_at", "last_seen_at", "presence_status", "date_status",
                "reported_listing_date", "last_run_id", "security_type", "source_date_kind")
BUSINESS_COLUMNS = ("code", "name", "market", "listing_date", "industry", "industry_code", "issuer_id", "presence_status", "date_status", "security_type")


def json_value(value):
    if isinstance(value, datetime):
        aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        return aware.astimezone(TAIPEI).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    return value


def encoded(value):
    return json.dumps(json_value(value), ensure_ascii=False, separators=(",", ":"))


class StockSyncError(MySQLRepositoryError):
    def __init__(self, message, run_id=None, *, commit_uncertain=False):
        self.run_id, self.commit_uncertain = run_id, commit_uncertain
        super().__init__(message)


class AmbiguousStockProfile(ValueError):
    pass


def _ensure_storage(cursor):
    # Refuse to reuse a same-named table from an unrelated application.
    expected = {
        "stock_master": set(DATA_COLUMNS),
        "stock_listing_plans": set(DATA_COLUMNS),
        "stock_master_history": {"id", "run_id", "code", "market", "object_type", "changed_at", "before_json", "after_json"},
        "stock_master_sync_runs": {"id", "trigger_kind", "schedule_key", "status", "started_at", "completed_at", "summary_json", "error_message"},
    }
    present = set()
    upgrades = []
    additions = {
        "security_type": "VARCHAR(8) NOT NULL DEFAULT 'STOCK'",
        "source_date_kind": "VARCHAR(32) NOT NULL DEFAULT 'official_report_date'",
    }
    for table, columns in expected.items():
        cursor.execute("SELECT COLUMN_NAME FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name=%s", (table,))
        actual = {row[0] for row in cursor.fetchall()}
        if actual and actual != columns:
            legacy = columns - set(additions)
            if table not in {"stock_master", "stock_listing_plans"} or not legacy <= actual <= columns:
                raise StockSyncError(f"{table} 與本功能資料結構不符；不修改既有表")
            upgrades.extend((table, column) for column in additions if column not in actual)
        if actual:
            cursor.execute("SELECT ENGINE FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=%s", (table,))
            if cursor.fetchone()[0] != "InnoDB":
                raise StockSyncError(f"{table} 必須是 InnoDB，否則無法保證回滾")
            cursor.execute("SELECT COLUMN_NAME FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name=%s AND INDEX_NAME='PRIMARY' ORDER BY SEQ_IN_INDEX", (table,))
            primary = tuple(row[0] for row in cursor.fetchall())
            if primary != (("code", "market") if table in {"stock_master", "stock_listing_plans"} else ("id",)):
                raise StockSyncError(f"{table} 主鍵不符，無法保證不重複")
            present.add(table)
    # Additive, restartable v1 -> v2 migration, only after ALL existing tables pass preflight.
    # Existing keys, dates and history stay intact; no broad ALTER/rename/rebuild.
    for table, column in upgrades:
        cursor.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {additions[column]}")
    def create(table, statement):
        if table not in present:
            cursor.execute(statement)
    create("stock_master_sync_runs", """CREATE TABLE IF NOT EXISTS stock_master_sync_runs (
        id CHAR(32) PRIMARY KEY, trigger_kind VARCHAR(16) NOT NULL, schedule_key VARCHAR(32) NULL,
        status VARCHAR(24) NOT NULL, started_at DATETIME(6) NOT NULL, completed_at DATETIME(6) NULL,
        summary_json LONGTEXT NULL, error_message TEXT NULL,
        INDEX idx_stock_runs_time(started_at), INDEX idx_stock_runs_schedule(schedule_key,status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
    for table in ("stock_master", "stock_listing_plans"):
        create(table, f"""CREATE TABLE IF NOT EXISTS `{table}` (
            code VARCHAR(6) NOT NULL, name VARCHAR(128) NOT NULL, market VARCHAR(4) NOT NULL,
            listing_date DATE NULL, industry VARCHAR(64) NULL, updated_at DATETIME(6) NOT NULL,
            industry_code VARCHAR(2) NULL, issuer_id VARCHAR(8) NULL,
            source_date DATE NOT NULL, source_url VARCHAR(256) NOT NULL,
            first_seen_at DATETIME(6) NOT NULL, last_seen_at DATETIME(6) NOT NULL,
            presence_status VARCHAR(24) NOT NULL, date_status VARCHAR(32) NOT NULL,
            reported_listing_date DATE NULL, last_run_id CHAR(32) NOT NULL,
            security_type VARCHAR(8) NOT NULL DEFAULT 'STOCK',
            source_date_kind VARCHAR(32) NOT NULL DEFAULT 'official_report_date',
            PRIMARY KEY(code,market), INDEX idx_stock_presence(market,presence_status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
    create("stock_master_history", """CREATE TABLE IF NOT EXISTS stock_master_history (
        id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY, run_id CHAR(32) NOT NULL,
        code VARCHAR(6) NOT NULL, market VARCHAR(4) NOT NULL, object_type VARCHAR(12) NOT NULL,
        changed_at DATETIME(6) NOT NULL, before_json LONGTEXT NULL, after_json LONGTEXT NOT NULL,
        INDEX idx_stock_history(code,market,id), INDEX idx_stock_history_run(run_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")


def _change(cursor, run_id, table, before, after, now):
    cursor.execute("INSERT INTO stock_master_history (run_id,code,market,object_type,changed_at,before_json,after_json) "
                   "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                   (run_id, after["code"], after["market"], "master" if table == "stock_master" else "plan", now,
                    encoded(before) if before else None, encoded(after)))


def _write_row(cursor, table, row):
    columns = ",".join(f"`{key}`" for key in DATA_COLUMNS)
    updates = ",".join(f"`{key}`=VALUES(`{key}`)" for key in DATA_COLUMNS if key not in {"code", "market", "first_seen_at"})
    cursor.execute(f"INSERT INTO `{table}` ({columns}) VALUES ({','.join(['%s']*len(DATA_COLUMNS))}) "
                   f"ON DUPLICATE KEY UPDATE {updates}", tuple(row[key] for key in DATA_COLUMNS))


def apply_snapshots(cursor, snapshots, run_id, now, previous_summary=None):
    """Called inside ONE transaction while holding the dedicated MySQL advisory lock."""
    today = now.replace(tzinfo=timezone.utc).astimezone(TAIPEI).date()
    validate_snapshots(snapshots, today)
    previous_sources = (previous_summary or {}).get("last_known_sources", (previous_summary or {}).get("sources", {}))
    for snapshot in snapshots:
        previous = previous_sources.get(snapshot.source_key, {})
        if previous.get("source_date") and snapshot.source_date.isoformat() < previous["source_date"]:
            raise StockSourceError(f"{snapshot.source_key} 來源日期倒退；保留既有資料")
        if len(snapshot.records) < previous.get("count", 0) * 0.8:
            raise StockSourceError(f"{snapshot.source_key} 名單縮減超過 20%；保留既有資料")
    existing = {}
    for table in ("stock_master", "stock_listing_plans"):
        cursor.execute(f"SELECT {','.join('`'+key+'`' for key in DATA_COLUMNS)} FROM `{table}`")
        existing[table] = {(r["code"], r["market"]): r for r in cursor.fetchall()}
    summary = {"new": 0, "changed": 0, "unchanged": 0, "newly_absent": 0, "scheduled": 0,
               "missing_dates": 0, "retained_dates": 0, "sources": {}, "warnings": []}
    seen = {"stock_master": set(), "stock_listing_plans": set()}
    covered = {(s.market, s.dataset) for s in snapshots}
    for snapshot in snapshots:
        summary["sources"][snapshot.source_key] = {"url": snapshot.source_url, "source_date": snapshot.source_date.isoformat(),
                                               "source_date_kind": snapshot.source_date_kind, "dataset": snapshot.dataset,
                                               "count": len(snapshot.records), "fingerprint": snapshot.fingerprint()}
        summary["warnings"].extend(snapshot.warnings)
        for record in snapshot.records:
            planned = record.listing_date is not None and record.listing_date > today
            table = "stock_listing_plans" if planned else "stock_master"
            key = record.code, record.market
            seen[table].add(key)
            before = existing[table].get(key)
            listed, date_status = record.listing_date, "provided" if record.listing_date else "missing"
            if listed is None:
                summary["missing_dates"] += 1
                if (before and before["listing_date"] and record.security_type == before["security_type"] == "ETF"
                        and record.name != before["name"]):
                    raise StockSourceError("ETF 缺少掛牌日且名稱改變，無法確認身分；保留舊資料")
                same_issuer = before and ((record.issuer_id and record.issuer_id == before["issuer_id"])
                    or (record.security_type == before["security_type"] == "ETF" and record.name == before["name"]))
                if before and before["listing_date"] and same_issuer:
                    listed, date_status = before["listing_date"], "retained_previous_official"
                    summary["retained_dates"] += 1
            industry, industry_code = record.industry, record.industry_code
            if industry_code is None and before and record.issuer_id and record.issuer_id == before["issuer_id"]:
                industry, industry_code = before["industry"], before["industry_code"]
            row = {"code": record.code, "name": record.name, "market": record.market,
                   "listing_date": listed, "industry": industry, "updated_at": now,
                   "industry_code": industry_code, "issuer_id": record.issuer_id,
                   "source_date": snapshot.source_date, "source_url": snapshot.source_url,
                   "first_seen_at": before["first_seen_at"] if before else now, "last_seen_at": now,
                   "presence_status": "present", "date_status": date_status,
                   "reported_listing_date": record.listing_date, "last_run_id": run_id,
                   "security_type": record.security_type, "source_date_kind": snapshot.source_date_kind}
            changed = before is None or any(before[column] != row[column] for column in BUSINESS_COLUMNS)
            if planned:
                summary["scheduled"] += 1
            else:
                summary["new" if before is None else "changed" if changed else "unchanged"] += 1
            if changed:
                _change(cursor, run_id, table, before, row, now)
            elif before:
                row["updated_at"] = before["updated_at"]
            _write_row(cursor, table, row)
    # Absence is merely a source observation, not proof of delisting. No deletion.
    for table in ("stock_master", "stock_listing_plans"):
        for key, before in existing[table].items():
            row_dataset = "etf" if before["security_type"] == "ETF" else "company"
            if (before["market"], row_dataset) not in covered:
                continue  # A company-only caller must never mark ETF rows absent, or vice versa.
            if key in seen[table] or before["presence_status"] != "present":
                continue
            activated = table == "stock_listing_plans" and key in seen["stock_master"]
            row = before | {"presence_status": "activated" if activated else "not_in_latest", "updated_at": now, "last_run_id": run_id}
            _change(cursor, run_id, table, before, row, now)
            _write_row(cursor, table, row)
            if table == "stock_master":
                summary["newly_absent"] += 1
    summary["fetched_count"] = sum(len(s.records) for s in snapshots)
    summary["last_known_sources"] = previous_sources | summary["sources"]
    return summary


class StockMasterRepository(MySQLInstrumentRepository):
    def synchronize(self, *, fetcher=fetch_snapshots, trigger="manual", schedule_key=None, now=None, retry_seconds=3600):
        if trigger not in {"manual", "weekly", "test"} or (schedule_key is not None and not re.fullmatch(r"weekly:\d{4}-\d{2}-\d{2}", schedule_key)):
            raise ValueError("更新來源／排程鍵不正確")
        aware_now = now or datetime.now(timezone.utc)
        if aware_now.tzinfo is None:
            raise ValueError("更新時間必須含時區")
        stamp = aware_now.astimezone(timezone.utc).replace(tzinfo=None)
        connection = cursor = None
        run_id = None
        acquired = commit_attempted = False
        lock_name = "stock-master:" + hashlib.sha256(self.settings.database.encode()).hexdigest()[:32]
        try:
            connection = self._connect_database(self._connector())
            cursor = connection.cursor()
            cursor.execute("SELECT GET_LOCK(%s,0)", (lock_name,))
            acquired = cursor.fetchone()[0] == 1
            connection.commit()
            if not acquired:
                return {"status": "busy", "message": "其他程序正在更新股票基本資料"}
            _ensure_storage(cursor)
            connection.commit()  # DDL may commit implicitly; never mix it with profile data.
            if schedule_key:
                cursor.execute("SELECT status,started_at FROM stock_master_sync_runs WHERE schedule_key=%s ORDER BY started_at DESC LIMIT 1", (schedule_key,))
                previous = cursor.fetchone()
                if previous and previous[0] == "completed":
                    return {"status": "already_completed", "schedule_key": schedule_key}
                if previous and previous[1] > stamp - timedelta(seconds=retry_seconds):
                    return {"status": "retry_wait", "schedule_key": schedule_key}
            # The exclusive session lock proves an older running owner no longer holds it.
            cursor.execute("UPDATE stock_master_sync_runs SET status='interrupted',completed_at=%s,error_message='前次程序中斷；既有資料保留' WHERE status='running'", (stamp,))
            run_id = uuid.uuid4().hex
            cursor.execute("INSERT INTO stock_master_sync_runs (id,trigger_kind,schedule_key,status,started_at) VALUES (%s,%s,%s,'running',%s)", (run_id, trigger, schedule_key, stamp))
            connection.commit()
            snapshots = fetcher()  # HTTP outside transaction; the dedicated lock remains held.
            validate_snapshots(snapshots, aware_now.astimezone(TAIPEI).date())
            connection.start_transaction()
            cursor.execute("SELECT summary_json FROM stock_master_sync_runs WHERE status='completed' ORDER BY completed_at DESC LIMIT 1")
            previous = cursor.fetchone()
            previous_summary = json.loads(previous[0]) if previous else None
            cursor.close()
            cursor = connection.cursor(dictionary=True)
            summary = apply_snapshots(cursor, snapshots, run_id, stamp, previous_summary)
            cursor.execute("UPDATE stock_master_sync_runs SET status='completed',completed_at=%s,summary_json=%s WHERE id=%s AND status='running'", (datetime.now(timezone.utc).replace(tzinfo=None), encoded(summary), run_id))
            if cursor.rowcount != 1:
                raise StockSyncError("更新紀錄不在 running 狀態")
            commit_attempted = True
            connection.commit()
            return {"status": "completed", "run_id": run_id, **summary}
        except Exception as error:
            if connection:
                try:
                    connection.rollback()
                    if run_id and not commit_attempted:
                        message = str(error) if isinstance(error, (StockSourceError, StockSyncError)) else type(error).__name__
                        cursor.execute("UPDATE stock_master_sync_runs SET status='failed',completed_at=%s,error_message=%s WHERE id=%s AND status='running'", (datetime.now(timezone.utc).replace(tzinfo=None), message[:2000], run_id))
                        connection.commit()
                except Exception:
                    pass  # An unavailable DB cannot reliably persist a failure log.
            reason = str(error) if isinstance(error, (StockSourceError, StockSyncError)) else type(error).__name__
            raise StockSyncError(f"股票基本資料更新失敗：{reason}", run_id, commit_uncertain=commit_attempted) from error
        finally:
            if connection:
                try:
                    if acquired:
                        cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
                        cursor.fetchone()
                except Exception:
                    pass
                try:
                    if cursor:
                        cursor.close()
                finally:
                    connection.close()

    def _read(self, action):
        connection = cursor = None
        try:
            connection = self._connect_database(self._connector())
            connection.start_transaction(readonly=True)
            cursor = connection.cursor(dictionary=True)
            return action(cursor)
        except (KeyError, AmbiguousStockProfile, StockSourceError):
            raise
        except Exception as error:
            raise MySQLRepositoryError("基本資料尚未初始化或 MySQL 暫時無法讀取") from error
        finally:
            if connection:
                try:
                    connection.rollback()
                finally:
                    if cursor:
                        cursor.close()
                    connection.close()

    def profile(self, code: str, market: str | None = None):
        validate_code(code)
        if market is not None and market not in SOURCES:
            raise StockSourceError("市場僅支援 TWSE／TPEx")
        def read(cursor):
            cursor.execute("SELECT " + ",".join("`"+c+"`" for c in DATA_COLUMNS) + " FROM stock_master WHERE code=%s ORDER BY market", (code,))
            history = cursor.fetchall()
            candidates = [row for row in history if row["market"] == market] if market else [row for row in history if row["presence_status"] == "present"]
            if not candidates and not market:
                candidates = history
            if not candidates:
                raise KeyError(code)
            if len(candidates) != 1:
                raise AmbiguousStockProfile("此代號有多個歷史市場，請用 market 指定 TWSE 或 TPEx")
            row = candidates[0]
            fields = {key: row[key] for key in ("code", "name", "market", "listing_date", "industry", "updated_at", "security_type")}
            fields["source"] = {key: row[key] for key in ("source_url", "source_date", "source_date_kind", "industry_code", "issuer_id", "last_seen_at", "presence_status", "date_status", "reported_listing_date")}
            fields["market_history"] = [{key: r[key] for key in ("market", "listing_date", "name", "security_type", "issuer_id", "presence_status", "first_seen_at", "last_seen_at")} for r in history]
            return json_value(fields)
        return self._read(read)

    def history(self, code: str, limit=50, offset=0):
        validate_code(code)
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("分頁範圍無效")
        def read(cursor):
            cursor.execute("SELECT COUNT(*) AS total FROM stock_master_history WHERE code=%s", (code,))
            total = cursor.fetchone()["total"]
            cursor.execute("SELECT * FROM stock_master_history WHERE code=%s ORDER BY id DESC LIMIT %s OFFSET %s", (code, limit, offset))
            rows = cursor.fetchall()
            for row in rows:
                row["before"] = json.loads(row.pop("before_json")) if row["before_json"] else None
                row["after"] = json.loads(row.pop("after_json"))
                row.pop("before_json", None)
            return json_value({"total": total, "items": rows, "limit": limit, "offset": offset})
        return self._read(read)

    def listing_plans(self, code: str):
        validate_code(code)
        def read(cursor):
            cursor.execute("SELECT code,name,market,listing_date AS planned_listing_date,industry,security_type,source_date,source_date_kind,source_url,presence_status,updated_at FROM stock_listing_plans WHERE code=%s ORDER BY market", (code,))
            return json_value({"code": code, "items": cursor.fetchall()})
        return self._read(read)

    def sync_runs(self, limit=10):
        if not 1 <= limit <= 100:
            raise ValueError("limit 必須介於 1～100")
        def read(cursor):
            cursor.execute("SELECT * FROM stock_master_sync_runs ORDER BY started_at DESC LIMIT %s", (limit,))
            rows = cursor.fetchall()
            for row in rows:
                row["summary"] = json.loads(row.pop("summary_json")) if row["summary_json"] else None
                row.pop("summary_json", None)
            return json_value({"items": rows})
        return self._read(read)
