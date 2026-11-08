"""V3 regression runner: isolated SQLite/config, block real DB/DLL/network."""
from contextlib import ExitStack
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


def main():
    root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
    sys.stdout.reconfigure(encoding="utf-8");sys.stderr.reconfigure(encoding="utf-8")
    with ExitStack() as stack:
        folder=stack.enter_context(TemporaryDirectory(prefix="v3-guided-offline-"))
        stack.enter_context(patch.dict(os.environ,{"CAPITAL_ALERT_DB":str(Path(folder)/"alerts.sqlite3"),
            "CAPITAL_BACKFILL_CONTROL_DB":str(Path(folder)/"control.sqlite3"),
            "CAPITAL_DAILY_BACKFILL_ENABLED":"false","MYSQL_PASSWORD":"offline-test-only"}))
        for target in ("app.services.mysql_instrument_repository.MySQLInstrumentRepository._connector",
                       "app.services.mysql_instrument_repository.MySQLInstrumentRepository._connect_database",
                       "app.services.capital_dll_quote_service.CapitalQuoteService._load_wrapper",
                       "socket.create_connection"):
            stack.enter_context(patch(target,side_effect=AssertionError("Offline test blocked real service: "+target)))
        suite=unittest.TestSuite()
        for name in ("ai_preview","ai_rules","ai_rule_validation","rules","alerts","notifications",
                     "screener","saved_screeners","indicators","signals","divergence","analysis_route"):
            suite.addTests(unittest.defaultTestLoader.discover(str(root/"tests"),pattern="test_"+name+".py"))
        result=unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__=="__main__": raise SystemExit(main())
