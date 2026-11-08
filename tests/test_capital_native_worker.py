"""Real spawned IPC processes, fake vendor: no DLL, credentials or MySQL."""
from datetime import date, datetime, timezone
import os
import threading
import time
import unittest
from unittest.mock import patch

from app.schemas.models import Candle, Quote, Tick
from app.services.capital_history_connection import is_history_connection_error
from app.services.candle_validation import CandleDataError
from app.services.capital_worker_runtime import ForwardingCache, MemoryWatchlistStore
from app.services.capital_worker_service import CapitalWorkerService, NativeWorkerConfig, CapitalApiError
from app.services.quote_cache import QuoteCache


def fake_native_worker(pipe, config):
    state=dict(pid=os.getpid(),quote_ready=False,status='ready',quote_connection_state='disconnected',
               status_message='fake',private_mb=20,working_set_mb=20,subscribed=[])
    pipe.send(dict(id=0,ok=True,state=state,events={}))
    while True:
        try:
            request=pipe.recv()
        except EOFError:
            break
        command,args=request['command'],request['arguments']
        result=None
        ok=True
        extra={}
        if command=='connect':
            if args['user_id']=='bad-user':
                ok=False
                extra=dict(error='bad-user bad-password login failed',kind='api')
            else:
                state.update(quote_ready=True,quote_connection_state='connected',subscribed=list(config['watchlist']))
                result=('connected','fake',True)
        elif command=='history':
            if args['symbol']=='BAD_VOLUME':
                pipe.send(dict(id=request['id'],ok=False,state=state,events={},kind='data',
                               error='negative volume',data_error=dict(field='volume',raw_value='-2070765642',
                                                                      timestamp='2017-12-04T00:00:00+08:00')))
                continue
            if args['symbol']=='CRASH':
                os._exit(7)
            if args['symbol']=='HANG':
                time.sleep(60)
            state['private_mb']=350
            result=[Candle(timestamp=datetime.combine(args['start_date'],datetime.min.time(),tzinfo=timezone.utc),
                           open=1,high=2,low=1,close=2,volume=100)]
        elif command=='subscribe':
            state['subscribed'].append(args['symbol'])
        elif command=='unsubscribe':
            state['subscribed']=[s for s in state['subscribed'] if s!=args['symbol']]
        elif command=='instruments':
            result=[]
        elif command=='shutdown':
            state.update(quote_ready=False)
        pipe.send(dict(id=request['id'],ok=ok,state=state,result=result,events={},**extra))
        if command=='shutdown':
            break
    pipe.close()


class WorkerTests(unittest.TestCase):
    def service(self, **overrides):
        config=NativeWorkerConfig(recycle_mb=300,connect_timeout=5,rpc_timeout=2,
                                  stop_timeout=0.2,poll_seconds=0.05,retry_seconds=0.05)
        service=CapitalWorkerService(QuoteCache(),watchlist_store=MemoryWatchlistStore({'2330':1}),
            config=config,worker_target=fake_native_worker,start_monitor=False,**overrides)
        self.addCleanup(service.stop)
        return service

    def connect(self, service):
        service.connect('fake-user','fake-password')
        self.assertTrue(service.quote_ready)

    def test_constructor_never_starts_worker_or_logs_in(self):
        service=self.service()
        self.assertIsNone(service._process)
        self.assertFalse(service.quote_ready)
        with self.assertRaises(CapitalApiError):
            service.fetch_stock_list(0)
        self.assertIsNone(service._process)

    def test_invalid_source_volume_survives_ipc_as_data_error_and_releases_pin(self):
        service=self.service()
        self.connect(service)
        with self.assertRaises(CandleDataError) as caught:
            service.fetch_historical_candles('BAD_VOLUME','week',date(2017,12,4),date(2017,12,10),cache_result=False)
        self.assertEqual(caught.exception.details['raw_value'],'-2070765642')
        self.assertFalse(is_history_connection_error(caught.exception))
        self.assertEqual(service.worker_snapshot()['pending_windows'],0)
        self.assertTrue(service.quote_ready)

    def test_window_pins_generation_until_commit_then_automatically_relogs(self):
        service=self.service()
        self.connect(service)
        original=service._process
        rows=service.fetch_historical_candles('2330','1m',date(2026,10,12),date(2026,10,12),cache_result=False)
        self.assertEqual(len(rows),1)
        self.assertTrue(service.worker_snapshot()['recycle_due'])
        self.assertFalse(service._maybe_recycle_locked())
        self.assertEqual(service._process.pid,original.pid)
        service.history_checkpoint(True)
        self.assertFalse(original.is_alive())
        self.assertEqual(original.exitcode,0)
        self.assertNotEqual(service._process.pid,original.pid)
        self.assertEqual(service.worker_snapshot()['recycle_count'],1)
        self.assertTrue(service.is_subscribed('2330'))
        self.assertEqual(service.worker_snapshot()['pending_windows'],0)

    def test_rollback_unpins_but_does_not_claim_a_successful_commit(self):
        service=self.service()
        self.connect(service)
        pid=service._process.pid
        service.fetch_historical_candles('2330','day',date(2026,10,12),date(2026,10,12),cache_result=False)
        service.history_checkpoint(False)
        self.assertEqual(service.worker_snapshot()['pending_windows'],0)
        self.assertEqual(service._process.pid,pid)
        self.assertEqual(service.worker_snapshot()['recycle_count'],0)

    def test_another_thread_cannot_acknowledge_the_writers_window(self):
        service=self.service()
        self.connect(service)
        pid=service._process.pid
        service.fetch_historical_candles('2330','day',date(2026,10,12),date(2026,10,12),cache_result=False)
        thread=threading.Thread(target=service.history_checkpoint,args=(True,))
        thread.start(); thread.join(2)
        self.assertEqual(service.worker_snapshot()['pending_windows'],1)
        self.assertEqual(service._process.pid,pid)
        service.history_checkpoint(True)
        self.assertNotEqual(service._process.pid,pid)

    def test_interactive_history_does_not_leave_a_persistent_write_pin(self):
        service=self.service()
        self.connect(service)
        service.fetch_historical_candles('2330','day',date(2026,10,12),date(2026,10,12))
        self.assertEqual(service.worker_snapshot()['pending_windows'],0)
        self.assertEqual(len(service.cache.get_historical_candles('2330','day')),1)
        self.assertTrue(service._maybe_recycle_locked())

    def test_new_heavy_requests_wait_while_an_overlimit_window_is_uncommitted(self):
        service=self.service()
        self.connect(service)
        service.fetch_historical_candles('2330','day',date(2026,10,12),date(2026,10,12),cache_result=False)
        failures=[]
        def other_reader():
            try:
                service.fetch_stock_list(0)
            except CapitalApiError as error:
                failures.append(error)
        thread=threading.Thread(target=other_reader)
        thread.start();thread.join(2)
        self.assertEqual(len(failures),1)
        self.assertTrue(is_history_connection_error(failures[0]))
        self.assertEqual(service.worker_snapshot()['pending_windows'],1)
        service.history_checkpoint(True)

    def test_failed_initial_login_does_not_retain_or_auto_retry_bad_credentials(self):
        service=self.service()
        with self.assertRaises(CapitalApiError): service.connect('bad-user','bad-password')
        self.assertIsNone(service._credentials)
        self.assertFalse(service._want_connected)
        self.assertIsNone(service._process)
        self.connect(service)

    def test_repeat_login_is_idempotent_and_does_not_spawn_second_worker(self):
        service=self.service()
        self.connect(service)
        pid=service._process.pid
        self.connect(service)
        self.assertEqual(service._process.pid,pid)
        with self.assertRaises(CapitalApiError):
            service.connect('another-user','another-password')
        self.assertEqual(service._process.pid,pid)

    def test_disconnect_stops_child_clears_credentials_and_cannot_auto_login(self):
        service=self.service()
        self.connect(service)
        original=service._process
        service.disconnect()
        self.assertIsNone(service._credentials)
        self.assertFalse(original.is_alive())
        self.assertFalse(service.quote_ready)
        self.assertIsNone(service._process)
        with self.assertRaises(CapitalApiError): service.fetch_stock_list(0)

    def test_worker_crash_is_connection_failure_and_does_not_leave_a_pin(self):
        service=self.service()
        self.connect(service)
        original=service._process
        with self.assertRaises(CapitalApiError) as raised:
            service.fetch_historical_candles('CRASH','day',date(2026,10,12),date(2026,10,12),cache_result=False)
        self.assertTrue(is_history_connection_error(raised.exception))
        self.assertEqual(service.worker_snapshot()['pending_windows'],0)
        self.connect(service)
        self.assertEqual(original.exitcode,7)
        self.assertNotEqual(service._process.pid,original.pid)

    def test_hung_owned_worker_can_be_reclaimed_without_killing_coordinator(self):
        service=self.service()
        self.connect(service)
        original=service._process
        with self.assertRaises(CapitalApiError):
            service.fetch_historical_candles('HANG','day',date(2026,10,12),date(2026,10,12),cache_result=False)
        service.disconnect()
        self.assertFalse(original.is_alive())
        self.assertEqual(service.worker_snapshot()['forced_exits'],1)

    def test_error_messages_and_telemetry_do_not_expose_credentials(self):
        service=self.service()
        with self.assertRaises(CapitalApiError) as raised:
            service.connect('bad-user','bad-password')
        self.assertNotIn('bad-user',str(raised.exception))
        self.assertNotIn('bad-password',str(raised.exception))
        self.assertNotIn('bad-password',str(service.worker_snapshot()))

    def test_telemetry_failure_does_not_discard_received_window(self):
        class BrokenControl:
            def update(self,**_values): raise RuntimeError('SQLite offline')
        service=self.service(control=BrokenControl())
        self.connect(service)
        service.fetch_historical_candles('2330','day',date(2026,10,12),date(2026,10,12),cache_result=False)
        service.history_checkpoint(True)
        self.assertTrue(service.quote_ready)


class CacheAndConfigTests(unittest.TestCase):
    def test_event_tail_and_api_cache_are_bounded_and_preserve_vendor_time(self):
        cache=ForwardingCache()
        stamp=datetime(2026,10,12,tzinfo=timezone.utc)
        for number in range(700):
            cache.add_tick('2330',Tick(timestamp=stamp,price=1,volume=number))
        cache.upsert_quote(Quote(symbol='2330',price=1,updated_at=stamp))
        cache.upsert_best5('2330',[(100,10)],[(200,20)])
        events=cache.drain()
        self.assertEqual(len(events['ticks']['2330']),500)
        destination=QuoteCache()
        destination.apply_native_events(events)
        self.assertEqual(destination.get_quote('2330').updated_at,stamp)
        self.assertEqual(len(destination.get_ticks('2330')),500)
        self.assertEqual(destination.get_best5('2330').bids[0].price,1)
        self.assertEqual(cache.drain()['ticks'],{})
        destination.reset_native_generation()
        self.assertEqual(destination.get_ticks('2330'),[])
        self.assertIsNone(destination.get_quote('2330'))

    def test_invalid_thresholds_fail_closed(self):
        for raw in ('0','nan','inf','4096','9999','garbage'):
            with self.subTest(raw=raw),patch.dict(os.environ,{'CAPITAL_WORKER_RECYCLE_MB':raw,'API_MEMORY_STOP_BACKFILL_MB':'4096'}):
                with self.assertRaises(ValueError): NativeWorkerConfig.from_env()


if __name__=='__main__': unittest.main()
