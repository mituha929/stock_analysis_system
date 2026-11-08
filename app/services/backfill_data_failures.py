"""Durable consecutive DATA failures; never reuse total job attempt_count."""
from datetime import datetime, timedelta, timezone
import json

from app.services.candle_validation import DATA_FAILURE_LIMIT, DATA_RETRY_SECONDS
from app.services.mysql_instrument_repository import MySQLRepositoryError


class BackfillDataFailures:
    def ensure_data_failure_storage(self):
        if getattr(self, "_data_failure_storage_ready", False):
            return
        with self.transaction() as cursor:
            cursor.execute("""CREATE TABLE IF NOT EXISTS candle_backfill_data_failures (
                job_table VARCHAR(64) NOT NULL, job_id BIGINT UNSIGNED NOT NULL,
                cursor_date DATE NULL, window_start DATE NULL, window_end DATE NULL,
                consecutive_failures INT UNSIGNED NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL, details JSON NOT NULL,
                first_failed_at DATETIME(6) NOT NULL, last_failed_at DATETIME(6) NOT NULL,
                updated_at DATETIME(6) NOT NULL,
                PRIMARY KEY(job_table,job_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
        self._data_failure_storage_ready = True

    def record_data_failure(self, job, first, last, error):
        self.ensure_data_failure_storage()
        table = self._validate_table_name(job["job_table"])
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        key = (table, job["id"])
        window = (job["next_date"], first, last)
        details = dict(error.details, symbol=job["symbol"], market_no=job["market_no"],
                       interval=job["interval_code"], cursor_date=str(job["next_date"]),
                       window_start=str(first), window_end=str(last))
        with self.transaction() as cursor:
            cursor.execute(f"SELECT p.owner_token,p.next_date,j.status FROM candle_backfill_progress p "
                           f"JOIN `{table}` j ON j.id=p.job_id WHERE p.job_table=%s AND p.job_id=%s FOR UPDATE", key)
            owner = cursor.fetchone()
            if owner is None or owner != (job["owner_token"], job["next_date"], "running"):
                raise MySQLRepositoryError("工作擁有者或斷點已變更，未累計資料錯誤")
            cursor.execute("SELECT cursor_date,window_start,window_end,consecutive_failures FROM candle_backfill_data_failures "
                           "WHERE job_table=%s AND job_id=%s FOR UPDATE", key)
            old = cursor.fetchone()
            count = int(old[3])+1 if old is not None and tuple(old[:3]) == window else 1
            failed = count >= DATA_FAILURE_LIMIT
            reason = (f"來源資料異常：同區間連續失敗 {count}/{DATA_FAILURE_LIMIT} 次；"
                      + ("已隔離此工作，其他商品可繼續；" if failed else f"保留斷點，{DATA_RETRY_SECONDS} 秒後重試；")
                      + str(error))[:4000]
            cursor.execute("""INSERT INTO candle_backfill_data_failures
                (job_table,job_id,cursor_date,window_start,window_end,consecutive_failures,last_error,details,
                 first_failed_at,last_failed_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE cursor_date=VALUES(cursor_date),window_start=VALUES(window_start),
                window_end=VALUES(window_end),consecutive_failures=VALUES(consecutive_failures),
                last_error=VALUES(last_error),details=VALUES(details),
                first_failed_at=IF(VALUES(consecutive_failures)=1,VALUES(first_failed_at),first_failed_at),
                last_failed_at=VALUES(last_failed_at),updated_at=VALUES(updated_at)""",
                (*key, *window, count, reason, json.dumps(details,ensure_ascii=False,default=str), now, now, now))
            cursor.execute(f"UPDATE `{table}` SET status=%s,last_error=%s,updated_at=%s WHERE id=%s AND status='running'",
                           ("failed" if failed else "pending", reason, now, job["id"]))
            if cursor.rowcount != 1:
                raise MySQLRepositoryError("工作狀態已變更，未保存資料錯誤計次")
            cursor.execute("UPDATE candle_backfill_progress SET owner_token=NULL WHERE job_table=%s AND job_id=%s", key)
            if job.get("phase") and not failed:
                cursor.execute("UPDATE candle_batch_work SET not_before=%s,last_round=%s WHERE job_table=%s AND job_id=%s",
                               (now+timedelta(seconds=DATA_RETRY_SECONDS),job["owner_token"],*key))
        return dict(count=count,failed=failed,reason=reason,details=details,
                    retry_at=(now+timedelta(seconds=DATA_RETRY_SECONDS)).replace(tzinfo=timezone.utc).isoformat())

    def reset_data_failures(self, cursor, job, now):
        if getattr(self, "_data_failure_storage_ready", False):
            # Same transaction as candle rows AND their next_date. A rollback
            # must not forgive the preceding failures. Retain the last raw
            # diagnostic for audit even when its active counter becomes zero.
            cursor.execute("UPDATE candle_backfill_data_failures SET consecutive_failures=0,updated_at=%s "
                           "WHERE job_table=%s AND job_id=%s", (now,job["job_table"],job["id"]))
