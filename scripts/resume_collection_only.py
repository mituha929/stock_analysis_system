"""Guarded operations for an explicitly approved, collection-only API restart.

Does not stop/start processes itself. Never resets a MySQL cursor or invokes
force/regrab. Secrets stay in .env/local HTTP payloads, not args or reports.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_capital_worker_recycle_live as support

TAIPEI = timezone(timedelta(hours=8))


def plan_digest(plans):
    return hashlib.sha256(json.dumps(plans, sort_keys=True, default=str).encode()).hexdigest()


def pin_collection(plans, cutoff):
    """Change the as-of only. Preserve work scopes, ranges and all old progress."""
    expected = {(market, interval) for market in (0, 1, 4, 5, 6)
                for interval in ('day', '1m', '5m', '15m', '60m', 'week', 'month')}
    support.require(len(plans) == 35 and {(p['market_no'], p['interval']) for p in plans} == expected,
                    'Expected the original 35 saved collection groups')
    result = deepcopy(plans)
    for plan in result:
        support.require(not plan.get('force') and not plan.get('fixed_range'),
                        'Do not alter an explicit forced/fixed historical range')
        support.require(plan.get('explicit_start') is None, 'Do not alter an explicit historical start')
        support.require(date.fromisoformat(str(plan['end_date'])) <= cutoff,
                        'Saved range is beyond the authorized cutoff')
        plan['as_of_date'] = cutoff.isoformat()
    return result


def progress_fingerprint(repo):
    """SELECT-only fingerprint of ALL durable MySQL collection checkpoints."""
    digest = hashlib.sha256()
    count = 0
    with repo.transaction() as q:
        q.execute('SELECT job_table,job_id,mode,target_bars,next_date,empty_windows,owner_token,updated_at '
                  'FROM candle_backfill_progress ORDER BY job_table,job_id')
        while rows := q.fetchmany(1000):
            for row in rows:
                digest.update(json.dumps(row, default=str, ensure_ascii=False).encode())
                digest.update(b'\n')
            count += len(rows)
    return dict(rows=count, sha256=digest.hexdigest())


def live_state():
    return support.api('/health'), support.api('/backfill/control')


def idle_guard(health, control, expected_pid):
    native = health.get('native_worker') or {}
    support.require(native.get('api_pid') == expected_pid, 'API PID changed; stop this operation')
    support.require(not health.get('quote_ready') and native.get('pid') is None,
                    'API must already be disconnected')
    support.require(control.get('pause_requested') is True and control.get('worker_active') is False,
                    'Collection must already be idle and manually paused')
    support.require(control.get('preparation_complete') is not False and
                    not control.get('preparation_pending') and not control.get('pending_schedule'),
                    'Preparation or a pending schedule must be resolved first')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('prepare', 'activate'))
    parser.add_argument('--allow-collection-resume', action='store_true')
    parser.add_argument('--api-pid', type=int, required=True)
    parser.add_argument('--as-of', type=date.fromisoformat, default=date(2026, 10, 22))
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args(argv)
    support.require(args.allow_collection_resume, 'Explicit collection-resume approval is required')
    manifest = args.manifest.resolve()
    support.require(manifest.parent == (ROOT/'output'/'diagnostics').resolve() and
                    manifest.name.startswith('collection-resume-') and manifest.suffix == '.json',
                    'Invalid operations report path')
    values = support.load_config()
    from app.services.backfill_control import BackfillControl
    from app.services.incremental_candle_repository import MySQLCandleRepository
    repo = MySQLCandleRepository()
    health, state = live_state()
    idle_guard(health, state, args.api_pid)

    if args.phase == 'prepare':
        support.require(not manifest.exists(), 'Do not overwrite an earlier operation report')
        before_plans = state.get('plans') or []
        pinned = pin_collection(before_plans, args.as_of)
        control = BackfillControl()
        with control.worker():
            saved = control.snapshot()
            support.require(saved.get('pause_requested') and
                            plan_digest(saved.get('plans')) == plan_digest(before_plans),
                            'Saved plans changed before the exclusive lease')
            progress = progress_fingerprint(repo)
            report = dict(prepared_at=datetime.now(TAIPEI), old_api_pid=args.api_pid,
                cutoff=args.as_of, original_plans=before_plans, original_plans_sha256=plan_digest(before_plans),
                pinned_plans_sha256=plan_digest(pinned), groups=len(pinned),
                progress_before=progress, previous_integrity_setting=values.get('CAPITAL_DAY_INTEGRITY_ENABLED'),
                config_changes=dict(CAPITAL_DAY_INTEGRITY_ENABLED='false', CAPITAL_WORKER_QUOTE_BACKEND='com'),
                safe_to_stop=False)
            manifest.write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2), encoding='utf-8')
            control.update(plans=pinned, manual_run=True, resume_pending=False,
                day_integrity_active=False, day_integrity_scheduled=False,
                day_integrity_schedule_reason='使用者要求：先完成2026-10-22這批回補，完整性驗證延後',
                day_integrity_deferred=dict(active=True, cutoff=args.as_of.isoformat(),
                    requested_at=datetime.now(TAIPEI).isoformat(), manifest=str(manifest),
                    previous_env_setting=values.get('CAPITAL_DAY_INTEGRITY_ENABLED')))
            after = progress_fingerprint(repo)
            support.require(after == progress, 'MySQL checkpoints changed during preparation')
            report.update(progress_after_prepare=after, safe_to_stop=True)
            manifest.write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2), encoding='utf-8')
        print(json.dumps(dict(phase='prepared', safe_to_stop=True, api_pid=args.api_pid,
            groups=len(pinned), cutoff=str(args.as_of), checkpoint_rows=progress['rows'],
            checkpoints_unchanged=True, manifest=str(manifest)), ensure_ascii=False), flush=True)
        return 0

    report = json.loads(manifest.read_text(encoding='utf-8'))
    support.require(report.get('safe_to_stop') and args.api_pid != report['old_api_pid'],
                    'A new, approved API process is required')
    support.require(values.get('CAPITAL_DAY_INTEGRITY_ENABLED', '').lower() in {'false', '0'},
                    'Integrity must be explicitly disabled before restarting')
    support.require((health.get('native_worker') or {}).get('quote_backend') == 'com', 'Expected COM backend')
    support.require(plan_digest(state.get('plans')) == report['pinned_plans_sha256'],
                    'Saved scope changed during restart')
    support.require(progress_fingerprint(repo) == report['progress_before'],
                    'MySQL checkpoints changed during restart; do not resume blindly')
    report.update(new_api_pid=args.api_pid, checkpoints_preserved_during_restart=True,
                  activated_at=datetime.now(TAIPEI).isoformat())
    # Write the guard result BEFORE login/resume so a later timeout never loses it.
    manifest.write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2), encoding='utf-8')
    response = support.api('/capital/connect', dict(user_id=values['CAPITAL_USER_ID'],
        password=values['CAPITAL_PASSWORD'], authority=int(values.get('CAPITAL_AUTHORITY') or 0)), timeout=55)
    health = support.api('/health')
    support.require(health.get('quote_ready') and health['native_worker'].get('quote_backend') == 'com',
                    'COM did not become ready; work remains paused')
    response = support.api('/backfill/resume', {})
    report.update(resume_requested=True, resume_message=response.get('message'), samples=[])
    manifest.write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2), encoding='utf-8')
    for index in range(12):
        health, state = live_state()
        point = dict(at=datetime.now(TAIPEI).isoformat(), state=state.get('state'),
            stage=state.get('stage'), worker_active=state.get('worker_active'),
            pause_requested=state.get('pause_requested'), quote_ready=health.get('quote_ready'),
            symbol=state.get('current_symbol'), market=state.get('current_market'), interval=state.get('current_interval'),
            windows=state.get('windows'), processed=state.get('processed'),
            integrity_active=state.get('day_integrity_active'), integrity_scheduled=state.get('day_integrity_scheduled'),
            window_end=state.get('window_end'), native_pid=health['native_worker'].get('pid'),
            native_private_mb=health['native_worker'].get('private_mb'),
            pause_reason=state.get('pause_reason'))
        report['samples'].append(point)
        manifest.write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2), encoding='utf-8')
        print(json.dumps(point, ensure_ascii=False, default=str), flush=True)
        support.require(not state.get('day_integrity_active'), 'Integrity unexpectedly active; check before proceeding')
        if health.get('quote_ready') and not state.get('pause_requested') and state.get('worker_active') and (
                int(state.get('windows') or 0) > 0 and state.get('state') == 'running'):
            report.update(collection_running=True, verified_at=datetime.now(TAIPEI).isoformat())
            manifest.write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2), encoding='utf-8')
            print(json.dumps(dict(phase='resumed', api_pid=args.api_pid, native_pid=point['native_pid'],
                integrity_enabled=False, cutoff=report['cutoff'], checkpoints_preserved=True,
                manifest=str(manifest)), ensure_ascii=False), flush=True)
            return 0
        if index < 11:
            time.sleep(5)
    raise RuntimeError('Resume accepted, but active committed collection not yet observed; inspect saved report')


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps(dict(error_type=type(error).__name__, error=support.redact(str(error))),
                         ensure_ascii=False), flush=True)
        raise SystemExit(1)
