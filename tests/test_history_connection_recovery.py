import threading
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.services.capital_history_connection import (
    CapitalHistoryConnectionError, CapitalHistoryError, assert_history_code,
    is_history_connection_error, with_history_connection_recovery,
)
from app.services.capital_history_service import (
    HistoryQuoteEvents, _ReusableHistoryComSession, request_history_candles,
)
from app.services.capital_dll_quote_service import CapitalApiError, CapitalQuoteService
from scripts.backfill_market_candles import CapitalKLineSession


class FakeQuote:
    def __init__(self, events, codes, enter_code=0):
        self.events = events
        self.codes = iter(codes)
        self.enter_code = enter_code
        self.requests = []
        self.enters = self.leaves = 0

    def SKQuoteLib_RequestKLineAMByDate(self, *args):
        self.requests.append(args)
        code = next(self.codes)
        if code == "disconnect":
            self.events.OnConnection(3002, 0)
            return 0
        if code == "timeout":
            return 0
        if code == 0:
            self.events.OnNotifyKLineData(args[0], "2026/10/07,10,12,9,11,100")
            self.events.OnKLineComplete("##")
        return code

    def SKQuoteLib_LeaveMonitor(self):
        self.leaves += 1
        self.events.OnConnection(3002, 0)
        return 0

    def SKQuoteLib_EnterMonitorLONG(self):
        self.enters += 1
        if self.enter_code == 0:
            self.events.OnConnection(3003, 0)
        return self.enter_code


@patch("app.services.capital_history_connection.time.sleep")
class HistoryConnectionRecoveryTests(unittest.TestCase):
    def request(self, quote, events, timeout=1):
        return request_history_candles(
            quote, Mock(), events, lambda: None,
            "2330", "week", date(2026, 10, 1), date(2026, 10, 7), timeout,
        )

    def ready_events(self):
        events = HistoryQuoteEvents()
        events.OnConnection(3003, 0)
        return events

    def test_1095_recovers_then_retries_exact_same_query(self, _sleep):
        events = self.ready_events()
        quote = FakeQuote(events, [1095, 0])
        candles = self.request(quote, events)
        self.assertEqual(len(candles), 1)
        self.assertEqual(quote.enters, 1)
        self.assertEqual(quote.leaves, 1)
        self.assertEqual(quote.requests[0], quote.requests[1])

    def test_recovers_after_second_attempt_without_infinite_retry(self, _sleep):
        events = self.ready_events()
        quote = FakeQuote(events, [1095, 1095, 0])
        self.assertEqual(len(self.request(quote, events)), 1)
        self.assertEqual(quote.enters, 2)
        self.assertEqual(len(quote.requests), 3)

    def test_exhausted_recovery_is_a_connection_error(self, _sleep):
        events = self.ready_events()
        quote = FakeQuote(events, [1095, 1095, 1095])
        with self.assertRaisesRegex(CapitalHistoryConnectionError, "恢復 2 次仍失敗") as caught:
            self.request(quote, events)
        self.assertEqual(caught.exception.code, 1095)
        self.assertEqual(quote.enters, 2)
        self.assertEqual(len(quote.requests), 3)

    def test_failed_reconnect_never_issues_next_query(self, _sleep):
        events = self.ready_events()
        quote = FakeQuote(events, [1095], enter_code=1095)
        with self.assertRaises(CapitalHistoryConnectionError):
            self.request(quote, events)
        self.assertEqual(len(quote.requests), 1)
        self.assertEqual(quote.enters, 2)

    def test_disconnect_during_wait_recovers(self, _sleep):
        events = self.ready_events()
        quote = FakeQuote(events, ["disconnect", 0])
        self.assertEqual(len(self.request(quote, events)), 1)
        self.assertEqual(quote.enters, 1)

    def test_stalled_response_recovers(self, _sleep):
        events = self.ready_events()
        quote = FakeQuote(events, ["timeout", 0])
        self.assertEqual(len(self.request(quote, events, timeout=0)), 1)
        self.assertEqual(quote.enters, 1)

    def test_disconnected_before_request_waits_for_recovery_first(self, _sleep):
        events = HistoryQuoteEvents()
        quote = FakeQuote(events, [0])
        self.assertEqual(len(self.request(quote, events)), 1)
        self.assertEqual(quote.enters, 1)
        self.assertEqual(len(quote.requests), 1)

    def test_non_connection_error_is_not_retried(self, _sleep):
        events = self.ready_events()
        quote = FakeQuote(events, [9999])
        with self.assertRaises(CapitalHistoryError) as caught:
            self.request(quote, events)
        self.assertFalse(is_history_connection_error(caught.exception))
        self.assertEqual(quote.enters, 0)

    def test_login_warning_only_allowed_on_login(self, _sleep):
        center = Mock()
        assert_history_code(2003, center, allow_login_warning=True)
        with self.assertRaises(CapitalHistoryError):
            assert_history_code(2003, center)

    def test_other_login_error_is_not_allowed(self, _sleep):
        with self.assertRaises(CapitalHistoryError):
            assert_history_code(2017, Mock(), allow_login_warning=True)

    def test_connection_classification_survives_wrapping(self, _sleep):
        error = CapitalApiError("wrapped")
        error.__cause__ = CapitalHistoryConnectionError("1095", code=1095)
        self.assertTrue(is_history_connection_error(error))
        self.assertFalse(is_history_connection_error(CapitalApiError("a stock error 9999")))

    def test_disconnect_and_failed_ready_callback_clear_readiness(self, _sleep):
        events = self.ready_events()
        for kind, code in [(3001, 0), (3002, 0), (3033, 0), (3003, 1095)]:
            events.OnConnection(3003, 0)
            events.OnConnection(kind, code)
            self.assertFalse(events.connected.is_set())

    def test_late_records_are_not_included_after_request(self, _sleep):
        events = self.ready_events()
        quote = FakeQuote(events, [0])
        self.request(quote, events)
        events.OnNotifyKLineData("2330", "2026/10/07,20,22,19,21,200")
        self.assertEqual(len(events.records), 0)

    def test_original_empty_success_is_not_retried(self, _sleep):
        reconnect = Mock()
        self.assertEqual(with_history_connection_recovery(lambda: [], reconnect), [])
        reconnect.assert_not_called()

    def test_session_start_preserves_connection_exception_type(self, _sleep):
        session = _ReusableHistoryComSession(None, ("test", "test", 0), 1, Mock())
        session.thread = Mock()
        session.ready.set()
        session.startup_error = CapitalHistoryConnectionError("startup offline", code=1095)
        with self.assertRaises(CapitalHistoryConnectionError):
            session.start()

    def test_startup_setup_error_stops_batch_instead_of_failing_every_stock(self, _sleep):
        session = _ReusableHistoryComSession(None, ("test", "test", 0), 1, Mock())
        session.thread = Mock()
        session.ready.set()
        session.startup_error = CapitalHistoryError("Login failed", code=2017)
        with self.assertRaises(CapitalHistoryConnectionError) as caught:
            session.start()
        self.assertEqual(caught.exception.code, 2017)

    def test_request_wait_budget_includes_all_recovery_attempts(self, _sleep):
        session = _ReusableHistoryComSession(None, ("test", "test", 0), 30, Mock())
        session.thread = Mock()
        done = Mock()
        done.wait.return_value = True
        with patch("app.services.capital_history_service._HistoryRequest") as request_type:
            request_type.return_value.done = done
            request_type.return_value.error = None
            request_type.return_value.result = []
            session.request("2330", "week", date(2026, 10, 1), date(2026, 10, 7))
        self.assertGreaterEqual(done.wait.call_args.args[0], 154)

    def test_cli_uses_the_same_recovery_logic(self, _sleep):
        from pathlib import Path
        events = self.ready_events()
        quote = FakeQuote(events, [1095, 0])
        session = CapitalKLineSession(Path("unused"), "test", "test", 0, 1)
        session.center, session.quote, session._event = Mock(), quote, events
        session._pythoncom = SimpleNamespace(PumpWaitingMessages=lambda: None)
        result = session.request("2330", "week", date(2026, 10, 1), date(2026, 10, 7), 1)
        self.assertEqual(len(result), 1)
        self.assertEqual(quote.enters, 1)

    def test_primary_quote_reconnect_does_not_login_again(self, _sleep):
        service = CapitalQuoteService.__new__(CapitalQuoteService)
        service._credentials = ("test", "test", 0)
        service._stocks_ready = threading.Event()
        service._history_client = Mock(timeout_seconds=0)
        service._history_client.fetch.return_value = []
        service.connect = Mock(side_effect=lambda *_args: service._stocks_ready.set())
        result = service.fetch_historical_candles(
            "2330", "week", date(2026, 10, 1), date(2026, 10, 7), cache_result=False,
        )
        self.assertEqual(result, [])
        service.connect.assert_called_once_with("test", "test", 0)

    def test_primary_quote_recovery_failure_is_classified(self, _sleep):
        service = CapitalQuoteService.__new__(CapitalQuoteService)
        service._credentials = ("test", "test", 0)
        service._stocks_ready = threading.Event()
        service._history_client = Mock(timeout_seconds=0)
        service.connect = Mock()
        with self.assertRaises(CapitalApiError) as caught:
            service.fetch_historical_candles("2330", "week", date(2026, 10, 1), date(2026, 10, 7))
        self.assertTrue(is_history_connection_error(caught.exception))
        self.assertEqual(service.connect.call_count, 2)
        service._history_client.fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
