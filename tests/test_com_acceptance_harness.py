"""Safety/validation tests for the explicitly opted-in COM live harness."""
from datetime import date, datetime
import importlib.util
import io
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.schemas.models import Candle

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'test_com_function_recycle_live.py'
SPEC = importlib.util.spec_from_file_location('com_acceptance_harness', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ComAcceptanceSafetyTests(unittest.TestCase):
    def test_opt_in_required_before_api_credentials_or_database_access(self):
        with patch.object(MODULE, 'check_idle') as api, patch('sys.stderr', new=io.StringIO()):
            with self.assertRaises(SystemExit):
                MODULE.main([])
            api.assert_not_called()

    def test_schema_must_be_this_invocations_exact_token(self):
        token = '012345abcdef'
        expected = 'capital_com_accept_' + token
        self.assertEqual(MODULE.owned_schema(expected, token), expected)
        for schema in ('capital_stock_analysis', 'mysql', 'capital_com_accept_ffffffffffff',
                       expected+';DROP DATABASE x'):
            with self.subTest(schema=schema), self.assertRaises(RuntimeError):
                MODULE.owned_schema(schema, token)
        with self.assertRaises(RuntimeError):
            MODULE.owned_schema(expected, '')

    def test_only_disconnected_idle_manually_paused_api_allowed(self):
        good = dict(quote_ready=False, native_pid=None, worker_active=False, pause_requested=True)
        with patch.object(MODULE, 'api_brief', return_value=good):
            self.assertEqual(MODULE.check_idle(), good)
        for change in (dict(quote_ready=True), dict(native_pid=1), dict(worker_active=True),
                       dict(pause_requested=False), dict(quote_ready=None)):
            with self.subTest(change=change), patch.object(MODULE, 'api_brief', return_value=good | change):
                with self.assertRaises(RuntimeError):
                    MODULE.check_idle()

    def test_control_hash_is_read_only_and_missing_file_not_created(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'control.sqlite3'
            with self.assertRaises(RuntimeError): MODULE.control_digest(path)
            self.assertFalse(path.exists())
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE control(id INTEGER PRIMARY KEY,payload TEXT)')
            db.execute("INSERT INTO control VALUES(1,?)", ('{"pause_requested":true}',))
            db.commit(); db.close()
            before = path.read_bytes()
            self.assertEqual(MODULE.control_digest(path), MODULE.control_digest(path))
            self.assertEqual(path.read_bytes(), before)

    def test_own_exclusive_worker_lease_is_not_misread_as_a_running_job(self):
        state = dict(quote_ready=False, native_pid=None, worker_active=True, pause_requested=True)
        with patch.object(MODULE, 'api_brief', return_value=state):
            self.assertEqual(MODULE.check_idle(own_worker_lease=True), state)
            with self.assertRaises(RuntimeError): MODULE.check_idle()
        with patch.object(MODULE, 'api_brief', return_value=state | dict(quote_ready=True)):
            with self.assertRaises(RuntimeError): MODULE.check_idle(own_worker_lease=True)

    def bar(self, **values):
        return Candle(**(dict(timestamp=datetime(2026, 10, 22), open=10, high=12, low=9,
                              close=11, volume=100) | values))

    def test_only_real_nonempty_unique_in_range_valid_prices_and_volume(self):
        day = date(2026, 10, 22)
        MODULE.validate_rows([self.bar()], day, day)
        for rows in ([], [self.bar(), self.bar()], [self.bar(timestamp=datetime(2026, 10, 21))],
                     [self.bar(high=9)], [self.bar(low=11)], [self.bar(close=0)],
                     [self.bar(close=float('inf'))]):
            with self.subTest(rows=rows), self.assertRaises(RuntimeError):
                MODULE.validate_rows(rows, day, day)

    def test_negative_volume_is_not_coerced_or_written(self):
        day = date(2026, 10, 22)
        with self.assertRaises(Exception): MODULE.validate_rows([self.bar(volume=-1)], day, day)

    def test_lowered_trigger_uses_real_usage_not_fake_memory_or_required_leak(self):
        self.assertEqual(MODULE.choose_trigger(250, 400), 360)
        self.assertEqual(MODULE.choose_trigger(250, 250), 225)
        for baseline, current in ((250, 0), (250, None), (None, 400), (300, float('inf'))):
            with self.subTest(baseline=baseline, current=current), self.assertRaises(RuntimeError):
                MODULE.choose_trigger(baseline, current)

    def test_available_cutoff_never_claims_missing_newest_day_is_complete(self):
        rows = [dict(symbol=s, interval='day', data_last=datetime(2026, 10, 22)) for _, s in MODULE.SYMBOLS]
        self.assertEqual(MODULE.available_cutoff(rows, date(2026, 10, 23)), date(2026, 10, 22))
        rows[1]['data_last'] = datetime(2026, 10, 21)
        self.assertEqual(MODULE.available_cutoff(rows, date(2026, 10, 23)), date(2026, 10, 21))
        with self.assertRaises(RuntimeError): MODULE.available_cutoff(rows[:2], date(2026, 10, 23))

    def test_smoke_ranges_are_bounded_and_keep_period_start_labels(self):
        reference = date(2026, 10, 22)
        for interval in MODULE.INTERVALS:
            first, last = MODULE.test_range(interval, reference)
            self.assertLessEqual(first, last)
            self.assertLessEqual(last, reference)
            self.assertLess((last-first).days, 140)
            if interval == 'week': self.assertEqual(first.weekday(), 0)
            if interval == 'month': self.assertEqual(first.day, 1)
            if interval.endswith('m'): self.assertEqual(first, last)


if __name__ == '__main__':
    unittest.main()
