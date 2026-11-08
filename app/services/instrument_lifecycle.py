"""Official security lifetimes. Unknown is never delisted; no candle deletion.

The issuance feed is NOT a termination feed. A put/call option or a partial
conversion is not an exit. convDelist supplies the first non-trading date.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import json
import logging
import re
from urllib.request import Request, urlopen

from app.services.stock_master_sources import parse_official_date

logger = logging.getLogger(__name__)
TAIPEI = timezone(timedelta(hours=8))
ISSUES_URL = 'https://www.tpex.org.tw/openapi/v1/bond_ISSBD5_data'
DELIST_URL = 'https://www.tpex.org.tw/www/zh-tw/bond/convDelist?response=json'
ISSUE_FIELDS = frozenset('Date IssuerCode IssuerName BondCode BondType SeriesNumber TrancheNumber IssueDate MaturityDate IssueAmount OutstandingAmount CouponRate InterestBasis CouponFrequency PaidFrequency BondRatingAgency BondRating IssuerRatingAgency IssuerRating GuarantorRatingAgency GuarantorRating ListingCountry PrincipalRepayment PrincipalRepaymentDescription ShortName ListingDate TenorYear TenorMonth ListingStatus Guaranteed GuaranteeDescription PutOptionDate PutOptionPrice Underwriter OutstandingChangeDate OutstandingChangeDescription Currency OfferingMethod Conversion/ExchangePriceAtIssuance Conversion/ExchangePeriodStartDate Conversion/ExchangePeriodEndDate Trustee'.split())


def today():
    return datetime.now(TAIPEI).date()


def parse_bond_issues(rows, observed_on):
    if not isinstance(rows, list) or not rows or len(rows) > 20000:
        raise ValueError('轉換債發行資料不是完整清單')
    result = {}
    for raw in rows:
        if not isinstance(raw, dict) or set(raw) != ISSUE_FIELDS or not all(isinstance(v, str) for v in raw.values()):
            raise ValueError('轉換債發行資料欄位或型別變動')
        snapshot = parse_official_date(raw['Date'])
        if snapshot > observed_on or raw['BondType'] != '5':
            raise ValueError('轉換債資料日期或種類不符')
        # Blank codes/private issues do not identify a tradable OTC instrument.
        code = raw['BondCode'].strip()
        if not code or raw['ListingStatus'] != '2':
            continue
        if not re.fullmatch(r'[0-9A-Z]{4,12}', code) or code in result or not raw['ShortName'].strip():
            raise ValueError('轉換債代碼重複或不合法')
        listed = parse_official_date(raw['ListingDate'], optional=True)
        maturity = parse_official_date(raw['MaturityDate'], optional=True)
        if listed and maturity and listed > maturity:
            raise ValueError('債券掛牌日晚於到期日')
        state, reason = 'unknown', None
        # Zero outstanding alone is not enough: require a dated, explicit cause.
        changed = parse_official_date(raw['OutstandingChangeDate'], optional=True)
        description = raw['OutstandingChangeDescription'].strip()
        try:
            issued, outstanding = Decimal(raw['IssueAmount']), Decimal(raw['OutstandingAmount'])
            if not issued.is_finite() or not outstanding.is_finite() or issued <= 0 or outstanding < 0:
                raise ValueError('債券餘額不合法')
        except InvalidOperation as error:
            raise ValueError('債券餘額格式變動') from error
        if outstanding == 0 and changed and changed <= min(snapshot, observed_on):
            if re.fullmatch(r'(?:公司債轉換(?:普通股股票)?|債權人轉換|投資人申請轉換|全數轉換|全數交換|公司債交換)', description):
                state, reason = 'fully_converted', description
            elif re.fullmatch(r'(?:公司債買回註銷|全數贖回|提前贖回|強制贖回|還清本金|全數買回)', description):
                state, reason = 'redeemed', description
        result[code] = dict(symbol=code, name=raw['ShortName'].strip(), listing_date=listed,
                            maturity_date=maturity, termination_date=None, trading_status=state,
                            terminal_reason=reason, source_date=snapshot, source_url=ISSUES_URL,
                            listing_source_url=ISSUES_URL if listed else None, maturity_source_url=ISSUES_URL if maturity else None,
                            terminal_source_url=ISSUES_URL if reason else None, raw_json=raw)
    return result


def parse_bond_delist(payload, observed_on):
    if not isinstance(payload, dict) or payload.get('stat') != 'ok' or len(payload.get('tables', [])) != 1:
        raise ValueError('轉換債下櫃資料回應不符')
    table = payload['tables'][0]
    if table.get('fields') != ['代碼', '簡稱', '下櫃日期'] or not isinstance(table.get('data'), list):
        raise ValueError('轉換債下櫃資料欄位變動')
    rows = table['data']
    if len(rows) > 20000 or table.get('totalCount') != len(rows):
        raise ValueError('下櫃資料不完整')
    result = {}
    for raw in rows:
        if (not isinstance(raw, list) or len(raw) != 3 or not all(isinstance(v, str) for v in raw)
                or not re.fullmatch(r'[0-9A-Z]{4,12}', raw[0]) or not raw[1].strip() or raw[0] in result):
            raise ValueError('下櫃資料列不合法或重複')
        result[raw[0]] = dict(symbol=raw[0], name=raw[1].strip(), listing_date=None, maturity_date=None,
            termination_date=parse_official_date(raw[2]), trading_status='terminated',
            terminal_reason='官方公告下櫃；下櫃日起不交易，具體贖回／轉換原因未由此表提供',
            source_date=observed_on, source_url=DELIST_URL, terminal_source_url=DELIST_URL, raw_json=raw)
        result[raw[0]].update(listing_source_url=None,maturity_source_url=None)
    return result


def read_official_json(url):
    request = Request(url, headers={'User-Agent': 'capital-api/lifecycle', 'Accept': 'application/json'})
    with urlopen(request, timeout=8) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('生命週期官方回應過大')
    return json.loads(raw.decode('utf-8-sig'))


@lru_cache(maxsize=1)
def fetch_bond_lifecycles(observed_on):
    """Each source validates atomically; an outage cannot erase cached exits."""
    day = date.fromisoformat(observed_on)
    result = {}
    for url, parser in ((ISSUES_URL, parse_bond_issues), (DELIST_URL, parse_bond_delist)):
        try:
            rows = parser(read_official_json(url), day)
            for code, row in rows.items():
                if code in result:
                    previous = result[code]
                    row = dict(row, listing_date=previous['listing_date'], maturity_date=previous['maturity_date'],
                               listing_source_url=previous['listing_source_url'], maturity_source_url=previous['maturity_source_url'],
                               raw_json=dict(issue=previous['raw_json'], delist=row['raw_json']))
                    if previous['trading_status'] in {'fully_converted', 'redeemed'}:
                        row.update(trading_status=previous['trading_status'], terminal_reason=previous['terminal_reason'])
                result[code] = row
        except Exception as error:
            logger.warning('商品生命週期來源 %s 無法核實，保留已存證據：%s', url, type(error).__name__)
    return tuple(result.values())


def blocked_reason(policy, on=None):
    """Scheduling uses NOW, not an old plan's as-of date. Maturity is inclusive."""
    p, day = policy or {}, on or today()
    termination = p.get('termination_date')
    if termination and day >= termination:
        return 'terminated'
    if p.get('trading_status') in {'fully_converted', 'redeemed'}:
        return p['trading_status']
    if p.get('maturity_date') and day > p['maturity_date']:
        return 'matured'
    if p.get('history_start') and day < p['history_start']:
        return 'not_yet_listed'
    return None


def active_bounds(policy, first, last):
    p = policy or {}
    first = max(first, p['history_start']) if p.get('history_start') else first
    last = min(last, p['maturity_date']) if p.get('maturity_date') else last
    last = min(last, p['termination_date']-timedelta(days=1)) if p.get('termination_date') else last
    return (first, last) if first <= last else None


class InstrumentLifecycleRepository:
    def reconcile_lifecycle_reviews(self, table, market, policies):
        """Close only wholly impossible dates; keep overlapping listing periods.

        Old 0/5 records remain auditable. Releasing a wait never moves a cursor
        or claims that price history is complete. Runs under the worker lease.
        """
        key = (table, market, today())
        done = {k for k in getattr(self, '_lifecycle_reconciled', set()) if k[2] == key[2]}
        if key in done or not policies:
            return
        jobs = 'odd_lot_candle_backfill_jobs' if market in (5, 6) else 'candle_backfill_jobs'
        with self.transaction() as q:
            q.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='candle_batch_work'")
            has_batch = q.fetchone()[0] == 1
            q.execute("SELECT DISTINCT symbol FROM candle_empty_history_reviews WHERE candle_table=%s AND market_no=%s "
                      "AND status IN('observing','pending_review','retry_requested')", (table, market))
            review_symbols = {str(r[0]) for r in q.fetchall()}
            for field, operator, value_key, reason in (
                    ('window_end', '<', 'history_start', 'before_confirmed_listing'),
                    ('window_start', '>=', 'termination_date', 'after_confirmed_termination'),
                    ('window_start', '>', 'maturity_date', 'after_confirmed_maturity')):
                rows = [(reason, p.get('terminal_source_url') if value_key == 'termination_date' else p.get('source'),
                         table, market, symbol, p[value_key]) for symbol, p in policies.items()
                        if p.get(value_key) and symbol in review_symbols]
                if rows:
                    q.executemany(f'''UPDATE candle_empty_history_reviews SET status='confirmed_no_bar',next_retry_at=NULL,
                        evidence=JSON_SET(evidence,'$.lifecycle_exclusion',%s,'$.confirmed_source',%s),updated_at=UTC_TIMESTAMP(6)
                        WHERE candle_table=%s AND market_no=%s AND symbol=%s AND {field}{operator}%s
                        AND status IN('observing','pending_review','retry_requested')''', rows)
            terminal = [(blocked_reason(p), jobs, market, symbol) for symbol, p in policies.items() if blocked_reason(p)]
            if terminal:
                q.executemany(f'''UPDATE `{jobs}` j JOIN candle_backfill_progress p ON p.job_table=%s AND p.job_id=j.id
                    SET j.status='superseded',j.last_error=CONCAT('商品生命週期停止自動查詢：',%s,'；保留行情與斷點，非補齊完成'),
                        j.updated_at=UTC_TIMESTAMP(6)
                    WHERE j.market_no=%s AND j.symbol=%s AND j.status IN('pending','waiting_review') AND p.owner_token IS NULL''',
                    [(table_name, reason, m, symbol) for reason, table_name, m, symbol in terminal])
            # Other unresolved dates for the same security continue to hold it.
            batch_join = 'LEFT JOIN candle_batch_work b ON b.job_table=p.job_table AND b.job_id=p.job_id' if has_batch else ''
            batch_update = ',b.not_before=NULL,b.last_round=NULL' if has_batch else ''
            q.execute(f'''UPDATE `{jobs}` j JOIN candle_backfill_progress p ON p.job_table=%s AND p.job_id=j.id
                JOIN candle_empty_job_waits w ON w.job_table=p.job_table AND w.job_id=p.job_id
                {batch_join}
                SET j.status='pending',j.last_error='生命週期已排除不可交易日期；保留原斷點待續抓',
                    w.next_retry_at=NULL{batch_update}
                WHERE j.market_no=%s AND j.status IN('pending','waiting_review') AND p.owner_token IS NULL
                AND EXISTS(SELECT 1 FROM candle_empty_history_reviews r WHERE r.job_table=p.job_table
                    AND r.job_id=p.job_id AND JSON_EXTRACT(r.evidence,'$.lifecycle_exclusion') IS NOT NULL)
                AND NOT EXISTS(SELECT 1 FROM candle_empty_history_reviews r WHERE r.job_table=p.job_table
                    AND r.job_id=p.job_id AND r.status IN('observing','pending_review','retry_requested'))''', (jobs, market))
        self._lifecycle_reconciled = done | {key}

    def ensure_lifecycle_storage(self):
        if getattr(self, '_lifecycle_ready', False):
            return
        with self.transaction() as q:
            q.execute('''CREATE TABLE IF NOT EXISTS instrument_lifecycle (
                market_no SMALLINT NOT NULL,symbol VARCHAR(32) NOT NULL,name VARCHAR(128) NOT NULL,
                listing_date DATE NULL,maturity_date DATE NULL,termination_date DATE NULL,
                trading_status VARCHAR(32) NOT NULL,terminal_reason VARCHAR(1000) NULL,
                source_date DATE NOT NULL,source_url VARCHAR(1024) NOT NULL,
                listing_source_url VARCHAR(1024) NULL,maturity_source_url VARCHAR(1024) NULL,
                terminal_source_url VARCHAR(1024) NULL,raw_json JSON NOT NULL,updated_at DATETIME(6) NOT NULL,
                PRIMARY KEY(market_no,symbol)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci''')
        self._lifecycle_ready = True

    def refresh_lifecycle(self):
        self.ensure_lifecycle_storage()
        day = today()
        if getattr(self, '_lifecycle_day', None) == day:
            return
        rows = fetch_bond_lifecycles(day.isoformat())  # NEVER inside SQL transaction.
        if rows:
            with self.transaction() as q:
                q.executemany('''INSERT INTO instrument_lifecycle
                    (market_no,symbol,name,listing_date,maturity_date,termination_date,trading_status,
                     terminal_reason,source_date,source_url,listing_source_url,maturity_source_url,terminal_source_url,raw_json,updated_at)
                    VALUES(1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6))
                    ON DUPLICATE KEY UPDATE
                    listing_date=COALESCE(VALUES(listing_date),listing_date),
                    maturity_date=COALESCE(VALUES(maturity_date),maturity_date),
                    termination_date=COALESCE(VALUES(termination_date),termination_date),
                    trading_status=IF(VALUES(trading_status)='unknown',trading_status,VALUES(trading_status)),
                    terminal_reason=COALESCE(VALUES(terminal_reason),terminal_reason),
                    terminal_source_url=COALESCE(VALUES(terminal_source_url),terminal_source_url),
                    listing_source_url=COALESCE(VALUES(listing_source_url),listing_source_url),
                    maturity_source_url=COALESCE(VALUES(maturity_source_url),maturity_source_url),
                    source_date=VALUES(source_date),source_url=VALUES(source_url),raw_json=VALUES(raw_json),
                    updated_at=VALUES(updated_at)''',
                    [(r['symbol'], r['name'], r['listing_date'], r['maturity_date'], r['termination_date'],
                      r['trading_status'], r['terminal_reason'], r['source_date'], r['source_url'],
                      r['listing_source_url'],r['maturity_source_url'],r['terminal_source_url'], json.dumps(r['raw_json'], ensure_ascii=False)) for r in rows])
        self._lifecycle_day = day

    def bond_lifecycle_policies(self, market):
        if market != 1 or not getattr(self, '_lifecycle_ready', False):
            return {}
        self.refresh_lifecycle()
        with self.transaction(dictionary=True) as q:
            q.execute('''SELECT l.* FROM instrument_lifecycle l JOIN instruments i
                ON i.market_no=l.market_no AND i.symbol=l.symbol AND i.name=l.name WHERE l.market_no=1''')
            rows = q.fetchall()
        return {str(r['symbol']): dict(r, history_start=r['listing_date'], reason='official_bond_listing',
                                       source=r['listing_source_url'] or r['source_url'], market='TPEx') for r in rows}

    def stop_lifecycle_job(self, job, reason):
        """Retain cursor, counts, candles and evidence; do not certify completion."""
        table = self._validate_table_name(job['job_table'])
        with self.transaction() as q:
            q.execute(f'''UPDATE `{table}` j JOIN candle_backfill_progress p ON p.job_table=%s AND p.job_id=j.id
                SET j.status='superseded',j.last_error=%s,j.updated_at=UTC_TIMESTAMP(6),p.owner_token=NULL
                WHERE j.id=%s AND j.status='running' AND p.owner_token=%s''',
                (table, '商品生命週期停止自動查詢：'+reason+'；保留行情及斷點，非補齊完成', job['id'], job['owner_token']))
            if q.rowcount < 1:
                raise ValueError('生命週期工作擁有者已變更，未修改工作')

