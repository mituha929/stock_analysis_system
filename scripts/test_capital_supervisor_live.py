"""Opt-in acceptance test of the PRODUCTION supervisor at a 400 MiB trigger.

Reuses the guarded API handoff and isolated-schema fixture. Unlike the earlier
prototype, one coordinator survives both real native-worker memory rotations;
the actual runner's post-COMMIT hook performs the recycling automatically.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path

import test_capital_worker_recycle_live as fixture

PROJECT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-live-handoff',action='store_true')
    parser.add_argument('--api-pid',type=int,required=True)
    parser.add_argument('--start',default='2026-10-12')
    parser.add_argument('--end',default='2026-10-13')
    args=parser.parse_args()
    values=fixture.load_config()
    from app.services.backfill_control import BackfillControl
    from app.services.backfill_engine import BackfillRunner
    from app.services.capital_worker_service import CapitalWorkerService,NativeWorkerConfig
    from app.services.capital_worker_runtime import MemoryWatchlistStore
    from app.services.incremental_candle_repository import MySQLCandleRepository
    from app.services.mysql_instrument_repository import MySQLSettings
    from app.services.quote_cache import QuoteCache
    from measure_native_worker_recycling import memory

    service=None
    coordinator_points=[]
    observations=[]
    def observe(_args,schema,run_dir,number,kernel,psapi,api_handle,audit_events):
        nonlocal service
        control=BackfillControl(run_dir/'control.sqlite3')
        calls,commits=[],[]
        if service is None:
            # Scope the smaller history window to this TEST and its children.
            os.environ['CAPITAL_KLINE_MAX_DAYS_1M']='1'
            os.environ['CAPITAL_SUSPENSION_SYNC_ENABLED']='false'
            class ObservedSupervisor(CapitalWorkerService):
                def fetch_historical_candles(self,symbol,interval,start,end,**options):
                    calls.append(dict(symbol=symbol,interval=interval,start=start,end=end,worker_pid=self._process.pid))
                    return super().fetch_historical_candles(symbol,interval,start,end,**options)
            # The closure for subsequent rounds is installed below.
            service=ObservedSupervisor(QuoteCache(),control=control,
                watchlist_store=MemoryWatchlistStore({'2330':1}),
                config=replace(NativeWorkerConfig.from_env(),recycle_mb=400))
            service.connect(values['CAPITAL_USER_ID'],values['CAPITAL_PASSWORD'],int(values.get('CAPITAL_AUTHORITY') or 0))
        else:
            # ObservedSupervisor's closure must point at the current call log.
            calls=observations[0]['calls_ref']
            calls.clear()
        old_process=service._process
        before_count=service.worker_snapshot()['recycle_count']
        accepted=service.is_subscribed('2330')
        coordinator_points.append(memory(kernel,psapi))
        class Repository(MySQLCandleRepository):
            def commit_window(self,*items,**options):
                fixture.require(service._process is old_process and old_process.is_alive(),
                                'Native worker exited before MySQL COMMIT')
                before=service.worker_snapshot()
                fixture.require(before['pending_windows']==1 and before['recycle_due'],
                                'Real native allocation did not arm the test memory trigger')
                count=super().commit_window(*items,**options)
                commits.append(dict(next_date=items[2],rows=len(items[1]),stored_count=count,
                                    native_before_commit=before))
                if number==1:
                    control.pause(True)
                return count
        repo=Repository(replace(MySQLSettings.from_env(),database=schema))
        control.pause(False)
        result=BackfillRunner(service,repo,control).run(control.saved_plans())
        after=service.worker_snapshot()
        fixture.require(len(commits)==len(calls)==1,'Expected one real window per checkpoint round')
        fixture.require(after['recycle_count']==before_count+1 and not old_process.is_alive()
                        and old_process.exitcode==0 and service._process.pid!=old_process.pid,
                        'Production supervisor did not automatically rotate after COMMIT')
        fixture.require(after['forced_exits']==0 and service.quote_ready,'Rotation did not restore a healthy login')
        point=dict(event='child_result',pid=old_process.pid,round=number,result=result,
                   calls=json.loads(json.dumps(calls,default=str)),commits=json.loads(json.dumps(commits,default=str)),
                   database=fixture.test_rows(repo),subscription_accepted=accepted,
                   native_after_rotation=after,coordinator_pid=os.getpid(),coordinator_memory=memory(kernel,psapi))
        audit_events.append(point)
        observations.append(dict(calls_ref=calls,point=point))
        fixture.emit('production_supervisor_round_passed',**{k:v for k,v in point.items() if k!='event'})
        return dict(round=number,worker_pid=old_process.pid,worker_exit_code=old_process.exitcode,
                    worker_result=point,worker_before_exit=commits[0]['native_before_commit'],
                    native_after_rotation=after,api_after_exit=memory(kernel,psapi,api_handle))

    def cleanup(report):
        if service is not None:
            service.stop()
            report.update(production_supervisor_test=True,test_recycle_mb=400,
                          production_default_recycle_mb=3072,coordinator_pid=os.getpid(),
                          coordinator_points=coordinator_points,
                          production_supervisor_final=service.worker_snapshot(),
                          automatic_memory_trigger_verified=len(observations)==2,
                          source_data_isolated=True)
    try:
        return fixture.parent_main(args,observer=observe,before_restore=cleanup)
    finally:
        if service is not None:
            service.stop()


if __name__=='__main__':
    raise SystemExit(main())
