from datetime import date, datetime, timezone
from unittest.mock import patch
import unittest

from app.schemas.models import Candle
from app.services.capital_history_service import CapitalHistoryClient


class ReusableHistorySessionTests(unittest.TestCase):
    def test_same_credentials_reuse_one_com_session(self):
        created = []

        class FakeSession:
            def __init__(self, _path, credentials, _timeout, _lock):
                self.credentials = credentials
                self.is_alive = True
                self.closed = False
                created.append(self)

            def start(self):
                pass

            def request(self, *_args):
                return [Candle(timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc), open=1, high=1, low=1, close=1, volume=1)]

            def close(self):
                self.closed = True

        with patch("app.services.capital_history_service._ReusableHistoryComSession", FakeSession):
            client = CapitalHistoryClient(dll_path="unused.dll")
            client.fetch("user", "password", 0, "2330", "day", date(2026, 1, 1), date(2026, 1, 1))
            client.fetch("user", "password", 0, "2317", "day", date(2026, 1, 1), date(2026, 1, 1))
            client.close()

        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].closed)


if __name__ == "__main__":
    unittest.main()
