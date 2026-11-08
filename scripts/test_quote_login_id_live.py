"""Opt-in, one-login quote connection test; no history, MySQL or API restart.

Uses an empty in-memory watchlist and the production native-worker supervisor,
with its monitor disabled to prevent automatic retries. Only this invocation's
child is stopped afterwards. The local API must already be idle and paused.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import sqlite3
import time
import urllib.request
import urllib.error

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def redact(value, secrets):
    if isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, '[REDACTED]')
    return value


def api_get(path):
    # Read-only, fixed localhost URL; never sends credentials to the API.
    with urllib.request.urlopen('http://127.0.0.1:8000/api/v1' + path, timeout=8) as response:
        return json.load(response)


def offline_brief(guard):
    from app.services.capital_process_lock import CapitalProcessLock
    path = Path(guard['values'].get('CAPITAL_BACKFILL_CONTROL_DB') or 'data/backfill_control.sqlite3')
    if not path.is_absolute():
        path = PROJECT / path
    if not path.is_file():
        raise RuntimeError('Existing backfill control file is required; no login attempted')
    if 'lease' not in guard:
        lease = CapitalProcessLock(path.with_suffix('.worker.lock'))
        lease.acquire(timeout_seconds=0)
        guard['lease'] = lease
    # Never initializes tables or updates the durable checkpoint/control file.
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        row = connection.execute('SELECT payload FROM control WHERE id=1').fetchone()
    finally:
        connection.close()
    if row is None:
        raise RuntimeError('Backfill control is empty; no login attempted')
    control = json.loads(row[0])
    return dict(api_pid=None, native_pid=None, quote_ready=False, phase='api_offline',
                worker_active=False, pause_requested=control.get('pause_requested'),
                backfill_state=control.get('state'), control_sha256=hashlib.sha256(row[0].encode()).hexdigest())


def api_brief(offline=None):
    try:
        health, control = api_get('/health'), api_get('/backfill/control')
    except urllib.error.URLError as error:
        if offline is None or not (isinstance(error.reason, ConnectionRefusedError)
                                   or getattr(error.reason, 'winerror', None) == 10061):
            raise
        return offline_brief(offline)
    native = health.get('native_worker') or {}
    return dict(api_pid=native.get('api_pid'), native_pid=native.get('pid'),
                quote_ready=health.get('quote_ready'), phase=native.get('phase'),
                worker_active=control.get('worker_active'), pause_requested=control.get('pause_requested'),
                backfill_state=control.get('state'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-live-login', action='store_true')
    parser.add_argument('--backend', choices=('dll', 'com'), default='dll')
    parser.add_argument('--allow-api-offline', action='store_true',
                        help='Only on connection refused: hold worker lock and read existing paused control file')
    args = parser.parse_args()
    if not args.allow_live_login:
        parser.error('--allow-live-login is required; no login attempted')

    from dotenv import dotenv_values
    values = dotenv_values(PROJECT / '.env')
    secrets = tuple(value for key, value in values.items()
                    if value and ('PASSWORD' in key or key == 'CAPITAL_USER_ID'))
    report = dict(started_at=datetime.now(timezone.utc).isoformat(), login_calls_requested=0,
                  api_restarted=False, history_requests=0, mysql_connections=0,
                  subscriptions_requested=0, background_monitor=False)
    service = None
    offline = dict(values=values) if args.allow_api_offline else None
    exit_code = 1
    try:
        user, password = values.get('CAPITAL_USER_ID'), values.get('CAPITAL_PASSWORD')
        if not user or not password:
            raise RuntimeError('Missing Capital credentials; no login attempted')
        authority = int(values.get('CAPITAL_AUTHORITY') or 0)
        if authority not in range(4):
            raise RuntimeError('Invalid Capital authority; no login attempted')
        report['quote_backend'] = args.backend
        for key in ('CAPITAL_SKDLL_PY', 'CAPITAL_SKCOM_DLL', 'CAPITAL_DLL_LOCK_FILE'):
            if values.get(key):
                os.environ[key] = values[key]
        report['before'] = api_brief(offline)
        before = report['before']
        if before['quote_ready'] is not False or before['native_pid'] is not None:
            raise RuntimeError('Local API is not disconnected; no login attempted')
        if before['worker_active'] is not False or before['pause_requested'] is not True:
            raise RuntimeError('Backfill is not idle and paused; no login attempted')

        from app.services.capital_worker_service import CapitalWorkerService, NativeWorkerConfig
        from app.services.capital_worker_runtime import MemoryWatchlistStore
        from app.services.quote_cache import QuoteCache

        # No app.main, repository, BackfillControl or database is instantiated.
        service = CapitalWorkerService(QuoteCache(), control=None,
                                       watchlist_store=MemoryWatchlistStore({}),
                                       config=NativeWorkerConfig(quote_backend=args.backend), start_monitor=False)
        started = time.monotonic()
        report['login_calls_requested'] = 1
        result = service.connect(user, password, authority)
        report['connect_elapsed_seconds'] = round(time.monotonic() - started, 3)
        report['result'] = result
        report['connected'] = service.quote_ready
        report['connected_snapshot'] = service.worker_snapshot()
        exit_code = 0 if service.quote_ready else 1
    except Exception as error:
        report.update(error_type=type(error).__name__, error=str(error), connected=False)
        if service is not None:
            report['failure_snapshot'] = service.worker_snapshot()
    finally:
        if service is not None:
            child = service._process
            try:
                service.stop()
                report['final_snapshot'] = service.worker_snapshot()
                report['owned_child_exited'] = child is None or child.exitcode is not None
                if not report['owned_child_exited']:
                    exit_code = 1
            except Exception as error:
                report.update(cleanup_error=str(error), cleanup_error_type=type(error).__name__)
                exit_code = 1
        try:
            report['after'] = api_brief(offline)
            before, after = report.get('before'), report['after']
            report['api_and_backfill_unchanged'] = before == after
            if before and before != after:
                exit_code = 1
        except Exception as error:
            report['after_check_error'] = str(error)
            exit_code = 1
        if offline and offline.get('lease') is not None:
            offline['lease'].release()
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        report = redact(report, secrets)
        directory = PROJECT / 'output' / 'diagnostics'
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / ('quote-' + args.backend + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json')
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(dict(report_path=str(target), **report), ensure_ascii=False, default=str), flush=True)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
