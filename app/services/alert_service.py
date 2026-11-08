"""Persistent edge-triggered alerts. SQLite commits state and events together."""
import hashlib
import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.encoders import jsonable_encoder

from app.schemas.alerts import AlertCreate
from app.services.notification_service import initialize_notifications, insert_event_notification

logger = logging.getLogger(__name__)


def utc_text(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


class AlertService:
    def __init__(self, path, evaluator, clock=time.time):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.evaluator = evaluator
        self.clock = clock
        self.stop_event = threading.Event()
        self.thread = None
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, version INTEGER NOT NULL, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS alert_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, alert_id TEXT NOT NULL, data TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS alert_events_alert ON alert_events(alert_id, sequence)")
            initialize_notifications(db)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _read(self, db, alert_id):
        row = db.execute("SELECT version, data FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if row is None:
            raise KeyError(alert_id)
        return row[0], json.loads(row[1])

    def _save(self, db, alert):
        db.execute("UPDATE alerts SET version=version+1, data=? WHERE id=?", (json.dumps(alert, ensure_ascii=False, allow_nan=False), alert["id"]))

    def list(self):
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM alerts ORDER BY id")]

    def get(self, alert_id):
        with self.connection() as db:
            return self._read(db, alert_id)[1]

    def create(self, payload: AlertCreate):
        now = self.clock()
        alert = {"id": str(uuid4()), "config": payload.model_dump(mode="json"), "state": "WAITING", "last_match": None,
                 "cooldown_until": None, "last_observation": None, "last_data_at": None, "previous_values": {},
                 "last_result": None, "last_error": None, "last_checked_at": None, "last_triggered_at": None,
                 "trigger_count": 0, "next_check_at": now, "created_at": utc_text(now), "updated_at": utc_text(now)}
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] >= 100:
                raise ValueError("最多支援 100 個提醒")
            db.execute("INSERT INTO alerts VALUES (?, 0, ?)", (alert["id"], json.dumps(alert, ensure_ascii=False)))
        return alert

    def change(self, alert_id, *, payload=None, enabled=None, reset=False):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            _, alert = self._read(db, alert_id)
            if payload is not None:
                alert["config"] = payload.model_dump(mode="json")
            if enabled is not None:
                if alert["config"]["enabled"] == enabled:
                    return alert
                alert["config"]["enabled"] = enabled
            # Editing/resuming/resetting starts a new baseline, never an artificial edge.
            alert.update(last_match=None, last_observation=None, last_data_at=None, previous_values={}, last_result=None, last_error=None,
                         state="COOLDOWN" if (alert["cooldown_until"] or 0) > self.clock() else "WAITING",
                         next_check_at=self.clock(), updated_at=utc_text(self.clock()))
            self._save(db, alert)
            return alert

    def delete(self, alert_id):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._read(db, alert_id)
            db.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
            # Trigger history remains available through the global events endpoint.

    def events(self, alert_id=None, limit=100):
        with self.connection() as db:
            if alert_id is None:
                rows = db.execute("SELECT data FROM alert_events ORDER BY sequence DESC LIMIT ?", (limit,))
            else:
                rows = db.execute("SELECT data FROM alert_events WHERE alert_id=? ORDER BY sequence DESC LIMIT ?", (alert_id, limit))
            return [json.loads(row[0]) for row in rows]

    def check(self, alert_id, force=False):
        with self.connection() as db:
            version, alert = self._read(db, alert_id)
        now = self.clock()
        if not alert["config"]["enabled"] or (not force and alert["next_check_at"] > now):
            return {"alert": alert, "event": None, "skipped": "disabled_or_not_due"}
        observation, error = None, None
        try:
            observation = jsonable_encoder(self.evaluator(alert))
            json.dumps(observation, allow_nan=False)
        except Exception as exc:
            error = str(getattr(exc, "detail", exc))
        # Never hold a database write lock during the external data fetch.
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                current_version, _ = self._read(db, alert_id)
            except KeyError:
                return {"alert": None, "event": None, "skipped": "deleted"}
            if current_version != version:
                return {"alert": self._read(db, alert_id)[1], "event": None, "skipped": "concurrent_change"}
            now = self.clock()
            cooling = (alert["cooldown_until"] or 0) > now
            if alert["state"] == "TRIGGERED":
                alert["state"] = "COOLDOWN"
            if not cooling and alert["state"] == "COOLDOWN":
                alert["state"] = "RESET"
            if alert["state"] == "RESET" and alert["last_match"] is False:
                alert["state"] = "WAITING"
            alert.update(last_checked_at=utc_text(now), next_check_at=now + alert["config"]["check_interval_seconds"], last_error=error)
            event = None
            if observation is not None and error is None:
                result = observation["result"]
                data_at = observation["data_at"]
                fingerprint = observation.get("key") or hashlib.sha256(json.dumps(observation, sort_keys=True).encode()).hexdigest()
                if alert["last_data_at"] is not None and data_at < alert["last_data_at"]:
                    alert["last_error"] = "忽略時間倒退的資料"
                elif fingerprint != alert["last_observation"]:
                    alert.update(last_result=result, last_data_at=data_at, last_observation=fingerprint)
                    if observation.get("values") is not None:
                        alert["previous_values"] = observation["values"]
                    if result["status"] != "insufficient_data":
                        matched = result["matched"]
                        if alert["last_match"] is False and matched and not cooling:
                            alert["state"] = "TRIGGERED"
                            alert["cooldown_until"] = now + alert["config"]["cooldown_seconds"]
                            alert["last_triggered_at"] = utc_text(now)
                            alert["trigger_count"] += 1
                            event = {"id": str(uuid4()), "alert_id": alert_id, "name": alert["config"]["name"], "symbol": alert["config"]["symbol"],
                                     "triggered_at": utc_text(now), "data_at": data_at, "source": alert["config"]["source"], "result": result}
                            db.execute("INSERT INTO alert_events(alert_id, data) VALUES (?, ?)", (alert_id, json.dumps(event, ensure_ascii=False)))
                            insert_event_notification(db, event)
                        elif not matched and not cooling:
                            alert["state"] = "WAITING"
                        alert["last_match"] = matched
            self._save(db, alert)
            return {"alert": alert, "event": event, "skipped": None}

    def run_due(self):
        for alert in self.list():
            if self.stop_event.is_set():
                break
            if alert["config"]["enabled"] and alert["next_check_at"] <= self.clock():
                try:
                    self.check(alert["id"])
                except KeyError:
                    pass
                except Exception:
                    logger.exception("Alert check failed: %s", alert["id"])

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        def work():
            while not self.stop_event.is_set():
                try:
                    self.run_due()
                except Exception:
                    logger.exception("Alert scheduler failed")
                self.stop_event.wait(1)
        self.thread = threading.Thread(target=work, name="alert-monitor", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()
