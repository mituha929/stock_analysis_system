import unittest
from unittest.mock import Mock

from app.services.capital_dll_quote_service import CapitalApiError, CapitalQuoteService
from app.services.quote_cache import QuoteCache


class _LoginMustNotRun:
    def Login(self, *_args):
        raise AssertionError("重複登入不應再呼叫群益 DLL Login")

    def ManageServerConnection(self, *_args):
        return 0


class ConnectIdempotencyTests(unittest.TestCase):
    def _connected_service(self) -> CapitalQuoteService:
        service = CapitalQuoteService(QuoteCache())
        service._started = True
        service._SK = _LoginMustNotRun()
        service._credentials = ("user", "password", 0)
        service._stocks_ready.set()
        service._status = "connected"
        service._status_message = "已連線國內行情主機，商品基本資料已就緒"
        service._quote_connection_state = "connected"
        return service

    def test_same_credentials_do_not_reset_a_healthy_connection(self):
        service = self._connected_service()

        response = service.connect("user", "password", 0)

        self.assertEqual(response[0], "connected")
        self.assertTrue(service.quote_ready)
        self.assertEqual(service.quote_connection_state, "connected")

    def test_different_credentials_require_disconnect_first(self):
        with self.assertRaises(CapitalApiError) as caught:
            self._connected_service().connect("other", "password", 0)
        self.assertIn("重啟 API", str(caught.exception))

    def test_disconnect_then_same_credentials_reconnects_without_login(self):
        service = self._connected_service()
        # Do not touch the real cross-process SKCOM lock: a local API process
        # may legitimately own it while this isolated state-machine test runs.
        service._process_lock = Mock()
        service._owns_process_lock = True
        service._announcement_received.set()
        service.disconnect()

        response = service.connect("user", "password", 0)

        self.assertEqual(response[0], "connecting")
        self.assertEqual(service.quote_connection_state, "connecting")


if __name__ == "__main__":
    unittest.main()
