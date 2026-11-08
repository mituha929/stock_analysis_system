"""Durable, machine-local pause/progress control shared by API and CLI.

SQLite is independent of MySQL so a database outage can still be reported.
The separate OS file lock prevents two workers from reclaiming each other's
running rows. No connection/login is performed by this module.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
import pytz
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from app.services.capital_process_lock import CapitalProcessLock, CapitalProcessLockError
from app.services.backfill_memory import memory_resume_ready


class BackfillControl:
    def __init__(self, path: Path | None = None) -> None:
        default = Path(__file__).resolve().parents[2] / "data" / "backfill_control.sqlite3"
        selected = path or Path(os.getenv("CAPITAL_BACKFILL_CONTROL_DB", str(default)))
        self.path = selected if selected.is_absolute() else default.parents[1] / selected

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS control (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
            connection.execute("INSERT OR IGNORE INTO control VALUES (1, '{}')")
            connection.commit()
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def snapshot(self) -> dict:
        if not self.path.exists():
            with self.connection():
                pass
        # Polling must not execute CREATE/INSERT or compete for a writer lock.
        connection = sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro",uri=True,timeout=5)
        try:
            row = connection.execute("SELECT payload FROM control WHERE id=1").fetchone()
            result = json.loads(row[0]) if row else {}
        except sqlite3.OperationalError as error:
            if "no such table: control" not in str(error):
                raise
            # Another first-time caller may have created the empty file but
            # not yet committed its initial table.
            with self.connection() as initialized:
                result = json.loads(initialized.execute("SELECT payload FROM control WHERE id=1").fetchone()[0])
        finally:
            connection.close()
        result.setdefault("pause_requested", False)
        return result

    def update(self, **values) -> dict:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = json.loads(connection.execute("SELECT payload FROM control WHERE id=1").fetchone()[0])
            result.update(values, updated_at=datetime.now(timezone.utc).isoformat())
            connection.execute("UPDATE control SET payload=? WHERE id=1", (json.dumps(result, ensure_ascii=False, default=str),))
        return result

    def pause(self, value: bool) -> dict:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = json.loads(connection.execute("SELECT payload FROM control WHERE id=1").fetchone()[0])
            priority = state.get("scheduled_daily_priority") or {}
            if priority.get("phase") == "waiting_stop" and value:
                priority.update(phase="cancelled", message="使用者要求暫停；取消本次排程切換")
                state["scheduled_daily_priority"] = priority
            takeover = state.get("manual_daily_run") or {}
            if takeover.get("phase") in {"waiting_stop", "parking", "ready"}:
                if not value:
                    raise RuntimeError("每日更新正在切換；請等待切換完成")
                if takeover.get("phase") == "parking":
                    takeover.update(pause_after_parking=True, message="使用者要求暫停；正在完成佇列保存")
                else:
                    takeover.update(phase="cancelled", state="cancelled", message="使用者要求暫停，已取消每日切換")
                state["manual_daily_run"] = takeover
            state.update(pause_requested=value,updated_at=datetime.now(timezone.utc).isoformat())
            connection.execute("UPDATE control SET payload=? WHERE id=1",(json.dumps(state,ensure_ascii=False,default=str),))
            return state

    def request_scheduled_priority(self, settings):
        """Let a due daily round preempt resumable collection at a safe boundary."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = json.loads(connection.execute("SELECT payload FROM control WHERE id=1").fetchone()[0])
            previous = state.get("scheduled_daily_priority") or {}
            if previous.get("phase") in {"waiting_stop", "ready", "running"}:
                return state, False
            if (state.get("pause_requested") or state.get("pause_kind") in {"memory", "database", "manual"}
                    or (state.get("manual_daily_run") or {}).get("phase") in {"waiting_stop", "parking", "ready"}
                    or not state.get("plans") or state.get("preparation_pending")
                    or state.get("preparation_complete") is False
                    or state.get("state") not in {"running", "waiting_backfill", "queued", "planning", "waiting_connection"}):
                return state, False
            now = datetime.now(timezone.utc).isoformat()
            state.update(pause_requested=True, scheduled_daily_priority=dict(
                id=uuid.uuid4().hex, phase="waiting_stop", state="waiting_stop",
                settings=settings, requested_at=now,
                message="每日排程已要求安全暫停目前抓取，等待目前區間寫入"), updated_at=now)
            connection.execute("UPDATE control SET payload=? WHERE id=1", (json.dumps(state, ensure_ascii=False, default=str),))
            return state, True

    def start_scheduled_priority(self, token):
        """Persist the interrupted scope before switching; MySQL jobs remain intact."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = json.loads(connection.execute("SELECT payload FROM control WHERE id=1").fetchone()[0])
            task = state.get("scheduled_daily_priority") or {}
            if task.get("id") != token or task.get("phase") != "waiting_stop":
                return False
            if state.get("pause_kind") == "database":
                task.update(phase="failed", state="error", message="目前區間保存失敗，保留原工作")
                state["scheduled_daily_priority"] = task
            else:
                task.update(phase="ready", state="ready", old_plans=state.get("plans") or [],
                            old_pending_schedule=state.get("pending_schedule"),
                            old_manual_run=state.get("manual_run", False),
                            message="原抓取工作與斷點已保留，正在執行每日更新")
                state.update(scheduled_daily_priority=task, plans=[], pending_schedule=None,
                             pause_requested=False, pause_kind=None, pause_reason=None,
                             state="idle", normal_round_complete=True, resume_pending=False)
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            connection.execute("UPDATE control SET payload=? WHERE id=1", (json.dumps(state, ensure_ascii=False, default=str),))
            return task.get("phase") == "ready"

    def start_scheduled_daily(self, token, pending_schedule):
        """Persist the daily run before any work so startup can resume it."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = json.loads(connection.execute("SELECT payload FROM control WHERE id=1").fetchone()[0])
            task = state.get("scheduled_daily_priority") or {}
            if task.get("id") != token or task.get("phase") != "ready" or state.get("pause_requested"):
                return False
            task.update(phase="running", state="running")
            state.update(scheduled_daily_priority=task, pending_schedule=pending_schedule,
                         source="api", manual_run=False, state="running", stage="syncing_board_instruments",
                         updated_at=datetime.now(timezone.utc).isoformat())
            connection.execute("UPDATE control SET payload=? WHERE id=1", (json.dumps(state, ensure_ascii=False, default=str),))
            return True

    def finish_scheduled_priority(self, token):
        """Restore both the old slow scope and any daily work still deferred."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = json.loads(connection.execute("SELECT payload FROM control WHERE id=1").fetchone()[0])
            task = state.get("scheduled_daily_priority") or {}
            if (task.get("id") != token or task.get("phase") != "running"
                    or state.get("pause_requested") or not state.get("normal_round_complete")
                    or state.get("state") not in {"completed", "waiting_backfill", "waiting_review"}):
                return False
            old_plans = task.get("old_plans") or []
            # Keep the exact historical range when resuming on a later day.
            for plan in old_plans:
                if plan.get("end_date") and plan.get("planned_on"):
                    plan["as_of_date"] = plan["end_date"]
                    plan["fixed_range"] = True
            task.update(phase="done", state="completed", message="每日更新正常輪完成；慢速補齊斷點已恢復")
            state.update(scheduled_daily_priority=task, plans=(state.get("plans") or []) + old_plans,
                         state="waiting_backfill", source="api", manual_run=bool(task.get("old_manual_run")),
                         pause_kind=None, pause_reason="每日更新正常輪完成；慢速補齊保留斷點等待續跑",
                         updated_at=datetime.now(timezone.utc).isoformat())
            connection.execute("UPDATE control SET payload=? WHERE id=1", (json.dumps(state, ensure_ascii=False, default=str),))
            return True

    def request_manual_daily(self, token, settings):
        """Atomically request a safe stop before replacing the active queue."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = json.loads(connection.execute("SELECT payload FROM control WHERE id=1").fetchone()[0])
            if (state.get("scheduled_daily_priority") or {}).get("phase") in {"waiting_stop", "ready", "running"}:
                raise RuntimeError("排程每日更新正在優先切換或執行；請等待完成後再手動啟動")
            previous = state.get("manual_daily_run") or {}
            local_zone = pytz.timezone(settings.get("timezone", "Asia/Taipei"))
            requested_on = datetime.now(local_zone).date().isoformat()
            previous_day = previous.get("requested_on")
            if previous_day is None and previous.get("requested_at"):
                try:
                    previous_day = datetime.fromisoformat(previous["requested_at"]).astimezone(local_zone).date().isoformat()
                except (ValueError, TypeError):
                    previous_day = None
            switching = previous.get("phase") in {"waiting_stop", "parking", "ready"}
            active_today = (previous.get("phase") == "running" and previous.get("state") == "running"
                            and previous_day == requested_on and not state.get("pause_requested")
                            and state.get("state") in {"running", "planning", "queued"})
            if switching or active_today:
                return state, False
            now = datetime.now(timezone.utc).isoformat()
            state.update(pause_requested=True, manual_daily_run=dict(id=token,phase="waiting_stop",
                state="waiting_stop",requested_at=now,settings=settings,
                requested_on=requested_on, replaces_request_id=previous.get("id"),
                original_state=state.get("state"),original_pause_kind=state.get("pause_kind"),
                message="已請求安全暫停，等待目前區間保存並釋放工作鎖"),updated_at=now)
            connection.execute("UPDATE control SET payload=? WHERE id=1",(json.dumps(state,ensure_ascii=False,default=str),))
            return state, True

    def update_manual_daily(self, token, allowed_phases, **values):
        """Compare-and-update a transition without undoing a user's pause."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = json.loads(connection.execute("SELECT payload FROM control WHERE id=1").fetchone()[0])
            takeover = state.get("manual_daily_run") or {}
            if takeover.get("id") != token or takeover.get("phase") not in allowed_phases:
                return False
            now = datetime.now(timezone.utc).isoformat()
            takeover.update(values,updated_at=now)
            state.update(manual_daily_run=takeover,updated_at=now)
            connection.execute("UPDATE control SET payload=? WHERE id=1",(json.dumps(state,ensure_ascii=False,default=str),))
            return True

    def finish_manual_daily_parking(self, token, archived_count):
        """Archive the old scope and switch the active queue in one SQLite transaction."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = json.loads(connection.execute("SELECT payload FROM control WHERE id=1").fetchone()[0])
            takeover = state.get("manual_daily_run") or {}
            if takeover.get("id") != token or takeover.get("phase") != "parking":
                raise RuntimeError("每日切換已取消或識別變更；保留原計畫")
            connection.execute("CREATE TABLE IF NOT EXISTS manual_daily_displaced (id TEXT PRIMARY KEY, payload TEXT NOT NULL, archived_at TEXT NOT NULL)")
            keys = ("plans", "pending_schedule", "resume_scope", "state", "stage", "source",
                    "pause_kind", "pause_reason", "target_bars", "normal_round_complete")
            archive = {key: state.get(key) for key in keys}
            now = datetime.now(timezone.utc).isoformat()
            connection.execute("INSERT OR IGNORE INTO manual_daily_displaced(id,payload,archived_at) VALUES(?,?,?)",
                               (token,json.dumps(archive,ensure_ascii=False,default=str),now))
            stopped = bool(takeover.get("pause_after_parking"))
            takeover.update(phase="cancelled" if stopped else "ready",state="cancelled" if stopped else "ready",
                            archived_jobs=archived_count,message="舊工作已移出佇列；使用者要求暫停" if stopped
                            else "舊工作已移出佇列，正在啟動每日更新",updated_at=now)
            state.update(state="paused" if stopped else "idle",stage="manual_daily_ready",source="api",manual_run=True,
                         plans=[],pending_schedule=None,resume_scope=None,resume_pending=False,
                         preparation_pending=False,preparation_complete=True,
                         pause_requested=stopped,pause_kind="manual" if stopped else None,
                         pause_reason="使用者要求暫停" if stopped else None,normal_round_complete=True,
                         manual_daily_run=takeover,updated_at=now)
            connection.execute("UPDATE control SET payload=? WHERE id=1",(json.dumps(state,ensure_ascii=False,default=str),))
            return state

    def start_manual_daily(self, token, pending_schedule):
        """Atomically persist the new daily run and its crash-recovery phase."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = json.loads(connection.execute("SELECT payload FROM control WHERE id=1").fetchone()[0])
            task = state.get("manual_daily_run") or {}
            if task.get("id") != token or task.get("phase") != "ready" or state.get("pause_requested"):
                return False
            now = datetime.now(timezone.utc).isoformat()
            task.update(phase="running",state="running",message="正在同步商品並執行每日更新",updated_at=now)
            state.update(source="api",manual_run=True,state="running",stage="syncing_board_instruments",
                         pause_reason=None,pending_schedule=pending_schedule,manual_daily_run=task,updated_at=now)
            connection.execute("UPDATE control SET payload=? WHERE id=1",(json.dumps(state,ensure_ascii=False,default=str),))
            return True

    def save_prepared_plans(self, plans):
        """Merge an explicitly prepared scope while keeping the worker paused.

        Caller owns the worker lock. No candles/cursors are stored in SQLite.
        A memory pause retains its recovery gate, even for a fresh prepare.
        """
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = json.loads(connection.execute("SELECT payload FROM control WHERE id=1").fetchone()[0])
            merged = {(p["market_no"],p["interval"]):p for p in state.get("plans",[])}
            merged.update({(p["market_no"],p["interval"]):p for p in plans})
            memory_paused = state.get("pause_kind") == "memory"
            state.update(plans=list(merged.values()),source="api",state="paused" if memory_paused else "prepared",
                         stage="prepared",pause_requested=True,resume_pending=False,resume_scope=None,
                         pending_schedule=None,pause_kind="memory" if memory_paused else "manual",
                         pause_reason="重抓範圍已準備；保持暫停，請按繼續回補",current_symbol=None,
                         processed=0,completed=0,insufficient_history=0,failed=0,windows=0,
                         queued_jobs=sum(int(p.get("planned_jobs") or 0) for p in merged.values()),
                         deferred=0,next_retry_at=None,current_market=None,current_interval=None,
                         current_target_bars=None,current_requery=False,group_plan=None,
                         updated_at=datetime.now(timezone.utc).isoformat())
            connection.execute("UPDATE control SET payload=? WHERE id=1",(json.dumps(state,ensure_ascii=False,default=str),))
        return state

    def request_resume(self, scope, target_bars, target_source="override"):
        """Persist one resume request; concurrent clicks cannot queue duplicates."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = json.loads(connection.execute("SELECT payload FROM control WHERE id=1").fetchone()[0])
            if state.get("resume_pending") and not state.get("pause_requested"):
                return state, False
            state.update(source="api",state="queued",stage="queued",pause_requested=False,
                pause_kind=None,pause_reason=None,resume_pending=True,resume_token=uuid.uuid4().hex,
                resume_scope=scope,target_bars=target_bars,resume_target_source=target_source,updated_at=datetime.now(timezone.utc).isoformat())
            connection.execute("UPDATE control SET payload=? WHERE id=1",(json.dumps(state,ensure_ascii=False,default=str),))
        return state, True

    def acquire_worker(self):
        """Reserve the same OS lock used by API and CLI, without waiting.

        The returned lease may be handed to a background task. Its caller must
        release it only after preparation AND execution have stopped.
        """
        lease = CapitalProcessLock(self.path.with_suffix(".worker.lock"))
        lease.acquire(timeout_seconds=0)
        return lease

    def finish_regrab(self, plans, token):
        """Publish all groups together; never unpause a concurrent user pause."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = json.loads(connection.execute("SELECT payload FROM control WHERE id=1").fetchone()[0])
            if state.get("regrab_token") != token:
                raise RuntimeError("完整重抓準備識別已變更；不啟動舊工作")
            paused = bool(state.get("pause_requested"))
            state.update(plans=plans,source="api",manual_run=True,
                preparation_pending=False,preparation_complete=True,preparation_done=35,
                state="prepared" if paused else "queued",stage="prepared" if paused else "queued",
                resume_pending=not paused,resume_scope=None,pending_schedule=None,
                pause_kind="manual" if paused else None,
                pause_reason="全部範圍已準備；使用者已要求暫停" if paused else None,
                current_symbol=None,current_market=None,current_interval=None,group_plan=None,
                current_target_bars=None,current_requery=False,current_refresh=False,
                processed=0,completed=0,insufficient_history=0,failed=0,windows=0,deferred=0,
                next_retry_at=None,queued_jobs=sum(int(p.get("planned_jobs") or 0) for p in plans),
                updated_at=datetime.now(timezone.utc).isoformat())
            connection.execute("UPDATE control SET payload=? WHERE id=1",(json.dumps(state,ensure_ascii=False,default=str),))
        return state

    def saved_plans(self) -> list[dict]:
        plans = self.snapshot().get("plans", [])
        for plan in plans:
            for key in ("start_date", "end_date", "as_of_date", "explicit_start"):
                if isinstance(plan.get(key), str):
                    plan[key] = date.fromisoformat(plan[key])
        return plans

    @contextmanager
    def worker(self):
        lock = CapitalProcessLock(self.path.with_suffix(".worker.lock"))
        with lock.hold(timeout_seconds=0):
            yield

    def worker_active(self) -> bool:
        try:
            with self.worker():
                return False
        except CapitalProcessLockError:
            return True


class BackfillRecovery:
    """Resume only saved API work when its prerequisites recover."""
    def __init__(self, capital, callback, cache) -> None:
        self.capital, self.callback, self.cache = capital, callback, cache
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        control = BackfillControl()
        state = control.snapshot()
        maintenance = state.get('day_integrity_maintenance') or {}
        if maintenance.get('active') and not control.worker_active():
            control.update(day_integrity_maintenance=dict(maintenance, active=False, day_integrity_active=False,
                state='interrupted', pause_reason='服務重新啟動；已提交缺口保留，下次休市日排程接續'))
        if state.get("source") == "api" and state.get("state") == "running" and not control.worker_active():
            control.update(state="paused" if state.get("pause_requested") else "waiting_connection",
                           pause_reason="服務重新啟動；已提交進度保留，等待續跑")
        if state.get("preparation_pending") and not control.worker_active():
            control.update(state="paused",stage="prepare_interrupted",preparation_pending=False,
                           preparation_complete=False,pause_requested=True,resume_pending=False,
                           pause_reason="完整重抓準備因服務中斷而停止；請重新按重抓全部資料，不啟動部分工作")
        self.thread = threading.Thread(target=self._run, name="backfill-recovery", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)

    def _run(self) -> None:
        import logging
        from app.services.taiwan_trading_calendar import trading_day_status
        logger = logging.getLogger(__name__)
        while not self.stop_event.wait(15):
            try:
                self.cache.prune_historical()
                control = BackfillControl()
                state = control.snapshot()
                # Memory observation is passive: keep its low-water timer and
                # telemetry current even on holidays, offline or manually
                # paused. Do not confuse readiness with permission to execute.
                # This API thread must not measure a CLI process on its behalf.
                memory_ready = memory_resume_ready(control) if state.get("source") == "api" else True
                # The observer (or a concurrent pause) may have updated state.
                state = control.snapshot()
                takeover = state.get("manual_daily_run") or {}
                if takeover.get("phase") in {"waiting_stop", "parking", "ready"}:
                    # The requested switch owns its own pause and retry gates.
                    # The callback first waits for the old worker's lease.
                    self.callback(self.capital, [], int(state.get("target_bars",1200)))
                    continue
                if (state.get("scheduled_daily_priority") or {}).get("phase") in {"waiting_stop", "ready"}:
                    self.callback(self.capital, [], int(state.get("target_bars",1200)))
                    continue
                priority = state.get("scheduled_daily_priority") or {}
                if (priority.get("phase") == "running" and not control.worker_active()
                        and state.get("normal_round_complete")
                        and state.get("state") in {"completed", "waiting_backfill", "waiting_review"}):
                    control.finish_scheduled_priority(priority["id"])
                    continue
                retry_due = not state.get("next_retry_at") or datetime.fromisoformat(state["next_retry_at"]) <= datetime.now(timezone.utc)
                if (state.get("source") == "api" and state.get("state") in {"running", "paused", "waiting_connection", "waiting_trading_day", "waiting_backfill", "queued", "planning"}
                        and (state.get("state") != "waiting_backfill" or retry_due)
                        and (state.get("pause_kind") != "data" or retry_due)
                        and not state.get("pause_requested") and not control.worker_active()
                        and not state.get("preparation_pending")
                        and state.get("preparation_complete") is not False
                        and self.capital.quote_ready
                        and (state.get("manual_run") or trading_day_status(datetime.now().date())[0])
                        and memory_ready):
                    plans = control.saved_plans()
                    if plans or state.get("resume_scope") or state.get("pending_schedule"):
                        self.callback(self.capital, plans, int(state.get("target_bars", 1200)))
            except Exception:
                logger.exception("回補恢復檢查失敗；已保存進度保留，下次重試")
