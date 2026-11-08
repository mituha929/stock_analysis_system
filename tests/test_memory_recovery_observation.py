"""Recovery polls use fake time/memory and an isolated SQLite control file."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.services.backfill_control import BackfillControl, BackfillRecovery
from app.services.backfill_memory import mark_memory_pause, memory_resume_ready
from app.services.process_memory import ProcessMemory


class RecoveryObservationTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory(prefix="memory-observer-test-"))
        self.enterContext(patch.dict(os.environ, {
            "API_MEMORY_WARNING_MB": "2048", "API_MEMORY_STOP_BACKFILL_MB": "4096",
            "API_MEMORY_RESUME_BACKFILL_MB": "3072",
            "API_MEMORY_RESUME_STABLE_SECONDS": "60", "API_MEMORY_RESUME_COOLDOWN_SECONDS": "120",
        }))
        self.start = datetime(2026, 10, 16, 10, tzinfo=timezone.utc)
        self.control = BackfillControl(Path(folder) / "control.sqlite3")
        self.plans = [dict(market_no=0, interval="day", end_date="2026-10-16", target_bars=1200)]
        self.control.update(source="api", state="paused", pause_kind="memory",
            pause_reason="original memory pause", pause_requested=False, plans=self.plans,
            target_bars=1200, memory_resume_ready=False,
            memory_retry_after=(self.start + timedelta(seconds=120)).isoformat())
        self.capital = SimpleNamespace(quote_ready=True)
        self.callback = Mock()
        self.cache = Mock()
        self.recovery = BackfillRecovery(self.capital, self.callback, self.cache)
        self.worker_active = False

    def poll(self, offsets, trading=True, memories=None, on_observed=None):
        ticks = iter(offsets)
        values = iter(memories or [ProcessMemory(200, 250)] * len(offsets))
        observed = []

        def observe(control):
            second = next(ticks)
            result = memory_resume_ready(control, self.start + timedelta(seconds=second), next(values))
            observed.append(result)
            if on_observed:
                on_observed(second)
            return result

        self.recovery.stop_event = Mock()
        self.recovery.stop_event.wait.side_effect = [False] * len(offsets) + [True]
        with patch("app.services.backfill_control.BackfillControl", return_value=self.control), \
             patch.object(self.control, "worker_active", return_value=self.worker_active), \
             patch("app.services.backfill_control.memory_resume_ready", side_effect=observe), \
             patch("app.services.taiwan_trading_calendar.trading_day_status", return_value=(trading, "fake calendar")):
            self.recovery._run()
        for call in self.recovery.stop_event.wait.call_args_list:
            self.assertEqual(call.args, (15,))
        return observed

    def assert_observed_and_preserved(self, last_offset):
        state = self.control.snapshot()
        self.assertEqual(state["memory_observed_at"], (self.start + timedelta(seconds=last_offset)).isoformat())
        self.assertTrue(state["memory_resume_ready"])
        self.assertEqual(state["memory_private_mb"], 250)
        self.assertEqual(state["plans"], self.plans)
        self.assertEqual(state["target_bars"], 1200)
        self.assertEqual(state["pause_kind"], "memory")
        self.assertEqual(state["pause_reason"], "original memory pause")
        return state

    def test_holiday_still_observes_until_ready_but_never_resumes(self):
        observed = self.poll([0, 15, 60, 119, 120], trading=False)
        self.assertEqual(observed, [False, False, False, False, True])
        self.assert_observed_and_preserved(120)
        self.callback.assert_not_called()
        self.assertEqual(self.cache.prune_historical.call_count, 5)

    def test_disconnected_still_observes_but_never_resumes_or_logs_in(self):
        self.capital.quote_ready = False
        self.poll([0, 120])
        self.assert_observed_and_preserved(120)
        self.callback.assert_not_called()

    def test_manual_pause_still_observes_without_clearing_user_pause(self):
        self.control.pause(True)
        self.poll([0, 120])
        state = self.assert_observed_and_preserved(120)
        self.assertTrue(state["pause_requested"])
        self.callback.assert_not_called()

    def test_worker_active_still_observes_without_starting_second_worker(self):
        self.worker_active = True
        self.poll([0, 120])
        self.assert_observed_and_preserved(120)
        self.callback.assert_not_called()

    def test_retry_backoff_still_observes_but_does_not_resume_early(self):
        self.control.update(state="waiting_backfill", next_retry_at="9999-01-01T00:00:00+00:00")
        self.poll([0, 120])
        self.assert_observed_and_preserved(120)
        self.callback.assert_not_called()

    def test_unready_memory_does_not_resume_even_if_other_gates_pass(self):
        self.assertEqual(self.poll([0, 60, 119]), [False, False, False])
        self.callback.assert_not_called()
        self.assertFalse(self.control.snapshot()["memory_resume_ready"])

    def test_resumes_once_after_all_conditions_recover_and_keeps_scope(self):
        self.capital.quote_ready = False
        self.poll([0, 120])
        self.callback.assert_not_called()
        self.capital.quote_ready = True
        self.assertEqual(self.poll([135]), [True])
        self.callback.assert_called_once_with(self.capital, self.control.saved_plans(), 1200)
        self.assert_observed_and_preserved(135)

    def test_memory_rise_and_missing_measurement_reset_stability(self):
        low = ProcessMemory(200, 250)
        values = [low, low, ProcessMemory(200, 3072), ProcessMemory(None, None), low, low]
        observed = self.poll([0, 60, 120, 135, 150, 210], trading=False, memories=values)
        self.assertEqual(observed, [False, False, False, False, False, True])
        self.assertEqual(self.control.snapshot()["memory_resume_below_since"],
                         (self.start + timedelta(seconds=150)).isoformat())
        self.callback.assert_not_called()

    def test_new_pause_invalidates_previous_ready_flag_and_timestamp(self):
        self.control.update(memory_resume_ready=True, memory_observed_at=self.start.isoformat(),
                            memory_resume_below_since=self.start.isoformat())
        mark_memory_pause(self.control)
        state = self.control.snapshot()
        self.assertFalse(state["memory_resume_ready"])
        self.assertIsNone(state["memory_observed_at"])
        self.assertIsNone(state["memory_resume_below_since"])

    def test_api_observer_does_not_measure_memory_for_cli_owner(self):
        self.control.update(source="cli")
        self.assertEqual(self.poll([0, 120]), [])
        self.assertFalse(self.control.snapshot()["memory_resume_ready"])
        self.assertNotIn("memory_observed_at", self.control.snapshot())
        self.callback.assert_not_called()

    def test_manual_pause_arriving_during_observation_is_rechecked(self):
        self.poll([0, 120], on_observed=lambda _second: self.control.pause(True))
        self.assert_observed_and_preserved(120)
        self.callback.assert_not_called()

    def test_empty_saved_work_observes_but_cannot_start_a_job(self):
        self.control.update(plans=[], resume_scope=None, pending_schedule=None)
        self.poll([0, 120])
        self.assertTrue(self.control.snapshot()["memory_resume_ready"])
        self.callback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
