import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import MagicMock, patch

from app.services.capital_process_lock import CapitalProcessLock
from app.services.external_market_quote_repository import QuoteWriteError
from app.services.external_market_quotes import QuoteFetchError, fetch_one
from app.services.external_market_quote_scheduler import ExternalQuoteSchedule, ExternalMarketQuoteScheduler
from app.services.external_market_sessions import PRODUCTS, ExternalMarketSessions, product_key, quote_quality
from test_external_market_quotes import batch, index_payload, cm_html

NOW = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)  # NY Monday 10:00, Taipei 23:00.


def utc(text):
    return datetime.fromisoformat(text).astimezone(timezone.utc)


class ExternalSessionTests(unittest.TestCase):
    def setUp(self):
        self.sessions = ExternalMarketSessions()

    def test_us_dst_and_winter_open_in_taipei(self):
        summer = self.sessions.session("NYSE", NOW.date().replace(month=7, day=6))
        winter = self.sessions.session("NYSE", NOW.date())
        taipei = timezone(timedelta(hours=8))
        self.assertEqual(summer[0].astimezone(taipei).strftime("%H:%M"), "21:30")
        self.assertEqual(winter[0].astimezone(taipei).strftime("%H:%M"), "22:30")

    def test_us_holiday_early_close_and_taiwan_holiday_are_independent(self):
        self.assertIsNone(self.sessions.session("NYSE", NOW.date().replace(month=11, day=26)))
        self.assertEqual(self.sessions.session("NYSE", NOW.date().replace(month=11, day=27))[1].hour, 13)
        self.assertIsNotNone(self.sessions.session("NYSE", NOW.date().replace(month=10, day=23)))
        self.assertIsNone(self.sessions.session("TAIFEX", NOW.date().replace(month=10, day=23)))

    def test_friday_night_includes_saturday_early_morning(self):
        now = utc("2026-11-07T02:30:00+08:00")
        for product in (PRODUCTS[0], PRODUCTS[-1]):
            plan = self.sessions.plan(product, now)
            self.assertEqual(plan.phase, "intraday")
            self.assertTrue(plan.session_key.endswith("2026-11-06"))

    def test_lunar_new_year_last_night_is_retained(self):
        plan = self.sessions.plan(PRODUCTS[-1], utc("2026-02-12T02:30:00+08:00"))
        self.assertEqual(plan.phase, "intraday")
        self.assertEqual(plan.session_key, "TAIFEX:2026-02-11")
        self.assertIsNone(self.sessions.session("TAIFEX", NOW.date().replace(month=2, day=12)))

    def test_close_buffer_and_unknown_year(self):
        before = self.sessions.plan(PRODUCTS[0], utc("2026-11-03T05:10:00+08:00"))
        after = self.sessions.plan(PRODUCTS[0], utc("2026-11-03T05:20:00+08:00"))
        self.assertEqual(before.phase, "close_wait")
        self.assertEqual(after.phase, "close")
        with self.assertRaises(ValueError):
            self.sessions.plan(PRODUCTS[-1], utc("2027-01-04T16:00:00+08:00"))

    def test_unknown_stale_delayed_and_future_are_distinct(self):
        plan = self.sessions.plan(PRODUCTS[0], NOW)
        self.assertEqual(quote_quality(None, NOW, plan)["freshness"], "unknown_time")
        self.assertEqual(quote_quality(plan.opens-timedelta(seconds=1), NOW, plan)["freshness"], "stale")
        later = NOW + timedelta(hours=2)
        self.assertEqual(quote_quality(NOW, later, plan)["freshness"], "delayed")
        self.assertEqual(quote_quality(NOW+timedelta(hours=1), NOW, plan)["freshness"], "future_time")
        self.assertTrue(quote_quality(NOW, NOW, plan)["usable_for_expected_session"])

    def test_weekend_latest_session_is_not_today_or_official_close(self):
        now = utc("2026-11-02T00:00:00+08:00")
        plan = self.sessions.plan(PRODUCTS[0], now)
        quality = quote_quality(plan.closes+timedelta(minutes=5), now, plan)
        self.assertEqual(quality["freshness"], "latest_session")
        self.assertEqual(quality["market_state"], "closed")
        self.assertIsNone(quality["source_trading_date"])
        self.assertFalse(quality["official_close_confirmed"])

    def test_close_pending_bounded_and_unknown_time_not_retried_forever(self):
        now = utc("2026-11-03T05:20:00+08:00")
        plan = self.sessions.plan(PRODUCTS[0], now)
        self.assertTrue(quote_quality(plan.closes-timedelta(minutes=2), now, plan)["close_pending"])
        self.assertFalse(quote_quality(plan.closes-timedelta(minutes=2), now+timedelta(hours=2), plan)["close_pending"])
        self.assertFalse(quote_quality(None, now, plan)["close_pending"])

    def test_fetch_one_does_not_request_other_products(self):
        reader = MagicMock(return_value=index_payload())
        self.assertEqual(fetch_one(*PRODUCTS[0], reader=reader).symbol, "DJI")
        reader.assert_called_once()
        with self.assertRaises(QuoteFetchError):
            fetch_one("invalid", "DJI", "index", reader=reader)


class ExternalQuoteScheduleTests(unittest.TestCase):
    def test_env_validation_and_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            config = ExternalQuoteSchedule.from_env()
        self.assertFalse(config.enabled)
        self.assertEqual(config.interval_minutes, 30)
        self.assertEqual(config.close_delay_minutes, 20)
        for key, value in (("ENABLED", "maybe"), ("INTERVAL_MINUTES", "7"), ("RETRY_SECONDS", "0"), ("CLOSE_DELAY_MINUTES", "91")):
            with self.subTest(key=key), patch.dict(os.environ, {"CAPITAL_EXTERNAL_QUOTES_"+key: value}, clear=True):
                with self.assertRaises(ValueError):
                    ExternalQuoteSchedule.from_env()
        self.assertEqual(config.slot(NOW).strftime("%H:%M"), "23:00")


class ExternalQuoteSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = ExternalQuoteSchedule(enabled=True, state_file=Path(self.temp.name)/"schedule.json")
        self.quotes = {product_key((q.source,q.symbol,q.session)): replace(q, fetched_at=NOW, quote_at=None if q.source=="cmoney" else NOW-timedelta(seconds=60)) for q in batch()}
        self.fetcher = MagicMock(side_effect=lambda *p: self.quotes[product_key(p)])
        self.repo = MagicMock()
        self.repo.save_observation.side_effect = lambda q,k,v: dict(status="updated", snapshot_id=1, batch_id="a"*32, quality=v)
        self.factory = MagicMock(return_value=self.repo)

    def service(self, **kwargs):
        return ExternalMarketQuoteScheduler(self.config, fetcher=self.fetcher, repository_factory=self.factory, **kwargs)

    def items(self, result):
        return {r["product"]:r for r in result["items"]}

    def test_disabled_no_files_or_io(self):
        service = ExternalMarketQuoteScheduler(replace(self.config,enabled=False),fetcher=self.fetcher)
        self.assertEqual(service.tick(NOW)["status"], "disabled")
        service.start()
        self.assertIsNone(service.thread)
        self.fetcher.assert_not_called()
        self.assertFalse(self.config.state_file.exists())

    def test_individual_success_and_restart_no_duplicate(self):
        service = self.service()
        self.assertTrue(all(r["status"]=="updated" for r in service.tick(NOW)["items"]))
        self.assertEqual(self.repo.save_observation.call_count,5)
        self.assertTrue(all(r["status"]=="already_completed" for r in self.service().tick(NOW+timedelta(minutes=1))["items"]))
        self.assertEqual(self.fetcher.call_count,5)

    def test_one_source_failure_does_not_block_other_four(self):
        def fetch(*p):
            if p[0]=="cmoney": raise QuoteFetchError("HTTP 403")
            return self.quotes[product_key(p)]
        self.fetcher.side_effect=fetch
        result=self.items(self.service().tick(NOW))
        self.assertEqual(result[product_key(PRODUCTS[-1])]["status"],"failed")
        self.assertEqual(self.repo.save_observation.call_count,4)
        retry=self.items(self.service().tick(NOW+timedelta(minutes=1)))
        self.assertEqual(retry[product_key(PRODUCTS[-1])]["status"],"retry_wait")
        self.assertEqual(self.fetcher.call_count,5)
        self.service().tick(NOW+timedelta(minutes=5))
        self.assertEqual(self.fetcher.call_count,6)

    def test_invalid_product_data_does_not_write_it(self):
        first=product_key(PRODUCTS[0])
        self.quotes[first]=replace(self.quotes[first],low_price=self.quotes[first].high_price+1)
        result=self.items(self.service().tick(NOW))
        self.assertEqual(result[first]["status"],"failed")
        self.assertEqual(self.repo.save_observation.call_count,4)

    def test_new_slot_records_unchanged_check_without_claiming_new_quote(self):
        service=self.service();service.tick(NOW)
        self.repo.save_observation.side_effect=lambda q,k,v:dict(status="unchanged",snapshot_id=1,batch_id="a"*32,quality=v)
        result=service.tick(NOW+timedelta(minutes=30))
        self.assertTrue(all(r["status"]=="unchanged" for r in result["items"]))
        self.assertEqual(self.repo.save_observation.call_count,10)

    def test_weekend_close_checked_once_not_every_half_hour(self):
        service=self.service();now=utc("2026-11-02T00:00:00+08:00")
        service.tick(now)
        self.assertTrue(all(r["status"]=="already_completed" for r in service.tick(now+timedelta(minutes=30))["items"]))
        self.assertEqual(self.fetcher.call_count,5)

    def test_missed_observations_logged_not_fabricated(self):
        service=self.service();service.tick(NOW)
        result=self.items(service.tick(NOW+timedelta(minutes=90)))
        self.assertEqual(result[product_key(PRODUCTS[0])]["missed_observations"],2)
        self.assertEqual(self.fetcher.call_count,10)

    def test_close_wait_then_retry_source_timestamp_only(self):
        service=self.service();before=utc("2026-11-03T05:10:00+08:00")
        self.assertTrue(all(r["status"]=="outside_session" for r in service.tick(before)["items"]))
        self.fetcher.assert_not_called()
        at=before+timedelta(minutes=10)
        result=self.items(service.tick(at))
        self.assertTrue(result[product_key(PRODUCTS[0])]["quality"]["close_pending"])
        self.assertFalse(result[product_key(PRODUCTS[-1])]["quality"]["close_pending"])
        service.tick(at+timedelta(minutes=1));self.assertEqual(self.fetcher.call_count,5)
        service.tick(at+timedelta(minutes=5));self.assertEqual(self.fetcher.call_count,9)
        service.tick(at+timedelta(hours=2));self.assertEqual(self.fetcher.call_count,9)

    def test_close_network_failures_expire(self):
        self.fetcher.side_effect=QuoteFetchError("offline")
        now=utc("2026-11-03T05:20:00+08:00")
        self.service().tick(now)
        result=self.service().tick(now+timedelta(hours=2))
        self.assertTrue(all(r["status"]=="close_retry_expired" for r in result["items"]))
        self.assertEqual(self.fetcher.call_count,5)

    def test_ambiguous_commit_holds_only_that_product(self):
        def save(q,k,v):
            if q.symbol=="DJI": raise QuoteWriteError("b"*32,True,RuntimeError())
            return dict(status="updated",batch_id="a"*32,quality=v)
        self.repo.save_observation.side_effect=save
        self.service().tick(NOW)
        result=self.items(self.service().tick(NOW+timedelta(minutes=6)))
        self.assertEqual(result[product_key(PRODUCTS[0])]["status"],"needs_review")
        self.assertEqual(self.fetcher.call_count,5)

    def test_corrupt_checkpoint_stops_all_io(self):
        self.config.state_file.write_text("bad-json",encoding="utf-8")
        self.assertEqual(self.service().tick(NOW)["status"],"state_error")
        self.fetcher.assert_not_called()

    def test_checkpoint_failure_before_write_stops_fetch(self):
        service=self.service()
        with patch.object(service,"_write_state",side_effect=PermissionError()):
            self.assertEqual(service.tick(NOW)["status"],"state_error")
        self.fetcher.assert_not_called()

    def test_v1_state_migrated_without_deleting_old_history(self):
        self.config.state_file.write_text(json.dumps(dict(version=1,history=[dict(status="completed")],batch_id="old")),encoding="utf-8")
        service=self.service();service.tick(NOW)
        state=service.status(NOW)["state"]
        self.assertEqual(state["version"],2)
        self.assertEqual(state["legacy_state"]["batch_id"],"old")
        self.assertEqual(state["history"][0]["status"],"completed")

    def test_calendar_failure_does_not_delete_existing_success(self):
        service=self.service();service.tick(NOW)
        with patch.object(service.sessions,"plan",side_effect=ValueError("unknown_year")):
            result=service.tick(NOW)
        self.assertTrue(all(r["status"]=="calendar_unverified" for r in result["items"]))
        self.assertEqual(self.fetcher.call_count,5)
        self.assertIsNotNone(service.status(NOW)["state"]["products"][product_key(PRODUCTS[0])]["last_success"])

    def test_same_and_other_instance_cannot_overlap(self):
        service=self.service();entered=threading.Event();finish=threading.Event()
        def fetch(*p):
            entered.set();finish.wait(5);return self.quotes[product_key(p)]
        self.fetcher.side_effect=fetch
        thread=threading.Thread(target=lambda:service.tick(NOW));thread.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertEqual(service.tick(NOW)["status"],"busy")
            self.assertEqual(self.service().tick(NOW)["status"],"busy")
        finally:
            finish.set();thread.join(5)

    def test_dll_lock_does_not_block_quote_checks(self):
        with CapitalProcessLock(Path(self.temp.name)/"capital_skcom.lock").hold(0):
            self.assertEqual(self.service().tick(NOW)["status"],"checked")

    def test_stop_during_fetch_does_not_write(self):
        service=self.service()
        def fetch(*p):service.stop_event.set();return self.quotes[product_key(p)]
        self.fetcher.side_effect=fetch;service.tick(NOW)
        self.repo.save_observation.assert_not_called()

    def test_generic_error_is_sanitized(self):
        self.factory.side_effect=RuntimeError("password=secret")
        result=self.service().tick(NOW)
        self.assertNotIn("secret",json.dumps(result))

    def test_status_recomputes_age_without_network(self):
        service=self.service();service.tick(NOW)
        status=service.status(NOW+timedelta(hours=2))
        self.assertEqual(status["items"][0]["current_quality"]["freshness"],"delayed")
        self.assertEqual(self.fetcher.call_count,5)

    def test_main_lifespan_manages_scheduler(self):
        from app.main import app,lifespan
        async def run():
            with patch("app.main.CapitalQuoteService"),patch("app.main.AlertService"),patch("app.main.DailyBackfillScheduler"),patch("app.main.WeeklyStockMasterScheduler"),patch("app.main.BackfillRecovery"),patch("app.main.ExternalMarketQuoteScheduler") as factory:
                async with lifespan(app):factory.return_value.start.assert_called_once()
                factory.return_value.stop.assert_called_once()
        asyncio.run(run())


class ExternalQuoteAPITests(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from app.api.external_market_quotes import router
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.app=FastAPI();self.app.include_router(router)
        self.app.state.external_market_quote_scheduler=ExternalMarketQuoteScheduler(
            ExternalQuoteSchedule(state_file=Path(self.temp.name)/"state.json"))

    def get(self,path):
        async def run():
            output=[]
            async def receive():return dict(type="http.request",body=b"",more_body=False)
            async def send(message):output.append(message)
            await self.app(dict(type="http",asgi={"version":"3.0"},http_version="1.1",method="GET",scheme="http",
                path=path,raw_path=path.encode(),query_string=b"",headers=[],client=("127.0.0.1",1),server=("test",80),root_path=""),receive,send)
            return output[0]["status"],json.loads(b"".join(m.get("body",b"") for m in output[1:]))
        return asyncio.run(run())

    def test_schedule_is_read_only(self):
        with patch("app.api.external_market_quotes.ExternalMarketQuoteRepository") as repo:
            status,body=self.get("/api/v1/external-market-quotes/schedule")
        self.assertEqual(status,200);self.assertEqual(body["schedule"]["interval_minutes"],30)
        repo.assert_not_called()

    def test_latest_keeps_unknown_source_time_and_check_separate(self):
        now=datetime.now(timezone.utc).replace(tzinfo=None)
        rows=[dict(source="cmoney",symbol="TXF1",session="night",snapshot=dict(quote_at_utc=None,fetched_at_utc=now),
                   last_check=dict(checked_at_utc=now,status="unchanged",quality={}))]
        with patch("app.api.external_market_quotes.ExternalMarketQuoteRepository") as repo:
            repo.return_value.latest.return_value=rows
            status,body=self.get("/api/v1/external-market-quotes/latest")
        self.assertEqual(status,200)
        self.assertEqual(body["items"][0]["current_quality"]["freshness"],"unknown_time")
        self.assertFalse(body["items"][0]["current_quality"]["usable_for_expected_session"])
        self.assertTrue(body["items"][0]["snapshot"]["fetched_at_utc"].endswith("+00:00"))

    def test_database_error_sanitized(self):
        with patch("app.api.external_market_quotes.ExternalMarketQuoteRepository",side_effect=RuntimeError("password=secret")):
            status,body=self.get("/api/v1/external-market-quotes/latest")
        self.assertEqual(status,503);self.assertNotIn("secret",str(body))
