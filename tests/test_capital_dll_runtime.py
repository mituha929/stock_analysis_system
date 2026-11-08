"""Pure fake DLL: loader lifecycle, vendor ABI and callback lock safety."""
from ctypes import CFUNCTYPE
from datetime import date
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.services.capital_dll_environment import QUOTE_CALLBACK_REGISTRATIONS
from app.services.capital_dll_quote_service import CapitalApiError, CapitalQuoteService
from app.services.capital_process_lock import CapitalProcessLockError
from app.services.capital_worker_runtime import MemoryWatchlistStore
from app.services.quote_cache import QuoteCache


class DllRuntimeTests(unittest.TestCase):
    def service(self, watchlist=None):
        directory = Path(self.enterContext(TemporaryDirectory(prefix='capital-fake-dll-')))
        wrapper = directory / 'SKDLLPython.py'
        wrapper.touch()
        (directory / 'libs').mkdir()
        self.enterContext(patch.dict(os.environ, {'PATH':os.environ.get('PATH','')}))
        handles = []
        def add_directory(path):
            handle = Mock()
            handles.append(handle)
            return handle
        add = self.enterContext(patch('app.services.capital_dll_quote_service.os.add_dll_directory',
                                     create=True, side_effect=add_directory))
        callbacks = {}
        sk = SimpleNamespace(_dll=SimpleNamespace())
        for export, type_name in QUOTE_CALLBACK_REGISTRATIONS:
            setattr(sk._dll, export, Mock())
            setattr(sk, type_name, CFUNCTYPE(None))
        for name in ('OnReplyMessage','OnConnection','OnNotifyQuoteLONG','OnNotifyBest5LONG','OnNotifyTicksLONG'):
            setattr(sk,name,Mock(side_effect=lambda callback,key=name:callbacks.__setitem__(key,callback)))
        def login(*args):
            callbacks['OnReplyMessage']('private-user','private-password secret-body')
            return SimpleNamespace(Code=0)
        sk.Login = Mock(side_effect=login)
        def connection(_login, status, target):
            self.assertEqual(target,1)
            callbacks['OnConnection']('SKQuote',3003 if status==0 else 3002)
            return 0
        sk.ManageServerConnection = Mock(side_effect=connection)
        sk.LoadCommodity = Mock(return_value=0)
        item = SimpleNamespace(strQuoteCode='0050',strStockName='ETF',strOrderCode='0050',strExpiryDate='')
        sk.RequestStockList = Mock(return_value=SimpleNamespace(AllTypeLists=[
            SimpleNamespace(TypeNo=24,TypeName='ETF',Items=[item])]))
        sk.SKQuoteLib_RequestStocks = Mock(return_value=0)
        sk.SKQuoteLib_RequestTicks = Mock(return_value=0)
        sk.GetMessage = Mock(return_value='FAKE')
        stock = SimpleNamespace(nCode=0,strStockNo='2330',strStockName='test',nClose=12000,nOpen=11900,
            nHigh=12100,nLow=11800,nRef=11850,nBid=11995,nBc=2,nAsk=12000,nAc=3,nTQty=10)
        sk.SKQuoteLib_GetStockByStockNo = Mock(return_value=stock)
        service = CapitalQuoteService(QuoteCache(),wrapper_path=str(wrapper),
                                      watchlist_store=MemoryWatchlistStore(watchlist or {}))
        service._process_lock = Mock()
        service._load_wrapper = Mock(return_value=SimpleNamespace(SK=sk))
        pc = SimpleNamespace(PumpWaitingMessages=Mock())
        self.enterContext(patch.dict('sys.modules',{'pythoncom':pc}))
        self.addCleanup(service.stop)
        return service, sk, callbacks, pc, add, handles

    def connect(self, service):
        service.connect('private-user','private-password')

    def test_lock_and_dll_search_are_ready_before_wrapper_import(self):
        s, sk, _, _, add, handles = self.service()
        original = s._load_wrapper.return_value
        def load(path):
            self.assertTrue(s._owns_process_lock)
            self.assertTrue(s._dll_search_active)
            add.assert_called_once_with(str(s.wrapper_path.parent/'libs'))
            self.assertTrue(path.is_absolute())
            self.assertEqual(Path(os.environ['PATH'].split(os.pathsep)[0]),s.wrapper_path.parent/'libs')
            return original
        s._load_wrapper.side_effect = load
        s.start(); s.start()
        s._process_lock.acquire.assert_called_once_with(timeout_seconds=0)
        s._load_wrapper.assert_called_once()
        self.assertIs(s._vendor_module.SK,sk)
        for export, type_name in QUOTE_CALLBACK_REGISTRATIONS:
            function = getattr(sk._dll,export)
            self.assertEqual(function.argtypes,[getattr(sk,type_name)])
            self.assertIsNone(function.restype)
        self.assertTrue(s.connection_diagnostics['callback_abi_configured'])

    def test_busy_sdk_prevents_even_loading_the_wrapper(self):
        s, sk, *_ = self.service()
        s._process_lock.acquire.side_effect = CapitalProcessLockError('SDK busy')
        with self.assertRaisesRegex(CapitalApiError,'SDK busy'):s.start()
        s._load_wrapper.assert_not_called()
        sk.Login.assert_not_called()
        self.assertFalse(s._owns_process_lock)

    def test_loader_failure_closes_search_handle_and_releases_lease(self):
        s, _, _, _, _, handles = self.service()
        s._load_wrapper.side_effect = RuntimeError('fake import failure')
        with self.assertRaises(RuntimeError):s.start()
        handles[0].close.assert_called_once()
        s._process_lock.release.assert_called_once()
        self.assertFalse(s._dll_search_active)

    def test_stop_and_reconnect_reopen_search_path_without_duplicate_login(self):
        s, sk, _, _, add, handles = self.service()
        self.connect(s)
        s.stop()
        handles[0].close.assert_called_once()
        self.connect(s)
        self.assertEqual(add.call_count,2)
        sk.Login.assert_called_once()
        self.assertTrue(s.quote_ready)
        self.assertIs(s._vendor_module.SK,sk)

    def test_subscription_restores_in_poll_not_inside_callback_thread(self):
        s, sk, callbacks, *_ = self.service({'2330':0})
        self.connect(s)
        sk.SKQuoteLib_RequestStocks.assert_not_called()
        s.poll()
        sk.SKQuoteLib_RequestStocks.assert_called_once_with('2330')
        callbacks['OnConnection']('SKQuote',3002)
        self.assertFalse(s.is_subscribed('2330'))
        callbacks['OnConnection']('SKQuote',3003)
        self.assertEqual(sk.SKQuoteLib_RequestStocks.call_count,1)
        s.poll()
        self.assertEqual(sk.SKQuoteLib_RequestStocks.call_count,2)
        sk.Login.assert_called_once()

    def test_quote_callback_completes_while_dll_call_lock_is_held(self):
        s, sk, callbacks, *_ = self.service()
        s.start()
        completed = threading.Event()
        native_threads = []
        sk.SKQuoteLib_GetStockByStockNo.side_effect = lambda *args:(
            native_threads.append(threading.get_ident()) or sk.SKQuoteLib_GetStockByStockNo.return_value)
        with s._lock:
            thread = threading.Thread(target=lambda:(callbacks['OnNotifyQuoteLONG'](0,'2330'),completed.set()))
            thread.start()
            self.assertTrue(completed.wait(0.5),'SDK callback must not wait for the initiating DLL call lock')
            sk.SKQuoteLib_GetStockByStockNo.assert_not_called()
        thread.join(1)
        callbacks['OnConnection']('SKQuote',3003)
        s.poll()
        self.assertEqual(native_threads,[threading.get_ident()])
        self.assertEqual(s.cache.get_quote('2330').price,120)

    def test_pending_quotes_are_bounded_and_late_cleanup_notifications_ignored(self):
        s, _, callbacks, *_ = self.service()
        self.connect(s)
        for n in range(3000):callbacks['OnNotifyQuoteLONG'](0,str(n))
        self.assertEqual(len(s._pending_quotes),2000)
        s.stop()
        callbacks['OnNotifyQuoteLONG'](0,'late')
        self.assertEqual(len(s._pending_quotes),0)

    def test_wrong_thread_cannot_issue_native_calls(self):
        s, sk, *_ = self.service()
        self.connect(s)
        errors=[]
        def wrong_thread():
            try:s.fetch_stock_list(0)
            except Exception as error:errors.append(error)
        thread=threading.Thread(target=wrong_thread)
        thread.start();thread.join(1)
        self.assertEqual(len(errors),1)
        self.assertIsInstance(errors[0],CapitalApiError)
        sk.RequestStockList.assert_not_called()

    def test_stock_list_loads_market_before_querying(self):
        s, sk, *_ = self.service()
        self.connect(s)
        calls = Mock()
        calls.attach_mock(sk.LoadCommodity,'load')
        calls.attach_mock(sk.RequestStockList,'query')
        rows=s.fetch_stock_list(0)
        self.assertEqual([c[0] for c in calls.mock_calls],['load','query'])
        self.assertEqual(rows[0].symbol,'0050')
        sk.LoadCommodity.assert_called_once_with(0)

    def test_commodity_failure_never_returns_partial_or_empty_replacement_list(self):
        s, sk, *_ = self.service()
        self.connect(s)
        sk.LoadCommodity.return_value = 1095
        with self.assertRaisesRegex(CapitalApiError,'1095'):s.fetch_stock_list(0)
        sk.RequestStockList.assert_not_called()

    def test_failed_manage_can_reconnect_same_session_instead_of_staying_connecting(self):
        s, sk, *_ = self.service()
        sk.ManageServerConnection.side_effect = [1095,0]
        with self.assertRaisesRegex(CapitalApiError,'1095'):self.connect(s)
        self.assertEqual(s.quote_connection_state,'disconnected')
        self.connect(s)
        self.assertEqual(sk.ManageServerConnection.call_count,2)
        sk.Login.assert_called_once()

    def test_announcement_wait_pumps_messages_instead_of_blocking_event_dispatch(self):
        s, sk, callbacks, pc, *_ = self.service()
        sk.Login.side_effect = lambda *args:SimpleNamespace(Code=0)
        sk.ManageServerConnection.side_effect = None
        sk.ManageServerConnection.return_value = 0
        pc.PumpWaitingMessages.side_effect = lambda:callbacks['OnReplyMessage']('account','private body')
        result=s.connect('private-user','private-password')
        pc.PumpWaitingMessages.assert_called()
        self.assertTrue(result[2])
        self.assertFalse(s.quote_ready)  # No fabricated 3003.

    def test_history_reconnect_wait_pumps_the_ready_event_without_second_login(self):
        s, sk, callbacks, pc, *_ = self.service()
        self.connect(s)
        s._history_client = Mock(timeout_seconds=1)
        s._history_client.fetch.return_value = []
        callbacks['OnConnection']('SKQuote',3002)
        sk.ManageServerConnection.side_effect = None
        sk.ManageServerConnection.return_value = 0
        pc.PumpWaitingMessages.side_effect = lambda:callbacks['OnConnection']('SKQuote',3003)
        rows=s.fetch_historical_candles('2330','day',date(2026,10,22),date(2026,10,22),cache_result=False)
        self.assertEqual(rows,[])
        sk.Login.assert_called_once()
        s._history_client.fetch.assert_called_once()


if __name__ == '__main__':
    unittest.main()
