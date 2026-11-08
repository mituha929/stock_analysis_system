"""Fake COM runtime only: STA ownership, callbacks, data paths and cleanup."""
from contextlib import nullcontext
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from app.services.capital_com_quote_service import CapitalComQuoteService
from app.services.capital_dll_quote_service import CapitalApiError
from app.services.capital_worker_runtime import MemoryWatchlistStore
from app.services.capital_worker_service import NativeWorkerConfig
from app.services.candle_validation import CandleDataError
from app.services.quote_cache import QuoteCache


class ComServiceTests(unittest.TestCase):
    def service(self, watchlist=None):
        s = CapitalComQuoteService(QuoteCache(), dll_path=__file__,
                                  watchlist_store=MemoryWatchlistStore(watchlist or {}))
        s._process_lock = Mock()
        center, quote, reply = Mock(), Mock(), Mock()
        center.SKCenterLib_Login.return_value = 0
        center.SKCenterLib_SetAuthority.return_value = 0
        quote.SKQuoteLib_EnterMonitorLONG.return_value = 0
        quote.SKQuoteLib_LeaveMonitor.return_value = 0
        quote.SKQuoteLib_IsConnected.return_value = 1
        quote.SKQuoteLib_GetQuoteStatus.return_value = (2, False, 0)
        quote.SKQuoteLib_RequestStocks.return_value = (0, 0)
        quote.SKQuoteLib_RequestTicks.return_value = (0, 0)
        quote.SKQuoteLib_CancelRequestStocks.return_value = 0
        quote.SKQuoteLib_CancelRequestTicks.return_value = 0
        sk = SimpleNamespace(SKCenterLib='center', SKQuoteLib='quote', SKReplyLib='reply',
            ISKCenterLib='ic', ISKQuoteLib='iq', ISKReplyLib='ir',
            _ISKQuoteLibEvents='eq', _ISKReplyLibEvents='er', SKSTOCKLONG=lambda:SimpleNamespace())
        client = Mock()
        client.GetModule.return_value = sk
        client.CreateObject.side_effect = lambda name, **kw: {'center':center,'quote':quote,'reply':reply}[name]
        client.GetEvents.side_effect = lambda *args, **kw: Mock(spec=['disconnect'])
        pc = Mock(COINIT_APARTMENTTHREADED=2)
        s._load_com_runtime = Mock(return_value=(pc, client))
        self.addCleanup(s.stop)
        return s, center, quote, client, pc

    def connected(self, watchlist=None):
        s, center, quote, client, pc = self.service(watchlist)
        s.connect('private-user', 'private-password')
        s._quote_events.OnConnection(3003, 0)
        return s, center, quote, client, pc

    def test_runtime_initializes_sta_and_retains_handlers_before_login(self):
        s, center, quote, client, pc = self.service()
        def login(*args):
            self.assertEqual(len(s._handlers), 2)
            self.assertEqual([c.kwargs['interface'] for c in client.GetEvents.call_args_list], ['er','eq'])
            pc.CoInitializeEx.assert_called_once_with(2)
            return 0
        center.SKCenterLib_Login.side_effect = login
        s.connect('private-user', 'private-password', 0)
        self.assertEqual(len(s._handlers), 2)
        quote.SKQuoteLib_EnterMonitorLONG.assert_called_once()
        self.assertFalse(s.quote_ready)
        self.assertEqual(s.connection_diagnostics['backend'], 'com')
        self.assertIsNone(s._SK)  # No ctypes wrapper or order object.

    def test_only_successful_3003_opens_readiness_and_real_error_code_is_preserved(self):
        s, *_ = self.service()
        s.connect('private-user', 'private-password')
        for kind, code in ((3001,0),(3003,1095)):
            s._quote_events.OnConnection(kind, code)
            self.assertFalse(s.quote_ready)
        s._quote_events.OnConnection(3003, 0)
        self.assertTrue(s.quote_ready)
        s._quote_events.OnConnection(3033, 3022)
        self.assertFalse(s.quote_ready)
        self.assertEqual(s.connection_events[-1], (3033,3022))
        self.assertEqual(s.connection_diagnostics['events'][-1]['error_code'], 3022)

    def test_announcement_acknowledges_without_exposing_account_or_message(self):
        s, *_ = self.service()
        s.start()
        self.assertEqual(s._reply_events.OnReplyMessage('private-user', 'private-password token=abc'), -1)
        encoded = json.dumps(s.connection_diagnostics)
        for value in ('private-user','private-password','token=abc'):
            self.assertNotIn(value, encoded)
        self.assertTrue(s._announcement_received.is_set())

    def test_callback_never_subscribes_and_poll_restores_once_on_owning_thread(self):
        s, center, quote, client, pc = self.connected({'2330':0})
        quote.SKQuoteLib_RequestStocks.assert_not_called()
        s.poll(); s.poll()
        quote.SKQuoteLib_RequestStocks.assert_called_once_with(0,'2330')
        quote.SKQuoteLib_RequestTicks.assert_called_once_with(0,'2330')
        self.assertTrue(s.is_subscribed('2330'))
        s._quote_events.OnConnection(3002,0)
        self.assertFalse(s.is_subscribed('2330'))
        s._quote_events.OnConnection(3003,0)
        s.poll()
        self.assertEqual(quote.SKQuoteLib_RequestStocks.call_count, 2)
        center.SKCenterLib_Login.assert_called_once()

    def test_repeat_login_is_idempotent_and_reconnect_does_not_login_again(self):
        s, center, quote, *_ = self.connected()
        s.connect('private-user','private-password')
        center.SKCenterLib_Login.assert_called_once()
        with self.assertRaises(CapitalApiError):
            s.connect('different-user','private-password')
        s._quote_events.OnConnection(3002,0)
        s.connect('private-user','private-password')
        self.assertEqual(quote.SKQuoteLib_EnterMonitorLONG.call_count, 2)
        center.SKCenterLib_Login.assert_called_once()

    def test_failed_login_never_enters_monitor_or_creates_history_session(self):
        s, center, quote, *_ = self.service()
        center.SKCenterLib_Login.return_value = 1000
        with self.assertRaisesRegex(CapitalApiError,'1000'):
            s.connect('private-user','private-password')
        quote.SKQuoteLib_EnterMonitorLONG.assert_not_called()
        self.assertIsNone(s._credentials)
        self.assertIsNone(s._history_client._session)
        self.assertEqual(s.connection_diagnostics['phase'],'connect_failed')

    def test_cross_thread_native_call_is_rejected(self):
        s, _, quote, *_ = self.connected()
        errors = []
        def other_thread():
            try:s.poll()
            except Exception as error:errors.append(error)
        worker = threading.Thread(target=other_thread)
        worker.start(); worker.join(2)
        self.assertEqual(len(errors),1)
        self.assertIsInstance(errors[0],CapitalApiError)
        quote.SKQuoteLib_RequestStocks.assert_not_called()

    def test_cleanup_ignores_late_ready_and_releases_handlers_apartment_and_lock(self):
        s, _, quote, _, pc = self.connected()
        events, handlers = s._quote_events, list(s._handlers)
        def leave():
            events.OnConnection(3002,0)
            events.OnConnection(3003,0)
            return 0
        quote.SKQuoteLib_LeaveMonitor.side_effect = leave
        s.stop(); s.stop()
        self.assertFalse(s.quote_ready)
        self.assertFalse(s._apartment)
        self.assertEqual(s.connection_diagnostics['phase'],'stopped')
        for handler in handlers:handler.disconnect.assert_called_once()
        pc.CoUninitialize.assert_called_once()
        s._process_lock.release.assert_called_once()
        self.assertTrue(all(not e['applied'] for e in s.connection_diagnostics['events']
                            if e['kind']=='connection_event' and e['during_cleanup']))

    def test_initialization_failure_still_releases_owned_resources(self):
        s, _, _, client, pc = self.service()
        client.GetEvents.side_effect = RuntimeError('fake event registration failure')
        with self.assertRaises(RuntimeError):s.start()
        self.assertFalse(s._apartment)
        pc.CoUninitialize.assert_called_once()
        s._process_lock.release.assert_called_once()

    def test_directory_cleanup_error_does_not_skip_apartment_or_lock_release(self):
        s, _, _, _, pc = self.connected()
        original = s._directory
        if original is not None:original.close()
        s._directory = Mock()
        s._directory.close.side_effect = OSError('fake close error')
        s.stop()
        pc.CoUninitialize.assert_called_once()
        s._process_lock.release.assert_called_once()

    def test_quote_callback_only_queues_and_poll_fetches_native_snapshot(self):
        s, _, quote, *_ = self.connected()
        stock = SimpleNamespace(bstrStockNo='2330',bstrStockName='test',nClose=12000,nOpen=11900,
            nHigh=12100,nLow=11800,nRef=11850,nBid=11995,nBc=2,nAsk=12000,nAc=3,nTQty=10)
        quote.SKQuoteLib_GetStockByIndexLONG.return_value = (stock,0)
        s._quote_events.OnNotifyQuoteLONG(0,10)
        quote.SKQuoteLib_GetStockByIndexLONG.assert_not_called()
        s.poll()
        self.assertEqual(s.cache.get_quote('2330').price,120)

    def test_history_uses_same_quote_and_existing_validation_without_second_login(self):
        s, center, quote, *_ = self.connected()
        def response(*args):
            s._quote_events.OnNotifyKLineData('2330','2026/10/22,100,110,90,105,1234')
            s._quote_events.OnKLineComplete('##')
            return 0
        quote.SKQuoteLib_RequestKLineAMByDate.side_effect = response
        with patch('app.services.capital_history_service.history_request_limiter.slot',side_effect=lambda:nullcontext()):
            rows = s.fetch_historical_candles('2330','day',date(2026,10,22),date(2026,10,22),cache_result=False)
        self.assertEqual(rows[0].volume,1234)
        self.assertEqual(s.cache.get_historical_candles('2330','day'),[])
        center.SKCenterLib_Login.assert_called_once()
        self.assertIsNone(s._history_client._session)

    def test_negative_volume_remains_data_error_not_a_silent_fix(self):
        s, _, quote, *_ = self.connected()
        def response(*args):
            s._quote_events.OnNotifyKLineData('2330','2026/10/22,100,110,90,105,-1')
            s._quote_events.OnKLineComplete('##')
            return 0
        quote.SKQuoteLib_RequestKLineAMByDate.side_effect = response
        with patch('app.services.capital_history_service.history_request_limiter.slot',side_effect=lambda:nullcontext()):
            with self.assertRaises(CandleDataError):
                s.fetch_historical_candles('2330','day',date(2026,10,22),date(2026,10,22))

    def test_typed_commodity_list_keeps_codes_categories_and_exclusions(self):
        s, _, quote, *_ = self.connected()
        def response(market):
            s._quote_events.OnNotifyCommodityListWithTypeNo(market,'%1%水泥%1101,台泥,0,1101,0.1,NTD;')
            s._quote_events.OnNotifyCommodityListWithTypeNo(market,'%24%ETF%0050,元大台灣50,0,0050,0.1,NTD;')
            s._quote_events.OnNotifyCommodityListWithTypeNo(market,'%98%權證%00000,excluded,0,x,0.1,NTD;')
            s._quote_events.OnNotifyCommodityListWithTypeNo(market,'##')
            return 0
        quote.SKQuoteLib_RequestStockList.side_effect = response
        rows = s.fetch_stock_list(0)
        self.assertEqual([r.symbol for r in rows],['1101','0050'])
        self.assertEqual(rows[1].category_no,24)
        self.assertEqual(s._list_rows,{})

    def test_bad_or_empty_commodity_batch_does_not_return_partial_rows(self):
        for raw in ('%1%水泥%1101,台泥,0,1101,0.1,NTD;broken;', ''):
            s, _, quote, *_ = self.connected()
            def response(market):
                if raw:s._quote_events.OnNotifyCommodityListWithTypeNo(market,raw)
                s._quote_events.OnNotifyCommodityListWithTypeNo(market,'##')
                return 0
            quote.SKQuoteLib_RequestStockList.side_effect = response
            with self.assertRaises(CapitalApiError):s.fetch_stock_list(0)
            self.assertEqual(s._list_rows,{})

    def test_numeric_status_query_does_not_replace_ready_event(self):
        s, *_ = self.service()
        s.connect('private-user','private-password')
        s.refresh_quote_connection_state()
        self.assertFalse(s.quote_ready)
        self.assertEqual(s.quote_connection_count,2)
        self.assertFalse(s.quote_out_limit)

    def test_pending_market_buffer_is_bounded(self):
        s, *_ = self.connected()
        for n in range(3000):s._quote_events.OnNotifyQuoteLONG(0,n)
        self.assertEqual(len(s._pending),2000)


class BackendConfigTests(unittest.TestCase):
    def test_backend_validation(self):
        self.assertEqual(NativeWorkerConfig(quote_backend='com').quote_backend,'com')
        self.assertEqual(NativeWorkerConfig(quote_backend='dll').quote_backend,'dll')
        with self.assertRaises(ValueError):NativeWorkerConfig(quote_backend='unknown')

    def test_production_default_is_com_but_env_can_explicitly_select_dll(self):
        self.assertEqual(NativeWorkerConfig().quote_backend,'com')
        with patch.dict('os.environ',{'CAPITAL_WORKER_QUOTE_BACKEND':'dll'}):
            self.assertEqual(NativeWorkerConfig.from_env().quote_backend,'dll')

    def test_api_offline_requires_explicit_flag_and_connection_refused(self):
        from scripts.test_quote_login_id_live import api_brief
        from urllib.error import URLError
        with patch('scripts.test_quote_login_id_live.api_get',side_effect=URLError(ConnectionRefusedError())), \
             patch('scripts.test_quote_login_id_live.offline_brief',return_value={'phase':'api_offline'}) as offline:
            with self.assertRaises(URLError):api_brief()
            offline.assert_not_called()
            self.assertEqual(api_brief({'values':{}}),{'phase':'api_offline'})
            offline.assert_called_once()

    def test_timeout_is_not_misrepresented_as_safe_api_offline(self):
        from scripts.test_quote_login_id_live import api_brief
        from urllib.error import URLError
        with patch('scripts.test_quote_login_id_live.api_get',side_effect=URLError(TimeoutError())), \
             patch('scripts.test_quote_login_id_live.offline_brief') as offline:
            with self.assertRaises(URLError):api_brief({'values':{}})
            offline.assert_not_called()

    def test_offline_control_is_read_only_and_holds_worker_lease(self):
        from scripts.test_quote_login_id_live import offline_brief
        guard = {'values':{'CAPITAL_BACKFILL_CONTROL_DB':str(Path(__file__).resolve())}}
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = ('{"pause_requested":true,"state":"paused"}',)
        with patch('sqlite3.connect',return_value=connection) as connect, \
             patch('app.services.capital_process_lock.CapitalProcessLock') as lock:
            result = offline_brief(guard)
            self.assertIn('?mode=ro',connect.call_args.args[0])
            self.assertTrue(result['pause_requested'])
            self.assertFalse(result['worker_active'])
            connection.execute.assert_called_once_with('SELECT payload FROM control WHERE id=1')
            connection.close.assert_called_once()
            lock.return_value.acquire.assert_called_once_with(timeout_seconds=0)
            self.assertIs(guard['lease'],lock.return_value)


if __name__ == '__main__':
    unittest.main()
