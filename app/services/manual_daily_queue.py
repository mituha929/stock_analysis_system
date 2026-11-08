"""Preserve and remove unfinished K-line jobs before a manual daily round."""
from datetime import datetime, timezone


def archive_and_supersede(repository, plans, token, pending_schedule=None):
    """Under the worker lease, archive old jobs before removing them from the queue.

    The archive and status changes use one MySQL transaction; repeating the
    same token after interruption is safe. Candles and checkpoint rows are
    never deleted. Existing failed/waiting-review jobs are not reclassified.
    """
    tables = {"candle_backfill_jobs", "odd_lot_candle_backfill_jobs"}
    tables.update(p["job_table"] for p in plans if p.get("job_table"))
    tables = sorted(repository._validate_table_name(name) for name in tables)
    with repository.transaction() as cursor:
        cursor.execute("""CREATE TABLE IF NOT EXISTS manual_daily_displaced_jobs (
            request_id VARCHAR(36) NOT NULL, job_table VARCHAR(64) NOT NULL,
            job_id BIGINT UNSIGNED NOT NULL, original_status VARCHAR(24) NOT NULL,
            job_snapshot JSON NOT NULL, archived_at DATETIME(6) NOT NULL,
            PRIMARY KEY(request_id,job_table,job_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with repository.transaction() as cursor:
        cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=DATABASE()")
        existing = {str(row[0]) for row in cursor.fetchall()}
        for table in tables:
            if table not in existing:
                continue
            cursor.execute(f"""INSERT INTO manual_daily_displaced_jobs
                (request_id,job_table,job_id,original_status,job_snapshot,archived_at)
                SELECT %s,%s,j.id,j.status,JSON_OBJECT(
                  'market_no',j.market_no,'symbol',j.symbol,'interval_code',j.interval_code,
                  'start_date',j.start_date,'end_date',j.end_date,'attempt_count',j.attempt_count,
                  'received_count',j.received_count,'last_error',j.last_error,
                  'started_at',j.started_at,'completed_at',j.completed_at,'updated_at',j.updated_at,
                  'mode',p.mode,'target_bars',p.target_bars,'next_date',p.next_date,
                  'empty_windows',p.empty_windows,'owner_token',p.owner_token,'progress_updated_at',p.updated_at,
                  'phase',w.phase,'needs_history',w.needs_history,'history_floor',w.history_floor,
                  'history_next',w.history_next,'not_before',w.not_before,'last_round',w.last_round),%s
                FROM `{table}` j
                LEFT JOIN candle_backfill_progress p ON p.job_table=%s AND p.job_id=j.id
                LEFT JOIN candle_batch_work w ON w.job_table=%s AND w.job_id=j.id
                WHERE j.status IN ('pending','running')
                ON DUPLICATE KEY UPDATE job_id=manual_daily_displaced_jobs.job_id""", (token,table,now,table,table))
            cursor.execute(f"""UPDATE `{table}` SET status='superseded',
                last_error='已由手動每日更新移出執行佇列；原工作與斷點已存檔',updated_at=%s
                WHERE status IN ('pending','running')""", (now,))
        run_ids = {int(p['run_id']) for p in plans if p.get('run_id')}
        if (pending_schedule or {}).get('run_id'):
            run_ids.add(int(pending_schedule['run_id']))
        if run_ids and 'daily_backfill_runs' in existing:
            placeholders = ','.join(['%s'] * len(run_ids))
            cursor.execute(f"""UPDATE daily_backfill_runs SET status='superseded',stage='superseded',
                message='已由手動每日更新移出佇列；既有行情與工作斷點已保存',
                completed_at=%s,updated_at=%s WHERE id IN ({placeholders})
                AND status IN ('running','paused')""", (now,now,*sorted(run_ids)))
        # The stored archive count remains stable if the transaction was
        # committed immediately before an API interruption and is retried.
        cursor.execute("SELECT COUNT(*) FROM manual_daily_displaced_jobs WHERE request_id=%s", (token,))
        return int(cursor.fetchone()[0])
