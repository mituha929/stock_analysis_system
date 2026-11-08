"""Daily rolling re-query planning; never resets initialization state."""
from datetime import date, datetime, timedelta, timezone

from app.services.backfill_policy import initial_start, period_start
from app.services.backfill_eligibility import backfill_symbol_predicate
from app.services.mysql_instrument_repository import MySQLRepositoryError
from app.services.instrument_lifecycle import blocked_reason


def plan_refresh_batch(repo, market, interval, end, initial_target, refresh_bars,
                       candles, jobs, instrument_table, force=False, only_symbols=None,
                       aggregate_from_day=False, force_completed_refresh=False, fresh_daily=False):
    instruments = repo._validate_table_name(instrument_table)
    mode = "day_aggregate" if aggregate_from_day else "refresh"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    scope = " AND i.symbol IN (" + ",".join(["%s"] * len(only_symbols)) + ")" if only_symbols else ""
    if only_symbols is not None and not only_symbols:
        return dict(queued_jobs=0, start_date=end, end_date=end, refresh_bars=refresh_bars,
                    initialization_jobs=0, incremental_jobs=0)
    from app.services.listing_history import listing_floor
    policies = repo.load_listing_policies(candles, market)
    with repo.transaction(dictionary=True) as cursor:
        derived_join = (" LEFT JOIN candle_daily_aggregate_state a ON a.candle_table=%s AND a.symbol=i.symbol "
                        "AND a.interval_code=%s AND a.candle_time=%s ") if aggregate_from_day else ""
        checked = "a.as_of_date" if aggregate_from_day else "k.checked_through"
        cursor.execute(f"""SELECT i.symbol,{checked} AS checked_through FROM `{instruments}` i
            LEFT JOIN candle_check_state k ON k.candle_table=%s AND k.symbol=i.symbol AND k.interval_code=%s
            {derived_join}
            WHERE i.market_no=%s AND i.is_active=TRUE AND i.category_no NOT IN(94,98)
            AND NOT(i.market_no=1 AND i.category_no=198) AND {backfill_symbol_predicate('i')}{scope} ORDER BY i.symbol""",
            (candles, interval,
             *((candles, interval, datetime.combine(period_start(interval, end), datetime.min.time())) if aggregate_from_day else ()),
             market, *(only_symbols or [])))
        instruments_rows = cursor.fetchall()
        cursor.execute(f"""SELECT j.id,j.symbol,j.start_date,j.status,p.mode,p.target_bars,w.phase
            FROM `{jobs}` j LEFT JOIN candle_backfill_progress p ON p.job_table=%s AND p.job_id=j.id
            LEFT JOIN candle_batch_work w ON w.job_table=p.job_table AND w.job_id=p.job_id
            WHERE j.market_no=%s AND j.interval_code=%s AND j.end_date=%s ORDER BY j.id DESC""",
            (jobs, market, interval, end))
        old_rows = cursor.fetchall()
        occupied = {(str(r["symbol"]), r["start_date"]): r for r in old_rows}
        existing_refresh = {}
        def preserve_cursor(row):
            return row["status"] == "running" or (row["status"] == "pending" and
                (fresh_daily or row["phase"] in {"catchup", "history"}))
        for row in old_rows:
            if row["mode"] == mode and not preserve_cursor(row):
                existing_refresh.setdefault(str(row["symbol"]), row)
        choices = {}
        for row in instruments_rows:
            symbol = str(row["symbol"])
            if blocked_reason(policies.get(symbol)) or ((policies.get(symbol) or {}).get('history_start') or end) > end:
                continue
            old = existing_refresh.get(symbol)
            if old and (old['status'] == 'waiting_review' or
                        old["status"] in {"pending", "running", "failed"} and not force):
                choices[symbol] = old["start_date"]
                continue
            if (row["checked_through"] and row["checked_through"] >= end
                    and not force and not force_completed_refresh):
                continue
            # A search floor is a safety boundary, NOT the first query size.
            # Avoid reusing an older initialization/range job's unique key.
            floor = initial_start(interval, end, max(initial_target, refresh_bars))
            boundary = listing_floor(policies.get(symbol), interval)
            if boundary:
                floor = max(floor, boundary)
            for _ in range(100):
                occupied_row = occupied.get((symbol, floor))
                if not occupied_row or (occupied_row["mode"] == mode and not preserve_cursor(occupied_row)):
                    break
                if floor <= date(1900, 1, 1):
                    raise MySQLRepositoryError("日常工作日期鍵與既有歷史工作衝突，未覆寫舊工作")
                floor = max(date(1900, 1, 1), period_start(interval, floor - timedelta(days=1)))
            else:
                raise MySQLRepositoryError("無法安全建立日常工作日期鍵，未覆寫歷史工作")
            choices[symbol] = floor
        if choices:
            cursor.executemany(f"INSERT IGNORE INTO `{jobs}` (market_no,symbol,interval_code,start_date,end_date,status,received_count,updated_at) VALUES(%s,%s,%s,%s,%s,'pending',0,%s)",
                [(market, s, interval, floor, end, now) for s, floor in choices.items()])
        cursor.execute(f"""SELECT j.id,j.symbol,j.start_date,j.status,p.mode FROM `{jobs}` j
            LEFT JOIN candle_backfill_progress p ON p.job_table=%s AND p.job_id=j.id
            WHERE j.market_no=%s AND j.interval_code=%s AND j.end_date=%s""", (jobs, market, interval, end))
        selected = [r for r in cursor.fetchall() if choices.get(str(r["symbol"])) == r["start_date"]
                    and r["mode"] in (None, mode)
                    and not (force_completed_refresh and r["mode"] == mode and r["status"] == "failed")]
        failed_ids = [r["id"] for r in selected if r["mode"] == mode and r["status"] == "failed" and not force]
        if failed_ids:
            marks = ",".join(["%s"] * len(failed_ids))
            cursor.execute(f"UPDATE `{jobs}` SET status='pending',updated_at=%s WHERE id IN({marks}) AND status='failed'",
                           (now, *failed_ids))
        # Pending refresh cursors/counters are authoritative across retries.
        fresh = [r for r in selected if r["status"] not in {"running", "waiting_review"}
                 and not (r["mode"] == mode and r["status"] in {"pending", "failed"} and not force)]
        if fresh:
            marks = ",".join(["%s"] * len(fresh))
            cursor.execute(f"UPDATE `{jobs}` SET status='pending',received_count=0,completed_at=NULL,last_error=NULL,updated_at=%s WHERE id IN({marks})",
                           (now, *(r["id"] for r in fresh)))
            cursor.executemany("""INSERT INTO candle_backfill_progress(job_table,job_id,mode,target_bars,next_date,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE mode=VALUES(mode),target_bars=VALUES(target_bars),
                next_date=VALUES(next_date),empty_windows=0,owner_token=NULL,updated_at=VALUES(updated_at)""",
                [(jobs, r["id"], mode, refresh_bars, end, now) for r in fresh])
            cursor.executemany("""INSERT INTO candle_batch_work(job_table,job_id,phase,needs_history,history_floor,history_next)
                VALUES(%s,%s,'latest',FALSE,%s,%s) ON DUPLICATE KEY UPDATE phase='latest',needs_history=FALSE,
                history_floor=VALUES(history_floor),history_next=VALUES(history_next),not_before=NULL,last_round=NULL""",
                [(jobs, r["id"], max(r["start_date"], listing_floor(policies.get(str(r["symbol"])), interval) or r["start_date"]), r["start_date"]) for r in fresh])
    return dict(queued_jobs=len(selected), start_date=min(choices.values(), default=end), end_date=end,
                refresh_bars=refresh_bars, initialization_jobs=0, incremental_jobs=len(selected),
                **({"fresh_job_ids":[r["id"] for r in selected]} if fresh_daily else {}),
                oldest_checked_through=min((r["checked_through"] for r in instruments_rows if r["checked_through"]), default=None))
