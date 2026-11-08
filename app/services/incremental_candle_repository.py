"""Additive checkpoints over the existing candle/job tables.

Each successful window commits candle rows AND its cursor in one transaction.
Old jobs/candles are retained. Only waiting jobs replaced by a new plan are
marked superseded; an active worker is protected by the worker file lock.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json

from app.services.backfill_policy import period_start, select_range, complete_aggregate, completed_end
from app.services.backfill_eligibility import backfill_symbol_predicate
from app.services.batch_candle_planning import BatchCandlePlanning
from app.services.day_candle_integrity_repository import DayIntegrityRepository
from app.services.backfill_data_failures import BackfillDataFailures
from app.services.empty_history_review import EmptyHistoryReviewRepository
from app.services.candle_validation import CandleDataError, validate_candle_volumes
from app.services.history_exclusions import excluded_periods
from app.services.listing_history import ListingHistoryRepository, listing_floor
from app.services.instrument_lifecycle import InstrumentLifecycleRepository, blocked_reason
from app.services.trade_activity import TradeActivityRepository
from app.services.mysql_instrument_repository import MySQLCandleRepository as LegacyRepository, MySQLRepositoryError


class MySQLCandleRepository(InstrumentLifecycleRepository, TradeActivityRepository, ListingHistoryRepository, DayIntegrityRepository, BackfillDataFailures, EmptyHistoryReviewRepository, BatchCandlePlanning, LegacyRepository):
    @staticmethod
    def _fetch_duration_seconds(started_at, completed_at, attempt_count):
        # started_at is reset on retry/resume. Without a complete attempt
        # ledger, using the final segment would understate those job times.
        if attempt_count != 1 or started_at is None or completed_at is None:
            return None
        seconds = (completed_at-started_at).total_seconds()
        return seconds if seconds >= 0 else None

    @contextmanager
    def transaction(self, dictionary=False):
        connection = cursor = None
        try:
            connection = self._connect_database(self._connector())
            cursor = connection.cursor(dictionary=dictionary)
            yield cursor
            connection.commit()
        except BaseException as error:
            if connection is not None:
                connection.rollback()
            if isinstance(error, CandleDataError):
                raise
            if isinstance(error, Exception):
                raise MySQLRepositoryError(f"K 線進度資料庫操作失敗：{error}") from error
            raise
        finally:
            if cursor is not None:
                cursor.close()
            if connection is not None:
                connection.close()

    def ensure_incremental_storage(self, candle_table, job_table):
        candle_table, job_table = self.ensure_backfill_storage(candle_table, job_table)
        with self.transaction() as cursor:
            self._ensure_candle_table(cursor, candle_table)
            self._ensure_backfill_job_table(cursor, job_table)
            cursor.execute("""CREATE TABLE IF NOT EXISTS candle_backfill_progress (
                job_table VARCHAR(64) NOT NULL, job_id BIGINT UNSIGNED NOT NULL,
                mode VARCHAR(20) NOT NULL, target_bars INT UNSIGNED NOT NULL,
                next_date DATE NULL, empty_windows INT UNSIGNED NOT NULL DEFAULT 0,
                owner_token VARCHAR(36) NULL, updated_at DATETIME(6) NOT NULL,
                PRIMARY KEY(job_table, job_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS candle_sync_state (
                candle_table VARCHAR(64) NOT NULL, symbol VARCHAR(32) NOT NULL,
                interval_code VARCHAR(8) NOT NULL, initialized BOOLEAN NOT NULL DEFAULT FALSE,
                history_exhausted BOOLEAN NOT NULL DEFAULT FALSE,
                verified_through DATE NULL, target_bars INT UNSIGNED NOT NULL,
                updated_at DATETIME(6) NOT NULL,
                PRIMARY KEY(candle_table, symbol, interval_code)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
        self.ensure_data_failure_storage()
        self.ensure_empty_review_storage()
        self.ensure_lifecycle_storage()
        return candle_table, job_table

    def plan_incremental(self, market, interval, end, target, candle_table, job_table,
                         instrument_table, force=False, explicit_start=None, only_symbols=None):
        candle_table, job_table = self.ensure_incremental_storage(candle_table, job_table)
        instrument_table = self._validate_table_name(instrument_table)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if only_symbols is not None and not only_symbols:
            return dict(queued_jobs=0,start_date=end,end_date=end,initialization_jobs=0,incremental_jobs=0)
        symbol_filter = ("AND i.symbol IN ("+",".join(["%s"]*len(only_symbols))+") ") if only_symbols is not None else ""
        policies = self.load_listing_policies(candle_table, market)
        with self.transaction(dictionary=True) as cursor:
            cursor.execute(
                f"SELECT i.symbol, COUNT(c.candle_time) AS candle_count, MAX(c.candle_time) AS latest, "
                "s.initialized, s.history_exhausted, s.verified_through "
                f"FROM `{instrument_table}` i LEFT JOIN `{candle_table}` c ON c.symbol=i.symbol "
                "AND c.interval_code=%s AND c.candle_time < %s AND c.source<>'capital_day_partial' "
                "LEFT JOIN candle_sync_state s ON s.candle_table=%s AND s.symbol=i.symbol AND s.interval_code=%s "
                "WHERE i.market_no=%s AND i.is_active=TRUE AND i.category_no NOT IN (94,98) "
                f"AND NOT(i.market_no=1 AND i.category_no=198) AND {backfill_symbol_predicate('i')} "
                + symbol_filter + "GROUP BY i.symbol,s.initialized,s.history_exhausted,s.verified_through ORDER BY i.symbol",
                (interval, end + timedelta(days=1), candle_table, interval, market,*(only_symbols or [])),
            )
            choices = {}
            for row in cursor.fetchall():
                selected_policy = policies.get(str(row['symbol'])) or {}
                if blocked_reason(selected_policy) or (selected_policy.get('history_start') or end) > end:
                    continue
                latest = row["latest"].date() if row["latest"] else None
                selection = (("range", period_start(interval, explicit_start)) if explicit_start else select_range(
                    interval, end, int(row["candle_count"]), latest, row, target, force,
                ))
                if selection is not None:
                    floor = listing_floor(policies.get(str(row["symbol"])), interval)
                    if floor:
                        selection = (selection[0], max(selection[1], floor))
                    if selection[1] > end:
                        continue
                    choices[str(row["symbol"])] = selection
            if choices:
                cursor.executemany(
                    f"INSERT INTO `{job_table}` (market_no,symbol,interval_code,start_date,end_date,status,updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,'pending',%s) ON DUPLICATE KEY UPDATE "
                    "status=IF(status='running',status,'pending'), "
                    "completed_at=IF(status='running',completed_at,NULL), updated_at=IF(status='running',updated_at,VALUES(updated_at))",
                    [(market, symbol, interval, start, end, now) for symbol, (_mode, start) in choices.items()],
                )
            cursor.execute(f"SELECT id,symbol,start_date,status FROM `{job_table}` WHERE market_no=%s AND interval_code=%s AND end_date=%s", (market, interval, end))
            selected = [row for row in cursor.fetchall() if row["symbol"] in choices and row["start_date"] == choices[row["symbol"]][1]]
            for row in selected:
                mode, start = choices[row["symbol"]]
                cursor.execute(
                    "INSERT IGNORE INTO candle_backfill_progress(job_table,job_id,mode,target_bars,next_date,updated_at) VALUES(%s,%s,%s,%s,%s,%s)",
                    (job_table, row["id"], mode, target, end if mode == "initialization" else start, now),
                )
                if force and row["status"] != "running":
                    cursor.execute("UPDATE candle_backfill_progress SET mode=%s,target_bars=%s,next_date=%s,empty_windows=0,owner_token=NULL,updated_at=%s WHERE job_table=%s AND job_id=%s", (mode,target,end if mode == "initialization" else start,now,job_table,row["id"]))
            # Retain old ranges as audit history, but don't run obsolete years
            # of pending queries after the actual candle data is up-to-date.
            ids = [row["id"] for row in selected]
            exclusion = " AND j.id NOT IN (" + ",".join(["%s"] * len(ids)) + ")" if ids else ""
            cursor.execute(
                f"UPDATE `{job_table}` j INNER JOIN `{instrument_table}` i ON i.market_no=j.market_no AND i.symbol=j.symbol "
                "SET j.status='superseded',j.last_error='已由逐檔增量計畫取代；既有資料及紀錄保留',j.updated_at=%s "
                f"WHERE j.market_no=%s AND j.interval_code=%s AND j.end_date<=%s AND j.status='pending' AND i.is_active=TRUE AND {backfill_symbol_predicate('i')} " + symbol_filter + exclusion,
                (now, market, interval, end, *(only_symbols or []), *ids),
            )
            return {"queued_jobs":len(selected), "start_date":min((r["start_date"] for r in selected),default=end),
                    "end_date":end,"initialization_jobs":sum(choices[r["symbol"]][0]=="initialization" for r in selected),
                    "incremental_jobs":sum(choices[r["symbol"]][0]!="initialization" for r in selected)}

    def recover_incremental_jobs(self, plan):
        table = self._validate_table_name(plan["job_table"])
        with self.transaction() as cursor:
            # Caller must own the exclusive worker lock. A crash may leave
            # running rows, but no live worker can own them at this point.
            cursor.execute(f"UPDATE `{table}` j JOIN candle_backfill_progress p ON p.job_table=%s AND p.job_id=j.id SET j.status='pending',p.owner_token=NULL WHERE j.status='running' AND j.market_no=%s AND j.interval_code=%s AND j.end_date<=%s AND {backfill_symbol_predicate('j')}", (table,plan["market_no"],plan["interval"],plan["end_date"]))

    def failed_symbols(self, market, interval, job_table, instrument_table):
        table = self._validate_table_name(job_table)
        instruments = self._validate_table_name(instrument_table)
        with self.transaction() as cursor:
            cursor.execute(f"WITH ranked AS (SELECT j.symbol,j.status,ROW_NUMBER() OVER(PARTITION BY j.symbol ORDER BY j.updated_at DESC,j.id DESC) AS rn FROM `{table}` j JOIN `{instruments}` i ON i.symbol=j.symbol AND i.market_no=j.market_no WHERE j.market_no=%s AND j.interval_code=%s AND j.status<>'superseded' AND i.is_active=TRUE AND {backfill_symbol_predicate('i')}) SELECT symbol FROM ranked WHERE rn=1 AND status='failed'",(market,interval))
            return [str(row[0]) for row in cursor.fetchall()]

    def claim_incremental_job(self, plan, owner):
        table = self._validate_table_name(plan["job_table"])
        instruments = self._validate_table_name(plan.get("instrument_table", "odd_lot_instruments" if plan["market_no"] in (5,6) else "instruments"))
        with self.transaction(dictionary=True) as cursor:
            batch = bool(plan.get("batch_v2"))
            batch_join = " JOIN candle_batch_work w ON w.job_table=p.job_table AND w.job_id=p.job_id " if batch else " "
            batch_fields = ",w.phase,w.needs_history,w.history_floor,w.history_next" if batch else ""
            phase = plan.get("queue_phase")
            if batch and phase in {"latest","catchup","history"}:
                batch_filter = f" AND w.phase='{phase}' "
            else:
                batch_filter = " AND w.phase IN ('catchup','history') " if batch else ""
            if getattr(self, '_empty_review_ready', False):
                # Also protects legacy non-batch saved plans and manual
                # resume from same-day empty reclaims in a tight loop.
                batch_filter += (" AND NOT EXISTS(SELECT 1 FROM candle_empty_job_waits er "
                                 "WHERE er.job_table=p.job_table AND er.job_id=p.job_id "
                                 "AND er.next_retry_at>UTC_TIMESTAMP(6)) ")
            if batch:
                if not plan.get("ignore_backoff"):
                    batch_filter += " AND (w.not_before IS NULL OR w.not_before<=UTC_TIMESTAMP(6)) "
                batch_filter += " AND (w.last_round IS NULL OR w.last_round<>%s) "
            if plan.get("refresh_bars") is not None:
                mode = "day_aggregate" if plan.get("aggregate_from_day") else "refresh"
                batch_filter += f" AND p.mode='{mode}' "
            extra_values = []
            if plan.get("only_symbols") is not None:
                symbols = plan["only_symbols"]
                if not symbols: return None
                batch_filter += " AND j.symbol IN ("+",".join(["%s"]*len(symbols))+") "
                extra_values.extend(symbols)
            if plan.get("allowed_ids") is not None:
                ids = plan["allowed_ids"]
                if not ids: return None
                batch_filter += " AND j.id IN ("+",".join(["%s"]*len(ids))+") "
                extra_values.extend(ids)
            cursor.execute(
                f"SELECT j.*,p.mode,p.target_bars,p.next_date,p.empty_windows{batch_fields} FROM `{table}` j "
                "JOIN candle_backfill_progress p ON p.job_table=%s AND p.job_id=j.id "
                + batch_join +
                f"JOIN `{instruments}` i ON i.market_no=j.market_no AND i.symbol=j.symbol "
                f"WHERE i.is_active=TRUE AND i.category_no NOT IN(94,98) AND NOT(i.market_no=1 AND i.category_no=198) AND {backfill_symbol_predicate('i')} "
                "AND j.status='pending' AND j.market_no=%s AND j.interval_code=%s AND j.end_date=%s "
                + batch_filter + "ORDER BY j.id LIMIT 1 FOR UPDATE",
                (table,plan["market_no"],plan["interval"],plan["end_date"],*([owner] if batch else []),*extra_values),
            )
            job = cursor.fetchone()
            if job is None:
                return None
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            cursor.execute(f"UPDATE `{table}` SET status='running',attempt_count=attempt_count+1,started_at=%s,updated_at=%s,last_error=NULL WHERE id=%s",(now,now,job["id"]))
            cursor.execute("UPDATE candle_backfill_progress SET owner_token=%s,updated_at=%s WHERE job_table=%s AND job_id=%s",(owner,now,table,job["id"]))
            job.update(owner_token=owner,job_table=table,candle_table=plan["candle_table"],fixed_range=bool(plan.get("fixed_range")),
                       initial_target_bars=plan.get("target_bars",1200))
            return job

    @staticmethod
    def _write_rows(cursor, table, job, candles, now):
        if not candles:
            return
        validate_candle_volumes(candles)
        preserve = job.get("listing_preserve_before")
        if preserve:
            preserve = period_start(job['interval_code'], preserve)
        protected = [c for c in candles if preserve and c.timestamp.date() < preserve]
        replaceable = [c for c in candles if not preserve or c.timestamp.date() >= preserve]
        columns = "(symbol,interval_code,candle_time,open_price,high_price,low_price,close_price,volume,source,fetched_at)"
        values = lambda rows: [(job["symbol"],job["interval_code"],c.timestamp.replace(tzinfo=None),c.open,c.high,c.low,c.close,c.volume,
                               job.get("candle_source", "capital_history"),now) for c in rows]
        # Existing pre-listing data has priority, including transferred OTC
        # history. Missing older bars can still be inserted; no old row changes.
        if protected:
            cursor.executemany(f"INSERT INTO `{table}` {columns} VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE symbol=symbol", values(protected))
        if not replaceable:
            return
        cursor.executemany(
            f"INSERT INTO `{table}` (symbol,interval_code,candle_time,open_price,high_price,low_price,close_price,volume,source,fetched_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
            "open_price=VALUES(open_price),high_price=VALUES(high_price),low_price=VALUES(low_price),close_price=VALUES(close_price),volume=VALUES(volume),source=VALUES(source),fetched_at=VALUES(fetched_at)",
            values(replaceable),
        )

    def commit_daily_source(self, job, candles):
        """Durably save each native response before allowing worker recycling.

        The aggregate job's cursor/count do NOT move yet. An interrupted
        aggregate window can safely re-query its durable daily inputs.
        """
        table = self._validate_table_name(job["candle_table"])
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self.transaction() as cursor:
            cursor.execute("SELECT owner_token FROM candle_backfill_progress WHERE job_table=%s AND job_id=%s FOR UPDATE",
                           (job["job_table"], job["id"]))
            owner = cursor.fetchone()
            if owner is None or owner[0] != job["owner_token"]:
                raise MySQLRepositoryError("工作擁有者已變更，未寫入彙整來源日 K")
            self._write_rows(cursor, table, dict(job, interval_code="day", candle_source="capital_history"), candles, now)

    def _write_daily_aggregates(self, cursor, table, job, candles, now):
        from app.services.daily_candle_aggregation import aggregate_source, COMPLETE_SOURCE, PARTIAL_SOURCE
        from app.services.backfill_policy import aggregate_period_end
        for bar in candles:
            stamp = bar.timestamp.replace(tzinfo=None)
            key = (table, job["symbol"], job["interval_code"], stamp)
            cursor.execute("SELECT as_of_date FROM candle_daily_aggregate_state "
                           "WHERE candle_table=%s AND symbol=%s AND interval_code=%s AND candle_time=%s FOR UPDATE", key)
            previous = cursor.fetchone()
            if previous and previous[0] > job["end_date"]:
                # Old paused work must never regress a newer snapshot.
                continue
            source = aggregate_source(job["interval_code"], stamp.date(), job["end_date"], now)
            following = (stamp + timedelta(days=7) if job["interval_code"] == "week" else
                         (stamp.replace(day=28) + timedelta(days=4)).replace(day=1))
            bounds = (job["symbol"], job["interval_code"], stamp, following)
            cursor.execute(f"SELECT candle_time,open_price,high_price,low_price,close_price,volume,source,fetched_at "
                           f"FROM `{table}` WHERE symbol=%s AND interval_code=%s AND candle_time>=%s AND candle_time<%s FOR UPDATE",
                           bounds)
            old_rows = cursor.fetchall()
            if job.get("listing_preserve_before") and stamp.date() < period_start(job['interval_code'],job["listing_preserve_before"]) and old_rows:
                # Do not canonicalize/delete an old alias or replace a stored
                # period entirely before the listing period. The listing
                # week/month itself must remain refreshable as more days trade.
                continue
            if source == PARTIAL_SOURCE and any(old[6] != PARTIAL_SOURCE and complete_aggregate(
                    job["interval_code"], stamp.date(), old[7],
                    aggregate_period_end(job["interval_code"], stamp.date())) for old in old_rows):
                continue
            # Legacy vendor labels can be the first TRADING day (e.g. Tuesday
            # after a Monday holiday), rather than the calendar period start.
            # Keep one canonical key, retaining removed alias rows as an audit
            # payload in the same transaction, not two logical weekly bars.
            aliases = [old for old in old_rows if old[0] != stamp]
            self._write_rows(cursor, table, dict(job, candle_source=source), [bar], now)
            if aliases:
                cursor.execute(f"DELETE FROM `{table}` WHERE symbol=%s AND interval_code=%s "
                               "AND candle_time>=%s AND candle_time<%s AND candle_time<>%s", (*bounds, stamp))
            cursor.execute("""INSERT INTO candle_daily_aggregate_state
                (candle_table,symbol,interval_code,candle_time,as_of_date,is_complete,source_bars,updated_at,replaced_rows)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
                as_of_date=VALUES(as_of_date),is_complete=VALUES(is_complete),
                source_bars=VALUES(source_bars),updated_at=VALUES(updated_at),
                replaced_rows=IF(VALUES(replaced_rows) IS NULL,replaced_rows,
                    JSON_MERGE_PRESERVE(COALESCE(replaced_rows,JSON_ARRAY()),VALUES(replaced_rows)))""",
                (*key, job["end_date"], source == COMPLETE_SOURCE, bar.source_bars, now,
                 json.dumps(aliases,default=str) if aliases else None))

    def commit_window(self, job, candles, next_date, empty_windows, skip_source=None, window_start=None, window_end=None):
        table = self._validate_table_name(job["candle_table"])
        job_table = self._validate_table_name(job["job_table"])
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        exclusions = ([] if job["mode"] == "day_aggregate" or window_start is None or window_end is None
                      else excluded_periods(job["symbol"],job["interval_code"],window_start,window_end))
        if exclusions:
            self.ensure_history_exclusion_storage()
        with self.transaction() as cursor:
            cursor.execute("SELECT owner_token FROM candle_backfill_progress WHERE job_table=%s AND job_id=%s FOR UPDATE",(job_table,job["id"]))
            owner = cursor.fetchone()
            if owner is None or owner[0] != job["owner_token"]:
                raise MySQLRepositoryError("回補工作擁有者已變更，未寫入本區間")
            if job["mode"] == "day_aggregate":
                self._write_daily_aggregates(cursor,table,job,candles,now)
            else:
                self._write_rows(cursor,table,job,candles,now)
            for rule in exclusions:
                cursor.execute("INSERT INTO candle_backfill_skips(job_table,job_id,start_date,end_date,source,observed_on) "
                    "VALUES(%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE source=VALUES(source),observed_on=VALUES(observed_on)",
                    (job_table,job["id"],rule.start,rule.end,f"history_exclusion:{rule.id}; {rule.reason}",date.today()))
            if job.get("phase"):
                if skip_source:
                    cursor.execute("INSERT INTO candle_backfill_skips(job_table,job_id,start_date,end_date,source,observed_on) VALUES(%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE source=VALUES(source),observed_on=VALUES(observed_on)",
                        (job_table,job["id"],window_start,window_end,skip_source,date.today()))
                if job["phase"] != "history":
                    checked = job["end_date"] if job["mode"] in {"refresh", "day_aggregate"} else next_date-timedelta(days=1)
                    cursor.execute("INSERT INTO candle_check_state(candle_table,symbol,interval_code,checked_through,updated_at) VALUES(%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE checked_through=GREATEST(checked_through,VALUES(checked_through)),updated_at=VALUES(updated_at)",(table,job["symbol"],job["interval_code"],checked,now))
                    if not skip_source:
                        verified = completed_end(job["interval_code"], checked) if job["mode"] == "day_aggregate" else checked
                        cursor.execute("INSERT INTO candle_sync_state(candle_table,symbol,interval_code,initialized,history_exhausted,verified_through,target_bars,updated_at) VALUES(%s,%s,%s,FALSE,FALSE,%s,%s,%s) ON DUPLICATE KEY UPDATE verified_through=GREATEST(COALESCE(verified_through,VALUES(verified_through)),VALUES(verified_through)),updated_at=VALUES(updated_at)",(table,job["symbol"],job["interval_code"],verified,job.get("initial_target_bars",job["target_bars"]),now))
            cursor.execute("UPDATE candle_backfill_progress SET next_date=%s,empty_windows=%s,updated_at=%s WHERE job_table=%s AND job_id=%s",(next_date,empty_windows,now,job_table,job["id"]))
            cursor.execute(f"UPDATE `{job_table}` SET received_count=COALESCE(received_count,0)+%s,updated_at=%s WHERE id=%s AND status='running'",(len(candles),now,job["id"]))
            self.reset_data_failures(cursor, job, now)
            if getattr(self, '_empty_review_ready', False):
                self.resolve_empty_rows(cursor, job, candles, now)
                if skip_source and window_start is not None and window_end is not None:
                    self.resolve_empty_skip(cursor,job,window_start,window_end,skip_source,now)
            count = (int(job.get("received_count") or 0)+len(candles)) if job["mode"] in {"refresh", "day_aggregate"} else self._stored_count(cursor,table,job)
        job.update(next_date=next_date,empty_windows=empty_windows)
        if job["mode"] in {"refresh", "day_aggregate"}: job["received_count"] = count
        return count

    def ensure_history_exclusion_storage(self):
        # Also support legacy saved plans which predate batch storage. Creating
        # the additive audit table is outside, never inside, the commit transaction.
        if getattr(self, "_history_exclusion_storage_ready", False):
            return
        with self.transaction() as cursor:
            cursor.execute("""CREATE TABLE IF NOT EXISTS candle_backfill_skips (
                job_table VARCHAR(64) NOT NULL, job_id BIGINT UNSIGNED NOT NULL,
                start_date DATE NOT NULL, end_date DATE NOT NULL,
                source VARCHAR(512) NOT NULL, observed_on DATE NOT NULL,
                PRIMARY KEY(job_table,job_id,start_date,end_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
        self._history_exclusion_storage_ready = True

    def stored_count(self, job):
        if job.get("mode") in {"refresh", "day_aggregate"}:
            return int(job.get("received_count") or 0)
        table = self._validate_table_name(job["candle_table"])
        with self.transaction() as cursor:
            return self._stored_count(cursor,table,job)

    @staticmethod
    def aggregate_extents(cursor, table, interval, symbols, as_of, dictionary=False):
        """Only two small columns per bar, streamed in bounded chunks."""
        if not symbols: return {}
        marks = ",".join(["%s"]*len(symbols))
        cursor.execute(f"SELECT symbol,candle_time,fetched_at FROM `{table}` WHERE interval_code=%s AND symbol IN({marks}) AND candle_time<%s AND source<>'capital_day_partial'",
                       (interval,*symbols,as_of+timedelta(days=1)))
        extents, periods = {}, {}
        while True:
            rows = cursor.fetchmany(256)
            if not rows: break
            for row in rows:
                symbol,stamp,written = (row["symbol"],row["candle_time"],row["fetched_at"]) if dictionary else row
                if complete_aggregate(interval,stamp.date(),written,as_of,periods):
                    item = extents.setdefault(symbol,dict(candle_count=0,earliest=stamp,latest=stamp))
                    item.update(candle_count=item["candle_count"]+1,earliest=min(item["earliest"],stamp),latest=max(item["latest"],stamp))
        return extents

    def _stored_count(self,cursor,table,job):
        if job["interval_code"] in {"week","month"}:
            return self.aggregate_extents(cursor,table,job["interval_code"],[job["symbol"]],job["end_date"]).get(job["symbol"],{}).get("candle_count",0)
        cursor.execute(f"SELECT COUNT(*) FROM `{table}` WHERE symbol=%s AND interval_code=%s AND candle_time<%s",(job["symbol"],job["interval_code"],job["end_date"]+timedelta(days=1)))
        return int(cursor.fetchone()[0])

    def fail_incremental_job(self, job, reason):
        table = self._validate_table_name(job["job_table"])
        with self.transaction() as cursor:
            cursor.execute(f"UPDATE `{table}` j JOIN candle_backfill_progress p ON p.job_table=%s AND p.job_id=j.id SET j.status='failed',j.last_error=%s,j.updated_at=UTC_TIMESTAMP(6),p.owner_token=NULL WHERE j.id=%s AND p.owner_token=%s",(table,reason[:4000],job["id"],job["owner_token"]))

    def finish_incremental_job(self, job, count, exhausted=False):
        table = self._validate_table_name(job["job_table"])
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        insufficient = job["mode"] in {"initialization", "refresh", "day_aggregate"} and count < job["target_bars"]
        status = "insufficient_history" if insufficient else "completed"
        with self.transaction() as cursor:
            cursor.execute("SELECT owner_token FROM candle_backfill_progress WHERE job_table=%s AND job_id=%s FOR UPDATE",(table,job["id"]))
            owner = cursor.fetchone()
            if owner is None or owner[0] != job["owner_token"]:
                raise MySQLRepositoryError("回補工作擁有者已變更，未完成工作")
            prefix = "日常回抓不足：本輪取得" if job["mode"] in {"refresh", "day_aggregate"} else "歷史不足：現有"
            reason = (f"{prefix} {count}/{job['target_bars']} 根；已到搜尋下限 {job.get('history_floor',job['start_date'])}；"
                      f"最後斷點 {job.get('next_date')}；空區間 {job.get('empty_windows',0)} 次；不代表來源沒有更早資料") if insufficient else None
            if insufficient and job.get("listing_boundary_reached"):
                policy = job["listing_policy"]
                reason = (f"掛牌歷史不足：{count}/{job['target_bars']} 根；已查至適用掛牌邊界 {policy['history_start']}（含當日）；"
                          "保留實際資料，不宣稱達到目標根數；區間內缺口仍須獨立驗收；來源："+policy["source"])
            cursor.execute(f"UPDATE `{table}` SET status=%s,completed_at=%s,updated_at=%s,last_error=%s WHERE id=%s AND status='running'",(status,now,now,reason,job["id"]))
            initialized = job["mode"] == "initialization" or count >= job["target_bars"] or bool(job.get("listing_boundary_reached"))
            if job["mode"] not in {"refresh", "day_aggregate"}:
                cursor.execute("""INSERT INTO candle_sync_state(candle_table,symbol,interval_code,initialized,history_exhausted,verified_through,target_bars,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
                initialized=initialized OR VALUES(initialized),history_exhausted=history_exhausted OR VALUES(history_exhausted),
                verified_through=IF(VALUES(verified_through) IS NULL,verified_through,GREATEST(COALESCE(verified_through,VALUES(verified_through)),VALUES(verified_through))),
                target_bars=VALUES(target_bars),updated_at=VALUES(updated_at)""",
                (job["candle_table"],job["symbol"],job["interval_code"],initialized,exhausted,None if job.get("phase") else job["end_date"],job["target_bars"],now))
            cursor.execute("UPDATE candle_backfill_progress SET owner_token=NULL,updated_at=%s WHERE job_table=%s AND job_id=%s",(now,table,job["id"]))
        return status

    def release_incremental_job(self, job, reason):
        table = self._validate_table_name(job["job_table"])
        with self.transaction() as cursor:
            cursor.execute(f"UPDATE `{table}` j JOIN candle_backfill_progress p ON p.job_table=%s AND p.job_id=j.id SET j.status='pending',j.last_error=%s,j.updated_at=UTC_TIMESTAMP(6),p.owner_token=NULL WHERE j.id=%s AND j.status='running' AND p.owner_token=%s",(table,reason[:4000],job["id"],job["owner_token"]))

    def actual_data_through(self, markets, intervals, candle_table, instrument_table, details=False):
        candle_table = self._validate_table_name(candle_table)
        instrument_table = self._validate_table_name(instrument_table)
        with self.transaction() as cursor:
            marks=",".join(["%s"]*len(markets))
            result, seen, unfinished = {}, {}, {}
            cursor.execute(f"SELECT market_no,symbol FROM `{instrument_table}` WHERE market_no IN ({marks}) AND is_active=TRUE AND category_no NOT IN(94,98) AND NOT(market_no=1 AND category_no=198) AND {backfill_symbol_predicate()}",tuple(markets))
            eligible = {}
            for market,symbol in cursor.fetchall():
                eligible.setdefault(str(symbol), []).append(int(market))
            # GROUP BY the leftmost index prefix enables a loose index/skip
            # scan. Correlated MAX and even ORDER BY/LIMIT were verified to
            # sort/scan each symbol's bars on this installation.
            cursor.execute(f"SELECT symbol,interval_code,MAX(candle_time) FROM `{candle_table}` GROUP BY symbol,interval_code")
            wanted = set(intervals)
            ends = {i:completed_end(i) for i in wanted if i in {"week","month"}}
            for symbol,interval,latest in cursor.fetchall():
                if interval not in wanted or latest is None:
                    continue
                for market in eligible.get(str(symbol), ()):
                    key = (market,str(interval))
                    value = latest.date()
                    result[key] = min(result.get(key,value),value)
                    seen[key] = seen.get(key,0)+1
                    if interval in ends and value > ends[interval]:
                        unfinished[key] = unfinished.get(key,0)+1
            if not details: return result
            return {key:dict(data_through=value,data_symbols=seen[key],unfinished_symbols=unfinished.get(key,0)) for key,value in result.items()}

    def get_backfill_dashboard_status(self, market_nos, intervals, candle_table, job_table, instrument_table):
        markets, intervals = list(market_nos),list(intervals)
        if not markets or not intervals:
            raise MySQLRepositoryError("市場別與 K 線週期不可為空")
        candles,jobs,instruments = map(self._validate_table_name,(candle_table,job_table,instrument_table))
        marks, codes = ",".join(["%s"]*len(markets)),",".join(["%s"]*len(intervals))
        items = {(m,i):dict(storage_ready=False,active_instruments=0,tracked_symbols=0,
            job_counts=dict(pending=0,running=0,completed=0,failed=0,insufficient_history=0,waiting_review=0),
            average_fetch_seconds=None,average_fetch_sample_count=0,
            pending_phases=dict(latest=0,catchup=0,history=0,unknown=0),
            checked_through=None,checked_symbols=0,data_missing_symbols=0,unfinished_symbols=0,
            data_through=None,oldest_successful_update_at=None,latest_successful_update_at=None,
            latest_status_at=None,latest_failure_symbol=None,latest_failure_error=None,latest_failure_at=None)
            for m in markets for i in intervals}
        with self.transaction() as cursor:
            cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name IN(%s,%s,%s,%s,%s)",(candles,jobs,instruments,"candle_batch_work","candle_check_state"))
            existing = {str(r[0]) for r in cursor.fetchall()}
            ready = {candles,jobs,instruments}.issubset(existing)
            for item in items.values(): item["storage_ready"] = ready
            if instruments in existing:
                cursor.execute(f"SELECT market_no,COUNT(*) FROM `{instruments}` WHERE market_no IN({marks}) AND is_active=TRUE AND category_no NOT IN(94,98) AND NOT(market_no=1 AND category_no=198) AND {backfill_symbol_predicate()} GROUP BY market_no",tuple(markets))
                counts = dict(cursor.fetchall())
                for (market,_),item in items.items(): item["active_instruments"] = int(counts.get(market,0))
            if {jobs,instruments}.issubset(existing):
                phase_field = "w.phase" if "candle_batch_work" in existing else "NULL"
                check_field = "k.checked_through" if "candle_check_state" in existing else "NULL"
                joins = (" LEFT JOIN candle_batch_work w ON w.job_table=%s AND w.job_id=j.id " if "candle_batch_work" in existing else "")
                joins += (" LEFT JOIN candle_check_state k ON k.candle_table=%s AND k.symbol=j.symbol AND k.interval_code=j.interval_code " if "candle_check_state" in existing else "")
                join_args = ([jobs] if "candle_batch_work" in existing else [])+([candles] if "candle_check_state" in existing else [])
                # Rank only narrow columns; do not copy TEXT errors/OHLCV
                # through a window-function sort twice on every refresh.
                cursor.execute(f"""WITH ranked AS (
                    SELECT id,market_no,symbol,interval_code,updated_at,
                    ROW_NUMBER() OVER(PARTITION BY market_no,interval_code,symbol ORDER BY updated_at DESC,id DESC) AS rn
                    FROM `{jobs}` WHERE market_no IN({marks}) AND interval_code IN({codes}) AND status<>'superseded'
                ) SELECT j.market_no,j.interval_code,j.symbol,j.status,j.updated_at,j.last_error,j.id,
                    j.started_at,j.completed_at,j.attempt_count,{phase_field},{check_field}
                    FROM ranked r JOIN `{jobs}` j ON j.id=r.id
                    JOIN `{instruments}` i ON i.market_no=j.market_no AND i.symbol=j.symbol
                    {joins}
                    WHERE r.rn=1 AND i.is_active=TRUE AND i.category_no NOT IN(94,98)
                    AND NOT(i.market_no=1 AND i.category_no=198) AND {backfill_symbol_predicate('i')}""",(*markets,*intervals,*join_args))
                failure_order = {}
                duration_totals = {}
                for row in cursor.fetchall():
                    market,interval,symbol,state,updated,error,job_id,started_at,completed_at,attempt_count = row[:10]
                    phase,checked = row[10:12] if len(row)>=12 else (None,None)
                    item = items[(int(market),str(interval))]
                    item["tracked_symbols"] += 1
                    if state in item["job_counts"]: item["job_counts"][state] += 1
                    if state == "pending":
                        item["pending_phases"][phase if phase in item["pending_phases"] else "unknown"] += 1
                    if checked:
                        item["checked_through"] = min(item["checked_through"] or checked,checked)
                        item["checked_symbols"] += 1
                    item["latest_status_at"] = max(item["latest_status_at"] or updated,updated)
                    if state in {"completed","insufficient_history"}:
                        item["oldest_successful_update_at"] = min(item["oldest_successful_update_at"] or updated,updated)
                        item["latest_successful_update_at"] = max(item["latest_successful_update_at"] or updated,updated)
                        seconds = self._fetch_duration_seconds(started_at,completed_at,attempt_count)
                        if seconds is not None:
                            group = (int(market),str(interval))
                            duration_totals[group] = duration_totals.get(group,0.0)+seconds
                            item["average_fetch_sample_count"] += 1
                    key = (int(market),str(interval))
                    if state == "failed" and (key not in failure_order or (updated,job_id)>failure_order[key]):
                        failure_order[key] = (updated,job_id)
                        item.update(latest_failure_symbol=str(symbol),latest_failure_error=error,latest_failure_at=updated)
                for key,total in duration_totals.items():
                    items[key]["average_fetch_seconds"] = round(total/items[key]["average_fetch_sample_count"],3)
        if ready:
            actual = self.actual_data_through(markets,intervals,candles,instruments,details=True)
            for key,item in items.items():
                extent = actual.get(key,{})
                if isinstance(extent,dict):
                    item["data_through"] = extent.get("data_through")
                    item["data_missing_symbols"] = max(0,item["active_instruments"]-extent.get("data_symbols",0))
                    item["unfinished_symbols"] = extent.get("unfinished_symbols",0)
                else:
                    item["data_through"] = extent
        return dict(storage_ready=ready,items=items)
