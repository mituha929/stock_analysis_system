"""Optional weekly official-profile refresh. Independent of Capital/K-line work."""
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import logging
import os
import threading

from app.services.stock_master_sources import TAIPEI
from app.services.stock_master_repository import StockMasterRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StockMasterSchedule:
    enabled: bool = False
    weekday: int = 6  # Monday=0, Sunday=6.
    local_time: time = time(20, 0)
    retry_seconds: int = 3600

    @classmethod
    def from_env(cls):
        enabled = os.getenv("CAPITAL_STOCK_MASTER_WEEKLY_ENABLED", "false").strip().lower()
        if enabled not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            raise ValueError("CAPITAL_STOCK_MASTER_WEEKLY_ENABLED 必須是布林值")
        try:
            weekday = int(os.getenv("CAPITAL_STOCK_MASTER_WEEKDAY", "6"))
            at = datetime.strptime(os.getenv("CAPITAL_STOCK_MASTER_TIME", "20:00"), "%H:%M").time()
            retry = int(os.getenv("CAPITAL_STOCK_MASTER_RETRY_SECONDS", "3600"))
            if weekday not in range(7) or not 300 <= retry <= 86400:
                raise ValueError()
        except ValueError as error:
            raise ValueError("基本資料排程：weekday=0～6，time=HH:MM，retry_seconds=300～86400") from error
        return cls(enabled in {"true", "1", "yes", "on"}, weekday, at, retry)

    def due_key(self, now: datetime):
        local = now.astimezone(TAIPEI)
        due = local.date() - timedelta(days=(local.weekday() - self.weekday) % 7)
        if due == local.date() and local.time().replace(tzinfo=None) < self.local_time:
            due -= timedelta(days=7)
        return "weekly:" + due.isoformat()


class WeeklyStockMasterScheduler:
    def __init__(self, config=None, repository_factory=StockMasterRepository):
        self.config = config or StockMasterSchedule.from_env()
        self.repository_factory = repository_factory
        self.stop_event = threading.Event()
        self.thread = None
        self._completed_key = None
        self._retry_at = None

    def tick(self, now=None):
        if not self.config.enabled:
            return {"status": "disabled"}
        now = now or datetime.now(timezone.utc)
        key = self.config.due_key(now)
        if key == self._completed_key:
            return {"status": "already_completed", "schedule_key": key}
        if self._retry_at and now < self._retry_at:
            return {"status": "retry_wait", "schedule_key": key}
        try:
            result = self.repository_factory().synchronize(trigger="weekly", schedule_key=key, retry_seconds=self.config.retry_seconds)
            if result["status"] in {"completed", "already_completed"}:
                self._completed_key, self._retry_at = key, None
            else:
                self._retry_at = now + timedelta(seconds=self.config.retry_seconds)
            return result
        except Exception as error:
            self._retry_at = now + timedelta(seconds=self.config.retry_seconds)
            logger.warning("官方股票基本資料更新失敗，舊資料保留；%s", type(error).__name__)
            return {"status": "failed", "error_type": type(error).__name__}

    def _run(self):
        while not self.stop_event.is_set():
            self.tick()
            self.stop_event.wait(60)

    def start(self):
        if self.config.enabled and self.thread is None:
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._run, name="official-stock-master-weekly", daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

