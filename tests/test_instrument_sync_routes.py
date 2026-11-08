import unittest
from unittest.mock import call, patch

from app.api.routes import sync_board_lot_instruments, sync_odd_lot_instruments
from app.schemas.models import Instrument


class _Capital:
    def __init__(self):
        self.requested_markets = []

    def fetch_stock_list(self, market_no: int):
        self.requested_markets.append(market_no)
        return [Instrument(
            market_no=market_no,
            category_no=24,
            category_name="測試分類",
            symbol=f"TEST{market_no}",
            name="測試商品",
            order_symbol=f"TEST{market_no}",
        )]


class InstrumentSyncRouteTests(unittest.TestCase):
    @patch("app.api.routes.MySQLInstrumentRepository")
    def test_board_lot_endpoint_syncs_markets_to_instruments(self, repository_type):
        repository_type.return_value.sync_market.side_effect = [
            (101, 1, False), (102, 1, False), (103, 1, True),
        ]
        capital = _Capital()

        responses = sync_board_lot_instruments(batch_size=500, capital=capital)

        self.assertEqual(capital.requested_markets, [0, 1, 4])
        self.assertEqual([response.market_no for response in responses], [0, 1, 4])
        self.assertEqual({response.table_name for response in responses}, {"instruments"})
        self.assertEqual([response.resumed for response in responses], [False, False, True])
        self.assertEqual(
            repository_type.return_value.sync_market.call_args_list,
            [
                call(0, unittest.mock.ANY, 500, "instruments"),
                call(1, unittest.mock.ANY, 500, "instruments"),
                call(4, unittest.mock.ANY, 500, "instruments"),
            ],
        )

    @patch("app.api.routes.MySQLInstrumentRepository")
    def test_odd_lot_endpoint_syncs_markets_to_odd_lot_table(self, repository_type):
        repository_type.return_value.sync_market.side_effect = [
            (201, 1, False), (202, 1, False),
        ]
        capital = _Capital()

        responses = sync_odd_lot_instruments(batch_size=250, capital=capital)

        self.assertEqual(capital.requested_markets, [5, 6])
        self.assertEqual([response.market_no for response in responses], [5, 6])
        self.assertEqual({response.table_name for response in responses}, {"odd_lot_instruments"})
        self.assertEqual(
            repository_type.return_value.sync_market.call_args_list,
            [
                call(5, unittest.mock.ANY, 250, "odd_lot_instruments"),
                call(6, unittest.mock.ANY, 250, "odd_lot_instruments"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
