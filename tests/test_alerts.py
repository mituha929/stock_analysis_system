import asyncio
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from app.main import app, lifespan
from app.rules.engine import evaluate
from app.schemas.alerts import AlertCreate
from app.schemas.models import Quote
from app.services.alert_observation import observe_alert
from app.services.alert_service import AlertService
from app.services.quote_cache import QuoteCache
from test_rules import request
import test_rules


def config(**kwargs):
    return AlertCreate.model_validate({"name": "價格提醒", "symbol": "2330", "rule": {"field": "price", "operator": ">", "value": 100}, "cooldown_seconds": 10, **kwargs})


class AlertStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "alerts.db"
        self.now = 1000
        self.sample = 1
        self.matched = False
        self.error = None
        self.service = AlertService(self.path, self.observe, lambda: self.now)
        self.alert = self.service.create(config())

    def observe(self, alert):
        if self.error:
            raise ValueError(self.error)
        return {"key": str(self.sample), "data_at": self.sample, "result": {"matched": self.matched is True, "status": "insufficient_data" if self.matched is None else "matched" if self.matched else "not_matched"}}

    def check(self, matched, seconds=1, fresh=True):
        self.matched = matched
        self.now += seconds
        if fresh:
            self.sample += 1
        return self.service.check(self.alert["id"], force=True)

    def trigger(self):
        self.check(False)
        return self.check(True)

    def test_first_true_only_establishes_baseline(self):
        self.assertIsNone(self.check(True)["event"])
        self.assertEqual(self.service.events(), [])
        self.check(False)
        self.assertIsNotNone(self.check(True)["event"])

    def test_edge_cooldown_and_persistent_true(self):
        result = self.trigger()
        self.assertEqual(result["alert"]["state"], "TRIGGERED")
        self.assertEqual(self.check(True)["alert"]["state"], "COOLDOWN")
        self.assertIsNone(self.check(True, seconds=20)["event"])
        self.assertEqual(self.service.get(self.alert["id"])["state"], "RESET")
        self.assertEqual(self.check(False)["alert"]["state"], "WAITING")
        self.assertIsNotNone(self.check(True)["event"])
        self.assertEqual(len(self.service.events()), 2)

    def test_cooldown_edges_are_consumed_not_delayed(self):
        self.trigger()
        self.check(False)
        self.assertIsNone(self.check(True)["event"])
        self.assertIsNone(self.check(True, seconds=20)["event"])
        self.check(False)
        self.assertIsNotNone(self.check(True)["event"])

    def test_false_during_cooldown_rearms_after_expiry(self):
        self.trigger()
        self.check(False)
        self.assertIsNotNone(self.check(True, seconds=20)["event"])

    def test_unknown_does_not_create_false_edge(self):
        self.trigger()
        self.check(None, seconds=20)
        self.assertIsNone(self.check(True)["event"])
        self.check(False)
        self.check(None)
        self.assertIsNotNone(self.check(True)["event"])

    def test_errors_preserve_state_and_recover(self):
        self.check(False)
        self.error = "database unavailable"
        result = self.check(True)
        self.assertEqual(result["alert"]["last_error"], self.error)
        self.assertFalse(result["alert"]["last_match"])
        self.error = None
        result = self.check(True)
        self.assertIsNotNone(result["event"])
        self.assertIsNone(result["alert"]["last_error"])

    def test_duplicate_sample_not_evaluated_as_new_edge(self):
        self.check(False)
        self.assertIsNone(self.check(True, fresh=False)["event"])
        self.assertFalse(self.service.get(self.alert["id"])["last_match"])

    def test_backwards_sample_rejected(self):
        self.check(False)
        self.sample = 0
        result = self.check(True, fresh=False)
        self.assertIsNone(result["event"])
        self.assertIn("時間倒退", result["alert"]["last_error"])

    def test_restart_does_not_repeat_trigger(self):
        self.trigger()
        self.service = AlertService(self.path, self.observe, lambda: self.now)
        self.assertIsNone(self.check(True, seconds=30)["event"])
        self.assertEqual(len(self.service.events()), 1)
        self.assertEqual(self.service.get(self.alert["id"])["trigger_count"], 1)

    def test_disable_resume_and_reset_baseline(self):
        self.check(False)
        self.service.change(self.alert["id"], enabled=False)
        self.assertEqual(self.check(True)["skipped"], "disabled_or_not_due")
        self.service.change(self.alert["id"], enabled=True)
        self.assertIsNone(self.check(True)["event"])
        self.check(False)
        self.check(True)
        deadline = self.service.get(self.alert["id"])["cooldown_until"]
        self.service.change(self.alert["id"], reset=True)
        self.assertEqual(self.service.get(self.alert["id"])["cooldown_until"], deadline)
        self.assertIsNone(self.check(True, seconds=20)["event"])

    def test_update_rebaselines_and_retains_history(self):
        self.trigger()
        changed = self.service.change(self.alert["id"], payload=config(name="修改"))
        self.assertEqual(changed["config"]["name"], "修改")
        self.assertIsNone(changed["last_match"])
        self.assertEqual(changed["trigger_count"], 1)

    def test_delete_retains_event_history(self):
        self.trigger()
        self.service.delete(self.alert["id"])
        self.assertEqual(self.service.list(), [])
        self.assertEqual(len(self.service.events(self.alert["id"])), 1)

    def test_concurrent_checks_commit_only_one_event(self):
        self.check(False)
        barrier = threading.Barrier(2)
        def observe(alert):
            barrier.wait(timeout=5)
            return {"key": "new", "data_at": 10, "result": {"matched": True, "status": "matched"}}
        self.service.evaluator = observe
        other = AlertService(self.path, observe, lambda: self.now)
        outputs = []
        threads = [threading.Thread(target=lambda s=s: outputs.append(s.check(self.alert["id"], force=True))) for s in (self.service, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(6)
        self.assertEqual(len(outputs), 2)
        self.assertEqual(len(self.service.events()), 1)
        self.assertEqual(sum(o["event"] is not None for o in outputs), 1)

    def test_concurrent_disable_cancels_inflight_trigger(self):
        self.check(False)
        def observe(alert):
            self.service.change(alert["id"], enabled=False)
            return {"data_at": 10, "result": {"matched": True, "status": "matched"}}
        self.service.evaluator = observe
        self.assertEqual(self.service.check(self.alert["id"], force=True)["skipped"], "concurrent_change")
        self.assertEqual(self.service.events(), [])

    def test_due_interval_and_scheduler_shutdown(self):
        self.service.run_due()
        checked = self.service.get(self.alert["id"])["last_checked_at"]
        self.now += 1
        self.service.run_due()
        self.assertEqual(self.service.get(self.alert["id"])["last_checked_at"], checked)
        self.service.start()
        self.service.stop()
        self.assertFalse(self.service.thread.is_alive())


class AlertAdapterTests(unittest.TestCase):
    def test_quote_cross_sample_identity_and_freshness(self):
        cache = QuoteCache()
        capital = SimpleNamespace(quote_ready=True, cache=cache)
        payload = config(source="quote", rule={"field": "price", "operator": "cross_above", "value": 100})
        alert = {"config": payload.model_dump(mode="json"), "previous_values": {"price": 99}}
        cache.upsert_quote(Quote(symbol="2330", price=101, volume=100, prev_close=100, updated_at=datetime.fromtimestamp(1000, timezone.utc)))
        first = observe_alert(alert, capital, 1001)
        self.assertTrue(first["result"]["matched"])
        alert["previous_values"] = first["values"]
        second = observe_alert(alert, capital, 1002)
        self.assertEqual(first["key"], second["key"])
        self.assertFalse(second["result"]["matched"])
        with self.assertRaises(ValueError):
            observe_alert(alert, capital, 1100)
        capital.quote_ready = False
        with self.assertRaises(ValueError):
            observe_alert(alert, capital, 1001)

    def test_mysql_adapter_reuses_v2_and_marks_storage_limit(self):
        response = test_rules.RuleContextAndAPITests().response()
        with patch("app.services.alert_observation.analysis", return_value=response):
            result = observe_alert({"config": config().model_dump(mode="json")}, None, 0)
        self.assertTrue(result["result"]["matched"])
        self.assertIn("已儲存", result["result"]["warnings"][-1])

    def test_quote_rejects_technical_fields_and_invalid_config(self):
        for kwargs in ({"source": "quote", "rule": {"field": "ma20", "operator": ">", "value": 100}}, {"name": " "}, {"check_interval_seconds": 0}, {"cooldown_seconds": -1}, {"enabled": "false"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                config(**kwargs)


class AlertAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = AlertService(Path(self.temp.name) / "alerts.db", lambda alert: {"data_at": 1, "result": {"matched": False, "status": "not_matched"}})
        app.state.alert_service = self.service
        self.addCleanup(lambda: delattr(app.state, "alert_service"))

    def test_http_create_check_manage_and_history(self):
        status, alert = asyncio.run(request("POST", "/api/v1/alerts", config().model_dump(mode="json")))
        self.assertEqual(status, 201)
        base = "/api/v1/alerts/" + alert["id"]
        for method, path, body in (("GET", base, None), ("POST", base + "/check", {}), ("PATCH", base + "/enabled", {"enabled": False}), ("PUT", base, config(name="新版").model_dump(mode="json")), ("POST", base + "/reset", {}), ("GET", "/api/v1/alerts/events", None), ("GET", "/api/v1/alerts", None)):
            self.assertEqual(asyncio.run(request(method, path, body))[0], 200)
        self.service.delete(alert["id"])
        self.assertEqual(asyncio.run(request("GET", base))[0], 404)

    def test_http_validation_and_check_error_visibility(self):
        status, _ = asyncio.run(request("POST", "/api/v1/alerts", {**config().model_dump(mode="json"), "cooldown_seconds": -1}))
        self.assertEqual(status, 422)
        alert = self.service.create(config())
        self.service.evaluator = lambda a: (_ for _ in ()).throw(ValueError("資料未就緒"))
        status, result = asyncio.run(request("POST", f"/api/v1/alerts/{alert['id']}/check"))
        self.assertEqual(status, 200)
        self.assertEqual(result["alert"]["last_error"], "資料未就緒")
        self.assertIsNone(result["event"])

    def test_lifespan_starts_and_stops_monitor(self):
        async def run():
            with patch("app.main.CapitalQuoteService") as capital, patch.dict("os.environ", {"CAPITAL_ALERT_DB": str(Path(self.temp.name) / "lifespan.db")}):
                async with lifespan(app):
                    monitor = app.state.alert_service
                    self.assertTrue(monitor.thread.is_alive())
                self.assertFalse(monitor.thread.is_alive())
                capital.return_value.stop.assert_called_once()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
