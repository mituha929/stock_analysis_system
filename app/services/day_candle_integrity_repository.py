"""Additive, persistent daily-date audit/repair ledger. No candle deletion."""
from datetime import datetime, timedelta, timezone
import json

from app.services.backfill_eligibility import backfill_symbol_predicate
from app.services.day_candle_integrity import POLICY_VERSION
from app.services.mysql_instrument_repository import MySQLRepositoryError
from app.services.day_candle_integrity import DayCalendar


class DayIntegrityRepository:
    def ensure_day_integrity_storage(self):
        if getattr(self, '_day_integrity_ready', False):
            return
        with self.transaction() as q:
            q.execute("""CREATE TABLE IF NOT EXISTS candle_day_integrity (
                candle_table VARCHAR(64) NOT NULL,market_no SMALLINT NOT NULL,symbol VARCHAR(32) NOT NULL,
                as_of_date DATE NOT NULL,target_bars INT NOT NULL,policy_version INT NOT NULL,
                status VARCHAR(40) NOT NULL,search_start DATE NOT NULL,stored_bars INT NOT NULL,
                unresolved_count INT NOT NULL,audit_json JSON NOT NULL,owner_token VARCHAR(36) NULL,
                updated_at DATETIME(6) NOT NULL,PRIMARY KEY(candle_table,market_no,symbol)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            q.execute("""CREATE TABLE IF NOT EXISTS candle_day_gaps (
                candle_table VARCHAR(64) NOT NULL,market_no SMALLINT NOT NULL,symbol VARCHAR(32) NOT NULL,
                candle_date DATE NOT NULL,status VARCHAR(32) NOT NULL,reason VARCHAR(1000) NOT NULL,
                source VARCHAR(1024) NULL,attempt_count INT NOT NULL DEFAULT 0,
                empty_windows INT NOT NULL DEFAULT 0,data_failures INT NOT NULL DEFAULT 0,
                last_start DATE NULL,last_end DATE NULL,next_retry_at DATETIME(6) NULL,
                updated_at DATETIME(6) NOT NULL,PRIMARY KEY(candle_table,market_no,symbol,candle_date),
                INDEX idx_day_gap_due(status,next_retry_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
        self._day_integrity_ready = True
        self.ensure_empty_review_storage()

    def day_integrity_symbols(self, plan):
        table = self._validate_table_name(plan['instrument_table'])
        symbols = plan.get('only_symbols')
        if symbols is not None and not symbols:
            return []
        extra = ' AND i.symbol IN ('+','.join(['%s']*len(symbols))+')' if symbols is not None else ''
        with self.transaction() as q:
            q.execute(f"SELECT i.symbol FROM `{table}` i WHERE i.market_no=%s AND i.is_active=TRUE "
                      f"AND i.category_no NOT IN(94,98) AND NOT(i.market_no=1 AND i.category_no=198) "
                      f"AND {backfill_symbol_predicate('i')}"+extra+' ORDER BY i.symbol',
                      (plan['market_no'], *(symbols or [])))
            return [str(row[0]) for row in q.fetchall()]

    def day_integrity_dates(self, table, symbol, as_of, target):
        table = self._validate_table_name(table)
        with self.transaction() as q:
            q.execute(f"SELECT DATE(candle_time) AS d FROM `{table}` WHERE symbol=%s AND interval_code='day' "
                      "AND candle_time<%s GROUP BY DATE(candle_time) ORDER BY d DESC LIMIT %s",
                      (symbol, as_of+timedelta(days=1), target))
            return sorted(row[0] for row in q.fetchall())

    def previous_day_assessment(self, table, market, symbol):
        with self.transaction(dictionary=True) as q:
            q.execute('SELECT * FROM candle_day_integrity WHERE candle_table=%s AND market_no=%s AND symbol=%s',
                      (table, market, symbol))
            return q.fetchone()

    def save_day_assessment(self, table, market, symbol, assessment, present, owner):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        audit = {k: v for k, v in assessment.items() if k not in {'issues', 'confirmed', 'closed'}}
        with self.transaction() as q:
            q.execute("""INSERT INTO candle_day_integrity VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE as_of_date=VALUES(as_of_date),target_bars=VALUES(target_bars),
                policy_version=VALUES(policy_version),status=VALUES(status),search_start=VALUES(search_start),
                stored_bars=VALUES(stored_bars),unresolved_count=VALUES(unresolved_count),
                audit_json=VALUES(audit_json),owner_token=VALUES(owner_token),updated_at=VALUES(updated_at)""",
                (table, market, symbol, assessment['as_of'], assessment['target_bars'], POLICY_VERSION,
                 assessment['status'], assessment['search_start'], assessment['stored_bars'],
                 assessment['unresolved_count'], json.dumps(audit, default=str), owner, now))
            if assessment.get('listing_boundary'):
                q.execute("UPDATE candle_day_gaps SET status='confirmed_no_bar',reason='before_confirmed_listing',source=%s,"
                          "next_retry_at=NULL,updated_at=%s WHERE candle_table=%s AND market_no=%s AND symbol=%s "
                          "AND candle_date<%s AND status IN('unresolved','failed_data','pending_review')",
                          (assessment['listing_policy']['source'], now, table, market, symbol, assessment['listing_boundary']))
            lifecycle = assessment.get('listing_policy') or {}
            for key, operator in (('maturity_date', '>'), ('termination_date', '>=')):
                if lifecycle.get(key):
                    q.execute("UPDATE candle_day_gaps SET status='confirmed_no_bar',reason='outside_instrument_lifetime',"
                              "source=%s,next_retry_at=NULL,updated_at=%s WHERE candle_table=%s AND market_no=%s AND symbol=%s "
                              f"AND candle_date{operator}%s AND status IN('unresolved','failed_data','pending_review')",
                              (lifecycle.get('terminal_source_url') or lifecycle.get('source'), now, table, market, symbol, lifecycle[key]))
            for rows in (assessment['issues'], assessment['confirmed']):
                if rows:
                    q.executemany("""INSERT INTO candle_day_gaps
                        (candle_table,market_no,symbol,candle_date,status,reason,source,updated_at)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
                        status=IF(status IN('failed_data','pending_review') AND VALUES(status)='unresolved',status,VALUES(status)),
                        reason=IF(status IN('failed_data','pending_review'),reason,VALUES(reason)),source=VALUES(source),updated_at=VALUES(updated_at)""",
                        [(table, market, symbol, row['day'], row['status'],
                          row['reason']+(': '+row['description'] if row.get('description') else ''), row.get('source'), now)
                         for row in rows])
            # Previously unknown calendar dates may now be confirmed closed.
            # Close existing ledger entries only; do not create millions of
            # weekend rows or leave a phantom retry after calendar recovery.
            closed_by_source = {}
            for row in assessment.get('closed', []):
                closed_by_source.setdefault(row['source'], []).append(row['day'])
            for source, days in closed_by_source.items():
                for offset in range(0, len(days), 400):
                    chunk = days[offset:offset+400]
                    q.execute("UPDATE candle_day_gaps SET status='confirmed_no_bar',reason='market_closed',source=%s,"
                              "next_retry_at=NULL,updated_at=%s WHERE candle_table=%s AND market_no=%s AND symbol=%s "
                              "AND candle_date IN ("+','.join(['%s']*len(chunk))+')',
                              (source, now, table, market, symbol, *chunk))
            present = sorted(set(present))
            for offset in range(0, len(present), 400):
                chunk = present[offset:offset+400]
                q.execute("UPDATE candle_day_gaps SET status='resolved',reason='bar_present',next_retry_at=NULL,"
                          "data_failures=0,updated_at=%s WHERE candle_table=%s AND market_no=%s AND symbol=%s "
                          "AND candle_date IN ("+','.join(['%s']*len(chunk))+')', (now, table, market, symbol, *chunk))

    def due_day_gap(self, table, market, symbol):
        with self.transaction(dictionary=True) as q:
            q.execute("""SELECT g.* FROM candle_day_gaps g JOIN candle_day_integrity a
                ON a.candle_table=g.candle_table AND a.market_no=g.market_no AND a.symbol=g.symbol
                WHERE g.candle_table=%s AND g.market_no=%s AND g.symbol=%s AND g.status='unresolved'
                AND g.candle_date BETWEEN a.search_start AND a.as_of_date
                AND (g.next_retry_at IS NULL OR g.next_retry_at<=UTC_TIMESTAMP(6))
                ORDER BY g.attempt_count,g.candle_date DESC LIMIT 1""", (table, market, symbol))
            return q.fetchone()

    def review_only_day_symbols(self, plans):
        """Review-only gaps do not gate useful collection in other periods."""
        result = {}
        with self.transaction() as q:
            for table, market in {(p['candle_table'],p['market_no']) for p in plans}:
                q.execute("""SELECT g.symbol,SUM(g.status='pending_review') FROM candle_day_gaps g
                    JOIN candle_day_integrity a ON a.candle_table=g.candle_table
                    AND a.market_no=g.market_no AND a.symbol=g.symbol
                    WHERE g.candle_table=%s AND g.market_no=%s
                    AND g.candle_date BETWEEN a.search_start AND a.as_of_date
                    AND g.status IN('unresolved','failed_data','pending_review') GROUP BY g.symbol
                    HAVING SUM(g.status='pending_review')>0 AND SUM(g.status<>'pending_review')=0""", (table,market))
                result.update({(market,str(symbol)):int(count) for symbol,count in q.fetchall()})
        return result

    def commit_day_gap_window(self, table, market, symbol, first, last, candles, owner, retry_seconds, listing_policy=None):
        table = self._validate_table_name(table)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        present = sorted({c.timestamp.date() for c in candles})
        with self.transaction() as q:
            q.execute('SELECT owner_token FROM candle_day_integrity WHERE candle_table=%s AND market_no=%s AND symbol=%s FOR UPDATE',
                      (table, market, symbol))
            row = q.fetchone()
            if row is None or row[0] != owner:
                raise MySQLRepositoryError('日 K 驗收擁有者已變更，未寫入缺口區間')
            self._write_rows(q, table, dict(symbol=symbol, interval_code='day',
                listing_preserve_before=(listing_policy or {}).get('listing_date')), candles, now)
            q.execute("""UPDATE candle_day_gaps SET attempt_count=attempt_count+1,empty_windows=empty_windows+1,
                last_start=%s,last_end=%s,data_failures=0,next_retry_at=%s,updated_at=%s
                WHERE candle_table=%s AND market_no=%s AND symbol=%s AND status='unresolved'
                AND candle_date BETWEEN %s AND %s""",
                (first, last, now+timedelta(seconds=retry_seconds), now, table, market, symbol, first, last))
            if present:
                q.execute("UPDATE candle_day_gaps SET status='resolved',reason='capital_history_repaired',"
                          "empty_windows=0,data_failures=0,next_retry_at=NULL,updated_at=%s "
                          "WHERE candle_table=%s AND market_no=%s AND symbol=%s AND candle_date IN ("+
                          ','.join(['%s']*len(present))+')', (now, table, market, symbol, *present))
            if getattr(self, '_empty_review_ready', False):
                # An expanded reply may contain other dates. Count absence of
                # EACH already-recorded gap, not absence of the entire response.
                q.execute("SELECT candle_date FROM candle_day_gaps WHERE candle_table=%s AND market_no=%s "
                          "AND symbol=%s AND status='unresolved' AND candle_date BETWEEN %s AND %s FOR UPDATE",
                          (table, market, symbol, first, last))
                missing = [r[0] for r in q.fetchall() if r[0] not in present]
                sessions = DayCalendar()
                activity = self.trade_activity_dates(table, symbol, first, last, cursor=q)
                from app.services.instrument_lifecycle import active_bounds
                for day in missing:
                    if sessions.status(day)[0] is not True or active_bounds(listing_policy, day, day) is None:
                        continue
                    result = self._observe_empty(q, dict(candle_table=table,market_no=market,symbol=symbol,
                        interval_code='day',listing_policy=listing_policy,trade_activity=activity), day, day,
                        now.replace(tzinfo=timezone.utc), evidence=dict(request_start=str(first),request_end=str(last)))
                    q.execute("UPDATE candle_day_gaps SET status=%s,reason=%s,next_retry_at=%s WHERE "
                              'candle_table=%s AND market_no=%s AND symbol=%s AND candle_date=%s',
                              ('pending_review' if result['pending_review'] else 'unresolved', result['reason'],
                               result['retry_at'], table, market, symbol, day))
                self.resolve_empty_rows(q, dict(candle_table=table,market_no=market,symbol=symbol,
                                               interval_code='day'), candles, now)
            q.execute("UPDATE candle_day_integrity SET status='needs_reaudit',updated_at=%s "
                      "WHERE candle_table=%s AND market_no=%s AND symbol=%s", (now, table, market, symbol))

    def record_day_gap_data_error(self, table, market, symbol, first, last, error):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self.transaction() as q:
            q.execute("""UPDATE candle_day_gaps SET
                data_failures=IF(last_start=%s AND last_end=%s,data_failures+1,1),
                status=IF(data_failures>=5,'failed_data','unresolved'),reason=%s,
                last_start=%s,last_end=%s,next_retry_at=%s,updated_at=%s
                WHERE candle_table=%s AND market_no=%s AND symbol=%s AND status='unresolved'
                AND candle_date BETWEEN %s AND %s""",
                (first, last, str(error)[:1000], first, last, now+timedelta(seconds=15), now,
                 table, market, symbol, first, last))

    def finish_verified_short_day_jobs(self, table, market, symbol, as_of, proof):
        """Only evidence-backed complete available history can end a short refresh."""
        jobs = 'odd_lot_candle_backfill_jobs' if market in (5, 6) else 'candle_backfill_jobs'
        description = ('掛牌歷史不足：已核實適用掛牌邊界至截止日的可用日 K；不足回抓目標，非完整1200根；'
                       if proof.startswith('掛牌歷史不足') else
                       '已驗證首次交易日至截止日的全部可用日 K；不足回抓目標，非完整1200根；')
        with self.transaction() as q:
            q.execute(f"UPDATE `{jobs}` j JOIN candle_backfill_progress p ON p.job_table=%s AND p.job_id=j.id "
                      "SET j.status='insufficient_history',j.completed_at=UTC_TIMESTAMP(6),j.updated_at=UTC_TIMESTAMP(6),"
                      "j.last_error=%s WHERE j.market_no=%s AND j.symbol=%s AND j.interval_code='day' "
                      "AND j.end_date=%s AND j.status='pending' AND p.owner_token IS NULL "
                      "AND p.mode='refresh' AND COALESCE(j.received_count,0)<p.target_bars",
                      (jobs, description+proof,
                       market, symbol, as_of))

    def day_integrity_report(self, markets, *, limit=200, offset=0, include_resolved=False):
        """Read only; GET never creates tables, contacts Capital, or queues work."""
        with self.transaction(dictionary=True) as q:
            q.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema=DATABASE() "
                      "AND table_name IN ('candle_day_integrity','candle_day_gaps')")
            if q.fetchone()['n'] != 2:
                return dict(storage_ready=False, summary=[], total=0, items=[], assessment_total=0, assessments=[],
                            counts=dict(symbols=0,dates=0))
            marks = ','.join(['%s']*len(markets))
            q.execute(f"SELECT market_no,as_of_date,target_bars,status,COUNT(*) AS symbols "
                      f"FROM candle_day_integrity WHERE market_no IN ({marks}) "
                      "GROUP BY market_no,as_of_date,target_bars,status ORDER BY market_no,status", tuple(markets))
            summary = q.fetchall()
            assessment_filter = '' if include_resolved else " AND status NOT IN ('complete','complete_available_history')"
            q.execute(f'SELECT COUNT(*) AS n FROM candle_day_integrity WHERE market_no IN ({marks})'+assessment_filter,
                      tuple(markets))
            assessment_total = q.fetchone()['n']
            q.execute(f'SELECT * FROM candle_day_integrity WHERE market_no IN ({marks})'+assessment_filter+
                      ' ORDER BY market_no,symbol LIMIT %s OFFSET %s', (*markets, limit, offset))
            assessments = q.fetchall()
            for assessment in assessments:
                assessment.pop('owner_token', None)
                if isinstance(assessment.get('audit_json'), str):
                    assessment['audit_json'] = json.loads(assessment['audit_json'])
            state_filter = '' if include_resolved else " AND g.status IN ('unresolved','failed_data','pending_review')"
            base = (" FROM candle_day_gaps g JOIN candle_day_integrity a ON a.candle_table=g.candle_table "
                    "AND a.market_no=g.market_no AND a.symbol=g.symbol "
                    f"WHERE g.market_no IN ({marks}) AND g.candle_date BETWEEN a.search_start AND a.as_of_date"+state_filter)
            q.execute('SELECT COUNT(*) AS n,COUNT(DISTINCT g.candle_table,g.market_no,g.symbol) AS symbols'+base, tuple(markets))
            counts = q.fetchone()
            total = counts['n']
            q.execute('SELECT g.*,a.as_of_date,a.target_bars,a.stored_bars,a.status AS audit_status'+base+
                      ' ORDER BY g.market_no,g.symbol,g.candle_date DESC LIMIT %s OFFSET %s', (*markets, limit, offset))
            return dict(storage_ready=True, summary=summary, total=total, items=q.fetchall(),
                        counts=dict(symbols=int(counts['symbols']),dates=int(total)),
                        assessment_total=assessment_total, assessments=assessments)
