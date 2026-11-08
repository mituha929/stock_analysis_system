"""No real DLL/login: shared event routing, sanitized diagnostics and timeout."""
from datetime import datetime
import json
import os
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from app.api.routes import health
from app.services.capital_connection_diagnostics import ConnectionDiagnostics, connection_source
from app.services.capital_dll_quote_service import CapitalApiError, CapitalQuoteService
from app.services.capital_worker_runtime import MemoryWatchlistStore, native_worker_main
from app.services.capital_worker_service import CapitalWorkerService, NativeWorkerConfig
from app.services.quote_cache import QuoteCache


def service():
    result=CapitalQuoteService(QuoteCache(),watchlist_store=MemoryWatchlistStore({}))
    result._process_lock=Mock()
    result._history_client=Mock()
    result._started=True
    result._SK=Mock()
    return result


class ConnectionRoutingTests(unittest.TestCase):
    def test_only_exact_domestic_quote_source_can_set_ready(self):
        for source in ('account-id','SKProxy','',None,b'account-id','SKQuote-other'):
            with self.subTest(source=source):
                s=service();s._on_connection(source,3003)
                self.assertFalse(s.quote_ready)
                self.assertEqual(s.connection_events,[])
                event=s.connection_diagnostics['events'][-1]
                self.assertFalse(event['applied'])
                self.assertEqual(event['channel'],'other')

    def test_reply_disconnect_or_connect_does_not_change_ready_quote_state(self):
        for code in (3001,3002,3033,5001):
            s=service();s._on_connection('SKQuote',3003)
            s._on_connection('account-id',code)
            self.assertTrue(s.quote_ready)
            self.assertEqual(s.quote_connection_state,'connected')
            self.assertEqual(s.status,'connected')
            self.assertEqual(s.connection_events,[(3003,0)])

    def test_quote_connected_is_not_ready_until_quote_3003(self):
        s=service();s._on_connection('SKQuote',3001)
        self.assertFalse(s.quote_ready)
        self.assertEqual(s.quote_connection_state,'connecting')
        s._on_connection('SKQuote',3003)
        self.assertTrue(s.quote_ready)

    def test_real_quote_disconnect_and_reconnect_clear_old_readiness(self):
        for code in (3001,3002,3033):
            s=service();s._on_connection('SKQuote',3003)
            s._on_connection('SKQuote',code)
            self.assertFalse(s.quote_ready)
            self.assertEqual(s.connection_diagnostics['last_quote_code'],code)

    def test_byte_source_is_supported_without_leaking_account_ids(self):
        self.assertEqual(connection_source(b'SKQuote'),('domestic_quote','SKQuote'))
        self.assertEqual(connection_source(b'private-account'),('other','[REDACTED]'))
        s=service();s._on_connection(b'SKQuote',3003)
        self.assertTrue(s.quote_ready)

    def test_cleanup_disconnect_is_tagged_and_late_ready_does_not_resurrect(self):
        s=service();s._on_connection('SKQuote',3003)
        def disconnect(*args):
            self.assertEqual(args,('',1,1))
            s._on_connection('SKQuote',3002)
            s._on_connection('account-id',3002)
            return 0
        s._SK.ManageServerConnection.side_effect=disconnect
        s.stop()
        s._on_connection('SKQuote',3003)
        self.assertFalse(s.quote_ready)
        events=[e for e in s.connection_diagnostics['events'] if e['kind']=='connection_event']
        self.assertTrue(all(e['during_cleanup'] for e in events[1:]))
        self.assertTrue(all(not e['applied'] for e in events[1:]))
        self.assertEqual(s.connection_diagnostics['phase'],'stopped')

    def test_unrelated_ready_does_not_trigger_subscriptions(self):
        s=service();s._subscriptions_pending=True
        with patch.object(s,'_subscribe_pending') as restore, \
             patch.dict('sys.modules',{'pythoncom':SimpleNamespace(PumpWaitingMessages=lambda:None)}):
            s._on_connection('account-id',3003)
            s.poll()
            restore.assert_not_called()
            s._on_connection('SKQuote',3003)
            restore.assert_not_called()  # Never from inside the SDK callback.
            s.poll()
            restore.assert_called_once()


class ConnectionDiagnosticTests(unittest.TestCase):
    def connect(self,s,login_code=0,quote_code=0):
        s._SK.Login.return_value=SimpleNamespace(Code=login_code)
        def opening(*args):
            self.assertEqual(args,('',0,1))
            return quote_code
        s._SK.ManageServerConnection.side_effect=opening
        s.poll=Mock(side_effect=lambda:s._announcement_received.set())
        return s.connect('private-user','private-password')

    def test_calls_record_actual_return_codes_without_arguments(self):
        s=service();self.connect(s)
        events=s.connection_diagnostics['events']
        calls=[e for e in events if e['kind']=='sdk_return']
        self.assertEqual([(e['operation'],e['return_code']) for e in calls],
                         [('Login',0),('ManageServerConnection.connect',0)])
        self.assertEqual(s.connection_diagnostics['phase'],'waiting_quote_ready')
        self.assertIsNotNone(s.connection_diagnostics['started_at'])
        self.assertTrue(all('elapsed_ms' in e for e in events))
        for e in events:datetime.fromisoformat(e['at'])
        self.assertNotIn('private-user',json.dumps(s.connection_diagnostics))
        self.assertNotIn('private-password',json.dumps(s.connection_diagnostics))

    def test_failed_login_never_calls_manage_and_records_sdk_code(self):
        s=service()
        with self.assertRaises(CapitalApiError):self.connect(s,login_code=2017)
        s._SK.ManageServerConnection.assert_not_called()
        self.assertEqual(s.connection_diagnostics['phase'],'connect_failed')
        self.assertTrue(any(e.get('return_code')==2017 for e in s.connection_diagnostics['events']))

    def test_failed_manage_is_not_misreported_as_waiting_for_ready(self):
        s=service()
        with self.assertRaises(CapitalApiError):self.connect(s,quote_code=1095)
        self.assertEqual(s.connection_diagnostics['phase'],'connect_failed')
        self.assertTrue(any(e.get('return_code')==1095 for e in s.connection_diagnostics['events']))

    def test_raw_event_error_code_is_unknown_not_a_fabricated_success(self):
        s=service();s._on_connection('SKQuote',3001)
        event=s.connection_diagnostics['events'][-1]
        self.assertEqual(event['code'],3001)
        self.assertIsNone(event['error_code'])
        self.assertEqual(s.connection_events,[(3001,0)])  # Legacy wire format.

    def test_login_ids_and_announcement_contents_are_never_retained(self):
        s=service()
        s._on_connection('private-user',3002)
        s._on_reply_message('private-user','private-password account=123456789 token=secret')
        encoded=json.dumps(s.connection_diagnostics)
        for sensitive in ('private-user','private-password','123456789','token=secret'):
            self.assertNotIn(sensitive,encoded)
        self.assertTrue(s._announcement_received.is_set())

    def test_bounded_buffer_and_snapshot_mutation_are_safe(self):
        s=service()
        for n in range(200):s._on_connection('SKQuote',3001)
        snapshot=s.connection_diagnostics
        self.assertEqual(len(snapshot['events']),50)
        self.assertEqual(len(s.connection_events),10)
        self.assertEqual(snapshot['event_count'],200)
        snapshot['events'][0]['source']='tampered'
        self.assertNotEqual(s.connection_diagnostics['events'][0]['source'],'tampered')

    def test_invalid_event_is_ignored_without_logging_untrusted_value(self):
        s=service();s._on_connection('private-user','private-password')
        self.assertFalse(s.quote_ready)
        self.assertNotIn('private-password',str(s.connection_diagnostics))

    def test_timeout_snapshot_precedes_cleanup_and_deadline_race_is_ready(self):
        s=service();self.connect(s)
        s._on_connection('account-id',3001)
        self.assertTrue(s.mark_connection_timeout())
        failed=s.connection_diagnostics
        self.assertEqual(failed['phase'],'timeout')
        self.assertIsNone(failed['last_quote_code'])
        s._SK.ManageServerConnection.side_effect=lambda *args:0
        s.stop()
        self.assertEqual(failed['phase'],'timeout')
        self.assertFalse(any(e['kind']=='cleanup_start' for e in failed['events']))
        s=service();s._on_connection('SKQuote',3003)
        self.assertFalse(s.mark_connection_timeout())
        self.assertEqual(s.connection_diagnostics['phase'],'quote_ready')

    def test_native_callbacks_are_not_blocked_by_the_dll_call_lock(self):
        s=service();finished=threading.Event()
        with s._lock:
            t=threading.Thread(target=lambda:(s._on_connection('SKQuote',3003),finished.set()))
            t.start()
            self.assertTrue(finished.wait(1))
        t.join(1)
        self.assertTrue(s.quote_ready)

    def test_health_exposes_safe_structured_diagnostics(self):
        s=service();s._on_connection('private-user',3002);s._on_connection('SKQuote',3003)
        response=health(s)
        self.assertTrue(response.quote_ready)
        self.assertEqual(response.connection_diagnostics['last_quote_code'],3003)
        self.assertNotIn('private-user',response.model_dump_json())


class QuoteLoginIdCompatibilityTests(unittest.TestCase):
    def service(self, codes):
        s = service()
        s._SK.Login.return_value = SimpleNamespace(Code=0)
        s._SK.ManageServerConnection.side_effect = codes
        s.poll = Mock(side_effect=lambda:s._announcement_received.set())
        return s

    def test_login_first_uses_same_authenticated_id_once_without_relogin(self):
        s = self.service([1000, 0])
        s.connect('private-user', 'private-password')
        s._SK.Login.assert_called_once_with('private-user', 'private-password', 0)
        self.assertEqual([c.args for c in s._SK.ManageServerConnection.call_args_list],
                         [('', 0, 1), ('private-user', 0, 1)])
        # A successful call is not itself a quote-ready event.
        self.assertFalse(s.quote_ready)
        s._on_connection('private-user', 3003)
        self.assertFalse(s.quote_ready)
        s._on_connection('SKQuote', 3003)
        self.assertTrue(s.quote_ready)
        snapshot = json.dumps(s.connection_diagnostics)
        self.assertNotIn('private-user', snapshot)
        self.assertNotIn('private-password', snapshot)
        calls = [e for e in s.connection_diagnostics['events']
                 if e['kind'] == 'sdk_return' and e['operation'].endswith('.connect')]
        self.assertEqual([(e['login_id_mode'], e['return_code']) for e in calls],
                         [('empty', 1000), ('authenticated_user', 0)])

    def test_successful_empty_id_does_not_use_fallback(self):
        s = self.service([0])
        s.connect('private-user', 'private-password')
        s._SK.ManageServerConnection.assert_called_once_with('', 0, 1)
        self.assertFalse(s._quote_use_login_id)

    def test_other_quote_errors_do_not_use_fallback(self):
        s = self.service([1095])
        with self.assertRaisesRegex(CapitalApiError, '1095'):
            s.connect('private-user', 'private-password')
        s._SK.ManageServerConnection.assert_called_once_with('', 0, 1)

    def test_login_first_from_login_does_not_call_quote_or_retry_login(self):
        s = self.service([])
        s._SK.Login.return_value = SimpleNamespace(Code=1000)
        with self.assertRaisesRegex(CapitalApiError, '1000'):
            s.connect('private-user', 'private-password')
        s._SK.Login.assert_called_once()
        s._SK.ManageServerConnection.assert_not_called()
        self.assertIsNone(s._credentials)

    def test_fallback_failure_propagates_actual_code_and_never_loops(self):
        for code in (1000, 1095):
            with self.subTest(code=code):
                s = self.service([1000, code])
                with self.assertRaisesRegex(CapitalApiError, str(code)):
                    s.connect('private-user', 'private-password')
                self.assertEqual(s._SK.ManageServerConnection.call_count, 2)
                s._SK.Login.assert_called_once()
                self.assertFalse(s.quote_ready)
                self.assertEqual(s.connection_diagnostics['phase'], 'connect_failed')

    def test_cleanup_matches_fallback_and_reconnect_keeps_authenticated_session(self):
        s = self.service([1000, 0, 0, 1000, 0])
        s.connect('private-user', 'private-password')
        s._on_connection('SKQuote', 3003)
        s.stop()
        s._SK.ManageServerConnection.assert_called_with('private-user', 1, 1)
        self.assertFalse(s.quote_ready)
        s.connect('private-user', 'private-password')
        s._SK.Login.assert_called_once()
        self.assertEqual([c.args for c in s._SK.ManageServerConnection.call_args_list][-2:],
                         [('', 0, 1), ('private-user', 0, 1)])
        self.assertFalse(s.quote_ready)
        s._on_connection('SKQuote', 3003)
        self.assertTrue(s.quote_ready)

    def test_fallback_exception_is_logged_without_untrusted_exception_text(self):
        s = self.service([1000, RuntimeError('private-user private-password')])
        with self.assertRaises(RuntimeError):
            s.connect('private-user', 'private-password')
        self.assertEqual(s._SK.ManageServerConnection.call_count, 2)
        self.assertNotIn('private-user', json.dumps(s.connection_diagnostics))
        self.assertNotIn('private-password', json.dumps(s.connection_diagnostics))


class NativeRuntimeDiagnosticTests(unittest.TestCase):
    def run_worker(self,pump):
        s=service()
        def login(*args,**kwargs):
            s._connection_diagnostics.begin()
            s._connection_diagnostics.phase='waiting_quote_ready'
            return ('connecting','waiting',True)
        s.connect=Mock(side_effect=login)
        s._SK.ManageServerConnection.side_effect=lambda *args:0
        requests=iter([dict(id=1,command='connect',arguments=dict(user_id='private-user',password='private-password')),
                       dict(id=2,command='shutdown')])
        pipe=Mock();pipe.poll.return_value=True;pipe.recv.side_effect=lambda:next(requests)
        counter=[0]
        def pumping():
            counter[0]+=1
            if counter[0]>1:pump(s)
        with patch('app.services.capital_dll_quote_service.CapitalQuoteService',return_value=s), \
             patch.dict('sys.modules',{'pythoncom':SimpleNamespace(PumpWaitingMessages=pumping)}), \
             patch('app.services.capital_worker_runtime.time.monotonic',return_value=0), \
             patch('app.services.capital_worker_runtime.time.sleep'):
            native_worker_main(pipe,dict(watchlist={},connect_timeout=0,quote_backend='dll'))
        messages=[call.args[0] for call in pipe.send.call_args_list]
        return messages

    def test_pending_ready_callback_is_pumped_before_deadline_failure(self):
        messages=self.run_worker(lambda s:s._on_connection('SKQuote',3003))
        reply=messages[1]
        self.assertTrue(reply['ok'])
        self.assertTrue(reply['state']['quote_ready'])
        self.assertEqual(reply['state']['connection_diagnostics']['last_quote_code'],3003)

    def test_other_channel_does_not_pass_runtime_wait_and_failure_has_pre_cleanup_details(self):
        messages=self.run_worker(lambda s:s._on_connection('private-user',3003))
        reply=messages[1]
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['kind'],'connection')
        self.assertEqual(reply['state']['connection_diagnostics']['phase'],'timeout')
        self.assertNotIn('private-user',json.dumps(reply))
        self.assertEqual(messages[2]['state']['connection_diagnostics']['phase'],'stopped')


def diagnostic_native_worker(pipe,config):
    """Actual spawned IPC process, only synthetic events and synthetic secrets."""
    state=dict(pid=os.getpid(),quote_ready=False,status_message='fake worker',private_mb=20,
               connection_events=[],connection_diagnostics=dict(phase='idle',events=[]))
    pipe.send(dict(id=0,ok=True,state=state,events={}))
    while True:
        try:request=pipe.recv()
        except EOFError:break
        command=request['command'];ok=True;extra={};result=None
        if command=='connect':
            if request['arguments']['user_id']=='fail-user':
                state.update(quote_ready=False,connection_events=[],connection_diagnostics=dict(
                    phase='timeout',last_quote_code=None,events=[dict(kind='connection_event',source='[REDACTED]',
                    channel='other',code=3001,error_code=None,applied=False)]))
                ok=False;extra=dict(error='waiting for SKQuote/3003 timed out',kind='connection')
            else:
                state.update(quote_ready=True,connection_events=[(3003,0)],quote_connection_state='connected',
                             connection_diagnostics=dict(phase='quote_ready',last_quote_code=3003,events=[]))
                result=('connected','fake quote ready',True)
        elif command=='shutdown':
            state.update(quote_ready=False,connection_events=[(3002,0)],connection_diagnostics=dict(
                phase='stopped',last_quote_code=None,events=[dict(kind='connection_event',source='SKQuote',
                channel='domestic_quote',code=3002,error_code=None,applied=False,during_cleanup=True)]))
        pipe.send(dict(id=request['id'],ok=ok,state=state,result=result,events={},**extra))
        if command=='shutdown':break
    pipe.close()


class SupervisorDiagnosticTests(unittest.TestCase):
    def service(self):
        s=CapitalWorkerService(QuoteCache(),watchlist_store=MemoryWatchlistStore({}),start_monitor=False,
            config=NativeWorkerConfig(recycle_mb=300,connect_timeout=5,rpc_timeout=2,stop_timeout=0.2),
            worker_target=diagnostic_native_worker)
        self.addCleanup(s.stop)
        return s

    def test_failure_snapshot_is_preserved_before_cleanup_and_credentials_are_not_exposed(self):
        s=self.service()
        with self.assertRaises(CapitalApiError):s.connect('fail-user','private-password')
        state=s.worker_snapshot()
        self.assertIsNone(state['pid'])
        self.assertEqual(state['connection_diagnostics']['phase'],'stopped')
        failed=state['last_connect_failure']
        self.assertEqual(failed['connection_diagnostics']['phase'],'timeout')
        self.assertEqual(failed['connection_events'],[])
        self.assertFalse(any(e.get('during_cleanup') for e in failed['connection_diagnostics']['events']))
        encoded=json.dumps(state)
        self.assertNotIn('fail-user',encoded)
        self.assertNotIn('private-password',encoded)
        failed['connection_diagnostics']['phase']='tampered'
        self.assertEqual(s.worker_snapshot()['last_connect_failure']['connection_diagnostics']['phase'],'timeout')

    def test_successful_next_attempt_clears_old_failure_and_exposes_ready_diagnostics(self):
        s=self.service()
        with self.assertRaises(CapitalApiError):s.connect('fail-user','private-password')
        s.connect('good-user','private-password')
        self.assertTrue(s.quote_ready)
        self.assertIsNone(s.worker_snapshot()['last_connect_failure'])
        self.assertIsNone(s.worker_snapshot()['last_error'])
        self.assertEqual(s.connection_diagnostics['last_quote_code'],3003)


if __name__=='__main__':unittest.main()
