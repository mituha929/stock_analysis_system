import unittest

from app.services.capital_dll_quote_service import CapitalApiError, CapitalQuoteService
from app.services.mysql_instrument_repository import MySQLInstrumentRepository, MySQLRepositoryError
from app.services.quote_cache import QuoteCache


class _Item:
    strQuoteCode = "2330"
    strStockName = "台積電"
    strOrderCode = "2330"
    strExpiryDate = ""


class _Category:
    TypeNo = 24
    TypeName = "半導體業"
    Items = [_Item()]


class _StockList:
    AllTypeLists = [_Category()]


class _Wrapper:
    def LoadCommodity(self, market_no: int):
        self.loaded_market = market_no
        return 0

    def RequestStockList(self, market_no: int):
        assert self.loaded_market == market_no
        self.market_no = market_no
        return _StockList()


class StockListTests(unittest.TestCase):
    def _service(self) -> CapitalQuoteService:
        service = CapitalQuoteService(QuoteCache())
        service._credentials = ("user", "password", 0)
        service._stocks_ready.set()
        service._SK = _Wrapper()
        return service

    def test_parses_category_and_product_fields(self):
        result = self._service().fetch_stock_list(0)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].category_name, "半導體業")
        self.assertEqual(result[0].symbol, "2330")
        self.assertIsNone(result[0].expiry_date)

    def test_limit_can_be_applied_to_the_parsed_result(self):
        result = self._service().fetch_stock_list(0)
        self.assertEqual(len(result[:1]), 1)

    def test_rejects_unsupported_market(self):
        with self.assertRaises(CapitalApiError):
            self._service().fetch_stock_list(2)

    def test_excludes_warrants_and_beneficiary_certificates(self):
        class ExcludedCategory:
            TypeNo = 98
            TypeName = "權證"
            Items = [_Item()]

        class MixedStockList:
            AllTypeLists = [_Category(), ExcludedCategory()]

        service = self._service()
        service._SK.RequestStockList = lambda _market_no: MixedStockList()
        result = service.fetch_stock_list(0)
        self.assertEqual([item.category_no for item in result], [24])

    def test_custom_table_name_is_strictly_validated(self):
        self.assertEqual(MySQLInstrumentRepository._validate_table_name("listed_instruments"), "listed_instruments")
        with self.assertRaises(MySQLRepositoryError):
            MySQLInstrumentRepository._validate_table_name("instruments; DROP TABLE instruments")


if __name__ == "__main__":
    unittest.main()
