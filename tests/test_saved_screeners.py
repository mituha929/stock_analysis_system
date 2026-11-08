import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from app.main import app
from app.api.screener import scan_lock
from app.schemas.saved_screener import SavedScreenerCreate, SavedScreenerUpdate
from app.services.alert_service import AlertService
from app.services.saved_screener_store import SavedScreenerStore, StrategyConflict, InvalidStoredStrategy
from test_screener import FakeRepository
from test_rules import request


def definition(**kwargs):
    return SavedScreenerCreate.model_validate({"name": "站上均線", "description": "測試策略", "configuration": {"universe": {"kind": "symbols", "symbols": ["2330"]}, "rule": {"field": "price", "operator": ">", "value": {"field": "ma20"}}}, **kwargs})


class SavedScreenerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "local.db"
        self.clock = 1000
        self.alerts = AlertService(self.path, lambda a: None, lambda: self.clock)
        self.store = SavedScreenerStore(self.alerts)

    def test_roundtrip_defaults_restart_and_no_alerts(self):
        created = self.store.create(definition())
        reopened = SavedScreenerStore(AlertService(self.path, lambda a: None))
        self.assertEqual(reopened.get(created["id"]), created)
        self.assertEqual(created["configuration"]["history_bars"], 360)
        self.assertEqual(len(created["configuration"]["sort"]), 3)
        self.assertEqual(created["revision"], 1)
        self.assertEqual(self.alerts.list(), [])
        self.assertEqual(self.alerts.events(), [])

    def test_update_version_and_created_time(self):
        created = self.store.create(definition())
        self.clock += 5
        update = SavedScreenerUpdate.model_validate({**definition(name="新策略").model_dump(mode="json"), "expected_revision": 1})
        changed = self.store.update(created["id"], update)
        self.assertEqual(changed["revision"], 2)
        self.assertEqual(changed["created_at"], created["created_at"])
        self.assertNotEqual(changed["updated_at"], created["updated_at"])
        with self.assertRaises(StrategyConflict):
            self.store.update(created["id"], update)
        self.assertEqual(self.store.get(created["id"])["name"], "新策略")

    def test_same_names_have_independent_ids_and_pagination(self):
        first = self.store.create(definition())
        self.clock += 1
        second = self.store.create(definition())
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.store.list(1)["items"][0]["id"], second["id"])
        self.assertEqual(self.store.list(1, 1)["items"][0]["id"], first["id"])
        self.assertEqual(self.store.list()["total"], 2)

    def test_delete_isolated_from_other_strategies(self):
        first, second = self.store.create(definition()), self.store.create(definition())
        self.store.delete(first["id"])
        with self.assertRaises(KeyError):
            self.store.get(first["id"])
        self.assertEqual(self.store.get(second["id"]), second)
        with self.assertRaises(KeyError):
            self.store.delete(first["id"])

    def test_invalid_definition_rejected(self):
        for change in ({"name": "  "}, {"configuration": {"rule": {"field": "invented", "operator": ">", "value": 0}}}, {"configuration": {"rule": {"field": "price", "operator": ">", "value": 0}, "sort": []}}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                definition(**change)
        self.assertEqual(definition(name="  中文策略  ").name, "中文策略")

    def test_corrupt_stored_configuration_not_silently_executed(self):
        created = self.store.create(definition())
        with self.alerts.connection() as db:
            db.execute("UPDATE saved_screeners SET configuration=? WHERE id=?", ('{"rule":null}', created["id"]))
        with self.assertRaises(InvalidStoredStrategy):
            self.store.get(created["id"])

    def install(self):
        app.state.saved_screener_store = self.store
        self.addCleanup(lambda: delattr(app.state, "saved_screener_store"))

    def test_http_crud_validation_and_conflict(self):
        self.install()
        status, created = asyncio.run(request("POST", "/api/v1/screeners", definition().model_dump(mode="json")))
        self.assertEqual(status, 201)
        path = "/api/v1/screeners/" + created["id"]
        self.assertEqual(asyncio.run(request("GET", path))[1], created)
        self.assertEqual(asyncio.run(request("GET", "/api/v1/screeners"))[1]["total"], 1)
        update = {**definition().model_dump(mode="json"), "expected_revision": 1}
        self.assertEqual(asyncio.run(request("PUT", path, update))[1]["revision"], 2)
        self.assertEqual(asyncio.run(request("PUT", path, update))[0], 409)
        self.assertEqual(asyncio.run(request("POST", "/api/v1/screeners", {"name": "missing config"}))[0], 422)
        self.assertEqual(asyncio.run(request("POST", "/api/v1/screeners/unknown/run"))[0], 404)

    def test_run_saved_config_through_existing_pipeline(self):
        self.install()
        created = self.store.create(definition())
        with patch("app.api.screener.ScreenerRepository") as factory:
            factory.return_value.__enter__.return_value = FakeRepository()
            status, result = asyncio.run(request("POST", f"/api/v1/screeners/{created['id']}/run"))
        self.assertEqual(status, 200)
        self.assertEqual(result["items"][0]["symbol"], "2330")
        self.assertEqual(result["saved_screener"], created)
        self.assertEqual(self.store.get(created["id"]), created)

    def test_saved_watchlist_uses_current_list_each_run(self):
        self.install()
        body = definition().model_dump(mode="json")
        body["configuration"]["universe"] = {"kind": "watchlist"}
        created = self.store.create(SavedScreenerCreate.model_validate(body))
        symbols = ["2330"]
        with patch.object(app.state, "capital_service", SimpleNamespace(watchlist=lambda: symbols), create=True), patch("app.api.screener.ScreenerRepository") as factory:
            factory.return_value.__enter__.return_value = FakeRepository()
            path = f"/api/v1/screeners/{created['id']}/run"
            self.assertEqual(asyncio.run(request("POST", path))[1]["universe_count"], 1)
            symbols.clear()
            self.assertEqual(asyncio.run(request("POST", path))[1]["universe_count"], 0)

    def test_run_uses_shared_scan_lock(self):
        self.install()
        created = self.store.create(definition())
        scan_lock.acquire()
        try:
            self.assertEqual(asyncio.run(request("POST", f"/api/v1/screeners/{created['id']}/run"))[0], 409)
        finally:
            scan_lock.release()


if __name__ == "__main__":
    unittest.main()
