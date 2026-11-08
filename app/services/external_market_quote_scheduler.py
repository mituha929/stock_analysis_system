"""Independent market-session quote checks with bounded retries and durable state."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import threading
import uuid

from app.services.capital_process_lock import CapitalProcessLock, CapitalProcessLockError
from app.services.external_market_quotes import QuoteFetchError, fetch_one
from app.services.external_market_quote_repository import ExternalMarketQuoteRepository, QuoteWriteError
from app.services.external_market_sessions import PRODUCTS, ExternalMarketSessions, product_key, quote_quality

logger = logging.getLogger(__name__)
PROJECT = Path(__file__).resolve().parents[2]
TAIPEI = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class ExternalQuoteSchedule:
    enabled: bool = False
    interval_minutes: int = 30
    retry_seconds: int = 300
    state_file: Path = PROJECT / "data" / "external_market_quotes_schedule.json"
    close_delay_minutes: int = 20
    close_retry_minutes: int = 90
    max_age_seconds: int = 2700

    @classmethod
    def from_env(cls):
        enabled = os.getenv("CAPITAL_EXTERNAL_QUOTES_ENABLED", "false").strip().lower()
        if enabled not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            raise ValueError("CAPITAL_EXTERNAL_QUOTES_ENABLED 必須是布林值")
        try:
            minutes = int(os.getenv("CAPITAL_EXTERNAL_QUOTES_INTERVAL_MINUTES", "30"))
            retry = int(os.getenv("CAPITAL_EXTERNAL_QUOTES_RETRY_SECONDS", "300"))
            delay = int(os.getenv("CAPITAL_EXTERNAL_QUOTES_CLOSE_DELAY_MINUTES", "20"))
            close_retry = int(os.getenv("CAPITAL_EXTERNAL_QUOTES_CLOSE_RETRY_MINUTES", "90"))
            max_age = int(os.getenv("CAPITAL_EXTERNAL_QUOTES_MAX_AGE_SECONDS", "2700"))
            if (minutes not in {5, 10, 15, 20, 30, 60} or not 60 <= retry <= 86400
                    or not 1 <= delay < close_retry <= 240 or not 60 <= max_age <= 86400):
                raise ValueError()
        except ValueError as error:
            raise ValueError("外部報價排程設定無效：請檢查間隔、重試、收盤緩衝與新鮮度秒數") from error
        path = Path(os.getenv("CAPITAL_EXTERNAL_QUOTES_STATE_FILE", str(cls.state_file))).expanduser()
        return cls(enabled in {"true", "1", "yes", "on"}, minutes, retry,
                   (path if path.is_absolute() else PROJECT / path).resolve(), delay, close_retry, max_age)

    def slot(self, now):
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("排程時間必須包含時區")
        local = now.astimezone(TAIPEI)
        return local.replace(minute=local.minute // self.interval_minutes * self.interval_minutes, second=0, microsecond=0)


class ExternalMarketQuoteScheduler:
    def __init__(self, config=None, *, fetcher=fetch_one, repository_factory=ExternalMarketQuoteRepository, sessions=None):
        self.config = config or ExternalQuoteSchedule.from_env()
        self.fetcher, self.repository_factory = fetcher, repository_factory
        self.sessions = sessions or ExternalMarketSessions()
        self.stop_event = threading.Event()
        self.thread = None
        self._gate = threading.Lock()

    def _read_state(self):
        try:
            data = json.loads(self.config.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 2, "products": {}, "history": []}
        if not isinstance(data, dict) or not isinstance(data.get("history"), list):
            raise ValueError("排程紀錄格式無效")
        if data.get("version") == 1:
            return {"version": 2, "products": {}, "history": data["history"][-100:],
                    "legacy_state": {key: value for key, value in data.items() if key != "history"}}
        if data.get("version") != 2 or not isinstance(data.get("products"), dict):
            raise ValueError("排程紀錄版本無效")
        if any(key not in {product_key(p) for p in PRODUCTS} or not isinstance(value, dict)
               for key, value in data["products"].items()):
            raise ValueError("排程商品紀錄無效")
        return data

    def _write_state(self, data):
        path = self.config.state_file
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _record(self, state, key, result, *, terminal=False):
        data = {**state, "products": {**state["products"], key: result}}
        if terminal:
            data["history"] = [*state["history"], {"product": key, **result}][-100:]
        self._write_state(data)
        return data

    def _plan(self, product, now):
        return self.sessions.plan(product, now, self.config.interval_minutes, self.config.close_delay_minutes)

    def _quality(self, quote_at, now, plan):
        return quote_quality(quote_at, now, plan, max_age_seconds=self.config.max_age_seconds,
                             close_retry_minutes=self.config.close_retry_minutes)

    def status(self, now=None):
        now = now or datetime.now(timezone.utc)
        config = dict(enabled=self.config.enabled, interval_minutes=self.config.interval_minutes,
                      timezone="Asia/Taipei", retry_seconds=self.config.retry_seconds,
                      close_delay_minutes=self.config.close_delay_minutes,
                      close_retry_minutes=self.config.close_retry_minutes, max_age_seconds=self.config.max_age_seconds,
                      state_file=str(self.config.state_file), calendar_file=str(self.sessions.path))
        try:
            state = self._read_state()
            items = []
            for product in PRODUCTS:
                key = product_key(product)
                item = dict(source=product[0], symbol=product[1], session=product[2], last_check=state["products"].get(key))
                try:
                    plan = self._plan(product, now)
                    item["market_session"] = dict(key=plan.session_key, phase=plan.phase,
                        opens=plan.opens.isoformat(), closes=plan.closes.isoformat(),
                        scheduled_at=plan.scheduled_at.isoformat()) if plan else None
                    previous = item["last_check"] or {}
                    if plan and previous.get("checked_at"):
                        at = datetime.fromisoformat(previous["quote_at"]) if previous.get("quote_at") else None
                        item["current_quality"] = self._quality(at, now, plan)
                except (OSError, ValueError, KeyError, TypeError):
                    item["calendar_status"] = "unverified"
                items.append(item)
            return dict(schedule=config, running=self._gate.locked(), state=state, items=items,
                        note="快照不是30分鐘K線；停機缺失時段不補造。抓取成功不保證新成交，時間未知不得視為最新行情。")
        except (OSError, ValueError, TypeError):
            return dict(schedule=config, status="state_error", message="排程紀錄無法讀取；停止寫入，請檢查紀錄檔")

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
        except (OSError, ValueError, TypeError) as error:
            logger.warning("外部報價排程紀錄無法使用（%s）", type(error).__name__)
            return {"status": "state_error", "error_type": type(error).__name__}
        finally:
            self._gate.release()

    def _tick(self, now):
        state, results = self._read_state(), []
        for product in PRODUCTS:
            if self.stop_event.is_set():
                break
            key = product_key(product)
            previous = state["products"].get(key, {})
            try:
                plan = self._plan(product, now)
            except (OSError, ValueError, KeyError, TypeError):
                result = dict(previous, status="calendar_unverified",
                              message="該市場日曆缺失／年度未核對，保留行情並暫停此商品抓取")
                if previous.get("status") != result["status"]:
                    state = self._record(state, key, result, terminal=True)
                results.append(dict(product=key, **result))
                continue
            if plan is None or plan.phase == "close_wait":
                results.append(dict(product=key, status="outside_session"))
                continue
            if previous.get("schedule_key") == plan.key:
                if (previous.get("status") == "failed" and plan.phase == "close"
                        and now >= plan.closes + timedelta(minutes=self.config.close_retry_minutes)):
                    state = self._record(state, key, {**previous, "status": "close_retry_expired", "retry_at": None}, terminal=True)
                    results.append(dict(product=key, status="close_retry_expired"))
                    continue
                if previous.get("status") in {"updated", "unchanged", "already_checked", "source_regressed", "close_retry_expired"}:
                    at = datetime.fromisoformat(previous["quote_at"]) if previous.get("quote_at") else None
                    current_quality = self._quality(at, now, plan)
                    if not current_quality["close_pending"]:
                        if (previous.get("quality") or {}).get("close_pending"):
                            state = self._record(state, key, {**previous, "quality": {**previous["quality"], **current_quality}, "retry_at": None}, terminal=True)
                        results.append(dict(product=key, status="already_completed"))
                        continue
                if previous.get("status") in {"writing", "commit_uncertain"}:
                    results.append(dict(product=key, status="needs_review", batch_id=previous.get("batch_id")))
                    continue
                if previous.get("retry_at") and now < datetime.fromisoformat(previous["retry_at"]):
                    results.append(dict(product=key, status="retry_wait", retry_at=previous["retry_at"]))
                    continue
            if previous.get("status") in {"writing", "commit_uncertain"}:
                state["history"] = [*state["history"], {"product": key, **previous,
                    "status": "commit_uncertain", "message": "前次寫入結果待查核；本輪只檢查最新時段"}][-100:]
            last_slot = plan.opens - timedelta(minutes=self.config.interval_minutes)
            if previous.get("market_session_key") == plan.session_key and previous.get("scheduled_at"):
                last_slot = datetime.fromisoformat(previous["scheduled_at"])
            period_seconds = self.config.interval_minutes * 60
            if plan.phase == "close":
                steps = int(((plan.closes-plan.opens).total_seconds()-1) // period_seconds)
                last_intraday = plan.opens + timedelta(seconds=steps*period_seconds)
                missed = max(0, int((last_intraday-last_slot).total_seconds() // period_seconds))
            else:
                missed = max(0, int((plan.scheduled_at-last_slot).total_seconds() // period_seconds)-1)
            result = dict(status="fetching", schedule_key=plan.key, market_session_key=plan.session_key,
                          scheduled_at=plan.scheduled_at.isoformat(), started_at=now.isoformat(),
                          checked_at=None, quote_at=None, batch_id=None, retry_at=None,
                          missed_observations=missed, last_success=previous.get("last_success"))
            coverage = dict(data_kind="quote_snapshot", coverage_status="snapshot_only",
                            missed_observations=missed, previous_check_at=previous.get("checked_at"),
                            missed_range={"from": (last_slot+timedelta(seconds=period_seconds)).isoformat(),
                                          "through": (last_slot+timedelta(seconds=missed*period_seconds)).isoformat()} if missed else None)
            result["coverage"] = coverage
            state = self._record(state, key, result)
            quote = repository = None
            try:
                quote = self.fetcher(*product)
                quote.validate()
                if (quote.source, quote.symbol, quote.session) != product:
                    raise QuoteFetchError("抓取商品與排程商品不符")
                if self.stop_event.is_set():
                    break
                quality = self._quality(quote.quote_at, now, plan)
                quality.update(coverage)
                repository = self.repository_factory()
                state = self._record(state, key, {**result, "status": "writing"})
                saved = repository.save_observation(quote, plan.key, quality)
                result.update(saved, checked_at=saved.get("checked_at", quote.fetched_at.isoformat()),
                              quote_at=saved.get("quote_at", quote.quote_at.isoformat() if quote.quote_at else None))
                result["last_success"] = dict(checked_at=result["checked_at"], batch_id=saved["batch_id"], status=saved["status"])
                if saved["quality"]["close_pending"]:
                    result["retry_at"] = (now + timedelta(seconds=self.config.retry_seconds)).isoformat()
            except Exception as error:
                uncertain = isinstance(error, QuoteWriteError) and error.commit_uncertain
                result.update(status="commit_uncertain" if uncertain else "failed", error_type=type(error).__name__,
                              message=str(error) if isinstance(error, (QuoteFetchError, QuoteWriteError)) else "商品更新失敗，既有行情保留",
                              batch_id=error.batch_id if isinstance(error, QuoteWriteError) else None,
                              retry_at=(now + timedelta(seconds=self.config.retry_seconds)).isoformat())
                if not uncertain:
                    try:
                        (repository or self.repository_factory()).record_failure(product, plan.key, now, type(error).__name__, coverage)
                    except Exception:
                        result["database_failure_log"] = "unavailable_local_log_retained"
                logger.warning("外部報價更新失敗 product=%s type=%s", key, type(error).__name__)
            state = self._record(state, key, result, terminal=True)
            results.append(dict(product=key, **result))
        return dict(status="checked", items=results)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as error:
                logger.warning("外部報價排程異常（%s）", type(error).__name__)
            self.stop_event.wait(15)

    def start(self):
        if self.config.enabled and self.thread is None:
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._run, name="external-market-quotes", daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
