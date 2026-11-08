"""Bounded stage telemetry and hysteresis for backfill memory protection.

This monitors process-private memory, not just Python allocations. GC is rate
limited; it is not a promise that COM/native allocations can be released.
"""
from __future__ import annotations

import gc
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

from app.services.process_memory import current_process_memory, memory_limit_mb, memory_status

logger = logging.getLogger(__name__)
_PROCESS_INSTANCE = uuid.uuid4().hex


class BackfillMemoryPause(RuntimeError):
    pass


def limits():
    stop = memory_limit_mb("API_MEMORY_STOP_BACKFILL_MB", 4096)
    warning = min(memory_limit_mb("API_MEMORY_WARNING_MB", 2048), stop)
    resume = min(memory_limit_mb("API_MEMORY_RESUME_BACKFILL_MB", 3072), max(1, stop - 1))
    return warning, stop, resume


class BackfillMemoryGuard:
    def __init__(self, control, cache=None, provider=None):
        self.control, self.cache = control, cache
        self.provider = provider or current_process_memory
        self.warning, self.stop, self.resume = limits()
        saved = control.snapshot()
        same_process = saved.get("memory_process_instance") == _PROCESS_INSTANCE
        gc_time = saved.get("memory_gc_monotonic")
        self.last_gc = float(gc_time) if same_process and gc_time is not None else float("-inf")
        self.last_gc_window = 0
        self.samples = list(saved.get("memory_samples",[]))[-32:]
        last = self.samples[-1] if self.samples else {}
        self.last_value = last.get("private_mb",last.get("working_set_mb")) if last.get("pid") == os.getpid() else None
        throttle = saved.get("memory_throttle_monotonic")
        self.last_throttle = float(throttle) if same_process and throttle is not None else float("-inf")

    def check(self, stage, windows=0):
        if self.cache is not None:
            self.cache.prune_historical()
        memory = self.provider()
        state = memory_status(memory, self.warning, self.stop)
        now = time.monotonic()
        gc_interval = memory_limit_mb("API_MEMORY_GC_INTERVAL_SECONDS", 30)
        periodic = windows - self.last_gc_window >= 50
        collected = False
        if (state in {"warning", "backfill_stopped"} or periodic) and now - self.last_gc >= gc_interval:
            gc.collect()
            self.last_gc, self.last_gc_window = now, windows
            memory = self.provider()
            state = memory_status(memory, self.warning, self.stop)
            collected = True
        value = memory.private_usage_mb if memory.private_usage_mb is not None else memory.working_set_mb
        sample = dict(stage=stage, at=datetime.now(timezone.utc).isoformat(),
                      pid=os.getpid(),
                      private_mb=memory.private_usage_mb, working_set_mb=memory.working_set_mb,
                      delta_mb=round(value-self.last_value, 1) if value is not None and self.last_value is not None else None,
                      gc=collected)
        self.last_value = value
        self.samples = [*self.samples[-31:], sample]
        self.control.update(memory_private_mb=memory.private_usage_mb,
                            memory_working_set_mb=memory.working_set_mb, memory_state=state,
                            memory_stage=stage, memory_samples=self.samples,
                            memory_warning_mb=self.warning, memory_stop_mb=self.stop,
                            memory_resume_mb=self.resume,memory_gc_pid=os.getpid(),
                            memory_process_instance=_PROCESS_INSTANCE,
                            memory_gc_monotonic=self.last_gc if self.last_gc != float("-inf") else None)
        if collected or state == "backfill_stopped" or abs(sample["delta_mb"] or 0) >= 64:
            logger.info("回補階段記憶體 %s：%s", stage, sample)
        if state == "warning" and stage.startswith("before_") and now-self.last_throttle >= gc_interval:
            # Bounded stage backpressure, never an unbounded sleep or a
            # per-stock tax. Recovery still requires the low-water mark.
            time.sleep(min(5,memory_limit_mb("API_MEMORY_STAGE_DELAY_SECONDS",1)))
            self.last_throttle = now
            self.control.update(memory_throttle_monotonic=now)
        return state == "backfill_stopped"

    def require(self, stage):
        if self.check(stage):
            raise BackfillMemoryPause(f"{stage}：程序記憶體達 {self.stop} MB 保護門檻")


def mark_memory_pause(control):
    now = datetime.now(timezone.utc)
    cooldown = memory_limit_mb("API_MEMORY_RESUME_COOLDOWN_SECONDS", 120)
    return control.update(memory_resume_below_since=None,
                          memory_resume_ready=False, memory_observed_at=None,
                          memory_retry_after=(now+timedelta(seconds=cooldown)).isoformat(),
                          memory_paused_pid=os.getpid())


def memory_resume_ready(control, now=None, memory=None):
    """Never bypass a memory pause on manual clicks, scheduler or recovery.

    Readiness is established by repeated observations below the low-water
    mark. A new process starts a NEW observation period, not an old timer.
    Unavailable memory measurements fail closed for a saved memory pause.
    """
    state = control.snapshot()
    if state.get("pause_kind") != "memory":
        return True
    now = now or datetime.now(timezone.utc)
    memory = memory or current_process_memory()
    warning, stop, resume = limits()
    value = memory.private_usage_mb if memory.private_usage_mb is not None else memory.working_set_mb
    updates = dict(memory_observed_at=now.isoformat(),
                   memory_private_mb=memory.private_usage_mb, memory_working_set_mb=memory.working_set_mb,
                   memory_state=memory_status(memory, warning, stop), memory_resume_mb=resume)
    def timestamp(raw):
        try:
            stamp = datetime.fromisoformat(raw)
            return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None
    since = timestamp(state.get("memory_resume_below_since"))
    if state.get("memory_observer_pid") != os.getpid() or state.get("memory_observer_instance") != _PROCESS_INSTANCE:
        since = None
    if value is None or value >= resume:
        since = None
    elif since is None:
        since = now
    stable = memory_limit_mb("API_MEMORY_RESUME_STABLE_SECONDS", 60)
    retry = timestamp(state.get("memory_retry_after"))
    ready = since is not None and (now-since).total_seconds() >= stable and (retry is None or now >= retry)
    updates.update(memory_resume_below_since=since.isoformat() if since else None,
                   memory_observer_pid=os.getpid(),memory_observer_instance=_PROCESS_INSTANCE,memory_resume_ready=ready)
    control.update(**updates)
    return ready
