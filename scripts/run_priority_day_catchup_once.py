"""One-off, checkpoint-preserving priority pass for the 11 approved day jobs.

Uses the already-connected API's normal runner; never logs in, restarts an API,
replans a market, changes a cursor directly, or deletes data. Default: inspect.
"""
import argparse
from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
import time
import uuid

import test_capital_worker_recycle_live as support

ROOT = Path(__file__).resolve().parents[1]
JOB_IDS = (618485, 618486, 620027, 620073, 620501, 620634,
           620858, 620873, 621109, 621137, 621296)
AS_OF = date(2026, 10, 20)


def digest(plans):
    return hashlib.sha256(json.dumps(plans, sort_keys=True, default=str).encode()).hexdigest()


def jobs():
    from app.services.incremental_candle_repository import MySQLCandleRepository
    repo = MySQLCandleRepository()
    connection = repo._connect_database(repo._connector())
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("""SELECT j.id,j.market_no,j.symbol,i.name,j.status,j.interval_code,
            j.end_date,j.received_count,j.attempt_count,j.last_error,p.mode,p.target_bars,
            p.next_date,p.owner_token,w.phase,w.not_before
            FROM candle_backfill_jobs j
            JOIN candle_backfill_progress p ON p.job_table='candle_backfill_jobs' AND p.job_id=j.id
            JOIN candle_batch_work w ON w.job_table=p.job_table AND w.job_id=p.job_id
            JOIN instruments i ON i.market_no=j.market_no AND i.symbol=j.symbol
            WHERE j.id IN (""" + ",".join(["%s"] * len(JOB_IDS)) + ") ORDER BY j.market_no,j.id", JOB_IDS)
        return cursor.fetchall()
    finally:
        cursor.close()
        connection.close()


def wait_stopped(timeout=120):
    deadline = time.monotonic() + timeout
    while True:
        state = support.api('/backfill/control')
        if not state.get('worker_active'):
            return state
        support.require(time.monotonic() < deadline, 'Waiting for safe boundary timed out; leave API paused')
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--symbols', nargs='+', help='Optional subset of the 11 approved job symbols')
    parser.add_argument('--rounds', type=int, choices=(1, 2, 3), default=1,
                        help='Bounded manual retry passes, preserving every committed cursor')
    args = parser.parse_args()
    support.load_config()
    from app.services.backfill_control import BackfillControl
    before_jobs = jobs()
    support.require(len(before_jobs) == len(JOB_IDS), 'Approved job identities changed')
    requested = set(args.symbols) if args.symbols else {row['symbol'] for row in before_jobs}
    support.require(requested and requested <= {row['symbol'] for row in before_jobs}, 'Unknown symbol outside approved jobs')
    pending = [row for row in before_jobs if row['status'] == 'pending' and row['symbol'] in requested]
    support.require(all(row['market_no'] in (0, 1) and row['interval_code'] == 'day'
                        and row['end_date'] == AS_OF and row['phase'] == 'catchup'
                        and row['mode'] == 'refresh' and row['target_bars'] == 10
                        and row['owner_token'] is None for row in pending), 'Pending job scope changed')
    initial = support.api('/backfill/control')
    support.emit('priority_inspection', jobs=before_jobs, control=support.brief(initial))
    if not args.apply or not pending:
        return 0
    support.require(date.today() == AS_OF, 'This one-off operation is limited to the approved day')
    support.require(not initial.get('pause_requested') and initial.get('worker_active'),
                    'Original automatic work is not active; do not clear a user pause')
    support.require(support.api('/health').get('quote_ready'), 'Current API is not connected')
    token = uuid.uuid4().hex
    report_path = ROOT / 'output' / 'diagnostics' / f'priority-day-catchup-{token}.json'
    report = dict(token=token, before_jobs=before_jobs, original_plans=None)

    def save():
        report_path.write_text(json.dumps(support.redact(report), ensure_ascii=False,
                                         default=str, indent=2), encoding='utf-8')

    control = BackfillControl()
    original = scoped = None
    scoped_changed = False
    finished = False
    try:
        support.api('/backfill/pause', {})
        wait_stopped()
        with control.worker():
            original = control.snapshot()
            support.require(original.get('pause_requested'), 'Safe pause was cleared concurrently')
            support.require(not original.get('pending_schedule'), 'A new daily schedule was queued')
            scoped = []
            for market in (0, 1):
                selected = [row for row in pending if row['market_no'] == market]
                if not selected:
                    continue
                candidates = [plan for plan in original.get('plans', [])
                              if plan['market_no'] == market and plan['interval'] == 'day']
                support.require(len(candidates) == 1, 'Original daily plan is ambiguous')
                plan = deepcopy(candidates[0])
                support.require(plan.get('batch_v2') and plan.get('planner_version') == 3
                                and plan.get('planned_on') == AS_OF.isoformat()
                                and plan.get('end_date') == AS_OF.isoformat(), 'Do not replan original daily work')
                plan.pop('run_id', None)  # A small priority pass must not finish the full daily-run log.
                plan.update(only_symbols=[row['symbol'] for row in selected],
                            allowed_ids=[row['id'] for row in selected], planned_jobs=len(selected))
                scoped.append(plan)
            report.update(original_plans=original['plans'], original_control=support.brief(original),
                          plans_sha256=digest(original['plans']), scoped_plans=scoped)
            save()  # Durable recovery copy BEFORE publishing the temporary scope.
            control.update(plans=scoped, state='paused', stage='priority_catchup_prepared',
                           resume_pending=False, next_retry_at=None, priority_catchup_token=token)
            scoped_changed = True
        for pass_number in range(1, args.rounds + 1):
            response = support.api('/backfill/resume', {})
            support.emit('priority_started', pass_number=pass_number, message=response.get('message'), report=str(report_path))
            deadline = time.monotonic() + 180
            while True:
                state = support.api('/backfill/control')
                if not state.get('worker_active') and not state.get('resume_pending') and state.get('state') not in {'running', 'queued', 'planning'}:
                    report['priority_result'] = support.brief(state)
                    finished = True
                    break
                support.require(time.monotonic() < deadline, 'Priority pass timed out; request safe pause, never kill')
                time.sleep(1)
            remaining = [row for row in jobs() if row['symbol'] in requested and row['status'] == 'pending']
            report.setdefault('passes', []).append(dict(pass_number=pass_number, remaining=remaining))
            save()
            if (not remaining or state.get('pause_requested')
                    or state.get('pause_kind') in {'memory', 'data', 'database', 'connection'}):
                break
    finally:
        if scoped_changed:
            current = support.api('/backfill/control')
            user_paused = bool(current.get('pause_requested'))
            support.api('/backfill/pause', {})
            wait_stopped()
            with control.worker():
                stopped = control.snapshot()
                support.require(stopped.get('priority_catchup_token') == token
                                and digest(stopped.get('plans', [])) == digest(scoped),
                                'Plan ownership changed; do not overwrite another operation')
                report['after_jobs'] = jobs()
                report['priority_stopped'] = support.brief(stopped)
                control.update(plans=original['plans'], state='paused', stage='priority_catchup_finished',
                               resume_pending=False, priority_catchup_token=None,
                               queued_jobs=original.get('queued_jobs'),
                               pause_requested=True, next_retry_at=None)
                report['plans_preserved'] = digest(control.snapshot()['plans']) == report['plans_sha256']
            save()
            can_resume = finished and not user_paused and stopped.get('pause_kind') not in {'memory', 'data', 'database', 'connection'}
            if can_resume:
                support.api('/backfill/resume', {})
                report['original_work_resumed'] = True
            else:
                report['original_work_resumed'] = False
            save()
            support.emit('priority_finished', jobs=report['after_jobs'], plans_preserved=report['plans_preserved'],
                         original_work_resumed=report['original_work_resumed'], report=str(report_path))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        support.emit('priority_failed', error=f'{type(error).__name__}: {error}')
        raise SystemExit(1)
