"""The same bounded-memory, checkpointed worker for API and CLI."""
from __future__ import annotations

from contextlib import nullcontext

import json
import logging
import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone

from app.services.backfill_control import BackfillControl
from app.services.backfill_policy import completed_end, history_window, refresh_start, complete_aggregate
from app.services.daily_candle_aggregation import (
    DailyAggregatePause, aggregate_daily, daily_aggregate_end, normalized_daily,
)
from app.services.backfill_targets import refresh_plan_targets, prioritize_day
from app.services.trading_suspensions import load_suspensions
from app.services.capital_history_connection import is_history_connection_error
from app.services.candle_validation import find_candle_data_error, validate_candle_volumes, DATA_FAILURE_LIMIT
from app.services.capital_history_service import max_history_days
from app.services.history_exclusions import allowed_history_ranges, excluded_label, exclusion_info
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.mysql_instrument_repository import MySQLRepositoryError
from app.services.process_memory import current_process_memory, memory_limit_mb
from app.services.backfill_memory import BackfillMemoryGuard, mark_memory_pause, memory_resume_ready
from app.services.day_candle_integrity import integrity_enabled, integrity_schedule, expanded_day_window
from app.services.listing_history import listing_floor
from app.services.taiwan_trading_calendar import query_trading_bounds, TaiwanTradingCalendarError
from app.services.empty_history_review import empty_review_days, calendar_period_end
from app.services.instrument_lifecycle import blocked_reason, active_bounds, today as lifecycle_today

logger = logging.getLogger(__name__)


class BackfillRunner:
    def __init__(self, capital, repository=None, control=None, source="api", progress=None,
                 max_jobs=None, on_job=None):
        self.capital = capital
        self.repository = repository or MySQLCandleRepository()
        self.control = control or BackfillControl()
        self.source, self.progress = source, progress
        self.max_jobs, self.on_job = max_jobs, on_job
        self.owner = str(uuid.uuid4())
        self.result = {"processed":0,"completed":0,"insufficient_history":0,"failed":0}
        self.windows = 0
        self.warning_mb = memory_limit_mb("API_MEMORY_WARNING_MB", 2048)
        self.stop_mb = memory_limit_mb("API_MEMORY_STOP_BACKFILL_MB", 4096)
        self.empty_limit = memory_limit_mb("CAPITAL_BACKFILL_EMPTY_WINDOW_LIMIT", 3)
        self.job_window_budget = memory_limit_mb("CAPITAL_BACKFILL_JOB_WINDOW_BUDGET", 3)
        self.recovery_budget = memory_limit_mb("CAPITAL_BACKFILL_RECOVERY_WINDOW_BUDGET", 100)
        self.retry_seconds = memory_limit_mb("CAPITAL_BACKFILL_RETRY_SECONDS", 900)
        self.suspensions = None
        self.suspension_day = None
        self.recovery_windows = 0
        self.latest_jobs = 0
        self.admitted = set()
        self.queued_jobs = 0
        self.user_resume = False
        self.memory_guard = BackfillMemoryGuard(self.control, getattr(capital,"cache",None),
                                                provider=lambda: current_process_memory())
        self.memory_stage = "worker_start"
        self.listing_policies = {}
        empty_review_days()  # Reject invalid policy before any vendor request.

    def review_count(self, plans):
        if getattr(type(self.repository), 'empty_review_count', None) is None:
            return 0
        return self.repository.empty_review_count(plans)

    def review_summary(self, plans):
        if getattr(type(self.repository), 'empty_review_summary', None) is not None:
            return self.repository.empty_review_summary(plans)
        # Legacy/test adapters may know only the number of records. Never
        # invent a product/date count by treating records as securities.
        amount = self.review_count(plans)
        return dict(records=amount, symbols=None if amount else 0,
                    dates=None if amount else 0, periods=None if amount else 0)

    def history_listing_policy(self, table, market, symbol):
        """A bounded per-run snapshot; also used by old plans and gap repairs."""
        if getattr(type(self.repository), "load_listing_policies", None) is None:
            return None
        key = (table, market, lifecycle_today())
        self.listing_policies = {k:v for k,v in self.listing_policies.items() if len(k) == 3 and k[2] == key[2]}
        if key not in self.listing_policies:
            self.listing_policies[key] = self.repository.load_listing_policies(table, market)
        return self.listing_policies[key].get(symbol)

    def prepare_group(self, plan):
        """Called at actual group entry, under the exclusive worker lock."""
        if not plan.get("batch_v2"):
            return
        today = date.today()
        session = (today,datetime.now().hour*60+datetime.now().minute>=14*60+30)
        if self.suspension_day not in (today,session):
            self.suspensions = load_suspensions()
            self.suspension_day = session
            self.control.update(suspensions=self.suspensions.status)
        # Preserve same-day committed checkpoints on resume. On a later day,
        # re-read actual data, keeping all unfinished history as audit records.
        derived = bool(plan.get("aggregate_from_day"))
        end = daily_aggregate_end(plan.get("as_of_date")) if derived else completed_end(plan["interval"],plan.get("as_of_date"))
        version = 4 if derived else 3
        if (plan.get("planner_version") == version and (plan.get("planned_on") == today.isoformat() or plan.get("fixed_range"))
                and plan.get("end_date") == end):
            self.control.update(group_plan=plan.get("summary"),current_market=plan["market_no"],current_interval=plan["interval"])
            return
        self.publish(stage="planning_group",current_symbol=None,current_market=plan["market_no"],current_interval=plan["interval"])
        summary = self.repository.plan_batch(plan["market_no"],plan["interval"],end,plan["target_bars"],
            plan["candle_table"],plan["job_table"],plan["instrument_table"],
            force=plan.get("force",False),explicit_start=plan.get("explicit_start"),
            only_symbols=plan.get("only_symbols"),suspensions=self.suspensions,recover_running=True,
            **({"refresh_bars":plan["refresh_bars"]} if plan.get("refresh_bars") is not None else {}),
            **({"aggregate_from_day":True} if derived else {}),
            **({"fresh_daily":True} if plan.get("fresh_daily") else {}),
            **({"force_completed_refresh":True} if plan.get("force_completed_refresh") else {}))
        plan.update(start_date=summary["start_date"],end_date=end,planned_on=today.isoformat(),planner_version=version,planned_jobs=summary["queued_jobs"],summary=summary)
        if "fresh_job_ids" in summary:
            plan["allowed_ids"] = summary["fresh_job_ids"]
        self.queued_jobs = sum(int(p.get("planned_jobs",0)) for p in self.plans)
        # Force is a one-time command, not permission to erase checkpoints
        # again after a crash or at the next retry.
        plan["force"] = False
        plan.pop("fresh_daily", None)
        self.control.update(plans=self.plans,group_plan=summary,queued_jobs=self.queued_jobs,
                            current_market=plan["market_no"],current_interval=plan["interval"])
        logger.info("族群批次規劃 %s/%s：%s",plan["market_no"],plan["interval"],summary)

    def run_group(self, plan, phase):
        # Even a same-day saved plan may contain prelisting waits or terminal
        # bonds. Reconcile BEFORE claim/backoff, not only after getting a job.
        self.history_listing_policy(plan['candle_table'], plan['market_no'], '')
        scoped = dict(plan,queue_phase=phase,ignore_backoff=self.user_resume)
        while self.max_jobs is None or self.result["processed"] < self.max_jobs:
            if plan.get("batch_v2") and self.max_jobs is not None and len(self.admitted) >= self.max_jobs:
                if phase == "latest": break
                scoped["allowed_ids"] = [job_id for table,job_id in self.admitted if table==plan["job_table"]]
            if phase != "latest" and self.recovery_windows >= self.recovery_budget:
                break
            if self.control.snapshot().get("pause_requested"):
                return self.pause("manual","已在查詢區間邊界安全暫停")
            if self.stage_check("before_claim"):
                return self.pause("memory",f"程序記憶體達 {self.stop_mb} MB 保護門檻")
            job = self.repository.claim_incremental_job(scoped,self.owner)
            if job is None: break
            self.admitted.add((job["job_table"],job["id"]))
            stopped = self.run_job(job)
            if stopped is not None: return stopped
        return None

    def publish(self, **values):
        values.setdefault("queued_jobs",self.queued_jobs)
        self.control.update(**{**self.result, **values})
        if self.progress is not None:
            self.progress(dict(self.result, **values))

    def pause(self, kind, reason, job=None):
        if job is not None:
            try:
                self.repository.release_incremental_job(job, reason)
            except MySQLRepositoryError:
                # Its persisted cursor is still authoritative. The exclusive
                # worker lock allows safe recovery of this running row later.
                logger.exception("資料庫仍無法連線，工作將於恢復後由已提交斷點接續")
        self.result.update(paused=True,pause_kind=kind,pause_reason=reason)
        if kind == "memory":
            mark_memory_pause(self.control)
        self.control.update(state="paused",**self.result)
        logger.warning("回補已暫停（%s）：%s",kind,reason)
        return dict(self.result)

    def memory_check(self):
        return self.memory_guard.check(self.memory_stage,self.windows)

    def stage_check(self, stage):
        self.memory_stage = stage
        return self.memory_check()

    def limit_reached(self):
        return self.max_jobs is not None and (
            self.result["processed"] >= self.max_jobs or len(self.admitted) >= self.max_jobs)

    def run_plan_passes(self, plans, phases=("latest", "catchup", "history")):
        """Explicit phases let ALL normal intervals precede any slow recovery."""
        if 'latest' in phases:
            for plan in plans:
                if self.control.snapshot().get("pause_requested"):
                    return self.pause("manual","已保存目前區間；停止準備後續族群")
                if self.stage_check("before_group_planning"):
                    return self.pause("memory",f"程序記憶體達 {self.stop_mb} MB 保護門檻")
                self.prepare_group(plan)
                if self.stage_check("after_group_planning"):
                    return self.pause("memory",f"程序記憶體達 {self.stop_mb} MB 保護門檻")
                self.repository.recover_incremental_jobs(plan)
                stopped = self.run_group(plan,"latest")
                if stopped is not None: return stopped
                if self.limit_reached(): break
        for phase in (p for p in phases if p != 'latest'):
            for plan in plans:
                if not plan.get("batch_v2") or not plan.get("planned_on"): continue
                stopped = self.run_group(plan,phase)
                if stopped is not None: return stopped
        return None

    def wait_for_pending(self, pending, retry_at, *, day_pending=0, audit=None):
        self.result["deferred"] = pending
        earliest = datetime.now(timezone.utc).replace(tzinfo=None)+timedelta(seconds=self.retry_seconds)
        reason = f"所選週期正常抓取輪已結束；{pending} 筆待補工作保留斷點，等待下輪補齊"
        if audit:
            summary = audit['summary']
            reason += (f"；日 K 待查 {summary.get('unresolved_symbols', summary['blocked_symbols'])} 檔"
                       f"／{summary['unresolved_dates']} 個日期")
        self.control.update(state="waiting_backfill",next_retry_at=(retry_at or earliest).replace(tzinfo=timezone.utc).isoformat(),
                            day_pending=day_pending,priority_phase='recovery',pause_reason=reason,**self.result)
        return dict(self.result,pause_reason=reason,day_first_waiting=False,day_pending=day_pending)

    def run(self, plans, target_bars=1200, *, owns_worker_lock=False):
        plans = prioritize_day(plans, key=lambda plan: plan["interval"])
        self.plans = plans
        self.queued_jobs = sum(int(p.get("planned_jobs",0)) for p in plans)
        # Only an orchestrator already holding the OS worker lease may opt out.
        # All ordinary callers retain the exclusive cross-process guard.
        with nullcontext() if owns_worker_lock else self.control.worker():
            previous = self.control.snapshot()
            if self.source == "api" and not memory_resume_ready(self.control,memory=current_process_memory()):
                # Includes a new refresh-stale request, not just /resume.
                # Do NOT replace saved scope or reset the cooldown on a poll.
                self.result.update(paused=True,pause_kind="memory",pause_reason="記憶體恢復條件尚未達成；保留原計畫與斷點")
                return dict(self.result)
            self.user_resume = bool(previous.get("resume_pending"))
            if (previous.get("state") in {"running","paused","waiting_connection","waiting_database","queued","planning","waiting_trading_day","waiting_backfill"}
                    and json.dumps(prioritize_day(previous.get("plans") or [],key=lambda plan:plan["interval"]),sort_keys=True,default=str) == json.dumps(plans,sort_keys=True,default=str)):
                for key in ("processed","completed","insufficient_history","failed"):
                    self.result[key] = int(previous.get(key,0))
            refresh_plan_targets(plans)
            if getattr(type(self.repository), "ensure_data_failure_storage", None) is not None:
                self.repository.ensure_data_failure_storage()
            if getattr(type(self.repository), 'ensure_empty_review_storage', None) is not None:
                self.repository.ensure_empty_review_storage()
            if getattr(type(self.repository), 'refresh_lifecycle', None) is not None:
                self.repository.refresh_lifecycle()
            self.control.update(source=self.source,state="running",plans=plans,target_bars=target_bars,
                                day_integrity_active=False,
                                pending_review=0,empty_review=None,
                                pending_review_symbols=None,pending_review_dates=None,pending_review_periods=None,
                                normal_round_complete=False,
                                audit_pending_symbols=0,audit_pending_dates=0,
                                worker_pid=os.getpid(),windows=0,pause_reason=None,
                                pause_kind=None,paused=False,resume_pending=False,started_at=time.time(),
                                next_retry_at=None,deferred=0,queued_jobs=self.queued_jobs,**self.result)
            try:
                day_plans = [plan for plan in plans if plan["interval"] == "day"]
                other_plans = [plan for plan in plans if plan["interval"] != "day"]
                self.control.update(day_first=bool(day_plans),day_pending=0)
                if day_plans:
                    self.control.update(priority_phase="day")
                    stopped = self.run_plan_passes(day_plans, ('latest',))
                    if stopped is not None: return stopped
                if other_plans and not self.limit_reached():
                    self.control.update(priority_phase="other")
                    stopped = self.run_plan_passes(other_plans, ('latest',))
                    if stopped is not None: return stopped
                if not self.limit_reached():
                    self.control.update(normal_round_complete=True,priority_phase='recovery')
                    # A scheduled priority round must hand control back as
                    # soon as its latest pass finishes. Both its own slow
                    # jobs and the displaced scope remain durable for retry.
                    priority = self.control.snapshot().get("scheduled_daily_priority") or {}
                    if priority.get("phase") != "running":
                        stopped = self.run_plan_passes(plans, ('catchup','history'))
                        if stopped is not None: return stopped
                audit = None
                if day_plans:
                    # Daily refresh counts are not proof of historical date
                    # completeness. Heavy checks are deferred to closures;
                    # old audit failures must not gate trading-day collection.
                    if all(plan.get("skip_day_integrity") for plan in day_plans):
                        audit_due, audit_reason = False, "手動每日更新不執行歷史完整性驗收"
                    else:
                        audit_due, audit_reason = integrity_schedule()
                    self.control.update(day_integrity_scheduled=audit_due,
                                        day_integrity_schedule_reason=audit_reason,
                                        day_integrity_active=False)
                    if (not self.limit_reached() and audit_due
                            and getattr(type(self.repository), 'ensure_day_integrity_storage', None) is not None):
                        from app.services.day_candle_integrity_runner import DayIntegrityAudit
                        try:
                            audit = DayIntegrityAudit(self, allowed=lambda: integrity_schedule()[0]).run(day_plans)
                        except MySQLRepositoryError:
                            raise
                        except Exception as error:
                            if is_history_connection_error(error):
                                return self.pause('connection', str(error))
                            return self.pause('day_integrity', f'日 K 驗收未完成：{error}')
                        if audit.get('stopped') is not None:
                            return audit['stopped']
                        if audit.get('schedule_deferred'):
                            self.control.update(day_integrity_active=False, day_integrity_scheduled=False,
                                                day_integrity_schedule_reason='已進入交易日；缺口保留至下次休市日驗收')
                            audit = None  # Its old snapshot is not a fresh result.
                reviews = self.review_summary(plans)
                if audit and audit.get('blocked'):
                    self.result.update(audit_pending_symbols=audit['blocked'],
                                       audit_pending_dates=audit['summary']['unresolved_dates'])
                if reviews['records']:
                    self.result.update(pending_review=reviews['records'],pending_review_symbols=reviews['symbols'],
                                       pending_review_dates=reviews['dates'],pending_review_periods=reviews['periods'])
                self.control.update(pending_review=reviews['records'],pending_review_symbols=reviews['symbols'],
                                    pending_review_dates=reviews['dates'],pending_review_periods=reviews['periods'])
                if self.limit_reached():
                    self.result["limited"] = True
                    self.control.update(state="limited",**self.result)
                else:
                    pending, retry_at = self.repository.batch_pending(plans) if any(p.get("batch_v2") for p in plans) else (0,None)
                    day_pending, _ = self.repository.batch_pending(day_plans) if any(p.get('batch_v2') for p in day_plans) else (0,None)
                    self.result["deferred"] = pending
                    if pending or audit and audit.get('blocked'):
                        if audit and audit.get('blocked'):
                            retry_at = min(retry_at,audit['retry_at']) if retry_at else audit['retry_at']
                        return self.wait_for_pending(pending,retry_at,day_pending=day_pending,
                                                     audit=audit if audit and audit.get('blocked') else None)
                    else:
                        self.control.update(state='waiting_review' if reviews['records'] else "error" if self.result["failed"] else "completed",
                                            priority_phase="finished",day_pending=0,next_retry_at=None,
                                            pause_reason='本輪正常抓取已結束；空資料保留待查，不自動重抓待查區間' if reviews['records'] else None,
                                            **self.result)
                return dict(self.result)
            except MySQLRepositoryError as error:
                return self.pause("database",str(error))

    def fetch_daily_aggregate_window(self, job, first, last):
        """Refresh the ENTIRE period range, never just the day-refresh tail.

        Each native daily response is committed before its pin is released.
        Aggregate OHLCV/cursor are committed separately only after all source
        chunks succeeded. No day/month partial sum is written on interruption.
        """
        daily = []
        boundary = (job.get("listing_policy") or {}).get("history_start")
        # Keep the week/month LABEL at the period start, but do not request
        # source daily bars before this security's confirmed listing boundary.
        cursor = max(first, boundary) if boundary else first
        maximum = max_history_days("day")
        while cursor <= last:
            if blocked_reason(job.get('listing_policy')):
                raise DailyAggregatePause('lifecycle', '商品已到期或終止交易，停止新的來源查詢；已保存行情保留')
            if self.control.snapshot().get("pause_requested"):
                raise DailyAggregatePause("manual", "來源日 K 已保存；週／月區間尚未提交，保留原斷點")
            if self.stage_check("before_daily_aggregate_source"):
                raise DailyAggregatePause("memory", "記憶體保護；週／月區間保留原斷點")
            end = min(last, cursor + timedelta(days=maximum - 1))
            self.publish(stage="requesting_daily_source",source_interval="day",
                         source_window_start=cursor,source_window_end=end)
            bounds = query_trading_bounds('day', cursor, end)
            if bounds is None:
                cursor = end + timedelta(days=1)
                continue
            rows = self.capital.fetch_historical_candles(job["symbol"],"day",*bounds,cache_result=False)
            committed = False
            try:
                stop_after_save = self.stage_check("after_daily_source_before_write")
                rows = normalized_daily(rows, cursor, end)
                self.repository.commit_daily_source(job, rows)
                committed = True
                daily.extend(rows)
            finally:
                rows.clear()
                if getattr(type(self.capital), "history_checkpoint", None) is not None:
                    self.capital.history_checkpoint(committed)
            self.windows += 1
            self.publish(stage="daily_source_saved",windows=self.windows)
            if stop_after_save or self.stage_check("after_daily_source_release"):
                raise DailyAggregatePause("memory", "來源日 K 已保存；週／月彙整尚未提交，保留原斷點")
            cursor = end + timedelta(days=1)
        if self.control.snapshot().get("pause_requested"):
            raise DailyAggregatePause("manual", "來源日 K 已保存；週／月彙整尚未提交，保留原斷點")
        self.publish(stage="aggregating_daily_source")
        return aggregate_daily(daily, job["interval_code"], first, last)

    def run_job(self, job):
        begin = time.monotonic()
        start = end = job["next_date"]
        try:
            count = self.repository.stored_count(job)
            cursor = job["next_date"]
            batch = bool(job.get("phase"))
            derived = job["mode"] == "day_aggregate"
            refresh = job["mode"] in {"refresh", "day_aggregate"}
            backward = refresh or (job.get("phase") == "history" if batch else job["mode"] == "initialization")
            lower = job["history_floor"] if backward and batch else job["start_date"]
            policy = self.history_listing_policy(job["candle_table"], job["market_no"], job["symbol"])
            job["listing_policy"] = policy
            stop_reason = blocked_reason(policy) or ('not_listed_as_of' if (policy or {}).get('history_start')
                and policy['history_start'] > job['end_date'] else None)
            if stop_reason:
                self.repository.stop_lifecycle_job(job, stop_reason)
                self.result['processed'] += 1
                self.publish(stage='lifecycle_stopped', lifecycle_reason=stop_reason, listing_policy=policy,
                             current_symbol=job['symbol'], current_market=job['market_no'], current_interval=job['interval_code'])
                return None
            job["listing_preserve_before"] = policy["listing_date"] if policy else None
            boundary = listing_floor(policy, job["interval_code"])
            if boundary:
                lower = max(lower, boundary)
                if not backward:
                    cursor = max(cursor, lower)
            job["listing_boundary_reached"] = False
            exhausted = False
            job_windows = 0
            while (cursor >= lower if backward else cursor <= job["end_date"]):
                stop_reason = blocked_reason(policy)
                if stop_reason:
                    self.repository.stop_lifecycle_job(job, stop_reason)
                    self.result['processed'] += 1
                    self.publish(stage='lifecycle_stopped', lifecycle_reason=stop_reason)
                    return None
                if backward and count >= job["target_bars"]:
                    break
                if batch and (job_windows >= self.job_window_budget or
                              (job["phase"] != "latest" and self.recovery_windows >= self.recovery_budget)):
                    self.repository.defer_batch_job(job,"達本輪區間查詢上限，保留斷點等待慢速補齊",self.retry_seconds)
                    self.publish(stage="deferred",checkpoint=cursor)
                    return None
                if self.control.snapshot().get("pause_requested"):
                    return self.pause("manual","已保存當前查詢區間進度",job)
                if self.stage_check("before_request"):
                    return self.pause("memory",f"程序記憶體達 {self.stop_mb} MB 保護門檻",job)
                maximum = max_history_days(job["interval_code"])
                query_lower = max(lower, refresh_start(job["interval_code"], cursor, job["target_bars"]-count)) if refresh else lower
                start, end = history_window(job["interval_code"], query_lower,
                                            job["end_date"], cursor, backward, maximum)
                if backward and job['interval_code'] == 'day' and integrity_enabled():
                    # Empty replies grow the next *disjoint* date interval;
                    # already counted refresh rows are never counted twice.
                    expanded_start, _ = expanded_day_window(cursor, lower, job['empty_windows'], maximum)
                    start = min(start, expanded_start)
                exclusions = [] if derived else exclusion_info(job["symbol"],job["interval_code"],start,end)
                exclusion_only = bool(exclusions) and not allowed_history_ranges(job["symbol"],job["interval_code"],start,end)
                self.publish(current_symbol=job["symbol"],current_market=job["market_no"],
                             day_integrity_active=False,
                             current_interval=job["interval_code"],mode=job["mode"],
                             queue_phase=job.get("phase"),
                             current_requery=bool(job.get("fixed_range")),
                             current_target_bars=job["target_bars"],
                             current_refresh=refresh,
                             current_aggregate_from_day=derived,
                             listing_policy=policy,listing_boundary_reached=False,listing_history_shortfall=False,
                             history_exclusions=exclusions,
                             window_start=start,window_end=end,checkpoint=job["next_date"],
                             windows=self.windows,stage="requesting")
                skip = self.suspensions.full_window(job["market_no"],job["symbol"],start,end) if batch and self.suspensions else None
                bounds = query_trading_bounds(job['interval_code'], start, end)
                # Week/month labels stay at period start. Request bounds are
                # real dates, so the source is never asked for prelisting days.
                valid = active_bounds(policy, start, end)
                if valid is None:
                    skip, bounds = 'instrument_lifecycle', None
                elif bounds is not None:
                    bounds = query_trading_bounds(job['interval_code'], *valid)
                if bounds is None:
                    skip = skip or 'trading_calendar'
                supports_review = getattr(type(self.repository), 'defer_empty_window', None) is not None
                if supports_review and not skip and not exclusion_only:
                    job['_query_backward'] = backward
                    review = self.repository.empty_review_overlap(job, start, end)
                    if review:
                        # A new daily refresh must still collect NEWER dates.
                        # Only stop at the held span, not the entire larger
                        # request that happens to overlap it.
                        from app.services.backfill_policy import period_start
                        held_first, held_last = review['window_start'], review['window_end']
                        if job['interval_code'] in {'week','month'}:
                            held_first = period_start(job['interval_code'],held_first)
                            held_last = calendar_period_end(job['interval_code'],held_last)
                        safe = ((max(start,held_last+timedelta(days=1)),end) if backward and end>held_last else
                                (start,min(end,held_first-timedelta(days=1))) if not backward and start<held_first else None)
                        bounds = query_trading_bounds(job['interval_code'],*safe) if safe else None
                        if bounds is None:
                            result = self.repository.hold_empty_review(job, review)
                            self.publish(stage='empty_pending_review', empty_review=result)
                            return None
                        start,end = safe
                        exclusions = [] if derived else exclusion_info(job['symbol'],job['interval_code'],start,end)
                        exclusion_only = bool(exclusions) and not allowed_history_ranges(job['symbol'],job['interval_code'],start,end)
                        self.publish(window_start=start,window_end=end)
                if not skip:
                    valid = active_bounds(policy, *bounds) if bounds else None
                    bounds = query_trading_bounds(job['interval_code'], *valid) if valid else None
                    if bounds is None:
                        skip = 'instrument_lifecycle'
                candles = ([] if skip else self.fetch_daily_aggregate_window(job,start,end) if derived
                           else self.capital.fetch_historical_candles(
                               job["symbol"],job["interval_code"],*bounds,cache_result=False))
                # A non-empty raw reply filtered by period completion/listing
                # policy is NOT a successful source-empty observation.
                source_empty = not candles
                window_committed = False
                try:
                    # Always commit a received window before honoring a
                    # memory pause; otherwise resume would repeat this query.
                    stop_after_save = self.stage_check("after_request_before_write")
                    # Ignore a vendor row for a period after the requested
                    # as-of date. Daily-derived aggregates deliberately retain
                    # current periods, explicitly marked partial by the writer.
                    candles = [c for c in candles if c.timestamp.date() <= job["end_date"]]
                    if boundary:
                        candles = [c for c in candles if c.timestamp.date() >= boundary]
                    if exclusions:
                        candles = [c for c in candles if not excluded_label(job["symbol"],job["interval_code"],c.timestamp.date())]
                    if refresh:
                        # Non-overlapping whole-period windows make the durable
                        # received_count a unique per-run count, not COUNT(*) of
                        # old database history. Keep all valid excess rows.
                        written = datetime.now(timezone.utc)
                        unique = {c.timestamp:c for c in candles if start <= c.timestamp.date() <= end
                                  and (derived or complete_aggregate(job["interval_code"], c.timestamp.date(), written, job["end_date"]))}
                        candles = list(unique.values())
                        del unique
                    validate_candle_volumes(candles)
                    if source_empty and supports_review and not skip and not exclusion_only:
                        result = self.repository.defer_empty_window(job, start, end)
                        window_committed = True  # Durable observation; cursor intentionally unchanged.
                        self.publish(stage='empty_pending_review' if result['pending_review'] else 'empty_observing',
                                     empty_review=result, checkpoint=job['next_date'])
                        return None
                    empty = job["empty_windows"] if skip or exclusion_only else int(job["empty_windows"])+1 if not candles else 0
                    next_date = start-timedelta(days=1) if backward else end+timedelta(days=1)
                    count = self.repository.commit_window(job,candles,next_date,empty,
                        skip_source=skip,window_start=start,window_end=end) if batch or exclusions or (skip and supports_review) else self.repository.commit_window(job,candles,next_date,empty)
                    window_committed = True
                finally:
                    candles.clear()
                    del candles
                    if getattr(type(self.capital), "history_checkpoint", None) is not None and not skip and not derived:
                        self.capital.history_checkpoint(window_committed)
                cursor = next_date
                if not derived:
                    self.windows += 1
                if not skip:
                    job_windows += 1
                    if batch and job["phase"] != "latest": self.recovery_windows += 1
                self.publish(stage="saved",checkpoint=cursor,windows=self.windows,stored_bars=count,
                             elapsed_seconds=round(time.monotonic()-begin,3))
                if self.stage_check("after_write_release") or stop_after_save:
                    return self.pause("memory",f"程序記憶體達 {self.stop_mb} MB 保護門檻",job)
                if backward and not skip and not exclusion_only and empty >= self.empty_limit:
                    if batch and job['interval_code'] == 'day' and integrity_enabled() and cursor >= lower:
                        # A short burst boundary is not proof that older bars
                        # do not exist. Persist the cursor and retry soon;
                        # the independent 1200-date audit lists real gaps.
                        if job_windows >= self.job_window_budget:
                            self.repository.defer_batch_job(job,
                                f'日 K 擴大區間後仍無資料；現有 {count}/{job["target_bars"]} 根；下一日 {cursor}，待查證',
                                memory_limit_mb('CAPITAL_DAY_GAP_RETRY_SECONDS', 900))
                            self.publish(stage='deferred_day_verification', checkpoint=cursor)
                            return None
                        continue
                    if batch and cursor >= lower:
                        delay = memory_limit_mb("CAPITAL_BACKFILL_EMPTY_RETRY_SECONDS",86400)
                        self.repository.defer_batch_job(job,
                            f"連續 {empty} 個區間回傳空資料；現有 {count}/{job['target_bars']} 根；"
                            f"下一探查日 {cursor}，搜尋下限 {lower}；延後探查，不代表更舊資料不存在",delay)
                        self.publish(stage="deferred_empty_history",checkpoint=cursor,stored_bars=count)
                        return None
                    exhausted = True
                    logger.info("%s %s 連續 %s 個查詢區間無資料，停止向前探查",job["symbol"],job["interval_code"],empty)
                    break
            if boundary and backward and cursor < boundary:
                job["listing_boundary_reached"] = True
            if batch and (not backward or refresh):
                self.latest_jobs += 1
                self.publish(latest_checked_jobs=self.latest_jobs,stage="latest_checked")
                if not job["listing_boundary_reached"] and self.repository.batch_latest_finished(job,count):
                    return None
            status = self.repository.finish_incremental_job(job,count,exhausted or (backward and count<job["target_bars"]))
            self.result["processed"] += 1
            self.result[status] += 1
            if self.on_job:
                self.on_job(job,status,count,time.monotonic()-begin)
            self.publish(stage="job_completed",stored_bars=count,listing_policy=policy,
                         current_symbol=job["symbol"],current_market=job["market_no"],current_interval=job["interval_code"],
                         current_target_bars=job["target_bars"],current_refresh=refresh,current_aggregate_from_day=derived,
                         checkpoint=job["next_date"],
                         listing_boundary_reached=job["listing_boundary_reached"],
                         listing_history_shortfall=bool(job["listing_boundary_reached"] and count < job["target_bars"]))
        except KeyboardInterrupt:
            self.control.pause(True)
            return self.pause("manual","使用者中斷；已提交區間保留",job)
        except Exception as error:
            if isinstance(error, TaiwanTradingCalendarError):
                return self.pause('calendar', str(error), job)
            if getattr(type(self.repository), 'record_empty_query_error', None) is not None:
                try:
                    self.repository.record_empty_query_error(job, start, end, error)
                except MySQLRepositoryError:
                    logger.warning('空資料查詢錯誤附註未能保存；原始錯誤與斷點保留', exc_info=True)
            if isinstance(error, DailyAggregatePause):
                return self.pause(error.kind, str(error), job)
            data_error = find_candle_data_error(error)
            if data_error is not None:
                try:
                    failure = self.repository.record_data_failure(job,start,end,data_error)
                except MySQLRepositoryError as database_error:
                    return self.pause("database",str(database_error),job)
                self.control.update(data_failure=dict(job_id=job["id"],symbol=job["symbol"],
                    interval=job["interval_code"],count=failure["count"],limit=DATA_FAILURE_LIMIT,
                    window_start=start,window_end=end,details=failure["details"],failed=failure["failed"]))
                if not failure["failed"]:
                    self.control.update(next_retry_at=failure["retry_at"])
                    return self.pause("data",failure["reason"])
                self.result["processed"] += 1
                self.result["failed"] += 1
                self.publish(stage="data_failed",last_error=failure["reason"],next_retry_at=None)
                return None
            if isinstance(error,MySQLRepositoryError):
                return self.pause("database",str(error),job)
            if is_history_connection_error(error):
                return self.pause("connection",str(error),job)
            self.repository.fail_incremental_job(job,str(error))
            self.result["processed"] += 1
            self.result["failed"] += 1
            self.publish(stage="job_failed",last_error=str(error))
        return None
