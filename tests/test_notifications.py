import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.main import app
from app.services.alert_service import AlertService
from app.services.notification_service import NotificationService, insert_event_notification
from test_alerts import config
from test_rules import request


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "alerts.db"
        self.now = 1000
        self.matched = False
        self.alerts = AlertService(self.path, lambda a: {"data_at": self.now, "result": {"matched": self.matched, "status": "matched" if self.matched else "not_matched", "reasons": ["價格高於門檻"]}}, lambda: self.now)
        self.store = NotificationService(self.alerts)

    def trigger(self):
        alert = self.alerts.create(config())
        self.alerts.check(alert["id"], force=True)
        self.now += 1
        self.matched = True
        return self.alerts.check(alert["id"], force=True)["event"]

    def test_event_creates_notification_with_reason(self):
        event = self.trigger()
        result = self.store.list()
        self.assertEqual(result["unread_count"], 1)
        self.assertEqual(result["items"][0]["event_id"], event["id"])
        self.assertEqual(result["items"][0]["message"], "價格高於門檻")
        self.assertEqual(result["items"][0]["desktop_status"], "pending")

    def test_event_notification_atomic_rollback(self):
        alert = self.alerts.create(config())
        self.alerts.check(alert["id"], force=True)
        self.now += 1
        self.matched = True
        with patch("app.services.alert_service.insert_event_notification", side_effect=RuntimeError("disk failure")), self.assertRaises(RuntimeError):
            self.alerts.check(alert["id"], force=True)
        self.assertEqual(self.alerts.events(), [])
        self.assertEqual(self.alerts.get(alert["id"])["trigger_count"], 0)
        self.assertIsNotNone(self.alerts.check(alert["id"], force=True)["event"])
        self.assertEqual(self.store.list()["unread_count"], 1)

    def test_restart_and_duplicate_event_do_not_duplicate_or_reset_read(self):
        event = self.trigger()
        item = self.store.list()["items"][0]
        self.store.mark(item["id"], True)
        with self.alerts.connection() as db:
            insert_event_notification(db, event)
        restarted = NotificationService(AlertService(self.path, lambda a: None))
        self.assertEqual(len(restarted.list()["items"]), 1)
        self.assertEqual(restarted.list()["unread_count"], 0)

    def test_legacy_backfill_does_not_dispatch_old_events(self):
        self.trigger()
        with self.alerts.connection() as db:
            db.execute("DROP TABLE notifications")
        restarted = NotificationService(AlertService(self.path, lambda a: None, lambda: self.now))
        self.assertEqual(restarted.list()["items"][0]["desktop_status"], "skipped_history")
        self.assertIsNone(restarted.claim()["notification"])

    def test_read_unread_and_missing(self):
        item = self.store.create_test()
        self.store.mark(item["id"], True)
        self.assertEqual(self.store.list(unread_only=True)["items"], [])
        self.store.mark(item["id"], False)
        self.assertEqual(self.store.list()["unread_count"], 1)
        with self.assertRaises(KeyError):
            self.store.mark("missing", True)

    def test_pagination_and_mark_all_preserves_new_arrivals(self):
        for _ in range(4):
            self.store.create_test()
        page = self.store.list(limit=2)
        second = self.store.list(before=page["next_before"], limit=2)
        self.assertEqual(len({i["id"] for i in page["items"] + second["items"]}), 4)
        self.assertIsNone(second["next_before"])
        new = self.store.create_test()
        self.store.mark_all(page["items"][0]["sequence"])
        self.assertEqual([i["id"] for i in self.store.list(unread_only=True)["items"]], [new["id"]])

    def test_claim_concurrency_at_most_once(self):
        self.store.create_test()
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.store.claim())) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sum(r["notification"] is not None for r in results), 1)
        restarted = NotificationService(AlertService(self.path, lambda a: None))
        self.assertIsNone(restarted.claim()["notification"])

    def test_ack_token_and_failure_are_persistent(self):
        item = self.store.create_test()
        claim = self.store.claim()
        self.assertFalse(self.store.acknowledge(item["id"], "wrong", "submitted")["accepted"])
        self.assertTrue(self.store.acknowledge(item["id"], claim["claim_token"], "failed", "permission")["accepted"])
        self.assertFalse(self.store.acknowledge(item["id"], claim["claim_token"], "submitted")["accepted"])
        self.assertEqual(self.store.list()["items"][0]["desktop_error"], "permission")
        self.assertIsNone(self.store.claim()["notification"])

    def test_expired_and_read_items_not_claimed(self):
        self.store.create_test()
        self.now += 301
        self.assertIsNone(self.store.claim()["notification"])
        self.assertEqual(self.store.list()["items"][0]["desktop_status"], "expired")
        item = self.store.create_test()
        self.store.mark(item["id"], True)
        self.assertIsNone(self.store.claim()["notification"])

    def test_test_notification_does_not_create_alert_or_event(self):
        self.assertEqual(self.store.create_test()["kind"], "test")
        self.assertEqual(self.alerts.list(), [])
        self.assertEqual(self.alerts.events(), [])

    def test_http_workflow_and_validation(self):
        app.state.alert_service = self.alerts
        self.addCleanup(lambda: delattr(app.state, "alert_service"))
        status, item = asyncio.run(request("POST", "/api/v1/notifications/test"))
        self.assertEqual(status, 201)
        status, listing = asyncio.run(request("GET", "/api/v1/notifications"))
        self.assertEqual(listing["unread_count"], 1)
        status, claim = asyncio.run(request("POST", "/api/v1/notifications/desktop/claim"))
        self.assertEqual(status, 200)
        status, ack = asyncio.run(request("POST", f"/api/v1/notifications/{item['id']}/desktop-ack", {"claim_token": claim["claim_token"], "status": "submitted"}))
        self.assertTrue(ack["accepted"])
        status, _ = asyncio.run(request("PATCH", f"/api/v1/notifications/{item['id']}/read", {"read": True}))
        self.assertEqual(status, 200)
        self.assertEqual(asyncio.run(request("PATCH", "/api/v1/notifications/missing/read", {"read": True}))[0], 404)
        self.assertEqual(asyncio.run(request("POST", "/api/v1/notifications/read-all", {"through_sequence": 0}))[0], 422)
        self.assertEqual(asyncio.run(request("PATCH", f"/api/v1/notifications/{item['id']}/read", {"read": "true"}))[0], 422)


if __name__ == "__main__":
    unittest.main()
