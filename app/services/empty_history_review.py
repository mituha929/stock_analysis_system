"""Empty is an observation, not proof of no trade. All writes are worker-owned.

The MySQL ledger is additive and co-located with candle checkpoints. No network,
vendor login, schema creation or connection occurs merely by importing this file.
"""
from datetime import datetime, time, timedelta, timezone
import json
import logging
import os

from app.services.mysql_instrument_repository import MySQLRepositoryError
from app.services.taiwan_trading_calendar import next_trading_day_after, query_trading_bounds
from app.services.backfill_policy import period_start
from app.services.instrument_lifecycle import active_bounds
from app.services.trade_activity import classify_missing

logger = logging.getLogger(__name__)
TAIPEI = timezone(timedelta(hours=8))


def empty_review_days():
    value = int(os.getenv('CAPITAL_EMPTY_REVIEW_TRADING_DAYS', '5'))
    if value not in (3, 4, 5):
        raise ValueError('CAPITAL_EMPTY_REVIEW_TRADING_DAYS 必須為 3、4 或 5')
    return value


def observation_session(now, last):
    """Only a completed trading-day observation of a completed target counts."""
    from app.services.day_candle_integrity import DayCalendar
    local = now.astimezone(TAIPEI)
    day = local.date()
    return day if (last <= day and local.time() >= time(14, 30)
                   and DayCalendar().status(day)[0] is True) else None


def next_observation_at(now):
    # At most one effective observation per trading day, after normal/odd-lot
    # sessions. Calling again today (even manual resume) cannot add a day.
    day = next_trading_day_after(now.astimezone(TAIPEI).date())
    return datetime.combine(day, time(14, 35), TAIPEI).astimezone(timezone.utc).replace(tzinfo=None)


def updated_observation(previous, now, last):
    days = set(json.loads(previous) if isinstance(previous, str) else previous or [])
    session = observation_session(now, last)
    if session is not None:
        days.add(session.isoformat())
    return sorted(days), len(days) >= empty_review_days()


def calendar_period_end(interval, label):
    first = period_start(interval,label)
    following = first+timedelta(days=7) if interval == 'week' else (first.replace(day=28)+timedelta(days=4)).replace(day=1)
    return following-timedelta(days=1)


def missing_periods(interval, first, last, policy=None):
    """Stable date/period keys even if tomorrow's request window has shifted."""
    cursor = period_start(interval, first) if interval in {'week','month'} else first
    while cursor <= last:
        end = calendar_period_end(interval,cursor) if interval in {'week','month'} else cursor
        valid = active_bounds(policy, max(cursor,first), min(end,last))
        if valid and query_trading_bounds('day', *valid) is not None:
            yield cursor,end
        cursor = end+timedelta(days=1)


def pending_review_text(result):
    if not result.get('pending_review'):
        return ''
    if result.get('pending_review_symbols') is None or result.get('pending_review_dates') is None:
        return f"空資料待查 {result['pending_review']} 筆紀錄（商品／日期數尚未統計）"
    message = f"待查 {result['pending_review_symbols']} 檔／{result['pending_review_dates']} 個日期"
    if result.get('pending_review_periods'):
        message += f"；另 {result['pending_review_periods']} 個週月期間"
    return message


class EmptyHistoryReviewRepository:
    def ensure_empty_review_storage(self):
        if getattr(self, '_empty_review_ready', False):
            return
        with self.transaction() as q:
            q.execute("""CREATE TABLE IF NOT EXISTS candle_empty_history_reviews (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                candle_table VARCHAR(64) NOT NULL,market_no SMALLINT NOT NULL,
                symbol VARCHAR(32) NOT NULL,interval_code VARCHAR(8) NOT NULL,
                window_start DATE NOT NULL,window_end DATE NOT NULL,
                status VARCHAR(32) NOT NULL,observation_days JSON NOT NULL,
                first_observed_at DATETIME(6) NOT NULL,last_observed_at DATETIME(6) NOT NULL,
                next_retry_at DATETIME(6) NULL,job_table VARCHAR(64) NULL,job_id BIGINT UNSIGNED NULL,
                checkpoint DATE NULL,evidence JSON NOT NULL,last_error TEXT NULL,
                error_count INT UNSIGNED NOT NULL DEFAULT 0,updated_at DATETIME(6) NOT NULL,
                PRIMARY KEY(id),
                UNIQUE KEY uq_empty_window(candle_table,market_no,symbol,interval_code,window_start,window_end),
                INDEX idx_empty_review(status,market_no),INDEX idx_empty_job(job_table,job_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            q.execute("""CREATE TABLE IF NOT EXISTS candle_empty_job_waits (
                job_table VARCHAR(64) NOT NULL,job_id BIGINT UNSIGNED NOT NULL,
                next_retry_at DATETIME(6) NULL,
                PRIMARY KEY(job_table,job_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
        self._empty_review_ready = True

    @staticmethod
    def _empty_key(job, first, last):
        return (job['candle_table'], job['market_no'], job['symbol'], job['interval_code'], first, last)

    def _observe_empty(self, q, job, first, last, now, evidence=None):
        """Called inside the SAME transaction that keeps/releases its cursor."""
        valid = active_bounds(job.get('listing_policy'), first, last)
        if valid is None:
            return dict(status='confirmed_no_bar',days=[],pending_review=False,retry_at=None,
                        reason='已確認在商品生命週期外，不記空回應次數',window_start=first,window_end=last,
                        no_trade_confirmed=True,classification='outside_instrument_lifetime',trade_confirmed=False)
        key = self._empty_key(job, first, last)
        q.execute('SELECT observation_days,status FROM candle_empty_history_reviews WHERE '
                  'candle_table=%s AND market_no=%s AND symbol=%s AND interval_code=%s '
                  'AND window_start=%s AND window_end=%s FOR UPDATE', key)
        old = q.fetchone()
        days, review = updated_observation(old[0] if old and old[1] != 'resolved' else [], now, last)
        review = review or bool(old and old[1] == 'pending_review')
        state = 'pending_review' if review else 'observing'
        stamp = now.astimezone(timezone.utc).replace(tzinfo=None)
        retry = None if review else next_observation_at(now)
        proof = dict(kind='successful_complete_response_missing_data',
                     no_trade_confirmed=False, threshold_days=empty_review_days(),
                     listing_policy=job.get('listing_policy'),
                     **classify_missing(job.get('trade_activity'), *valid), **(evidence or {}))
        q.execute("""INSERT INTO candle_empty_history_reviews
            (candle_table,market_no,symbol,interval_code,window_start,window_end,status,observation_days,
             first_observed_at,last_observed_at,next_retry_at,job_table,job_id,checkpoint,evidence,updated_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE status=VALUES(status),observation_days=VALUES(observation_days),
            last_observed_at=VALUES(last_observed_at),next_retry_at=VALUES(next_retry_at),
            job_table=VALUES(job_table),job_id=VALUES(job_id),checkpoint=VALUES(checkpoint),
            evidence=VALUES(evidence),updated_at=VALUES(updated_at)""",
            (*key, state, json.dumps(days), stamp, stamp, retry, job.get('job_table'), job.get('id'),
             job.get('next_date'), json.dumps(proof, ensure_ascii=False, default=str), stamp))
        reason = (f'有效空回應已觀察 {len(days)}/{empty_review_days()} 個不同交易日；'
                  + ('疑似無資料，等待查證，不代表無交易；保留斷點' if review else
                     '保留原區間與斷點，下一交易日收盤後再查；不宣稱歷史不存在'))
        reason += ('；已有成交證據但未取得 K：API 缺漏調查' if proof['trade_confirmed'] else
                   '；尚無成交證據：待查交易活動，不認定為 API 缺漏或無交易')
        return dict(status=state, days=days, pending_review=review, retry_at=retry, reason=reason,
                    window_start=first, window_end=last, no_trade_confirmed=False,
                    classification=proof['classification'], trade_confirmed=proof['trade_confirmed'])

    def defer_empty_window(self, job, first, last, *, now=None):
        """No candle write, next_date advance, checked-through or sync update."""
        table = self._validate_table_name(job['job_table'])
        now = now or datetime.now(timezone.utc)
        with self.transaction() as q:
            q.execute(f'SELECT p.owner_token,p.next_date,j.status FROM candle_backfill_progress p '
                      f'JOIN `{table}` j ON j.id=p.job_id WHERE p.job_table=%s AND p.job_id=%s FOR UPDATE',
                      (table, job['id']))
            if q.fetchone() != (job['owner_token'], job['next_date'], 'running'):
                raise MySQLRepositoryError('工作擁有者或斷點已變更，未記錄空回應')
            if getattr(type(self), 'trade_activity_dates', None) is not None:
                job['trade_activity'] = self.trade_activity_dates(job['candle_table'], job['symbol'], first, last, cursor=q)
            observations = [self._observe_empty(q,job,a,b,now,evidence=dict(request_start=str(first),request_end=str(last)))
                            for a,b in missing_periods(job['interval_code'],first,last,job.get('listing_policy'))]
            if not observations:
                raise MySQLRepositoryError('空回應區間沒有待觀察的交易日期；未移動斷點')
            result = max(observations,key=lambda r:(r['pending_review'],len(r['days'])))
            result = dict(result,window_start=first,window_end=last,observed_periods=len(observations))
            if getattr(type(self), 'reset_data_failures', None) is not None:
                self.reset_data_failures(q, job, now.astimezone(timezone.utc).replace(tzinfo=None))
            self._release_for_empty(q, job, result)
        logger.warning('回補空資料 %s/%s/%s %s~%s：%s', job['market_no'], job['symbol'],
                       job['interval_code'], first, last, result['reason'])
        return result

    def _release_for_empty(self, q, job, result):
        table = self._validate_table_name(job['job_table'])
        q.execute(f"UPDATE `{table}` SET status=%s,last_error=%s,updated_at=UTC_TIMESTAMP(6) "
                  "WHERE id=%s AND status='running'",
                  ('waiting_review' if result['pending_review'] else 'pending', result['reason'], job['id']))
        q.execute('UPDATE candle_backfill_progress SET owner_token=NULL WHERE job_table=%s AND job_id=%s',
                  (table, job['id']))
        q.execute('INSERT INTO candle_empty_job_waits(job_table,job_id,next_retry_at) VALUES(%s,%s,%s) '
                  'ON DUPLICATE KEY UPDATE next_retry_at=VALUES(next_retry_at)',
                  (table,job['id'],result['retry_at']))
        if job.get('phase'):
            q.execute("UPDATE candle_batch_work SET phase=IF(phase='latest','catchup',phase),"
                      'not_before=%s,last_round=%s WHERE job_table=%s AND job_id=%s',
                      (result['retry_at'], job['owner_token'], table, job['id']))

    def empty_review_overlap(self, job, first, last):
        with self.transaction(dictionary=True) as q:
            order = 'DESC' if job.get('_query_backward') else 'ASC'
            q.execute("SELECT * FROM candle_empty_history_reviews WHERE candle_table=%s AND market_no=%s "
                      "AND symbol=%s AND interval_code=%s AND status='pending_review' "
                      f'AND window_start<=%s AND window_end>=%s ORDER BY window_start {order} LIMIT 1',
                      (*self._empty_key(job, first, last)[:4], last, first))
            return q.fetchone()

    def hold_empty_review(self, job, record):
        table = self._validate_table_name(job['job_table'])
        result = dict(pending_review=True, retry_at=None,
                      reason=f"區間與待查證空資料 #{record['id']} 重疊；保留斷點，未重新查詢、未宣稱完成")
        with self.transaction() as q:
            q.execute('SELECT owner_token FROM candle_backfill_progress WHERE job_table=%s AND job_id=%s FOR UPDATE',
                      (table, job['id']))
            if q.fetchone() != (job['owner_token'],):
                raise MySQLRepositoryError('工作擁有者已變更，未保留待查證工作')
            self._release_for_empty(q, job, result)
        return result

    def resolve_empty_rows(self, q, job, candles, now):
        # Actual bars clear ONLY their own date/period. Other missing dates in
        # the same request retain their independent observation histories.
        days = sorted({period_start(job['interval_code'],c.timestamp.date()) for c in candles})
        if not days:
            return
        marks = ','.join(['%s']*len(days))
        q.execute("UPDATE candle_empty_history_reviews SET status='resolved',next_retry_at=NULL,updated_at=%s "
                  'WHERE candle_table=%s AND market_no=%s AND symbol=%s AND interval_code=%s '
                  f'AND window_start IN ({marks})',
                  (now, job['candle_table'], job['market_no'], job['symbol'], job['interval_code'], *days))
        if job.get('id') is not None:
            q.execute('DELETE FROM candle_empty_job_waits WHERE job_table=%s AND job_id=%s',
                      (job['job_table'],job['id']))

    def resolve_empty_skip(self, q, job, first, last, source, now):
        q.execute("UPDATE candle_empty_history_reviews SET status='confirmed_no_bar',next_retry_at=NULL,"
                  "evidence=JSON_SET(evidence,'$.confirmed_source',%s),updated_at=%s "
                  'WHERE candle_table=%s AND market_no=%s AND symbol=%s AND interval_code=%s '
                  "AND window_start>=%s AND window_end<=%s AND status IN('observing','pending_review','retry_requested')",
                  (source,now,*self._empty_key(job,first,last)))

    def empty_review_count(self, plans):
        keys = {(p['candle_table'], p['market_no'], p['interval']) for p in plans}
        total = 0
        with self.transaction() as q:
            for table, market, interval in keys:
                symbols = set().union(*(set(p.get('only_symbols') or []) for p in plans
                                      if (p['candle_table'],p['market_no'],p['interval']) == (table,market,interval)))
                scoped = all(p.get('only_symbols') is not None for p in plans
                             if (p['candle_table'],p['market_no'],p['interval']) == (table,market,interval))
                if scoped and not symbols:
                    continue
                extra = ' AND symbol IN ('+','.join(['%s']*len(symbols))+')' if scoped else ''
                q.execute("SELECT COUNT(*) FROM candle_empty_history_reviews WHERE candle_table=%s "
                          "AND market_no=%s AND interval_code=%s AND status='pending_review'"+extra,
                          (table, market, interval, *(sorted(symbols) if scoped else [])))
                total += int(q.fetchone()[0])
        return total

    @staticmethod
    def _empty_review_summary(q, condition, values):
        """Aggregate in SQL, not by loading all gap dates into API memory.

        A security is identified by storage/market/code, NOT by the number of
        intervals, gaps or attempts. Week/month labels are periods, not days.
        """
        q.execute("""SELECT COUNT(*) AS records,
            COUNT(DISTINCT candle_table,market_no,symbol) AS symbols,
            COUNT(DISTINCT CASE WHEN interval_code NOT IN('week','month') THEN candle_table END,
                           market_no,symbol,window_start) AS dates,
            COUNT(DISTINCT CASE WHEN interval_code IN('week','month') THEN candle_table END,
                           market_no,symbol,interval_code,window_start) AS periods
            FROM candle_empty_history_reviews """+condition, tuple(values))
        row = q.fetchone()
        return {name:int(row[name] if isinstance(row,dict) else row[index])
                for index,name in enumerate(('records','symbols','dates','periods'))}

    def empty_review_summary(self, plans):
        groups = {}
        for plan in plans:
            key = (plan['candle_table'],plan['market_no'],plan['interval'])
            selected = plan.get('only_symbols')
            if key not in groups:
                groups[key] = None if selected is None else set(selected)
            elif selected is None:
                groups[key] = None
            elif groups[key] is not None:
                groups[key].update(selected)
        conditions,values = [],[]
        for key,symbols in sorted(groups.items()):
            if symbols is not None and not symbols:
                continue
            conditions.append('(candle_table=%s AND market_no=%s AND interval_code=%s'+
                              (' AND symbol IN ('+','.join(['%s']*len(symbols))+')' if symbols is not None else '')+')')
            values.extend((*key,*(sorted(symbols) if symbols is not None else [])))
        if not conditions:
            return dict(records=0,symbols=0,dates=0,periods=0)
        with self.transaction() as q:
            return self._empty_review_summary(q,"WHERE status='pending_review' AND ("+' OR '.join(conditions)+')',values)

    def record_empty_query_error(self, job, first, last, error):
        # Errors are diagnostics, never a valid-empty observation. If a prior
        # successful empty exists, retain the error alongside that evidence.
        with self.transaction() as q:
            q.execute('UPDATE candle_empty_history_reviews SET error_count=error_count+1,last_error=%s,'
                      'updated_at=UTC_TIMESTAMP(6) WHERE candle_table=%s AND market_no=%s AND symbol=%s '
                      'AND interval_code=%s AND window_start<=%s AND window_end>=%s',
                      (str(error)[:4000], *self._empty_key(job, first, last)[:4], last, first))

    def request_empty_review_retry(self, review_id, reason):
        """Explicit operator retry under the worker lease; NEVER certify no trade.

        Keep the previous observation days: another empty response goes back
        to review immediately rather than starting an endless new five-day loop.
        """
        if not str(reason).strip() or len(reason) > 800:
            raise ValueError('人工重試必須提供 1～800 字原因')
        with self.transaction(dictionary=True) as q:
            q.execute("SELECT * FROM candle_empty_history_reviews WHERE id=%s AND status='pending_review' FOR UPDATE",
                      (int(review_id),))
            record = q.fetchone()
            if not record:
                raise ValueError('找不到指定的待查證紀錄；沒有修改任何工作')
            market = int(record['market_no'])
            if market not in (0,1,4,5,6):
                raise ValueError('不支援的股票市場')
            jobs = 'odd_lot_candle_backfill_jobs' if market in (5,6) else 'candle_backfill_jobs'
            q.execute("UPDATE candle_empty_history_reviews SET status='retry_requested',next_retry_at=NULL,"
                      "evidence=JSON_SET(evidence,'$.operator_retry_reason',%s,'$.operator_retry_at',"
                      "CAST(UTC_TIMESTAMP(6) AS CHAR)),updated_at=UTC_TIMESTAMP(6) WHERE id=%s",
                      (reason, int(review_id)))
            # Only jobs held for this security/range are released. Their
            # committed cursor and received_count remain completely unchanged.
            q.execute(f"UPDATE `{jobs}` j JOIN candle_backfill_progress p ON p.job_table=%s AND p.job_id=j.id "
                      "SET j.status='pending',j.last_error=%s,j.updated_at=UTC_TIMESTAMP(6) "
                      "WHERE j.status='waiting_review' AND p.owner_token IS NULL AND j.market_no=%s "
                      "AND j.symbol=%s AND j.interval_code=%s AND j.start_date<=%s AND j.end_date>=%s "
                      "AND NOT EXISTS(SELECT 1 FROM candle_empty_history_reviews e WHERE e.status='pending_review' "
                      "AND e.candle_table=%s AND e.market_no=j.market_no AND e.symbol=j.symbol "
                      "AND e.interval_code=j.interval_code AND e.window_start<=j.end_date AND e.window_end>=j.start_date)",
                      (jobs, '人工要求重新觀察；'+reason, market,record['symbol'],record['interval_code'],
                       record['window_end'],record['window_start'],record['candle_table']))
            q.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema=DATABASE() "
                      "AND table_name='candle_day_gaps'")
            if q.fetchone()['n']:
                q.execute("UPDATE candle_day_gaps SET status='unresolved',reason=%s,next_retry_at=NULL "
                          "WHERE candle_table=%s AND market_no=%s AND symbol=%s AND status='pending_review' "
                          "AND candle_date BETWEEN %s AND %s",
                          ('人工要求重新觀察；'+reason,record['candle_table'],market,record['symbol'],
                           record['window_start'],record['window_end']))
        return dict(id=int(review_id),status='retry_requested',no_trade_confirmed=False)

    def empty_history_report(self, markets, *, limit=200, offset=0, include_resolved=False):
        """SELECT-only; safe before deployment (missing table => not ready)."""
        with self.transaction(dictionary=True) as q:
            q.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema=DATABASE() "
                      "AND table_name='candle_empty_history_reviews'")
            if not q.fetchone()['n']:
                return dict(storage_ready=False, total=0, items=[],
                            counts=dict(records=0,symbols=0,dates=0,periods=0))
            marks = ','.join(['%s']*len(markets))
            condition = f' WHERE market_no IN ({marks})'
            if not include_resolved:
                condition += " AND status IN ('observing','pending_review','retry_requested')"
            q.execute('SELECT COUNT(*) AS n FROM candle_empty_history_reviews'+condition, tuple(markets))
            total = q.fetchone()['n']
            counts = self._empty_review_summary(q,condition,markets)
            q.execute('SELECT * FROM candle_empty_history_reviews'+condition+
                      ' ORDER BY market_no,symbol,interval_code,window_start,id LIMIT %s OFFSET %s',
                      (*markets, limit, offset))
            rows = q.fetchall()
            for row in rows:
                for field in ('observation_days', 'evidence'):
                    if isinstance(row[field], str):
                        row[field] = json.loads(row[field])
                row['no_trade_confirmed'] = row['status'] == 'confirmed_no_bar'
            return dict(storage_ready=True, total=total, items=rows, counts=counts)
