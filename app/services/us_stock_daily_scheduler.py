"""One successful completed daily refresh per US session, independent of Capital."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import threading
import uuid

from app.services.capital_process_lock import CapitalProcessLock, CapitalProcessLockError
from app.services.external_market_sessions import ExternalMarketSessions, PROJECT
from app.services.us_stock_daily import BAR_COUNT, COMPANIES, NY, StockDailyError, fetch_stock_daily, utc_naive
from app.services.us_stock_daily_repository import USStockDailyRepository, StockDailyWriteError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class USStockDailySchedule:
    enabled: bool = False
    close_delay_minutes: int = 20
    retry_seconds: int = 600
    state_file: Path = PROJECT / "data" / "us_stock_daily_schedule.json"

    @classmethod
    def from_env(cls):
        enabled = os.getenv("CAPITAL_US_STOCK_DAILY_ENABLED", "false").strip().lower()
        if enabled not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            raise ValueError("CAPITAL_US_STOCK_DAILY_ENABLED 必須是布林值")
        delay = int(os.getenv("CAPITAL_US_STOCK_DAILY_CLOSE_DELAY_MINUTES", "20"))
        retry = int(os.getenv("CAPITAL_US_STOCK_DAILY_RETRY_SECONDS", "600"))
        if not 20 <= delay <= 240 or not 60 <= retry <= 3600:
            raise ValueError("美股日K排程：收盤緩衝20～240分鐘，重試60～3600秒")
        path = Path(os.getenv("CAPITAL_US_STOCK_DAILY_STATE_FILE", str(cls.state_file))).expanduser()
        return cls(enabled in {"true", "1", "yes", "on"}, delay, retry,
                   (path if path.is_absolute() else PROJECT / path).resolve())


class USStockDailyScheduler:
    def __init__(self, config=None, *, sessions=None, fetcher=fetch_stock_daily, repository_factory=USStockDailyRepository):
        self.config = config or USStockDailySchedule.from_env()
        self.sessions = sessions or ExternalMarketSessions()
        self.fetcher, self.repository_factory = fetcher, repository_factory
        self.stop_event = threading.Event()
        self.thread = None
        self._gate = threading.Lock()

    def _plans(self, now):
        utc_naive(now)
        local = now.astimezone(NY)
        if local.year not in self.sessions.markets()["NYSE"]["verified_years"]:
            raise ValueError("美股日曆年度尚未核對")
        latest = upcoming = None
        for offset in range(32):
            day = local.date() - timedelta(days=offset)
            session = self.sessions.session("NYSE", day)
            if session and session[1] + timedelta(minutes=self.config.close_delay_minutes) <= now:
                latest = (day, session[1], session[1] + timedelta(minutes=self.config.close_delay_minutes))
                break
        for offset in range(32):
            day = local.date() + timedelta(days=offset)
            if day.year not in self.sessions.markets()["NYSE"]["verified_years"]:
                break
            session = self.sessions.session("NYSE", day)
            if session and session[1] + timedelta(minutes=self.config.close_delay_minutes) > now:
                upcoming = session[1] + timedelta(minutes=self.config.close_delay_minutes)
                break
        return latest, upcoming

    def _read_state(self):
        try:
            data = json.loads(self.config.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "market_date": None, "items": {}, "history": []}
        if (not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("items"), dict)
                or not isinstance(data.get("history"), list)
                or any(key not in COMPANIES or not isinstance(item, dict) for key, item in data["items"].items())):
            raise ValueError("美股日K排程紀錄格式錯誤")
        if data.get("market_date"):
            from datetime import date
            date.fromisoformat(data["market_date"])
        for item in [data, *data["items"].values()]:
            for key in ("retry_at", "database_retry_at"):
                if item.get(key):
                    utc_naive(datetime.fromisoformat(item[key]))
            if "attempt_count" in item and (type(item["attempt_count"]) is not int or item["attempt_count"] < 0):
                raise ValueError("美股日K重試紀錄錯誤")
        return data

    def _write_state(self, state):
        path = self.config.state_file
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _record(self, state, symbol, result, terminal=False):
        state["items"][symbol] = result
        if terminal:
            state["history"] = [*state["history"], dict(symbol=symbol, market_date=state["market_date"], **result)][-100:]
        self._write_state(state)

    def status(self, now=None):
        now = now or datetime.now(timezone.utc)
        result = dict(schedule=dict(enabled=self.config.enabled, exchange_timezone="America/New_York",
            close_delay_minutes=self.config.close_delay_minutes, retry_seconds=self.config.retry_seconds,
            maximum_retry_seconds=3600, bars_per_refresh=BAR_COUNT, symbols=list(COMPANIES),
            state_file=str(self.config.state_file), calendar_file=str(self.sessions.path)), running=self._gate.locked())
        try:
            state = self._read_state()
            result["state"] = state
            latest, upcoming = self._plans(now)
            result.update(target_market_date=latest[0].isoformat() if latest else None,
                          target_session_close=latest[1].isoformat() if latest else None,
                          target_scheduled_at=latest[2].isoformat() if latest else None,
                          next_scheduled_at=upcoming.isoformat() if upcoming else None)
            active = state["items"] if latest and state["market_date"] == latest[0].isoformat() else {}
            result["completed_symbols"] = [s for s, item in active.items() if item.get("status") in {"completed", "already_completed"}]
            result["note"] = "15檔已收盤日K；成功商品當日不重抓，最近250根重新核對並保留更早資料。不是盤中快照或官方結算。"
        except (ValueError, KeyError, TypeError, OSError):
            result.update(status="state_or_calendar_error", message="排程紀錄或美股日曆無法使用；不觸發抓取")
        return result

    def tick(self, now=None):
        if not self.config.enabled:
            return {"status": "disabled"}
        if not self._gate.acquire(blocking=False):
            return {"status": "busy"}
        try:
            with CapitalProcessLock(self.config.state_file.with_suffix(".lock")).hold(0):
                return self._tick(now or datetime.now(timezone.utc))
        except CapitalProcessLockError:
            return {"status": "busy"}
        except (ValueError, KeyError, TypeError, OSError) as error:
            logger.warning("美股日K排程紀錄／日曆錯誤 type=%s", type(error).__name__)
            return {"status": "state_or_calendar_error", "error_type": type(error).__name__}
        finally:
            self._gate.release()

    def _tick(self, now):
        latest, _ = self._plans(now)
        if latest is None:
            return {"status": "no_completed_session"}
        day = latest[0]
        state = self._read_state()
        if state["market_date"] != day.isoformat():
            state = dict(version=1, market_date=day.isoformat(), items={}, history=state["history"][-100:])
        if set(state["items"]) == set(COMPANIES) and all(item.get("status") in {"completed", "already_completed"} for item in state["items"].values()):
            return {"status": "already_completed", "market_date": day.isoformat()}
        if state.get("database_retry_at") and now < datetime.fromisoformat(state["database_retry_at"]):
            return {"status": "database_retry_wait"}
        try:
            repository = self.repository_factory()
            completed = repository.completed_symbols(day)
        except Exception as error:
            state.update(database_retry_at=(now+timedelta(seconds=self.config.retry_seconds)).isoformat(), database_error_type=type(error).__name__)
            self._write_state(state)
            return {"status": "database_unavailable", "error_type": type(error).__name__}
        state.pop("database_retry_at", None)
        state.pop("database_error_type", None)
        results = []
        for symbol in COMPANIES:
            if self.stop_event.is_set():
                break
            previous = state["items"].get(symbol, {})
            if symbol in completed:
                result = {**previous, "status": "already_completed", "bar_count": BAR_COUNT}
                self._record(state, symbol, result)
                results.append(dict(symbol=symbol, **result))
                continue
            if previous.get("retry_at") and now < datetime.fromisoformat(previous["retry_at"]):
                results.append(dict(symbol=symbol, status="retry_wait", retry_at=previous["retry_at"]))
                continue
            attempts = previous.get("attempt_count", 0) + 1
            result = dict(status="fetching", attempt_count=attempts, started_at=now.isoformat(), retry_at=None)
            self._record(state, symbol, result)
            try:
                batch = self.fetcher(symbol, day, sessions=self.sessions)
                if batch.symbol != symbol or batch.market_date != day:
                    raise StockDailyError("fetcher_returned_wrong_symbol_or_date")
                if self.stop_event.is_set():
                    break
                result["status"] = "writing"
                self._record(state, symbol, result)
                saved = repository.save(batch, sessions=self.sessions)
                if saved.get("status") not in {"completed", "already_completed", "busy"}:
                    raise StockDailyError("invalid_repository_result")
                result.update(saved, fetched_at=batch.fetched_at.isoformat())
                if saved["status"] == "busy":
                    result["retry_at"] = (now+timedelta(seconds=self.config.retry_seconds)).isoformat()
            except Exception as error:
                uncertain = isinstance(error, StockDailyWriteError) and error.commit_uncertain
                result.update(status="commit_uncertain" if uncertain else "failed", error_type=type(error).__name__,
                    message=str(error) if isinstance(error, (StockDailyError, StockDailyWriteError)) else "取得或寫入失敗；既有行情保留",
                    retry_at=(now+timedelta(seconds=min(3600, self.config.retry_seconds * 2**min(attempts-1, 6)))).isoformat())
                logger.warning("美股日K更新失敗 symbol=%s type=%s", symbol, type(error).__name__)
            self._record(state, symbol, result, terminal=True)
            results.append(dict(symbol=symbol, **result))
        return dict(status="checked", market_date=day.isoformat(), items=results)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as error:
                logger.warning("美股日K背景排程失敗 type=%s", type(error).__name__)
            self.stop_event.wait(60)

    def start(self):
        if self.config.enabled and self.thread is None:
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._run, name="us-stock-daily-close", daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
