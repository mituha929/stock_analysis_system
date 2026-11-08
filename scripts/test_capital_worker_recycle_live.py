"""Opt-in real Capital DLL/process/checkpoint integration test on Windows.

Pauses and disconnects the local API temporarily, never restarts it. Two fresh
workers automatically log in sequentially, using the real V2 batch runner and
repository against a NEW, retained test schema. Worker 1 requests a safe pause
after its first committed window; worker 2 resumes the same durable checkpoint.
This is NOT the production recycling implementation or a 4 GiB stress test.
"""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import suppress
import ctypes
from ctypes import wintypes
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
PREFIX = "DLL_RECYCLE_TEST "
SCHEMA_PATTERN = r"capital_dll_test_[0-9a-f]{12}"
SECRETS: tuple[str, ...] = ()


def redact(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if key.lower() in {"password", "user_id", "input"}
                else redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        for secret in SECRETS:
            value = value.replace(secret, "[REDACTED]")
    return value


def emit(event, **values):
    print(PREFIX + json.dumps(redact(dict(event=event, at=datetime.now(timezone.utc).isoformat(),
                                        **values)), ensure_ascii=False, default=str), flush=True)


def load_config():
    from dotenv import load_dotenv, dotenv_values
    global SECRETS
    values = dotenv_values(PROJECT / ".env")
    load_dotenv(PROJECT / ".env")
    SECRETS = tuple(value for key, value in values.items()
                    if value and ("PASSWORD" in key or key == "CAPITAL_USER_ID"))
    if not all(values.get(key) for key in ("CAPITAL_USER_ID", "CAPITAL_PASSWORD", "MYSQL_PASSWORD")):
        raise RuntimeError("Required credentials are missing; no login was attempted")
    return values


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def validate_schema(schema):
    require(bool(re.fullmatch(SCHEMA_PATTERN, schema)), "Not an invocation-owned test schema")
    return schema


def validate_run_dir(raw):
    path = Path(raw).resolve()
    root = (PROJECT / "output" / "diagnostics").resolve()
    require(path.parent == root and bool(re.fullmatch(r"dll-recycle-[0-9a-f]{12}", path.name)),
            "Unexpected diagnostic directory")
    return path


def dll_lock_path():
    return Path(os.getenv('CAPITAL_DLL_LOCK_FILE', str(PROJECT/'data'/'capital_skcom.lock'))).resolve()


def api(path, payload=None, timeout=12):
    # No automatic retry: a timed-out POST may still be active on the server.
    raw = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request("http://127.0.0.1:8000/api/v1" + path, data=raw,
        headers={"Content-Type": "application/json"}, method="POST" if raw is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
            require(response.status in (200, 202), "Unexpected HTTP status")
            return result
    except urllib.error.HTTPError as error:
        # Do not echo credential-bearing validation responses.
        raise RuntimeError(f"Local API {path} returned HTTP {error.code}") from None


def brief(state):
    keys = ("state", "stage", "worker_active", "worker_pid", "pause_requested", "pause_kind",
            "pause_reason", "current_symbol", "current_market", "current_interval", "processed", "windows")
    return {key: state.get(key) for key in keys}


def terminate_owned_if_running(kernel, handle):
    """An exit racing cleanup is success, not a reason to mask the test error."""
    code = wintypes.DWORD()
    require(bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))), 'Cannot inspect owned worker exit')
    if code.value != 259:
        return False
    kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.TerminateProcess.restype = wintypes.BOOL
    if not kernel.TerminateProcess(handle, 2):
        require(bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value != 259,
                'Could not stop the owned test worker')
    return True


def test_rows(repo):
    with repo.transaction(dictionary=True) as cursor:
        cursor.execute("SELECT j.id,j.status,j.attempt_count,p.next_date,p.owner_token,w.phase "
                       "FROM candle_backfill_jobs j JOIN candle_backfill_progress p "
                       "ON p.job_table='candle_backfill_jobs' AND p.job_id=j.id "
                       "JOIN candle_batch_work w ON w.job_table=p.job_table AND w.job_id=p.job_id")
        jobs = cursor.fetchall()
        cursor.execute("SELECT COUNT(*) AS rows_count,COUNT(DISTINCT candle_time) AS unique_times,"
                       "MIN(candle_time) AS first_bar,MAX(candle_time) AS last_bar FROM candles")
        candles = cursor.fetchone()
    return dict(jobs=jobs, candles=candles)


def production_checkpoint(repo, state):
    market = state.get("current_market")
    table = "odd_lot_candle_backfill_jobs" if market in (5, 6) else "candle_backfill_jobs"
    with repo.transaction(dictionary=True) as cursor:
        cursor.execute(f"SELECT j.id,j.status,j.received_count,j.updated_at,p.next_date,p.owner_token "
                       f"FROM `{table}` j LEFT JOIN candle_backfill_progress p "
                       "ON p.job_table=%s AND p.job_id=j.id WHERE j.market_no=%s "
                       "AND j.symbol=%s AND j.interval_code=%s ORDER BY j.updated_at DESC,j.id DESC LIMIT 1",
                       (table, market, state.get("current_symbol"), state.get("current_interval")))
        return dict(table=table, rows=cursor.fetchall())


def child_main(args):
    values = load_config()
    schema = validate_schema(args.schema)
    run_dir = validate_run_dir(args.run_dir)
    require(run_dir.is_dir(), "Supervisor directory is missing")
    # Overrides affect this new test process only, never the API or .env.
    os.environ.update(MYSQL_DATABASE=schema, CAPITAL_KLINE_MAX_DAYS_1M="1",
                      CAPITAL_SUSPENSION_SYNC_ENABLED="false", API_MEMORY_WARNING_MB="768",
                      API_MEMORY_STOP_BACKFILL_MB="1024", API_MEMORY_RESUME_BACKFILL_MB="768",
                      CAPITAL_BACKFILL_JOB_WINDOW_BUDGET="3")
    from app.services.backfill_control import BackfillControl
    from app.services.backfill_engine import BackfillRunner
    from app.services.capital_dll_quote_service import CapitalQuoteService
    from app.services.capital_process_lock import get_capital_process_lock
    from app.services.incremental_candle_repository import MySQLCandleRepository
    from app.services.mysql_instrument_repository import MySQLSettings
    from app.services.quote_cache import QuoteCache
    from app.services.watchlist_store import WatchlistStore
    from measure_native_worker_recycling import windows_api, memory
    import pythoncom

    kernel, psapi = windows_api()
    emit("child_baseline", pid=os.getpid(), round=args.child_round, memory=memory(kernel, psapi))
    control = BackfillControl(run_dir / "control.sqlite3")
    calls, commits = [], []

    class TestRepository(MySQLCandleRepository):
        def commit_window(self, *items, **options):
            count = super().commit_window(*items, **options)
            commits.append(dict(next_date=items[2], rows=len(items[1]), stored_count=count))
            if args.child_round == 1:
                control.pause(True)  # AFTER the real candle/cursor transaction commits.
            emit("window_committed", pid=os.getpid(), round=args.child_round, **commits[-1])
            return count

    class ObservedCapital(CapitalQuoteService):
        def fetch_historical_candles(self, symbol, interval, start, end, **options):
            calls.append(dict(symbol=symbol, interval=interval, start=start, end=end))
            rows = super().fetch_historical_candles(symbol, interval, start, end, **options)
            require(bool(rows), "Real history query returned no rows")
            require(all(start <= item.timestamp.date() <= end and item.volume >= 0 for item in rows),
                    "Source data failed bounded-range/nonnegative-volume checks")
            emit("history_received", pid=os.getpid(), round=args.child_round, rows=len(rows),
                 memory=memory(kernel, psapi), **calls[-1])
            return rows

    repo = TestRepository(replace(MySQLSettings.from_env(), database=schema))
    capital = ObservedCapital(QuoteCache(), watchlist_store=WatchlistStore(run_dir / "watchlist.json"))
    lock = get_capital_process_lock()
    acquired = False
    try:
        lock.acquire(timeout_seconds=0)  # Never compete with an active API/CLI login.
        acquired = True
        connected = capital.connect(values["CAPITAL_USER_ID"], values["CAPITAL_PASSWORD"],
                                    int(values.get("CAPITAL_AUTHORITY") or 0))
        emit('child_connect_returned', pid=os.getpid(), round=args.child_round, response=connected,
             quote_ready=capital.quote_ready, connection_events=capital.connection_events,
             watchlist=capital.watchlist(), subscription_accepted=capital.is_subscribed('2330'))
        deadline = time.monotonic() + 30
        next_observation = time.monotonic()
        while not capital.quote_ready:
            if time.monotonic() >= next_observation:
                emit('child_connection_wait', pid=os.getpid(), round=args.child_round,
                     quote_ready=capital.quote_ready, connection_state=capital.quote_connection_state,
                     message=capital.status_message, connection_events=capital.connection_events)
                next_observation = time.monotonic() + 10
            require(time.monotonic() < deadline, "Login completed but quote readiness timed out")
            # A standalone Windows worker has no GUI/event loop of its own.
            pythoncom.PumpWaitingMessages()
            time.sleep(0.05)
        # Restore the durable subscription explicitly after readiness as well
        # as through callbacks; callbacks may arrive before connect() returns.
        subscription_was_automatic = capital.is_subscribed('2330')
        subscription_error = None
        try:
            capital.subscribe('2330', 1)  # Same default item/page as the API schema.
        except Exception as error:
            # Tick permission/item limits are a separate acceptance criterion;
            # still exercise real history/checkpoint recovery, never hide this.
            subscription_error = f'{type(error).__name__}: {error}'
        emit("child_connected", pid=os.getpid(), round=args.child_round,
             quote_ready=capital.quote_ready, subscription_accepted=capital.is_subscribed("2330"),
             subscription_accepted_before_explicit_restore=subscription_was_automatic,
             subscription_error=subscription_error,
             memory=memory(kernel, psapi))
        control.pause(False)
        result = BackfillRunner(capital, repo, control).run(control.saved_plans())
        rows = test_rows(repo)
        require(len(calls) == 1 and len(commits) == 1, "Expected one bounded history window in this worker")
        require(not result.get("failed"), "Runner reported a failed test job")
        if args.child_round == 1:
            require(result.get("pause_kind") == "manual", "Worker 1 did not pause safely")
        else:
            require(result.get("completed") == 1 and rows["jobs"][0]["status"] == "completed",
                    "Worker 2 did not complete the persisted job")
        emit("child_result", pid=os.getpid(), round=args.child_round, result=result,
             calls=calls, commits=commits, database=rows, memory=memory(kernel, psapi),
             subscription_accepted=capital.is_subscribed('2330'), subscription_error=subscription_error)
    finally:
        capital.stop()
        if acquired:
            lock.release()
        emit("child_stopped", pid=os.getpid(), round=args.child_round, memory=memory(kernel, psapi))
    # Supervisor measures this still-live interpreter, then requests normal exit.
    require(sys.stdin.readline().strip() == "exit", "Missing normal-exit acknowledgement")
    emit("child_exiting", pid=os.getpid(), round=args.child_round, memory=memory(kernel, psapi))
    return 0


def observe_worker(args, schema, run_dir, number, kernel, psapi, api_handle, audit_events):
    from measure_native_worker_recycling import memory, system_memory, probe_existing_dll_lock
    require(min(system_memory(kernel)[key] for key in ("available_ram_mib", "available_commit_mib")) >= 768,
            "Insufficient headroom for a real DLL test worker")
    process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", str(Path(__file__).resolve()),
        "--child-round", str(number), "--schema", schema, "--run-dir", str(run_dir)], cwd=PROJECT,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
    messages, errors = queue.Queue(), deque(maxlen=6)
    def output_reader():
        for line in process.stdout:
            if line.startswith(PREFIX):
                try:
                    messages.put(json.loads(line[len(PREFIX):]))
                except ValueError:
                    pass
        messages.put(None)
    def error_reader():
        for line in process.stderr:
            errors.append(redact(line.strip())[:500])
    threads = [threading.Thread(target=output_reader, daemon=True),
               threading.Thread(target=error_reader, daemon=True)]
    for thread in threads:
        thread.start()
    events, handle, result = [], None, None
    deadline, heartbeat = time.monotonic() + 180, time.monotonic()
    try:
        while result is None or not any(item.get("event") == "child_stopped" for item in events):
            require(time.monotonic() < deadline, "Test worker exceeded its bounded deadline")
            try:
                item = messages.get(timeout=1)
            except queue.Empty:
                if time.monotonic() - heartbeat >= 10:
                    require(api('/backfill/control').get('pause_requested'), "Production pause was cleared")
                    emit("worker_wait", round=number, api_memory=memory(kernel, psapi, api_handle))
                    heartbeat = time.monotonic()
                continue
            require(item is not None, "Test worker exited unexpectedly: " + " | ".join(errors))
            events.append(item)
            audit_events.append(item)
            emit("worker_event", detail=item)
            if item["event"] == "child_baseline":
                handle = kernel.OpenProcess(0x411, False, item["pid"])
                require(bool(handle), "Cannot measure real worker interpreter")
            if item["event"] == "child_result":
                result = item
        before_exit = memory(kernel, psapi, handle)
        api_during = memory(kernel, psapi, api_handle)
        process.stdin.write("exit\n")
        process.stdin.flush()
        require(process.wait(timeout=20) == 0, "Test launcher did not exit normally")
        code = wintypes.DWORD()
        require(bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 0,
                "Real DLL worker did not exit normally")
        lock = probe_existing_dll_lock(dll_lock_path())
        require(lock.get("available") is True, "DLL lock was not released after worker exit")
        after_exit = memory(kernel, psapi, api_handle)
        answer = dict(round=number, worker_pid=result["pid"], launcher_pid=process.pid,
                      worker_before_exit=before_exit, worker_exit_code=code.value,
                      dll_lock_after_exit=lock, api_during=api_during, api_after_exit=after_exit,
                      worker_result=result, events=events)
        emit("worker_exit_verified", **{key:value for key,value in answer.items()
                                        if key not in {"events", "worker_result"}})
        return answer
    finally:
        if process.poll() is None:
            # Only this invocation's child is eligible. Never terminate the API.
            try:
                process.stdin.write("exit\n")
                process.stdin.flush()
                process.wait(timeout=8)
            except (OSError, subprocess.TimeoutExpired):
                # A venv python.exe can be only a launcher. Terminate the
                # handshake-identified interpreter, not merely its launcher.
                if handle:
                    terminate_owned_if_running(kernel, handle)
                else:
                    process.kill()
                process.wait(timeout=8)
        if handle:
            kernel.CloseHandle(handle)
        for thread in threads:
            thread.join(timeout=1)
        for stream in (process.stdin, process.stdout, process.stderr):
            with suppress(OSError):
                stream.close()


def parent_main(args, *, observer=None, before_restore=None):
    require(os.name == "nt" and args.allow_live_handoff and args.api_pid,
            "Windows, --allow-live-handoff and the verified --api-pid are required")
    values = load_config()
    from app.services.backfill_control import BackfillControl
    from app.services.incremental_candle_repository import MySQLCandleRepository
    from app.services.mysql_instrument_repository import MySQLSettings
    from app.services.watchlist_store import WatchlistStore
    from app.services.taiwan_trading_calendar import trading_day_status
    from measure_native_worker_recycling import windows_api, memory, system_memory, probe_existing_dll_lock
    import mysql.connector

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    require(end == start + timedelta(days=1) and end < date.today() and
            all(trading_day_status(day)[0] for day in (start, end)), "Use two adjacent completed trading days")
    token = uuid.uuid4().hex[:12]
    schema = validate_schema("capital_dll_test_" + token)
    run_dir = validate_run_dir(PROJECT / "output" / "diagnostics" / ("dll-recycle-" + token))
    run_dir.mkdir(parents=True, exist_ok=False)
    report_path = run_dir / "result.json"
    report = dict(passed=False, api_pid=args.api_pid, test_schema=schema, run_dir=str(run_dir),
                  real_capital_dll=True, production_api_restarted=False, production_candles_written=False,
                  threshold_stress_test=False, rounds=[])
    kernel, psapi = windows_api()
    api_handle = kernel.OpenProcess(0x410, False, args.api_pid)
    require(bool(api_handle), "Cannot open verified API process")
    original, initial_health, lease = None, None, None
    pause_attempted = disconnect_attempted = False
    try:
        original, initial_health = api('/backfill/control'), api('/health')
        require(original.get('worker_pid') == args.api_pid, "API PID no longer matches the live worker")
        require(initial_health.get('quote_ready') and not original.get('pause_requested'),
                "Expected a connected, unpaused API to restore after this opt-in test")
        require(min(system_memory(kernel)[key] for key in ('available_ram_mib','available_commit_mib')) >= 1024,
                "Less than 1 GiB headroom; refusing live handoff")
        report['before'] = dict(control=brief(original), health=initial_health,
                                api_memory=memory(kernel, psapi, api_handle), system=system_memory(kernel))
        settings = MySQLSettings.from_env()
        admin = mysql.connector.connect(host=settings.host, port=settings.port, user=settings.user,
                                       password=settings.password, connection_timeout=8)
        try:
            cursor = admin.cursor()
            cursor.execute(f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            admin.commit()
            cursor.close()
        finally:
            admin.close()
        repo = MySQLCandleRepository(replace(settings, database=schema))
        repo.ensure_batch_storage('candles', 'candle_backfill_jobs')
        with repo.transaction() as cursor:
            cursor.execute('CREATE TABLE instruments(market_no SMALLINT NOT NULL,symbol VARCHAR(32) NOT NULL,category_no INT NOT NULL,is_active BOOLEAN NOT NULL,PRIMARY KEY(market_no,symbol)) ENGINE=InnoDB')
            cursor.execute("INSERT INTO instruments VALUES(0,'2330',1,TRUE)")
        summary = repo.plan_batch(0,'1m',end,1200,'candles','candle_backfill_jobs','instruments',
                                  explicit_start=start,only_symbols=['2330'])
        plan = dict(market_no=0, interval='1m', start_date=start, end_date=end, as_of_date=end,
                    explicit_start=start, target_bars=1200, target_source='override',
                    candle_table='candles', job_table='candle_backfill_jobs', instrument_table='instruments',
                    batch_v2=True, fixed_range=True, planner_version=3, planned_on=date.today().isoformat(),
                    planned_jobs=1, only_symbols=['2330'], summary=summary)
        require(summary['queued_jobs'] == 1, 'Unexpected test plan')
        BackfillControl(run_dir/'control.sqlite3').update(plans=[plan],state='prepared',pause_requested=False)
        WatchlistStore(run_dir/'watchlist.json').save({'2330':1})
        emit('preflight_passed', test_schema=schema, report_path=str(report_path),
             production=brief(original), api_memory=report['before']['api_memory'])

        pause_attempted = True
        api('/backfill/pause', {})
        deadline = time.monotonic() + 75
        while api('/backfill/control').get('worker_active'):
            require(time.monotonic() < deadline, 'Production worker did not finish safe pause')
            time.sleep(1)
        control = BackfillControl()
        lease = control.acquire_worker()
        paused = control.snapshot()
        require(paused.get('pause_requested'), 'Production pause was cleared concurrently')
        production_repo = MySQLCandleRepository(settings)
        checkpoint = production_checkpoint(production_repo, paused)
        report['production_checkpoint_before_test'] = checkpoint
        saved_plans = json.dumps(paused.get('plans'), sort_keys=True, default=str)
        emit('production_safely_paused', control=brief(paused), checkpoint=checkpoint)
        disconnect_attempted = True
        api('/capital/disconnect', {}, timeout=50)
        require(not api('/health').get('quote_ready'), 'API did not disconnect')
        require(probe_existing_dll_lock(dll_lock_path()).get('available') is True,
                'Production DLL lock is still held; no test worker will start')
        for number in (1,2):
            row = (observer or observe_worker)(args,schema,run_dir,number,kernel,psapi,api_handle,
                                 report.setdefault('worker_attempt_events', []))
            report['rounds'].append(row)
            if number == 1:
                first = test_rows(repo)
                require(first['jobs'][0]['next_date'] == end and first['jobs'][0]['status'] == 'pending'
                        and first['candles']['rows_count'] > 0, 'First checkpoint was not durable')
                report['after_first_worker'] = first
        first, second = (row['worker_result'] for row in report['rounds'])
        require(first['pid'] != second['pid'], 'Workers must be distinct interpreter processes')
        require(first['calls'][0]['start'] == start.isoformat() and first['calls'][0]['end'] == start.isoformat()
                and second['calls'][0]['start'] == end.isoformat() and second['calls'][0]['end'] == end.isoformat(),
                'New worker did not start from the exact persisted checkpoint')
        final = test_rows(repo)
        expected_count = sum(row['commits'][0]['rows'] for row in (first,second))
        require(final['candles']['rows_count'] == final['candles']['unique_times'] == expected_count,
                'Missing or duplicate bars across worker boundary')
        require(final['jobs'][0]['next_date'] == end+timedelta(days=1)
                and final['jobs'][0]['owner_token'] is None, 'Final cursor/owner is incorrect')
        require(production_checkpoint(production_repo, paused) == checkpoint, 'Production checkpoint changed during test')
        require(json.dumps(control.snapshot().get('plans'),sort_keys=True,default=str) == saved_plans,
                'Production plan changed during test')
        subscription_passed = all(row.get('subscription_accepted') for row in (first,second))
        report.update(passed=subscription_passed, core_login_exit_resume_passed=True,
                      subscription_restore_passed=subscription_passed, final_test_database=final,
                      production_checkpoint_preserved=True, production_plans_preserved=True)
        emit('core_integration_checks_passed', test_database=final,
             production_checkpoint_preserved=True, subscription_restore_passed=subscription_passed)
    except Exception as error:
        report['error'] = redact(f'{type(error).__name__}: {error}')
        emit('test_failed', error=report['error'])
    finally:
        # All children have exited before giving the original API the DLL again.
        if before_restore is not None:
            try:
                before_restore(report)
            except Exception as error:
                report['passed'] = False
                report['cleanup_error'] = redact(f'{type(error).__name__}: {error}')
        if lease is not None:
            lease.release()
        restoration = {}
        try:
            if disconnect_attempted and initial_health and initial_health.get('quote_ready'):
                require(probe_existing_dll_lock(dll_lock_path()).get('available') is True,
                        'DLL is still owned; refusing concurrent production reconnect')
                api('/capital/connect', {'user_id':values['CAPITAL_USER_ID'],
                    'password':values['CAPITAL_PASSWORD'],'authority':int(values.get('CAPITAL_AUTHORITY') or 0)}, timeout=35)
                deadline = time.monotonic()+35
                while not api('/health').get('quote_ready'):
                    require(time.monotonic()<deadline, 'Original API did not regain quote readiness')
                    time.sleep(0.5)
                restoration['quote_reconnected'] = True
            if pause_attempted and original and not original.get('pause_requested'):
                restoration['resume_response'] = api('/backfill/resume',{}).get('message')
                time.sleep(3)
            restoration['control'] = brief(api('/backfill/control'))
            restoration['health'] = api('/health')
            code = wintypes.DWORD()
            require(bool(kernel.GetExitCodeProcess(api_handle,ctypes.byref(code))) and code.value==259,
                    'Original API process is not alive')
            restoration['original_api_pid_alive'] = True
            if pause_attempted:
                require(restoration['health'].get('quote_ready') and
                        not restoration['control'].get('pause_requested'), 'Production restoration is incomplete')
        except Exception as error:
            restoration['error'] = redact(f'{type(error).__name__}: {error}')
            report['passed'] = False
        report['restoration'] = restoration
        kernel.CloseHandle(api_handle)
        report_path.write_text(json.dumps(redact(report),ensure_ascii=False,default=str,indent=2),encoding='utf-8')
        emit('test_summary', passed=report['passed'], report_path=str(report_path),
             test_schema=schema, restoration=restoration)
    return 0 if report['passed'] else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-live-handoff',action='store_true')
    parser.add_argument('--api-pid',type=int)
    parser.add_argument('--start',default='2026-10-12')
    parser.add_argument('--end',default='2026-10-13')
    parser.add_argument('--child-round',type=int,choices=(1,2))
    parser.add_argument('--schema')
    parser.add_argument('--run-dir')
    args=parser.parse_args()
    try:
        return child_main(args) if args.child_round else parent_main(args)
    except Exception as error:
        emit('fatal_error',error=f'{type(error).__name__}: {error}')
        return 1


if __name__=='__main__':
    raise SystemExit(main())
