"""Run ONLY offline backfill tests, with real MySQL/DLL access blocked."""

from contextlib import ExitStack
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from tempfile import TemporaryDirectory
import os


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project_root))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    modules = [
        "tests.test_history_connection_recovery",
        "tests.test_backfill_connection_pause",
        "tests.test_capital_history_session",
        "tests.test_history_kline",
        "tests.test_history_limits_config",
        "tests.test_backfill_management",
        "tests.test_backfill_selection",
        "tests.test_stale_backfill_helpers",
        "tests.test_instrument_sync_routes",
        "tests.test_candle_sync_route",
        "tests.test_capital_dll_environment",
        "tests.test_connect_idempotency",
        "tests.test_incremental_backfill",
        "tests.test_process_memory",
        "tests.test_quote_cache",
        "tests.test_backfill_api_control",
        "tests.test_backfill_status_performance",
        "tests.test_backfill_average_duration",
        "tests.test_batch_candle_planning",
        "tests.test_trading_suspensions",
        "tests.test_backfill_targets",
        "tests.test_backfill_memory_safety",
        "tests.test_memory_recovery_observation",
        "tests.test_daily_refresh",
        "tests.test_complete_period_storage",
        "tests.test_prepare_all_backfill",
        "tests.test_regrab_autostart",
    ]
    guards = [
        "app.services.mysql_instrument_repository.MySQLInstrumentRepository._connector",
        "app.services.mysql_instrument_repository.MySQLInstrumentRepository._connect_database",
        "app.services.capital_history_service._ReusableHistoryComSession._run",
        "app.services.capital_history_service.CapitalHistoryClient._fetch_on_com_thread",
        "app.services.capital_dll_quote_service.CapitalQuoteService._load_wrapper",
        "scripts.backfill_market_candles.CapitalKLineSession._open",
        "app.services.trading_suspensions._download",
    ]
    with ExitStack() as stack:
        folder = stack.enter_context(TemporaryDirectory(prefix="capital-backfill-tests-"))
        stack.enter_context(patch.dict(os.environ, {
            "CAPITAL_BACKFILL_CONTROL_DB": str(Path(folder) / "control.sqlite3"),
            "CAPITAL_DAILY_BACKFILL_ENABLED": "false",
            "CAPITAL_SUSPENSION_SYNC_ENABLED": "false",
            "CAPITAL_DAILY_REFRESH_TARGETS": "",
            # Dummy settings only; guards below prohibit real database access.
            "MYSQL_PASSWORD": "offline-test-only",
            # Tests of default-sized windows must not inherit live .env tuning.
            # Empty entries select application defaults and prevent dotenv's
            # non-overriding load from filling in production values.
            **{f"CAPITAL_KLINE_MAX_DAYS_{interval.upper()}": ""
               for interval in ("1m", "5m", "15m", "60m", "day", "week", "month")},
        }))
        for target in guards:
            stack.enter_context(patch(target, side_effect=AssertionError(
                f"Offline test attempted real MySQL/DLL access: {target}",
            )))
        suite = unittest.TestSuite()
        if "--all" in sys.argv:
            suite.addTests(unittest.defaultTestLoader.discover(str(project_root / "tests")))
        else:
            for module in modules:
                suite.addTests(unittest.defaultTestLoader.discover(
                    str(project_root / "tests"), pattern=f"{module.rsplit('.', 1)[-1]}.py",
                ))
        result = unittest.TextTestRunner(verbosity=2 if "--verbose" in sys.argv else 1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
