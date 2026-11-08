"""Run unit tests with an isolated control DB and real vendor/MySQL blocked."""
from pathlib import Path
import os
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
sys.path.insert(0,str(PROJECT/'tests'))


def main():
    with TemporaryDirectory(prefix='capital-worker-unit-') as folder:
        for name in list(os.environ):
            if name.startswith(('CAPITAL_', 'API_MEMORY_', 'MYSQL_')):
                os.environ.pop(name)
        os.environ['CAPITAL_DAILY_BACKFILL_ENABLED']='false'
        os.environ['MYSQL_PASSWORD']='unit-test-only-not-a-real-password'
        from app.services.capital_dll_quote_service import CapitalQuoteService
        from app.services.capital_com_quote_service import CapitalComQuoteService
        from app.services.backfill_control import BackfillControl
        original_init=BackfillControl.__init__
        selected={'path':Path(folder)/'initial'/'control.sqlite3'}
        def isolated_init(self,path=None):
            if path is None and not os.getenv('CAPITAL_BACKFILL_CONTROL_DB'):
                path=selected['path']
            original_init(self,path)
        class IsolatedResult(unittest.TextTestResult):
            def startTest(self,test):
                selected['path']=Path(folder)/str(self.testsRun)/'control.sqlite3'
                super().startTest(test)
        with patch('dotenv.load_dotenv',return_value=False), \
             patch.object(BackfillControl,'__init__',isolated_init), \
             patch('mysql.connector.connect',side_effect=AssertionError('Real MySQL forbidden in unit tests')), \
             patch('app.services.listing_history.fetch_twse_listing_origins',return_value={}), \
             patch('app.services.listing_history.fetch_esb_listing_profiles',return_value=()), \
             patch('app.services.instrument_lifecycle.fetch_bond_lifecycles',return_value=()), \
             patch.object(CapitalQuoteService,'_load_wrapper',side_effect=AssertionError('Real Capital DLL forbidden in unit tests')), \
             patch.object(CapitalComQuoteService,'_load_com_runtime',side_effect=AssertionError('Real Capital COM forbidden in unit tests')):
            suite=unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:]) if len(sys.argv)>1 else unittest.defaultTestLoader.discover(str(PROJECT/'tests'))
            result=unittest.TextTestRunner(verbosity=1,resultclass=IsolatedResult).run(suite)
            return 0 if result.wasSuccessful() else 1


if __name__=='__main__':
    raise SystemExit(main())
