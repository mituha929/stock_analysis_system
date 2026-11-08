"""Small in-process daily scheduler for stale K-line maintenance."""

from __future__ import annotations

import logging
import json
import os
import threading
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable

import pytz
from app.services.backfill_targets import DEFAULT_TARGET_BARS, parse_targets, configured_refresh_targets, prioritize_day
from app.services.day_candle_integrity import integrity_schedule

from app.services.taiwan_trading_calendar import (
    TaiwanTradingCalendarError,
    latest_completed_trading_month,
    latest_completed_trading_week,
    is_last_trading_day_of_week,
    is_last_trading_day_of_month,
    refresh_official_holidays,
    trading_day_status,
)


logger = logging.getLogger(__name__)
ALLOWED_MARKETS = (0, 1, 4, 5, 6)
ALLOWED_INTERVALS = ("1m", "5m", "15m", "60m", "day", "week", "month")
DEFAULT_STATE_PATH = Path(__file__).resolve().parents[2] / "data" / "backfill_scheduler_state.json"


def _enabled(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _integer_list(value: str, allowed: tuple[int, ...], label: str) -> tuple[int, ...]:
    if value.strip().lower() == "all":
        return allowed
    try:
        result = tuple(dict.fromkeys(int(item.strip()) for item in value.split(",") if item.strip()))
    except ValueError as error:
        raise ValueError(f"{label} 必須是 all 或逗號分隔的整數") from error
    if not result or any(item not in allowed for item in result):
        raise ValueError(f"{label} 僅支援 all 或 {','.join(str(item) for item in allowed)}")
    return result


def _text_list(value: str, allowed: tuple[str, ...], label: str) -> tuple[str, ...]:
    if value.strip().lower() == "all":
        return allowed
    result = tuple(dict.fromkeys(item.strip().lower() for item in value.split(",") if item.strip()))
    if not result or any(item not in allowed for item in result):
        raise ValueError(f"{label} 僅支援 all 或 {','.join(allowed)}")
    return result


def _local_time(value: str) -> time:
    try:
        parsed = datetime.strptime(value.strip(), "%H:%M")
    except ValueError as error:
        raise ValueError("CAPITAL_DAILY_BACKFILL_TIME 必須使用 HH:MM，例如 18:30") from error
    return parsed.time()


def _target_map(value: str) -> dict[str, int]:
    """Parse ``interval:bars`` overrides on top of the recommended defaults."""
    return parse_targets(value)


@dataclass(frozen=True)
class DailyBackfillConfig:
    enabled: bool
    timezone: str
    local_time: time
    markets: tuple[int, ...]
    # Legacy mode adds week/month on completed-period cadence. Daily-derived
    # mode adds them every trading day, including the current partial period.
    intervals: tuple[str, ...]
    weekly_intervals: tuple[str, ...]
    monthly_intervals: tuple[str, ...]
    target_bars: int
    target_bars_by_interval: dict[str, int]
    resume: bool
    state_file: Path
    refresh_bars_by_interval: dict[str, int] = field(default_factory=configured_refresh_targets)
    aggregate_week_month_from_day: bool = True
    as_of_date: date | None = None

    @classmethod
    def from_env(cls) -> "DailyBackfillConfig":
        timezone_name = os.getenv("CAPITAL_DAILY_BACKFILL_TIMEZONE", "Asia/Taipei").strip()
        try:
            pytz.timezone(timezone_name)
        except pytz.UnknownTimeZoneError as error:
            raise ValueError(f"找不到每日回補時區：{timezone_name}") from error
        try:
            target_bars = int(os.getenv("CAPITAL_DAILY_BACKFILL_TARGET_BARS", "1200"))
        except ValueError as error:
            raise ValueError("CAPITAL_DAILY_BACKFILL_TARGET_BARS 必須是整數") from error
        if not 1 <= target_bars <= 10_000:
            raise ValueError("CAPITAL_DAILY_BACKFILL_TARGET_BARS 必須介於 1 到 10000")
        legacy_intervals = _text_list(
            os.getenv(
                "CAPITAL_DAILY_BACKFILL_INTERVALS",
                "1m,5m,15m,60m,day",
            ),
            ALLOWED_INTERVALS,
            "CAPITAL_DAILY_BACKFILL_INTERVALS",
        )
        # Older installations put all seven intervals in the daily variable.
        # Keep accepting that configuration while moving week/month to their
        # correct cadence automatically.
        daily_intervals = tuple(
            interval for interval in legacy_intervals if interval not in {"week", "month"}
        )
        weekly_intervals = _text_list(
            os.getenv("CAPITAL_WEEKLY_BACKFILL_INTERVALS", "week"),
            ALLOWED_INTERVALS,
            "CAPITAL_WEEKLY_BACKFILL_INTERVALS",
        )
        monthly_intervals = _text_list(
            os.getenv("CAPITAL_MONTHLY_BACKFILL_INTERVALS", "month"),
            ALLOWED_INTERVALS,
            "CAPITAL_MONTHLY_BACKFILL_INTERVALS",
        )
        if not daily_intervals:
            raise ValueError("CAPITAL_DAILY_BACKFILL_INTERVALS 必須至少包含一個每日週期")
        return cls(
            enabled=_enabled(os.getenv("CAPITAL_DAILY_BACKFILL_ENABLED", "false")),
            timezone=timezone_name,
            local_time=_local_time(os.getenv("CAPITAL_DAILY_BACKFILL_TIME", "18:30")),
            markets=_integer_list(
                os.getenv("CAPITAL_DAILY_BACKFILL_MARKETS", "all"), ALLOWED_MARKETS,
                "CAPITAL_DAILY_BACKFILL_MARKETS",
            ),
            intervals=daily_intervals,
            weekly_intervals=weekly_intervals,
            monthly_intervals=monthly_intervals,
            target_bars=target_bars,
            target_bars_by_interval=_target_map(os.getenv("CAPITAL_DAILY_BACKFILL_TARGETS", "")),
            resume=_enabled(os.getenv("CAPITAL_DAILY_BACKFILL_RESUME", "true")),
            state_file=Path(
                os.getenv("CAPITAL_BACKFILL_SCHEDULE_STATE_FILE", str(DEFAULT_STATE_PATH)).strip()
            ).expanduser(),
            aggregate_week_month_from_day=_enabled(os.getenv("CAPITAL_DAILY_AGGREGATE_FROM_DAY", "true")),
        )

    @property
    def configured_intervals(self) -> tuple[str, ...]:
        """Return every configured interval once, in execution order."""
        return tuple(prioritize_day(dict.fromkeys(
            (*self.intervals, *self.weekly_intervals, *self.monthly_intervals)
        )))

    def intervals_for(self, day: date) -> tuple[tuple[str, ...], str]:
        """Select intervals for *day* using the offline Taiwan calendar."""
        is_trading_day, reason = trading_day_status(day)
        if not is_trading_day:
            return (), reason
        if self.aggregate_week_month_from_day:
            return self.configured_intervals, "每日（日 K 彙整當期週／月）"
        selected = list(self.intervals)
        labels = ["每日"]
        if is_last_trading_day_of_week(day):
            selected.extend(self.weekly_intervals)
            labels.append("本週最後交易日")
        if is_last_trading_day_of_month(day):
            selected.extend(self.monthly_intervals)
            labels.append("本月最後交易日")
        return tuple(prioritize_day(dict.fromkeys(selected))), "、".join(labels)


class DailyBackfillScheduler:
    """Run a callback once per local calendar day after the configured time."""

    def __init__(
        self, config: DailyBackfillConfig,
        callback: Callable[[DailyBackfillConfig], tuple[str, str]],
        *, maintenance_callback: Callable[[DailyBackfillConfig], tuple[str, str]] | None = None,
    ) -> None:
        self.config = config
        self.callback = callback
        self.maintenance_callback = maintenance_callback
        self._timezone = pytz.timezone(config.timezone)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._tick_lock = threading.Lock()
        self._ticks: set[threading.Thread] = set()
        self._last_attempted_date = None
        self._state = "disabled" if not config.enabled else "waiting"
        self._last_started_at: datetime | None = None
        self._last_finished_at: datetime | None = None
        self._last_message: str | None = None
        self._last_calendar_refresh_date: date | None = None
        self._maintenance_retry_at: datetime | None = None

    def _load_checkpoint(self) -> dict[str, str]:
        if not self.config.state_file.exists():
            return {}
        try:
            payload = json.loads(self.config.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"無法讀取排程斷點：{self.config.state_file}") from error
        return {
            key: str(value) for key,value in payload.items()
            if key.startswith(("last_successful_week", "last_successful_month")) and value
        }

    def _save_checkpoint(self, checkpoint: dict[str, str]) -> None:
        path = self.config.state_file
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {**checkpoint, "updated_at": self._now().isoformat()}
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def _select_intervals(
        self, day: date,
    ) -> tuple[tuple[str, ...], str, dict[str, str]]:
        is_trading_day, day_reason = trading_day_status(day)
        if not is_trading_day:
            return (), day_reason, {}
        if self.config.aggregate_week_month_from_day:
            # Current week/month are revised every trading day. The old
            # completed-period checkpoint must not suppress today's snapshot.
            return self.config.configured_intervals, "每日交易日（日 K 彙整當期週／月）", {}
        checkpoint = self._load_checkpoint()
        selected = list(self.config.intervals if is_trading_day else ())
        labels = ["每日交易日"] if is_trading_day else [day_reason]
        completed_periods: dict[str, str] = {}

        week_end = latest_completed_trading_week(day)
        week_key = f"{week_end.isocalendar().year}-W{week_end.isocalendar().week:02d}"
        if any(checkpoint.get(f"last_successful_week:{market}") != week_key for market in self.config.markets):
            selected.extend(self.config.weekly_intervals)
            completed_periods["last_successful_week"] = week_key
            completed_periods.update({f"last_successful_week:{market}":week_key for market in self.config.markets})
            labels.append(
                "本週最後交易日" if week_end == day else f"補跑已完成交易週 {week_key}"
            )

        month_end = latest_completed_trading_month(day)
        month_key = f"{month_end.year}-{month_end.month:02d}"
        if any(checkpoint.get(f"last_successful_month:{market}") != month_key for market in self.config.markets):
            selected.extend(self.config.monthly_intervals)
            completed_periods["last_successful_month"] = month_key
            completed_periods.update({f"last_successful_month:{market}":month_key for market in self.config.markets})
            labels.append(
                "本月最後交易日" if month_end == day else f"補跑已完成交易月 {month_key}"
            )
        return tuple(prioritize_day(dict.fromkeys(selected))), "、".join(labels), completed_periods

    def _refresh_calendar(self, day: date) -> None:
        if self._last_calendar_refresh_date == day:
            return
        self._last_calendar_refresh_date = day
        try:
            _downloaded, message = refresh_official_holidays(day)
            logger.info(message)
            if day.month == 12:
                # TWSE commonly publishes the following year's table near
                # year-end.  The date parameter lets us cache it in advance;
                # an unavailable table is harmless and will be retried later.
                try:
                    _downloaded, next_message = refresh_official_holidays(
                        day, target_year=day.year + 1,
                    )
                    logger.info(next_message)
                except TaiwanTradingCalendarError as next_error:
                    logger.info("Next-year TWSE holiday schedule is not ready: %s", next_error)
        except TaiwanTradingCalendarError as error:
            # Keep using the last valid cache, the built-in 2026 list, and
            # TWSE_HOLIDAYS.  Retry on the next local calendar day.
            logger.warning("TWSE holiday refresh skipped: %s", error)

    def start(self) -> None:
        if not self.config.enabled or (self.thread and self.thread.is_alive()):
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="daily-kline-backfill", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join()
        with self._tick_lock:
            ticks = list(self._ticks)
        for tick in ticks:
            tick.join()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            configured_intervals = self.config.configured_intervals
            return {
                "enabled": self.config.enabled,
                "state": self._state,
                "timezone": self.config.timezone,
                "local_time": self.config.local_time.strftime("%H:%M"),
                "markets": list(self.config.markets),
                "intervals": list(configured_intervals),
                "target_bars": self.config.target_bars,
                "refresh_bars_by_interval": {i:self.config.refresh_bars_by_interval[i] for i in configured_intervals},
                "aggregate_week_month_from_day": self.config.aggregate_week_month_from_day,
                "target_bars_by_interval": {
                    interval: self.config.target_bars_by_interval[interval]
                    for interval in configured_intervals
                },
                "resume": self.config.resume,
                "next_run_at": self._next_run_at(),
                "last_started_at": self._last_started_at,
                "last_finished_at": self._last_finished_at,
                "last_message": self._last_message,
            }

    def _now(self) -> datetime:
        return datetime.now(self._timezone)

    def _scheduled_for(self, day) -> datetime:
        return self._timezone.localize(datetime.combine(day, self.config.local_time))

    def _next_run_at(self) -> datetime | None:
        if not self.config.enabled:
            return None
        now = self._now()
        if self._maintenance_retry_at and self._maintenance_retry_at.date() == now.date():
            return max(now, self._maintenance_retry_at)
        if self._state in {"waiting_connection", "waiting_database", "waiting_memory", "waiting_worker"}:
            return now + timedelta(seconds=15)
        scheduled = self._scheduled_for(now.date())
        if self._last_attempted_date == now.date() or scheduled <= now:
            scheduled += timedelta(days=1)
        return scheduled

    def _tick(self, now: datetime) -> None:
        """One bounded scheduling decision, independently testable without IO."""
        if not self.config.enabled:
            return
        if self._maintenance_retry_at and self._maintenance_retry_at.date() != now.date():
            # Do not carry a holiday retry into the next trading day's hours.
            self._maintenance_retry_at = None
        with self._tick_lock:
            if (now < self._scheduled_for(now.date()) or self._last_attempted_date == now.date()
                    or (self._maintenance_retry_at and now < self._maintenance_retry_at)):
                return
            self._last_attempted_date = now.date()
            self._maintenance_retry_at = None
        maintenance = False
        with self._lock:
            self._state = "running"
            self._last_started_at = now
            self._last_message = None
        try:
            selected_intervals, cadence_reason, completed_periods = self._select_intervals(now.date())
            if not selected_intervals:
                maintenance = bool(self.maintenance_callback and 'day' in self.config.configured_intervals
                                   and integrity_schedule()[0])
                if maintenance:
                    state, message = self.maintenance_callback(self.config)
                    message = f"排程類型：休市日日 K 完整性驗收；{message}"
                else:
                    state = "skipped"
                    message = f"{now.date().isoformat()} 為{cadence_reason}，今日不執行商品主檔與 K 線更新"
            else:
                effective_config = replace(self.config, intervals=selected_intervals, as_of_date=now.date())
                state, message = self.callback(effective_config)
                message = f"排程類型：{cadence_reason}；執行週期：{','.join(selected_intervals)}；{message}"
                if state == "completed" and completed_periods:
                    checkpoint = self._load_checkpoint()
                    checkpoint.update(completed_periods)
                    try:
                        self._save_checkpoint(checkpoint)
                    except OSError as error:
                        logger.exception("Failed to persist backfill scheduler checkpoint")
                        message += f"；警告：排程斷點寫入失敗，下次可能重複補跑：{error}"
        except Exception as error:
            logger.exception("Daily K-line backfill failed")
            message = str(error)
            state = "error"
            from app.services.mysql_instrument_repository import MySQLRepositoryError
            cause = error
            while cause is not None:
                if isinstance(cause,MySQLRepositoryError):
                    state = "waiting_database"
                    break
                cause = cause.__cause__
        with self._lock:
            if self._last_attempted_date == now.date():
                self._state = state
                self._last_finished_at = self._now()
                self._last_message = message
        # An older, long-running callback must not overwrite the status or
        # retry schedule of a newer day's already-started tick.
        if self._last_attempted_date != now.date():
            return
        if maintenance and state == 'waiting_backfill':
            seconds = max(15, int(os.getenv('CAPITAL_DAY_GAP_RETRY_SECONDS', '900')))
            self._maintenance_retry_at = self._now()+timedelta(seconds=seconds)
            if self._last_attempted_date == now.date():
                self._last_attempted_date = None
        elif state in {"waiting_connection", "waiting_database", "waiting_memory", "waiting_worker"}:
            # Readiness only; login is always explicitly controlled by user.
            if self._last_attempted_date == now.date():
                self._last_attempted_date = None

    def _run(self) -> None:
        while not self.stop_event.is_set():
            now = self._now()
            self._refresh_calendar(now.date())
            # A previous daily callback can still be processing slow recovery
            # tomorrow. Keep the clock alive so the next daily round can ask
            # that worker to stop at its committed boundary.
            if (now >= self._scheduled_for(now.date()) and self._last_attempted_date != now.date()
                    and (not self._maintenance_retry_at or now >= self._maintenance_retry_at)):
                tick = threading.Thread(target=self._run_tick, args=(now,), name="daily-kline-tick", daemon=True)
                with self._tick_lock:
                    self._ticks.add(tick)
                tick.start()
            self.stop_event.wait(15)

    def _run_tick(self, now):
        try:
            self._tick(now)
        finally:
            with self._tick_lock:
                self._ticks.discard(threading.current_thread())
