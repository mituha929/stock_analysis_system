"""Additive per-group planning and a separate, bounded slow-recovery queue."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.services.backfill_policy import initial_start, period_start
from app.services.backfill_eligibility import backfill_symbol_predicate
from app.services.mysql_instrument_repository import MySQLRepositoryError
from app.services.instrument_lifecycle import blocked_reason


def as_date(value):
    return value.date() if isinstance(value, datetime) else value


def choose_work(interval, end, count, earliest, latest, state, target, force=False, explicit_start=None, listing_boundary=None):
    checked = max((d for d in (state.get("checked_through"),state.get("verified_through")) if d),default=None)
    history = count < target and not bool(state.get("initialized")) and explicit_start is None
    if checked and checked >= end and not force and not history:
        return None
    latest_start = period_start(interval, min(explicit_start or checked or latest or end, end))
    floor = min(initial_start(interval, end, target),
                initial_start(interval, earliest, max(1, target-count)) if earliest else end)
    if listing_boundary:
        floor = max(floor, period_start(interval, listing_boundary))
        latest_start = max(latest_start, period_start(interval, listing_boundary))
    history_next = period_start(interval, earliest or latest_start)-timedelta(days=1)
    phase = "history" if checked and checked >= end and not force and history else "latest"
    return dict(start_date=min(floor, latest_start) if history else latest_start,
                next_date=history_next if phase == "history" else latest_start,
                history_floor=floor, history_next=history_next, needs_history=history,
                phase=phase, mode="initialization" if history else "range" if explicit_start else "incremental")


class BatchCandlePlanning:
    def ensure_batch_storage(self, candle_table, job_table):
        candles, jobs = self.ensure_incremental_storage(candle_table, job_table)
        with self.transaction() as cursor:
            cursor.execute("""CREATE TABLE IF NOT EXISTS candle_batch_work (
                job_table VARCHAR(64) NOT NULL, job_id BIGINT UNSIGNED NOT NULL,
                phase VARCHAR(12) NOT NULL, needs_history BOOLEAN NOT NULL,
                history_floor DATE NOT NULL, history_next DATE NOT NULL,
                not_before DATETIME(6) NULL, last_round VARCHAR(36) NULL,
                PRIMARY KEY(job_table,job_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS candle_check_state (
                candle_table VARCHAR(64) NOT NULL, symbol VARCHAR(32) NOT NULL,
                interval_code VARCHAR(8) NOT NULL, checked_through DATE NOT NULL,
                updated_at DATETIME(6) NOT NULL,
                PRIMARY KEY(candle_table,symbol,interval_code)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS candle_backfill_skips (
                job_table VARCHAR(64) NOT NULL, job_id BIGINT UNSIGNED NOT NULL,
                start_date DATE NOT NULL, end_date DATE NOT NULL,
                source VARCHAR(512) NOT NULL, observed_on DATE NOT NULL,
                PRIMARY KEY(job_table,job_id,start_date,end_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS candle_daily_aggregate_state (
                candle_table VARCHAR(64) NOT NULL, symbol VARCHAR(32) NOT NULL,
                interval_code VARCHAR(8) NOT NULL, candle_time DATETIME NOT NULL,
                as_of_date DATE NOT NULL, is_complete BOOLEAN NOT NULL,
                source_bars INT UNSIGNED NOT NULL, updated_at DATETIME(6) NOT NULL,
                replaced_rows JSON NULL,
                PRIMARY KEY(candle_table,symbol,interval_code,candle_time)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
        return candles, jobs

    def plan_batch(self, market, interval, end, target, candle_table, job_table, instrument_table,
                   force=False, explicit_start=None, only_symbols=None, suspensions=None, recover_running=False,
                   refresh_bars=None, aggregate_from_day=False, force_completed_refresh=False, fresh_daily=False):
        if aggregate_from_day and (interval not in {"week", "month"} or refresh_bars is None):
            raise MySQLRepositoryError("日 K 彙整僅適用週／月的日常回抓工作")
        candles, jobs = self.ensure_batch_storage(candle_table, job_table)
        if recover_running:
            self.recover_incremental_jobs(dict(job_table=jobs,market_no=market,interval=interval,end_date=end))
        if refresh_bars is not None:
            from app.services.daily_candle_refresh import plan_refresh_batch
            return plan_refresh_batch(self, market, interval, end, target, refresh_bars,
                                      candles, jobs, instrument_table, force, only_symbols,
                                      aggregate_from_day=aggregate_from_day,
                                      force_completed_refresh=force_completed_refresh, fresh_daily=fresh_daily)
        instruments = self._validate_table_name(instrument_table)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        choices, rows, checked_dates = {}, [], []
        if only_symbols is not None and not only_symbols:
            return dict(queued_jobs=0,start_date=end,end_date=end,initialization_jobs=0,incremental_jobs=0,
                        oldest_checked_through=None,suspended_symbols=0,unknown_lagging_symbols=0)
        scope = (" AND i.symbol IN ("+",".join(["%s"]*len(only_symbols))+")") if only_symbols is not None else ""
        suspended = lagging = 0
        from app.services.listing_history import listing_floor
        policies = self.load_listing_policies(candles, market)
        with self.transaction(dictionary=True) as cursor:
            # ONE read for the whole market/interval, rather than COUNT/MIN/MAX
            # for every symbol. Cursor != last traded timestamp (illiquid stocks).
            cursor.execute(f"""SELECT i.symbol,COUNT(c.candle_time) AS candle_count,
                MIN(c.candle_time) AS earliest,MAX(c.candle_time) AS latest,
                s.initialized,s.verified_through,k.checked_through
                FROM `{instruments}` i LEFT JOIN `{candles}` c ON c.symbol=i.symbol
                AND c.interval_code=%s AND c.candle_time<%s
                LEFT JOIN candle_sync_state s ON s.candle_table=%s AND s.symbol=i.symbol AND s.interval_code=%s
                LEFT JOIN candle_check_state k ON k.candle_table=%s AND k.symbol=i.symbol AND k.interval_code=%s
                WHERE i.market_no=%s AND i.is_active=TRUE AND i.category_no NOT IN(94,98)
                AND NOT(i.market_no=1 AND i.category_no=198) AND {backfill_symbol_predicate('i')}{scope}
                GROUP BY i.symbol,s.initialized,s.verified_through,k.checked_through ORDER BY i.symbol""",
                (interval,end+timedelta(days=1),candles,interval,candles,interval,market,*(only_symbols or [])))
            rows = cursor.fetchall()
            if interval in {"week","month"}:
                extents = self.aggregate_extents(cursor,candles,interval,[r["symbol"] for r in rows],end,dictionary=True)
                for row in rows:
                    row.update(extents.get(row["symbol"],dict(candle_count=0,earliest=None,latest=None)))
            # A lower goal may already be met by a waiting history job. Keep
            # that job as completed audit history (not superseded/untracked),
            # but ONLY when its latest-date check was already successful.
            cursor.execute(f"SELECT j.id,j.symbol,p.target_bars FROM `{jobs}` j JOIN candle_backfill_progress p ON p.job_table=%s AND p.job_id=j.id JOIN candle_batch_work w ON w.job_table=p.job_table AND w.job_id=p.job_id WHERE j.market_no=%s AND j.interval_code=%s AND j.end_date=%s AND j.status='pending' AND w.phase='history'",(jobs,market,interval,end))
            pending_history = {str(r["symbol"]):r for r in cursor.fetchall()}
            met_jobs, met_symbols = [], []
            for row in rows:
                symbol = str(row["symbol"])
                if blocked_reason(policies.get(symbol)) or ((policies.get(symbol) or {}).get('history_start') or end) > end:
                    continue
                checked = row["checked_through"] or row["verified_through"] or as_date(row["latest"])
                halted = bool(suspensions and suspensions.full_window(market,symbol,period_start(interval,end),end)
                              not in (None,"trading_calendar"))
                suspended += halted
                if checked and not halted: checked_dates.append(checked)
                if checked and (end-checked).days > 30 and not halted: lagging += 1
                work = choose_work(interval,end,int(row["candle_count"]),as_date(row["earliest"]),
                                   as_date(row["latest"]),row,target,force,explicit_start,
                                   listing_boundary=listing_floor(policies.get(symbol), interval))
                if work: choices[symbol] = work
                old = pending_history.get(symbol)
                successful = max((d for d in (row["checked_through"],row["verified_through"]) if d),default=None)
                if not work and old and int(old["target_bars"]) != target and int(row["candle_count"]) >= target and successful and successful >= end:
                    met_jobs.append(old["id"]); met_symbols.append(symbol)
            if met_jobs:
                cursor.executemany(f"UPDATE `{jobs}` SET status='completed',started_at=NULL,completed_at=%s,updated_at=%s,last_error=NULL WHERE id=%s AND status='pending'",[(now,now,j) for j in met_jobs])
                cursor.executemany("UPDATE candle_backfill_progress SET target_bars=%s,owner_token=NULL,updated_at=%s WHERE job_table=%s AND job_id=%s",[(target,now,jobs,j) for j in met_jobs])
                cursor.executemany("INSERT INTO candle_sync_state(candle_table,symbol,interval_code,initialized,history_exhausted,verified_through,target_bars,updated_at) VALUES(%s,%s,%s,TRUE,FALSE,NULL,%s,%s) ON DUPLICATE KEY UPDATE initialized=TRUE,target_bars=VALUES(target_bars),updated_at=VALUES(updated_at)",[(candles,s,interval,target,now) for s in met_symbols])
            if choices:
                cursor.executemany(f"INSERT INTO `{jobs}` (market_no,symbol,interval_code,start_date,end_date,status,updated_at) "
                    "VALUES(%s,%s,%s,%s,%s,'pending',%s) ON DUPLICATE KEY UPDATE "
                    "status=IF(status IN('running','waiting_review'),status,'pending'),completed_at=IF(status IN('running','waiting_review'),completed_at,NULL),"
                    "updated_at=IF(status IN('running','waiting_review'),updated_at,VALUES(updated_at))",
                    [(market,s,interval,w["start_date"],end,now) for s,w in choices.items()])
            cursor.execute(f"SELECT id,symbol,start_date,status FROM `{jobs}` WHERE market_no=%s AND interval_code=%s AND end_date=%s",(market,interval,end))
            selected = [r for r in cursor.fetchall() if r["symbol"] in choices and r["start_date"] == choices[r["symbol"]]["start_date"]]
            cursor.execute(f"SELECT w.job_id FROM candle_batch_work w JOIN `{jobs}` j ON j.id=w.job_id AND w.job_table=%s WHERE j.market_no=%s AND j.interval_code=%s AND j.end_date=%s",(jobs,market,interval,end))
            existing_work = {r["job_id"] for r in cursor.fetchall()}
            progress_rows, work_rows = [], []
            for row in selected:
                w = choices[row["symbol"]]
                # Upgrade legacy plans only while owning the worker lock. A
                # batch checkpoint already exists => never reset it on resume.
                if (row["id"] not in existing_work or force) and row["status"] not in {"running", "waiting_review"}:
                    progress_rows.append((jobs,row["id"],w["mode"],target,w["next_date"],now))
                    work_rows.append((jobs,row["id"],w["phase"],w["needs_history"],w["history_floor"],w["history_next"]))
            if progress_rows:
                cursor.executemany("INSERT INTO candle_backfill_progress(job_table,job_id,mode,target_bars,next_date,updated_at) VALUES(%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE mode=VALUES(mode),target_bars=VALUES(target_bars),next_date=VALUES(next_date),empty_windows=0,owner_token=NULL,updated_at=VALUES(updated_at)",progress_rows)
                cursor.executemany("INSERT INTO candle_batch_work(job_table,job_id,phase,needs_history,history_floor,history_next) VALUES(%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE phase=VALUES(phase),needs_history=VALUES(needs_history),history_floor=VALUES(history_floor),history_next=VALUES(history_next),not_before=NULL,last_round=NULL",work_rows)
            # Reuse same-range checkpoints with the NEW goal; do not leave an
            # old 1200 in progress metadata after week/month config changes.
            if selected:
                cursor.executemany("UPDATE candle_backfill_progress SET target_bars=%s WHERE job_table=%s AND job_id=%s AND owner_token IS NULL",[(target,jobs,r["id"]) for r in selected])
            ids = [r["id"] for r in selected]
            exclusion = " AND j.id NOT IN ("+",".join(["%s"]*len(ids))+")" if ids else ""
            cursor.execute(f"UPDATE `{jobs}` j JOIN `{instruments}` i ON i.market_no=j.market_no AND i.symbol=j.symbol "
                "SET j.status='superseded',j.last_error='由族群批次規劃取代；資料與紀錄保留',j.updated_at=%s "
                f"WHERE j.status='pending' AND j.market_no=%s AND j.interval_code=%s AND j.end_date<=%s AND i.is_active=TRUE AND {backfill_symbol_predicate('i')}"+scope+exclusion,
                (now,market,interval,end,*(only_symbols or []),*ids))
        return dict(queued_jobs=len(selected), start_date=min((r["start_date"] for r in selected),default=end),end_date=end,
                    initialization_jobs=sum(w["needs_history"] for w in choices.values()),
                    incremental_jobs=sum(not w["needs_history"] for w in choices.values()),
                    oldest_checked_through=min(checked_dates,default=None),suspended_symbols=suspended,
                    unknown_lagging_symbols=lagging)

    def batch_latest_finished(self, job, count):
        """Release the latest-gap phase; old history waits until ALL groups ran."""
        jobs, candles = map(self._validate_table_name,(job["job_table"],job["candle_table"]))
        if not job["needs_history"] or count >= job["target_bars"]:
            return False
        with self.transaction() as cursor:
            cursor.execute(f"SELECT MIN(candle_time) FROM `{candles}` WHERE symbol=%s AND interval_code=%s AND candle_time<%s",(job["symbol"],job["interval_code"],job["end_date"]+timedelta(days=1)))
            earliest = as_date(cursor.fetchone()[0])
            before = period_start(job["interval_code"],earliest)-timedelta(days=1) if earliest else job["history_next"]
            from app.services.listing_history import listing_floor
            floor = listing_floor(job.get("listing_policy"), job["interval_code"])
            if floor and before < floor:
                job["listing_boundary_reached"] = True
                return False
            cursor.execute(f"UPDATE `{jobs}` j JOIN candle_backfill_progress p ON p.job_table=%s AND p.job_id=j.id JOIN candle_batch_work w ON w.job_table=p.job_table AND w.job_id=p.job_id SET j.status='pending',p.next_date=%s,p.empty_windows=0,p.owner_token=NULL,w.phase='history',w.history_next=%s,w.not_before=NULL WHERE j.id=%s AND p.owner_token=%s",(jobs,before,before,job["id"],job["owner_token"]))
            if cursor.rowcount < 1: raise MySQLRepositoryError("工作擁有者已變更，未轉入歷史補齊")
        return True

    def defer_batch_job(self, job, reason, delay_seconds=900):
        table = self._validate_table_name(job["job_table"])
        with self.transaction() as cursor:
            cursor.execute(f"UPDATE `{table}` j JOIN candle_backfill_progress p ON p.job_table=%s AND p.job_id=j.id JOIN candle_batch_work w ON w.job_table=p.job_table AND w.job_id=p.job_id SET j.status='pending',j.last_error=%s,j.updated_at=UTC_TIMESTAMP(6),p.owner_token=NULL,w.phase=IF(w.phase='latest','catchup',w.phase),w.not_before=%s,w.last_round=%s WHERE j.id=%s AND p.owner_token=%s",(table,reason,datetime.now(timezone.utc).replace(tzinfo=None)+timedelta(seconds=delay_seconds),job["owner_token"],job["id"],job["owner_token"]))
            if cursor.rowcount < 1: raise MySQLRepositoryError("工作擁有者已變更，未修改補齊斷點")

    def batch_pending(self, plans):
        count, next_retry = 0, None
        with self.transaction() as cursor:
            for plan in plans:
                if not plan.get("batch_v2") or not plan.get("planned_on"): continue
                jobs = self._validate_table_name(plan["job_table"])
                instruments = self._validate_table_name(plan["instrument_table"])
                symbols = plan.get("only_symbols")
                if symbols is not None and not symbols: continue
                selected = " AND j.symbol IN ("+",".join(["%s"]*len(symbols))+")" if symbols is not None else ""
                ids = plan.get("allowed_ids")
                if ids is not None:
                    if not ids: continue
                    selected += " AND j.id IN ("+",".join(["%s"]*len(ids))+")"
                if plan.get("refresh_bars") is not None:
                    mode = "day_aggregate" if plan.get("aggregate_from_day") else "refresh"
                    selected += f" AND EXISTS(SELECT 1 FROM candle_backfill_progress rp WHERE rp.job_table=w.job_table AND rp.job_id=j.id AND rp.mode='{mode}')"
                cursor.execute(f"SELECT COUNT(*),MIN(w.not_before) FROM `{jobs}` j JOIN candle_batch_work w ON w.job_table=%s AND w.job_id=j.id JOIN `{instruments}` i ON i.symbol=j.symbol AND i.market_no=j.market_no WHERE i.is_active=TRUE AND i.category_no NOT IN(94,98) AND NOT(i.market_no=1 AND i.category_no=198) AND {backfill_symbol_predicate('i')} AND j.status='pending' AND j.market_no=%s AND j.interval_code=%s AND j.end_date=%s"+selected,(jobs,plan["market_no"],plan["interval"],plan["end_date"],*(symbols or []),*(ids or [])))
                amount, available = cursor.fetchone()
                count += int(amount)
                if available: next_retry = min(next_retry or available,available)
        return count, next_retry
