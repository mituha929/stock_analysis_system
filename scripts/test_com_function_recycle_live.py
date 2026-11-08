"""Opt-in COM acceptance: 3 symbols/7 intervals, isolated MySQL, one rotation.

Requires the production API already disconnected and paused. Never POSTs to
that API, never resumes it, and drops ONLY the schema created by this run.
The real production supervisor/runner/repository execute the rotation test.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_quote_login_id_live import api_brief, redact

TAIPEI = timezone(timedelta(hours=8))
SYMBOLS = ((0, '2330'), (1, '6488'), (0, '0050'))
INTERVALS = ('day', '1m', '5m', '15m', '60m', 'week', 'month')
SCHEMA_PREFIX = 'capital_com_accept_'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def owned_schema(schema, token):
    require(bool(re.fullmatch(r'[0-9a-f]{12}', token)) and
            schema == SCHEMA_PREFIX + token, 'Refusing a non-invocation-owned schema')
    return schema


def control_digest(path):
    require(path.is_file(), 'Existing production control file is required')
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        row = connection.execute('SELECT payload FROM control WHERE id=1').fetchone()
        require(row is not None, 'Production control is empty')
        return hashlib.sha256(row[0].encode()).hexdigest()
    finally:
        connection.close()


def check_idle(*, own_worker_lease=False):
    state = api_brief()
    require(state['quote_ready'] is False and state['native_pid'] is None,
            'Production API is connected; no login attempted')
    # The HTTP endpoint probes the OS lock, not a running-job counter. While
    # WE hold it to exclude production workers, it correctly reports True.
    require((own_worker_lease or state['worker_active'] is False) and state['pause_requested'] is True,
            'Production backfill is not idle and paused; no login attempted')
    return state


def validate_rows(rows, first, last):
    from app.services.candle_validation import validate_candle_volumes
    require(bool(rows), 'Source returned no candles for the explicit test range')
    validate_candle_volumes(rows)
    require(len({c.timestamp for c in rows}) == len(rows), 'Duplicate source timestamps')
    for c in rows:
        require(first <= c.timestamp.date() <= last, 'Source returned an out-of-range candle')
        require(all(math.isfinite(v) and v > 0 for v in (c.open, c.high, c.low, c.close)),
                'Invalid price in source candle')
        require(c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high,
                'Invalid source OHLC ordering')


def test_range(interval, as_of):
    from app.services.backfill_policy import completed_end, period_start
    last = completed_end(interval, as_of)
    days = {'day': 28, 'week': 49, 'month': 100}.get(interval, 0)
    first = period_start(interval, last - timedelta(days=days))
    return first, last


def scalar(repo, sql, parameters=()):
    with repo.transaction() as q:
        q.execute(sql, parameters)
        return q.fetchone()[0]


def checkpoints(repo):
    with repo.transaction(dictionary=True) as q:
        q.execute('SELECT j.id,j.symbol,j.interval_code,j.status,j.received_count,'
                  'p.next_date,p.owner_token FROM candle_backfill_jobs j '
                  'JOIN candle_backfill_progress p ON p.job_table=%s AND p.job_id=j.id '
                  'ORDER BY j.id', ('candle_backfill_jobs',))
        return q.fetchall()


def choose_trigger(baseline, current):
    require(baseline is not None and current is not None and
            all(math.isfinite(v) and v > 32 for v in (baseline, current)),
            'Measured native memory is required for the test trigger')
    # Arm with REAL usage above a lower, test-only threshold. Growth is not a
    # prerequisite for testing commit ordering, nor evidence of a native leak.
    return round(current * 0.9, 1)


def available_cutoff(smoke, requested):
    daily = [row for row in smoke if row['interval'] == 'day']
    require(len(daily) == len(SYMBOLS) and {row['symbol'] for row in daily} == {s for _, s in SYMBOLS},
            'Need a real daily result for every test symbol before choosing a source cutoff')
    cutoff = min(row['data_last'].date() for row in daily)
    require(cutoff <= requested, 'Daily source cutoff is after the requested date')
    return cutoff


def run_acceptance(values, settings, schema, run_dir, report, emit):
    from app.services.backfill_control import BackfillControl
    from app.services.backfill_engine import BackfillRunner
    from app.services.backfill_policy import completed_end
    from app.services.capital_worker_service import CapitalWorkerService, NativeWorkerConfig
    from app.services.capital_worker_runtime import MemoryWatchlistStore
    from app.services.incremental_candle_repository import MySQLCandleRepository
    from app.services.process_memory import current_process_memory
    from app.services.quote_cache import QuoteCache
    from app.services.taiwan_trading_calendar import latest_trading_day_on_or_before

    service = CapitalWorkerService(QuoteCache(), control=None,
        watchlist_store=MemoryWatchlistStore({}),
        config=NativeWorkerConfig(quote_backend='com'), start_monitor=False)
    report['login_requests_initial'] = 1
    try:
        service.connect(values['CAPITAL_USER_ID'], values['CAPITAL_PASSWORD'],
                        int(values.get('CAPITAL_AUTHORITY') or 0))
        baseline = service.worker_snapshot()
        report['initial_worker'] = baseline
        emit('com_connected', pid=baseline['pid'], private_mb=baseline['private_mb'])
        require(service.quote_ready and baseline['quote_backend'] == 'com', 'COM is not ready')
        repo = MySQLCandleRepository(replace(settings, database=schema))
        repo.ensure_batch_storage('candles', 'candle_backfill_jobs')
        with repo.transaction() as q:
            q.execute('CREATE TABLE instruments(market_no SMALLINT NOT NULL,symbol VARCHAR(32) NOT NULL,'
                      'category_no INT NOT NULL,is_active BOOLEAN NOT NULL,'
                      'PRIMARY KEY(market_no,symbol)) ENGINE=InnoDB')
            q.executemany('INSERT INTO instruments VALUES(%s,%s,1,TRUE)', SYMBOLS)

        # No official-list sync or production watchlist edits. A Saturday can
        # prove SDK acceptance, not fresh market ticks from a closed exchange.
        try:
            service.subscribe('2330', 1)
            report['subscription_before_rotation'] = service.is_subscribed('2330')
        except Exception as error:
            report['subscription_before_rotation'] = False
            report['subscription_error'] = str(error)
        report['fresh_live_tick_acceptance'] = 'not_asserted_by_this_harness'

        as_of = latest_trading_day_on_or_before(datetime.now(TAIPEI).date() - timedelta(days=1))
        report['requested_as_of'] = as_of
        for interval in INTERVALS:
            if interval == '1m':
                cutoff = available_cutoff(report['smoke'], as_of)
                report['latest_requested_day_available'] = cutoff == as_of
                report['source_data_through'] = cutoff
                if cutoff != as_of:
                    emit('source_cutoff_warning', requested=as_of, actual=cutoff,
                         message='Use confirmed historical availability; newest-day completeness is NOT passed')
                as_of = cutoff
            first, last = test_range(interval, as_of)
            for market, symbol in SYMBOLS:
                check_idle(own_worker_lease=True)
                committed = False
                began = time.monotonic()
                rows = service.fetch_historical_candles(symbol, interval, first, last, cache_result=False)
                try:
                    validate_rows(rows, first, last)
                    # Both calls perform real transactions; repeat same genuine
                    # source rows to prove idempotency, never fabricate prices.
                    repo.upsert_candles(symbol, interval, rows)
                    repo.upsert_candles(symbol, interval, rows)
                    count = scalar(repo, 'SELECT COUNT(*) FROM candles WHERE symbol=%s AND interval_code=%s',
                                   (symbol, interval))
                    require(count == len(rows), 'MySQL write/idempotency count mismatch')
                    committed = True
                    point = dict(market=market, symbol=symbol, interval=interval, first=first, last=last,
                        rows=len(rows), unique_rows=count, data_first=rows[0].timestamp,
                        data_last=rows[-1].timestamp, seconds=round(time.monotonic()-began, 3),
                        worker_private_mb=service.worker_snapshot()['private_mb'],
                        api_process_private_mb=current_process_memory().private_usage_mb)
                    report['smoke'].append(point)
                    emit('history_write_passed', **point)
                finally:
                    rows.clear()
                    service.history_checkpoint(committed)

        # Select the TEST threshold from actual measured usage. Do not fake
        # telemetry, allocate synthetic pressure, or approach 4 GiB.
        original_config = service.config
        threshold = choose_trigger(baseline['private_mb'], service.worker_snapshot()['private_mb'])
        report['test_recycle_mb'] = threshold
        report['production_recycle_mb'] = 3072
        service.config = replace(original_config, recycle_mb=threshold)
        first = latest_trading_day_on_or_before(as_of - timedelta(days=1))
        control = BackfillControl(run_dir / 'control.sqlite3')
        calls, commits = [], []
        old_process = service._process
        should_pause = True

        class ObservedRepository(MySQLCandleRepository):
            def commit_window(self, job, candles, next_date, empty_windows, **options):
                nonlocal should_pause
                require(bool(candles), 'Rotation window returned no source rows')
                before = service.worker_snapshot()
                require(before['pending_windows'] == 1, 'Uncommitted source window is not pinned')
                if should_pause:
                    require(before['recycle_due'], 'Real memory did not arm the lowered trigger')
                    require(not service._maybe_recycle_locked() and service._process is old_process
                            and old_process.is_alive(), 'Worker was recycled before COMMIT')
                count = super().commit_window(job, candles, next_date, empty_windows, **options)
                durable = checkpoints(self)
                require(durable[0]['next_date'] == next_date, 'Checkpoint not visible after COMMIT')
                commits.append(dict(next_date=next_date, rows=len(candles), checkpoint=durable,
                                    worker_before_commit=before))
                if should_pause:
                    control.pause(True)
                    # The old worker's threshold crossing is already latched.
                    # Restore the normal TEST configuration only AFTER COMMIT,
                    # before the actual runner's ACK launches the replacement.
                    # Thus a naturally low initial allocation in a replacement
                    # is not required to be below an artificial test threshold.
                    service.config = original_config
                    should_pause = False
                return count

        rotation_repo = ObservedRepository(replace(settings, database=schema))
        rotation_repo.ensure_batch_storage('rotation_candles', 'candle_backfill_jobs')
        summary = rotation_repo.plan_batch(0, '1m', as_of, 1200, 'rotation_candles',
            'candle_backfill_jobs', 'instruments', explicit_start=first, only_symbols=['2330'])
        require(summary['queued_jobs'] == 1, 'Rotation plan must contain exactly one job')
        plan = dict(market_no=0, interval='1m', start_date=first, explicit_start=first,
            end_date=as_of, as_of_date=as_of, target_bars=1200, target_source='override',
            candle_table='rotation_candles', job_table='candle_backfill_jobs', instrument_table='instruments',
            batch_v2=True, fixed_range=True, planner_version=3, planned_on=date.today().isoformat(),
            planned_jobs=1, only_symbols=['2330'], summary=summary)
        original_fetch = service.fetch_historical_candles
        def observed_fetch(symbol, interval, start_date, end_date, **options):
            check_idle(own_worker_lease=True)
            calls.append(dict(symbol=symbol, interval=interval, first=start_date, last=end_date,
                              worker_pid=service._process.pid))
            return original_fetch(symbol, interval, start_date, end_date, **options)
        service.fetch_historical_candles = observed_fetch
        control.update(plans=[plan], state='prepared', pause_requested=False)
        first_result = BackfillRunner(service, rotation_repo, control).run([plan])
        after = service.worker_snapshot()
        report['rotation'] = dict(first_result=first_result, before_pid=old_process.pid,
                                  after_worker=after, first_checkpoint=checkpoints(rotation_repo))
        require(len(calls) == len(commits) == 1 and first_result.get('pause_kind') == 'manual',
                'First round did not stop at exactly one committed window')
        require(after['recycle_count'] == 1 and not old_process.is_alive() and old_process.exitcode == 0,
                'Production supervisor did not rotate normally after COMMIT')
        require(after['pid'] != old_process.pid and after['pending_windows'] == 0 and service.quote_ready,
                'Replacement COM worker is not ready or still has a pending window')
        require(after['forced_exits'] == 0, 'Rotation forced a native exit')
        report['subscription_after_rotation'] = service.is_subscribed('2330')
        emit('post_commit_rotation_passed', old_pid=old_process.pid, new_pid=after['pid'],
             threshold_mb=threshold, before_mb=commits[0]['worker_before_commit']['private_mb'],
             after_mb=after['private_mb'])

        # Restore this TEST supervisor's standard threshold before continuation
        # so exactly one rotation is exercised. Never touches the real API.
        service.config = original_config
        control.pause(False)
        second_result = BackfillRunner(service, rotation_repo, control).run(control.saved_plans())
        final_jobs = checkpoints(rotation_repo)
        count = scalar(rotation_repo, 'SELECT COUNT(*) FROM rotation_candles')
        unique_count = scalar(rotation_repo, 'SELECT COUNT(DISTINCT candle_time) FROM rotation_candles')
        require(len(calls) == len(commits) == 2, 'Expected exactly two bounded rotation windows')
        require(calls[0]['first'] == calls[0]['last'] == first and
                calls[1]['first'] == calls[1]['last'] == as_of,
                'Resumed worker did not start at the exact durable checkpoint')
        require(final_jobs[0]['status'] == 'completed' and final_jobs[0]['owner_token'] is None and
                final_jobs[0]['next_date'] == as_of + timedelta(days=1), 'Final job/cursor is incorrect')
        require(count == unique_count == sum(c['rows'] for c in commits), 'Missing/duplicate resumed candles')
        require(not second_result.get('paused') and not second_result.get('failed'), 'Resume failed')
        report['rotation'].update(second_result=second_result, final_jobs=final_jobs,
            calls=calls, commits=commits, total_rows=count, unique_rows=unique_count,
            resumed_from_exact_checkpoint=True)
        require(report['subscription_before_rotation'] and report['subscription_after_rotation'],
                'History/rotation passed but quote subscription/restoration did not pass')
        report['checks_passed'] = True
    finally:
        service.stop()
        report['final_worker'] = service.worker_snapshot()
        report['owned_worker_exited'] = service._process is None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-live-test', action='store_true')
    args = parser.parse_args(argv)
    if not args.allow_live_test:
        parser.error('--allow-live-test is required; no login/database creation attempted')
    require(os.name == 'nt', 'Windows is required')
    from dotenv import dotenv_values
    import mysql.connector
    from app.services.capital_process_lock import CapitalProcessLock
    from app.services.mysql_instrument_repository import MySQLSettings
    from measure_native_worker_recycling import windows_api, system_memory

    values = dotenv_values(PROJECT / '.env')
    secrets = tuple(v for k, v in values.items() if v and ('PASSWORD' in k or k == 'CAPITAL_USER_ID'))
    require(all(values.get(k) for k in ('CAPITAL_USER_ID', 'CAPITAL_PASSWORD', 'MYSQL_PASSWORD')),
            'Required credentials are missing; no login attempted')
    for key, value in values.items():
        if value and key.startswith(('CAPITAL_', 'MYSQL_', 'API_MEMORY_', 'TWSE_')):
            os.environ[key] = value
    production_control = Path(values.get('CAPITAL_BACKFILL_CONTROL_DB') or 'data/backfill_control.sqlite3')
    if not production_control.is_absolute():
        production_control = PROJECT / production_control
    token = uuid.uuid4().hex[:12]
    schema = owned_schema(SCHEMA_PREFIX + token, token)
    run_dir = PROJECT / 'output' / 'diagnostics' / ('com-accept-' + token)
    run_dir.mkdir(parents=True, exist_ok=False)
    target = run_dir / 'result.json'
    lease = CapitalProcessLock(production_control.with_suffix('.worker.lock'))
    admin = None
    created = lease_held = False
    report = dict(started_at=datetime.now(TAIPEI), test_schema=schema, smoke=[], passed=False,
        checks_passed=False, production_api_mutated=False, production_candles_written=False,
        artificial_memory_pressure=False, full_market_regrab=False, background_monitor=False)
    def emit(event, **data):
        print(json.dumps(redact(dict(event=event, **data), secrets), ensure_ascii=False, default=str), flush=True)
    try:
        report['before'] = check_idle()
        lease.acquire(timeout_seconds=0)
        lease_held = True
        before_digest = control_digest(production_control)
        report['production_control_before'] = before_digest
        kernel, _ = windows_api()
        report['system_memory'] = system_memory(kernel)
        require(min(report['system_memory'][k] for k in ('available_ram_mib', 'available_commit_mib')) >= 1024,
                'Less than 1 GiB available headroom; no login attempted')
        settings = MySQLSettings.from_env()
        require(settings.database != schema, 'Test schema collides with production')
        # Production access is SELECT-only, before the independent test schema.
        production = mysql.connector.connect(host=settings.host, port=settings.port, user=settings.user,
            password=settings.password, database=settings.database, connection_timeout=8)
        try:
            q = production.cursor()
            q.execute('SELECT market_no,symbol FROM instruments WHERE is_active=TRUE AND '
                      "((market_no=0 AND symbol IN ('2330','0050')) OR (market_no=1 AND symbol='6488'))")
            require(set(q.fetchall()) == set(SYMBOLS), 'Chosen symbols are not in the active production master')
            q.close()
        finally:
            production.close()
        admin = mysql.connector.connect(host=settings.host, port=settings.port, user=settings.user,
                                         password=settings.password, connection_timeout=8)
        q = admin.cursor()
        try:
            q.execute(f'CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
            created = True
            admin.commit()
        finally:
            q.close()
        # Child-local overrides; do not edit .env or the real API's settings.
        os.environ.update(CAPITAL_KLINE_MAX_DAYS_1M='1', CAPITAL_SUSPENSION_SYNC_ENABLED='false',
            CAPITAL_DAY_INTEGRITY_ENABLED='false', CAPITAL_BACKFILL_RECOVERY_WINDOW_BUDGET='4',
            CAPITAL_BACKFILL_CONTROL_DB=str(run_dir / 'control.sqlite3'))
        emit('preflight_passed', test_schema=schema, api_pid=report['before']['api_pid'], report_path=str(target))
        run_acceptance(values, settings, schema, run_dir, report, emit)
    except Exception as error:
        report.update(error_type=type(error).__name__, error=str(error))
        emit('acceptance_failed', error_type=type(error).__name__, error=str(error))
    finally:
        if created:
            try:
                # Never enumerate/drop old test databases, use IF EXISTS, or
                # accept an external schema name. Only this run's CREATE target.
                owned_schema(schema, token)
                q = admin.cursor()
                try:
                    q.execute(f'DROP DATABASE `{schema}`')
                    admin.commit()
                    q.execute('SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name=%s', (schema,))
                    report['test_schema_removed'] = q.fetchone()[0] == 0
                finally:
                    q.close()
            except Exception as error:
                report['cleanup_error'] = str(error)
        if admin is not None:
            admin.close()
        if lease_held:
            lease.release()
            lease_held = False
        try:
            report['after'] = check_idle()
            report['api_unchanged'] = report.get('before') == report['after']
            report['production_control_after'] = control_digest(production_control)
            report['production_control_unchanged'] = (report.get('production_control_before') ==
                                                       report['production_control_after'])
        except Exception as error:
            report['after_check_error'] = str(error)
        report['passed'] = all(report.get(k) is True for k in ('checks_passed', 'owned_worker_exited',
            'test_schema_removed', 'api_unchanged', 'production_control_unchanged'))
        report['finished_at'] = datetime.now(TAIPEI)
        target.write_text(json.dumps(redact(report, secrets), ensure_ascii=False, default=str, indent=2), encoding='utf-8')
        emit('acceptance_summary', passed=report['passed'], report_path=str(target),
             smoke_checks=len(report['smoke']), schema_removed=report.get('test_schema_removed'),
             api_unchanged=report.get('api_unchanged'), control_unchanged=report.get('production_control_unchanged'))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
