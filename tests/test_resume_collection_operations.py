"""Offline checks for the non-regrabbing collection-only operations helper."""
from copy import deepcopy
import importlib.util
from datetime import date
from pathlib import Path
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1]/'scripts'/'resume_collection_only.py'
SPEC = importlib.util.spec_from_file_location('collection_resume_operations', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CollectionResumeOperationsTests(unittest.TestCase):
    def plans(self):
        return [dict(market_no=m, interval=i, end_date='2026-10-21', as_of_date=None,
            start_date='2025-01-01', planned_on='2026-10-22', refresh_bars=10, force=False)
            for m in (0, 1, 4, 5, 6) for i in ('day','1m','5m','15m','60m','week','month')]

    def test_only_as_of_changes_original_scope_ranges_counts_and_order_stay(self):
        plans = self.plans()
        original = deepcopy(plans)
        result = MODULE.pin_collection(plans, date(2026,10,22))
        self.assertEqual(plans, original)
        self.assertEqual(len(result), 35)
        for old, new in zip(original, result):
            self.assertEqual(new, old | dict(as_of_date='2026-10-22'))

    def test_refuses_missing_duplicate_forced_fixed_or_explicit_ranges(self):
        for changed in ('missing','duplicate','force','fixed_range','explicit_start','future'):
            plans = self.plans()
            if changed == 'missing': plans.pop()
            elif changed == 'duplicate': plans[-1] = deepcopy(plans[0])
            elif changed == 'future': plans[0]['end_date'] = '2026-10-23'
            else: plans[0][changed] = '2026-01-01' if changed == 'explicit_start' else True
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                MODULE.pin_collection(plans, date(2026,10,22))

    def test_requires_exact_idle_disconnected_pid_before_preparation(self):
        health = dict(quote_ready=False, native_worker=dict(api_pid=10,pid=None))
        control = dict(pause_requested=True,worker_active=False,preparation_complete=True)
        MODULE.idle_guard(health, control, 10)
        for h, c, pid in ((health,control,11), (health|dict(quote_ready=True),control,10),
                          (health, control|dict(worker_active=True),10),
                          (health, control|dict(pause_requested=False),10),
                          (health, control|dict(preparation_pending=True),10),
                          (health, control|dict(pending_schedule={'x':1}),10)):
            with self.assertRaises(RuntimeError): MODULE.idle_guard(h,c,pid)

    def test_explicit_opt_in_before_reading_credentials_or_calling_api(self):
        with patch.object(MODULE.support,'load_config') as config, patch.object(MODULE,'live_state') as api:
            with self.assertRaises(RuntimeError):
                MODULE.main(['prepare','--api-pid','10','--manifest','collection-resume-x.json'])
            config.assert_not_called()
            api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
