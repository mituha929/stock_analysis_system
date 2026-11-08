from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import copy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from app.services.capital_process_lock import CapitalProcessLock
from app.services.external_market_sessions import ExternalMarketSessions
from app.services.us_stock_daily import (BAR_COUNT, COMPANIES, StockDailyError, completed_days,
    fetch_stock_daily, parse_stock_history)
from app.services.us_stock_daily_scheduler import USStockDailySchedule, USStockDailyScheduler
from app.services.us_stock_daily_repository import USStockDailyRepository, StockDailyWriteError

TARGET = date(2026, 11, 2)
NOW = datetime(2026, 11, 2, 21, 20, tzinfo=timezone.utc)


def payload(symbol="TSM", target=TARGET):
    calendar = ExternalMarketSessions()
    days = completed_days(target, calendar)
    return {"chart": {"error": None, "result": [{
        "meta": {"symbol": symbol, "exchangeName": "NYQ" if COMPANIES[symbol].exchange == "NYSE" else "NMS",
                 "currency": "USD", "instrumentType": "EQUITY", "exchangeTimezoneName": "America/New_York",
                 "regularMarketTime": int(calendar.session("NYSE", target)[1].timestamp()), "longName": symbol},
        "timestamp": [int(calendar.session("NYSE", day)[0].timestamp()) for day in days],
        "indicators": {"quote": [{"open": [100]*251, "high": [110]*251, "low": [90]*251,
                                  "close": list(range(100, 110)) + [105]*241, "volume": [2**32]*251}],
                       "adjclose": [{"adjclose": [100]*251}]},
        "events": {"dividends": {"test": {"date": int(calendar.session("NYSE", target)[0].timestamp()), "amount": Decimal("0.1")}}}
    }]}}


def batch(symbol="TSM", target=TARGET, now=NOW):
    return parse_stock_history(payload(symbol, target), symbol, target, now, "https://example.test/history")


class StockDailySourceTests(unittest.TestCase):
    def test_full_series_returns_adr_mapping_precision_and_events(self):
        result = batch()
        self.assertEqual(len(result.rows), 250)
        self.assertEqual(result.rows[-1]["market_date"], TARGET)
        first = result.rows[0]
        self.assertEqual(first["previous_close"], Decimal("100"))
        self.assertEqual(first["change_percent"], Decimal("1.00000000"))
        self.assertEqual(first["underlying_symbol"], "2330")
        self.assertEqual(first["adr_underlying_shares"], 5)
        self.assertEqual(first["volume"], 2**32)
        self.assertEqual(json.loads(result.rows[-1]["source_record_json"])["corporate_actions"][0]["kind"], "dividends")

    def test_asml_and_other_adr_mappings_are_not_guessed(self):
        expected = {"ASML": (None, None), "UMC": ("2303", 5), "ASX": ("3711", 2), "CHT": ("2412", 10)}
        for symbol, values in expected.items():
            row = batch(symbol).rows[-1]
            self.assertEqual((row["underlying_symbol"], row["adr_underlying_shares"]), values)
        self.assertEqual(batch("ASML").rows[-1]["security_type"], "FOREIGN_ORDINARY_SHARE")

    def test_bad_identity_currency_exchange_and_timezone_rejected(self):
        for key, value in (("symbol", "GOOG"), ("currency", "TWD"), ("exchangeName", "NMS"), ("exchangeTimezoneName", "UTC")):
            data = payload();data["chart"]["result"][0]["meta"][key] = value
            with self.subTest(key=key), self.assertRaises(StockDailyError):
                parse_stock_history(data, "TSM", TARGET, NOW, "test")

    def test_bad_prices_volume_adjustment_and_alignment_rejected(self):
        for field, value in (("close", -1), ("close", 111), ("volume", -1), ("volume", 1.5), ("open", None), ("low", Decimal("NaN"))):
            data = payload();data["chart"]["result"][0]["indicators"]["quote"][0][field][-1] = value
            with self.subTest(field=field, value=value), self.assertRaises(StockDailyError):
                parse_stock_history(data, "TSM", TARGET, NOW, "test")
        data = payload();data["chart"]["result"][0]["indicators"]["adjclose"][0]["adjclose"][-1] = None
        with self.assertRaises(StockDailyError):parse_stock_history(data, "TSM", TARGET, NOW, "test")
        data = payload();data["chart"]["result"][0]["timestamp"].pop()
        with self.assertRaises(StockDailyError):parse_stock_history(data, "TSM", TARGET, NOW, "test")

    def test_duplicate_and_missing_required_day_rejected(self):
        for duplicate in (True, False):
            data = payload();stamps=data["chart"]["result"][0]["timestamp"]
            stamps[-1] = stamps[-2] if duplicate else stamps[0]-86400
            with self.assertRaises(StockDailyError):parse_stock_history(data, "TSM", TARGET, NOW, "test")

    def test_buffer_and_source_not_yet_at_close_rejected(self):
        with self.assertRaises(StockDailyError):batch(now=NOW-timedelta(seconds=1))
        data = payload();data["chart"]["result"][0]["meta"]["regularMarketTime"] -= 60
        with self.assertRaises(StockDailyError):parse_stock_history(data, "TSM", TARGET, NOW, "test")

    def test_newer_intraday_row_is_not_stored(self):
        data = payload();result=data["chart"]["result"][0]
        next_day=date(2026,11,3);calendar=ExternalMarketSessions()
        next_at=calendar.session("NYSE",next_day)[0]
        result["timestamp"].append(int(next_at.timestamp()))
        result["meta"]["regularMarketTime"]=int(next_at.timestamp())
        for values in result["indicators"]["quote"][0].values():values.append(None)
        result["indicators"]["adjclose"][0]["adjclose"].append(None)
        parsed=parse_stock_history(data,"TSM",TARGET,next_at+timedelta(minutes=1),"test")
        self.assertEqual(parsed.rows[-1]["market_date"],TARGET)

    def test_fetch_url_uses_daily_history_and_bounded_window(self):
        reader=MagicMock(return_value=payload())
        fetched=fetch_stock_daily("TSM",TARGET,reader=reader,now=NOW)
        url=reader.call_args.args[0];query=parse_qs(urlparse(url).query)
        self.assertEqual(query["interval"],["1d"])
        self.assertEqual(query["events"],["div,splits"])
        self.assertLess(int(query["period1"][0]),int(query["period2"][0]))
        self.assertEqual(len(fetched.rows),250)
        with self.assertRaises(StockDailyError):fetch_stock_daily("GOOG",TARGET,reader=reader,now=NOW)

    def test_invalid_batch_rejected_before_connect(self):
        good=batch();bad=replace(good,rows=good.rows[:-1])
        repo=USStockDailyRepository(SimpleNamespace(database="test"))
        repo._connector=MagicMock(side_effect=AssertionError("must not connect"))
        with self.assertRaises(StockDailyError):repo.save(bad)
        repo._connector.assert_not_called()


class StockDailyScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.config=USStockDailySchedule(enabled=True,state_file=Path(self.temp.name)/"daily.json")
        self.repo=MagicMock();self.done=set()
        self.repo.completed_symbols.side_effect=lambda day:set(self.done)
        def save(value,**kwargs):
            self.done.add(value.symbol)
            return dict(status="completed",bar_count=250,batch_id="a"*32)
        self.repo.save.side_effect=save
        self.fetcher=MagicMock(side_effect=lambda symbol,day,**kwargs:SimpleNamespace(symbol=symbol,market_date=day,fetched_at=NOW))
        self.factory=MagicMock(return_value=self.repo)

    def service(self,**kwargs):
        return USStockDailyScheduler(self.config,fetcher=self.fetcher,repository_factory=self.factory,**kwargs)

    def test_settings_validation_and_disabled_no_io(self):
        with patch.dict(os.environ,{},clear=True):self.assertFalse(USStockDailySchedule.from_env().enabled)
        for key,value in (("ENABLED","maybe"),("CLOSE_DELAY_MINUTES","19"),("RETRY_SECONDS","0")):
            with patch.dict(os.environ,{"CAPITAL_US_STOCK_DAILY_"+key:value},clear=True):
                with self.assertRaises(ValueError):USStockDailySchedule.from_env()
        service=USStockDailyScheduler(replace(self.config,enabled=False),fetcher=self.fetcher,repository_factory=self.factory)
        self.assertEqual(service.tick(NOW)["status"],"disabled");service.start()
        self.assertFalse(self.config.state_file.exists());self.factory.assert_not_called();self.fetcher.assert_not_called()

    def test_winter_summer_and_before_buffer(self):
        service=self.service()
        self.assertEqual(service._plans(NOW)[0][0],TARGET)
        self.assertEqual(service._plans(NOW-timedelta(seconds=1))[0][0],date(2026,10,30))
        summer=datetime(2026,7,6,20,20,tzinfo=timezone.utc)
        self.assertEqual(service._plans(summer)[0][0],date(2026,7,6))
        self.assertEqual(service._plans(summer)[0][2].astimezone(timezone(timedelta(hours=8))).strftime("%H:%M"),"04:20")
        self.assertEqual(service._plans(NOW)[0][2].astimezone(timezone(timedelta(hours=8))).strftime("%H:%M"),"05:20")

    def test_holidays_weekends_early_close_and_reviewed_2025(self):
        service=self.service()
        early=datetime(2026,11,27,18,20,tzinfo=timezone.utc)
        self.assertEqual(service._plans(early)[0][0],date(2026,11,27))
        self.assertEqual(service._plans(early-timedelta(seconds=1))[0][0],date(2026,11,25))
        self.assertEqual(service._plans(datetime(2026,11,29,12,tzinfo=timezone.utc))[0][0],date(2026,11,27))
        calendar=ExternalMarketSessions();self.assertIsNone(calendar.session("NYSE",date(2025,1,9)))
        self.assertEqual(calendar.session("NYSE",date(2025,7,3))[1].hour,13)

    def test_unknown_calendar_and_corrupt_state_do_not_fetch(self):
        self.assertEqual(self.service().tick(datetime(2029,1,3,tzinfo=timezone.utc))["status"],"state_or_calendar_error")
        self.config.state_file.write_text("bad-json",encoding="utf-8")
        self.assertEqual(self.service().tick(NOW)["status"],"state_or_calendar_error")
        self.fetcher.assert_not_called();self.factory.assert_not_called()

    def test_one_refresh_then_restart_and_weekend_no_duplicate(self):
        service=self.service();result=service.tick(NOW)
        self.assertEqual(len(result["items"]),15);self.assertEqual(self.repo.save.call_count,15)
        self.assertEqual(self.service().tick(NOW+timedelta(minutes=10))["status"],"already_completed")
        self.assertEqual(self.fetcher.call_count,15)
        self.config.state_file.unlink()
        self.service().tick(NOW+timedelta(minutes=20))
        self.assertEqual(self.fetcher.call_count,15)  # Database marker repairs a lost local checkpoint.

    def test_one_symbol_failure_backoff_and_other_fourteen_complete(self):
        def fetch(symbol,day,**kwargs):
            if symbol=="TSM":raise StockDailyError("source_not_ready")
            return SimpleNamespace(symbol=symbol,market_date=day,fetched_at=NOW)
        self.fetcher.side_effect=fetch
        service=self.service();first=service.tick(NOW)
        self.assertEqual(first["items"][0]["status"],"failed");self.assertEqual(len(self.done),14)
        service.tick(NOW+timedelta(minutes=1));self.assertEqual(self.fetcher.call_count,15)
        service.tick(NOW+timedelta(minutes=10));self.assertEqual(self.fetcher.call_count,16)
        state=json.loads(self.config.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["items"]["TSM"]["attempt_count"],2)
        self.assertEqual(datetime.fromisoformat(state["items"]["TSM"]["retry_at"]),NOW+timedelta(minutes=30))

    def test_commit_uncertain_reconciles_database_before_another_fetch(self):
        original=self.repo.save.side_effect
        def save(value,**kwargs):
            if value.symbol=="TSM":
                self.done.add("TSM")
                raise StockDailyWriteError("b"*32,True,RuntimeError())
            return original(value,**kwargs)
        self.repo.save.side_effect=save
        self.service().tick(NOW)
        self.service().tick(NOW+timedelta(minutes=1))
        self.assertEqual(self.fetcher.call_count,15)
        self.assertEqual(json.loads(self.config.state_file.read_text())["items"]["TSM"]["status"],"already_completed")

    def test_database_outage_does_not_fetch_and_has_retry(self):
        self.repo.completed_symbols.side_effect=RuntimeError("offline")
        self.assertEqual(self.service().tick(NOW)["status"],"database_unavailable")
        self.assertEqual(self.service().tick(NOW+timedelta(minutes=1))["status"],"database_retry_wait")
        self.fetcher.assert_not_called()

    def test_each_new_completed_day_runs_again_and_can_catch_up(self):
        self.service().tick(NOW);self.done.clear()
        next_time=NOW+timedelta(days=3)
        self.assertEqual(self.service().tick(next_time)["market_date"],"2026-11-05")
        self.assertEqual(self.fetcher.call_count,30)

    def test_stop_after_fetch_does_not_write_and_status_endpoint_is_read_only(self):
        service=self.service()
        def fetch(symbol,day,**kwargs):
            service.stop_event.set()
            return SimpleNamespace(symbol=symbol,market_date=day,fetched_at=NOW)
        self.fetcher.side_effect=fetch;service.tick(NOW);self.repo.save.assert_not_called()
        self.fetcher.reset_mock();self.factory.reset_mock()
        from app.api.external_market_quotes import us_stock_schedule
        answer=us_stock_schedule(SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(us_stock_daily_scheduler=service))))
        self.assertEqual(len(answer["schedule"]["symbols"]),15)
        self.fetcher.assert_not_called();self.factory.assert_not_called()

    def test_process_lock_blocks_other_scheduler(self):
        with CapitalProcessLock(self.config.state_file.with_suffix(".lock")).hold(0):
            self.assertEqual(self.service().tick(NOW)["status"],"busy")
        self.fetcher.assert_not_called()


if __name__=="__main__":unittest.main()
