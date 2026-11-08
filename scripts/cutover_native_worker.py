"""Prepare/verify an explicitly approved API restart; never kill processes.

PowerShell performs the verified stop/start between these phases. Credentials
are read internally and used only in a local POST, never written to the report.
"""
from datetime import datetime,timezone
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import test_capital_worker_recycle_live as support

ROOT=Path(__file__).resolve().parents[1]


def digest(control):
    return hashlib.sha256(json.dumps(control.get('plans',[]),sort_keys=True,default=str).encode()).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase',choices=('prepare','activate'))
    parser.add_argument('--api-pid',type=int,required=True)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--no-resume',action='store_true',
                        help='Restore login but preserve idle/manual-pause state; do not queue a new backfill')
    args=parser.parse_args()
    manifest=args.manifest.resolve()
    support.require(manifest.parent==(ROOT/'output'/'diagnostics').resolve()
                    and manifest.name.startswith('native-cutover-') and manifest.suffix=='.json',
                    'Unexpected cutover manifest path')
    values=support.load_config()
    from app.services.backfill_control import BackfillControl
    from app.services.incremental_candle_repository import MySQLCandleRepository
    from measure_native_worker_recycling import probe_existing_dll_lock

    if args.phase=='prepare':
        support.require(not manifest.exists(),'Do not overwrite an existing cutover report')
        original=support.api('/backfill/control')
        health=support.api('/health')
        # worker_pid is the LAST backfill runner, not necessarily this server.
        # An idle API restarted since the last run legitimately has a new PID.
        live_api_pid=(health.get('native_worker') or {}).get('api_pid') or original.get('worker_pid')
        support.require(live_api_pid==args.api_pid,'Live API PID no longer matches')
        support.api('/backfill/pause',{})
        deadline=time.monotonic()+90
        while support.api('/backfill/control').get('worker_active'):
            support.require(time.monotonic()<deadline,'Still waiting for safe pause; do not stop API')
            time.sleep(1)
        control=BackfillControl()
        with control.worker():
            paused=control.snapshot()
            support.require(paused.get('pause_requested'),'Pause was cleared concurrently')
            checkpoint=support.production_checkpoint(MySQLCandleRepository(),paused)
            support.api('/capital/disconnect',{},timeout=50)
            support.require(not support.api('/health').get('quote_ready'),'API did not disconnect')
            support.require(probe_existing_dll_lock(support.dll_lock_path()).get('available') is True,
                            'DLL is still held; do not stop/restart another process')
            report=dict(old_api_pid=args.api_pid,prepared_at=datetime.now(timezone.utc).isoformat(),
                        before=support.brief(original),before_health=health,paused=support.brief(paused),
                        plans_sha256=digest(paused),plan_count=len(paused.get('plans',[])),
                        checkpoint=checkpoint,safe_to_stop=True)
            manifest.write_text(json.dumps(support.redact(report),ensure_ascii=False,default=str,indent=2),encoding='utf-8')
            support.emit('cutover_prepared',manifest=str(manifest),**report)
        return 0

    report=json.loads(manifest.read_text(encoding='utf-8'))
    control=support.api('/backfill/control')
    health=support.api('/health')
    native=health.get('native_worker') or {}
    support.require(report.get('safe_to_stop') and args.api_pid!=report['old_api_pid'],'Not a new approved API process')
    support.require(native.get('enabled') and native.get('api_pid')==args.api_pid,'New supervisor is not loaded')
    support.require(digest(control)==report['plans_sha256'],'Saved plan changed during restart')
    support.require(control.get('pause_requested'),'Work was unpaused before verification')
    support.api('/capital/connect',{'user_id':values['CAPITAL_USER_ID'],'password':values['CAPITAL_PASSWORD'],
                                   'authority':int(values.get('CAPITAL_AUTHORITY') or 0)},timeout=50)
    health=support.api('/health')
    native=health['native_worker']
    support.require(health.get('quote_ready') and native.get('pid') and native['pid']!=args.api_pid,
                    'New native worker is not ready')
    if args.no_resume:
        support.require(not report['before'].get('worker_active'),'Original work was active; do not silently leave it idle')
        with BackfillControl().worker():
            BackfillControl().pause(bool(report['before'].get('pause_requested')))
        response=dict(message='已恢復登入並保留重啟前的閒置／手動暫停狀態；未重新排入回補')
    else:
        response=support.api('/backfill/resume',{})
    samples=[]
    for index in range(3):
        state=support.api('/backfill/control')
        point=dict(control=support.brief(state),health=support.api('/health'))
        samples.append(point)
        support.emit('new_api_verification',sample=index+1,**point)
        if index<2: time.sleep(5)
    expected_pause=bool(report['before'].get('pause_requested')) if args.no_resume else False
    support.require(bool(samples[-1]['control'].get('pause_requested'))==expected_pause and
                    samples[-1]['health'].get('quote_ready'),'New API has not restored backfill readiness')
    report.update(new_api_pid=args.api_pid,activated_at=datetime.now(timezone.utc).isoformat(),
                  plans_preserved=True,resumed_backfill=not args.no_resume,
                  resume_message=response.get('message'),samples=samples)
    manifest.write_text(json.dumps(support.redact(report),ensure_ascii=False,default=str,indent=2),encoding='utf-8')
    support.emit('cutover_complete',manifest=str(manifest),new_api_pid=args.api_pid,
                 native_worker=samples[-1]['health']['native_worker'])
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        support.emit('cutover_failed',error=f'{type(error).__name__}: {error}')
        raise SystemExit(1)
