"""Offline safety checks: never load Capital DLL or connect to MySQL."""
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'test_capital_worker_recycle_live.py'
SPEC = importlib.util.spec_from_file_location('capital_recycle_harness_under_test', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class HarnessSafetyTests(unittest.TestCase):
    def test_cleanup_does_not_terminate_an_already_exited_interpreter(self):
        def exit_code(_handle, pointer):
            pointer._obj.value = 1
            return True
        kernel=SimpleNamespace(GetExitCodeProcess=exit_code,TerminateProcess=unittest.mock.Mock())
        self.assertFalse(MODULE.terminate_owned_if_running(kernel,123))
        kernel.TerminateProcess.assert_not_called()

    def test_cleanup_accepts_a_worker_exiting_between_query_and_termination(self):
        codes=iter((259,1))
        def exit_code(_handle,pointer):
            pointer._obj.value=next(codes)
            return True
        kernel=SimpleNamespace(GetExitCodeProcess=exit_code,TerminateProcess=unittest.mock.Mock(return_value=False))
        self.assertTrue(MODULE.terminate_owned_if_running(kernel,123))

    def test_cleanup_reports_a_still_running_owned_worker_it_cannot_terminate(self):
        def exit_code(_handle,pointer):
            pointer._obj.value=259
            return True
        kernel=SimpleNamespace(GetExitCodeProcess=exit_code,TerminateProcess=unittest.mock.Mock(return_value=False))
        with self.assertRaisesRegex(RuntimeError,'Could not stop'):
            MODULE.terminate_owned_if_running(kernel,123)

    def test_only_generated_test_schema_is_accepted(self):
        self.assertEqual(MODULE.validate_schema('capital_dll_test_012345abcdef'),
                         'capital_dll_test_012345abcdef')
        for value in ('capital_stock_analysis', 'capital_dll_test_',
                      'capital_dll_test_012345abcdef;DROP DATABASE x', 'mysql'):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                MODULE.validate_schema(value)

    def test_only_owned_diagnostic_directory_is_accepted(self):
        root = MODULE.PROJECT / 'output' / 'diagnostics'
        expected = (root / 'dll-recycle-012345abcdef').resolve()
        self.assertEqual(MODULE.validate_run_dir(expected), expected)
        for path in (MODULE.PROJECT, root, root/'other', root/'nested'/'dll-recycle-012345abcdef'):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                MODULE.validate_run_dir(path)

    def test_redacts_nested_credentials_and_error_messages(self):
        with patch.object(MODULE, 'SECRETS', ('secret-id', 'secret-password')):
            result = MODULE.redact({'password':'x', 'rows':[{'input':'x'}],
                                    'error':'secret-id / secret-password'})
        self.assertEqual(result['password'], '[REDACTED]')
        self.assertEqual(result['rows'][0]['input'], '[REDACTED]')
        self.assertNotIn('secret-', result['error'])

    def test_live_handoff_requires_explicit_opt_in_before_any_api_call(self):
        with patch.object(MODULE, 'api') as http, patch.object(MODULE, 'load_config') as config:
            with self.assertRaises(RuntimeError):
                MODULE.parent_main(SimpleNamespace(allow_live_handoff=False, api_pid=123))
            http.assert_not_called()
            config.assert_not_called()

    def test_http_failure_does_not_expose_request_or_validation_body_or_retry(self):
        error = urllib.error.HTTPError('http://127.0.0.1:8000/',422,'bad',{},io.BytesIO(b'secret-password'))
        with patch.object(MODULE.urllib.request, 'urlopen', side_effect=error) as open_request:
            with self.assertRaisesRegex(RuntimeError, 'HTTP 422') as raised:
                MODULE.api('/capital/connect', {'password':'secret-password'})
            self.assertNotIn('secret-password', str(raised.exception))
            self.assertEqual(open_request.call_count,1)


if __name__=='__main__':
    unittest.main()
